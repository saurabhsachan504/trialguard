"""Payment provider webhooks.

Signatures are verified before anything is trusted, and every event id is
recorded so provider retries are idempotent.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import MessageOut
from app.services import billing as billing_service
from app.services.payments import get_provider
from app.services.payments.base import WebhookVerificationError

logger = logging.getLogger("trialguard.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _handle(request: Request, db: Session, expected: str) -> MessageOut:
    provider = get_provider()
    if provider.name != expected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{expected} is not the configured payment provider",
        )

    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        event = provider.parse_webhook(payload, headers)
    except WebhookVerificationError as exc:
        logger.warning("Rejected %s webhook: %s", expected, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature"
        )

    if billing_service.already_processed(db, event.provider, event.event_id):
        return MessageOut(detail="duplicate ignored")

    if not billing_service.record_event(db, event):
        return MessageOut(detail="duplicate ignored")

    billing_service.apply_event(db, event)
    db.commit()
    return MessageOut(detail="ok")


@router.post("/stripe", response_model=MessageOut)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    return await _handle(request, db, "stripe")


@router.post("/razorpay", response_model=MessageOut)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    return await _handle(request, db, "razorpay")


@router.post("/mock", response_model=MessageOut, include_in_schema=False)
async def mock_webhook(request: Request, db: Session = Depends(get_db)):
    return await _handle(request, db, "mock")
