"""Minimal operator endpoints, guarded by a static X-Admin-Key header."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_admin_user
from app.models import (
    Device,
    DeviceTrialLedger,
    Subscription,
    SubscriptionStatus,
    UsageEvent,
    User,
)
from app.schemas import MessageOut, UserOut
from app.services import devices as device_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_admin_user)])


class GrantTrialsRequest(BaseModel):
    email: str
    trial_limit: int = Field(ge=0, le=10_000)


class BlockDeviceRequest(BaseModel):
    device_hash: str
    blocked: bool = True
    reason: str | None = None


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    total_users = db.execute(select(func.count()).select_from(User)).scalar_one()
    active_subs = db.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status == SubscriptionStatus.active)
    ).scalar_one()
    total_runs = db.execute(select(func.count()).select_from(UsageEvent)).scalar_one()
    devices = db.execute(select(func.count()).select_from(Device)).scalar_one()
    exhausted = db.execute(
        select(func.count())
        .select_from(DeviceTrialLedger)
        .where(DeviceTrialLedger.trials_used >= 5)
    ).scalar_one()
    return {
        "users": total_users,
        "active_subscriptions": active_subs,
        "usage_events": total_runs,
        "devices": devices,
        "devices_with_exhausted_trials": exhausted,
        "mrr_usd": round(active_subs * 5.0, 2),
    }


@router.get("/users", response_model=list[UserOut])
def list_users(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    rows = db.execute(
        select(User).order_by(User.created_at.desc()).limit(min(limit, 200)).offset(offset)
    ).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


@router.post("/grant-trials", response_model=UserOut)
def grant_trials(payload: GrantTrialsRequest, db: Session = Depends(get_db)):
    user = db.execute(
        select(User).where(User.email == payload.email.lower())
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.trial_limit_override = payload.trial_limit
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/block-device", response_model=MessageOut)
def block_device(payload: BlockDeviceRequest, db: Session = Depends(get_db)):
    ledger = device_service.get_ledger(db, payload.device_hash)
    ledger.blocked = payload.blocked
    ledger.block_reason = payload.reason
    db.commit()
    return MessageOut(detail="updated")


@router.post("/reset-device-trials", response_model=MessageOut)
def reset_device_trials(payload: BlockDeviceRequest, db: Session = Depends(get_db)):
    ledger = device_service.get_ledger(db, payload.device_hash)
    ledger.trials_used = 0
    db.commit()
    return MessageOut(detail="device trial counter reset")
