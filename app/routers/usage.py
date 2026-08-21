"""Entitlement checks and trial consumption - the endpoints the extension calls."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_client_ip, get_current_user
from app.models import UsageEvent, User
from app.schemas import (
    ConsumeRequest,
    ConsumeResponse,
    EntitlementCheckRequest,
    EntitlementOut,
    UsageEventOut,
)
from app.services import entitlements

router = APIRouter(tags=["entitlement"])


@router.post("/entitlement/check", response_model=EntitlementOut)
def check_entitlement(
    payload: EntitlementCheckRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ip: str = Depends(get_client_ip),
):
    """Read-only: does this user/device have access right now?

    Call this when the popup opens to decide what UI to show. It never spends a
    trial - use /usage/consume for that.
    """
    ent, _device = entitlements.check(db, user, payload.device)
    db.commit()
    return ent


@router.post("/usage/consume", response_model=ConsumeResponse)
def consume_usage(
    payload: ConsumeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Spend one trial (or record one metered run for a subscriber).

    Returns 402 with the current entitlement when the allowance is exhausted.
    Send an Idempotency-Key header so a retried request is not charged twice.
    """
    key = payload.idempotency_key or idempotency_key
    result = entitlements.consume(
        db,
        user,
        payload.device,
        action=payload.action,
        idempotency_key=key,
        meta=payload.meta,
    )
    db.commit()
    return result


@router.get("/usage/history", response_model=list[UsageEventOut])
def usage_history(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    limit = max(1, min(limit, 200))
    rows = db.execute(
        select(UsageEvent)
        .where(UsageEvent.user_id == user.id)
        .order_by(UsageEvent.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [UsageEventOut.model_validate(r) for r in rows]
