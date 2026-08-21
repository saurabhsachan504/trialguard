"""The trial / subscription entitlement engine.

Everything that decides "may this user run the extension right now?" lives here.
It runs server-side only: the extension is untrusted code that the user can read
and edit, so it may *ask* but never *decide*.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Device,
    Subscription,
    SubscriptionStatus,
    UsageEvent,
    User,
)
from app.schemas import ConsumeResponse, DeviceFingerprint, EntitlementOut
from app.services import devices as device_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def trial_limit_for(user: User) -> int:
    if user.trial_limit_override is not None:
        return max(0, user.trial_limit_override)
    return settings.FREE_TRIAL_LIMIT


def active_subscription(db: Session, user: User) -> Subscription | None:
    """Newest subscription in a state that grants access and has not lapsed."""
    subs = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    ).scalars().all()

    for sub in subs:
        status_enum = (
            sub.status if isinstance(sub.status, SubscriptionStatus)
            else SubscriptionStatus(sub.status)
        )
        if not status_enum.grants_access:
            continue
        period_end = _aware(sub.current_period_end)
        # A small grace window absorbs webhook delivery delay around renewal.
        if period_end is not None and period_end < _now():
            continue
        return sub
    return None


def machine_remaining(db: Session, machine_hash: str | None) -> int:
    """Headroom left on the coarse hardware-only ledger."""
    if not machine_hash or not settings.ENFORCE_MACHINE_TRIAL_LIMIT:
        return 10**9
    ledger = device_service.get_ledger(db, machine_hash)
    return max(0, settings.MACHINE_TRIAL_LIMIT - ledger.trials_used)


def build_entitlement(
    db: Session,
    user: User,
    device_hash: str | None = None,
    machine_hash: str | None = None,
) -> EntitlementOut:
    limit = trial_limit_for(user)
    upgrade_url = f"{settings.APP_BASE_URL}{settings.API_PREFIX}/billing/checkout"

    # ---- OWNER ALLOWLIST -------------------------------------------------
    if user.email.lower() in settings.unlimited_emails and user.is_active:
        return EntitlementOut(
                        allowed=True,
            reason="owner_unlimited",
            plan="subscription",
            trials_limit=limit,
            trials_used=user.trials_used,
            trials_remaining=limit,
            device_trials_used=0,
            device_trials_remaining=limit,
            upgrade_url=None,
        )
    # ----------------------------------------------------------------------

    device_used = 0
    device_remaining = limit
    if device_hash:
        ledger = device_service.get_ledger(db, device_hash)
        device_used = ledger.trials_used
        device_remaining = max(0, limit - ledger.trials_used)
        if ledger.blocked:
            return EntitlementOut(
                allowed=False,
                reason="device_blocked",
                plan="blocked",
                trials_limit=limit,
                trials_used=user.trials_used,
                trials_remaining=0,
                device_trials_used=device_used,
                device_trials_remaining=0,
                upgrade_url=upgrade_url,
            )

    user_remaining = max(0, limit - user.trials_used)

    sub = active_subscription(db, user)
    if sub is not None:
        return EntitlementOut(
            allowed=user.is_active,
            reason="subscription_active" if user.is_active else "account_disabled",
            plan="subscription" if user.is_active else "blocked",
            trials_limit=limit,
            trials_used=user.trials_used,
            trials_remaining=user_remaining,
            device_trials_used=device_used,
            device_trials_remaining=device_remaining,
            subscription_status=str(
                sub.status.value if isinstance(sub.status, SubscriptionStatus) else sub.status
            ),
            current_period_end=_aware(sub.current_period_end),
            upgrade_url=None,
        )

    if not user.is_active:
        return EntitlementOut(
            allowed=False,
            reason="account_disabled",
            plan="blocked",
            trials_limit=limit,
            trials_used=user.trials_used,
            trials_remaining=0,
            device_trials_used=device_used,
            device_trials_remaining=device_remaining,
            upgrade_url=upgrade_url,
        )

    if settings.REQUIRE_EMAIL_VERIFICATION and not user.email_verified:
        return EntitlementOut(
            allowed=False,
            reason="email_verification_required",
            plan="free_trial",
            trials_limit=limit,
            trials_used=user.trials_used,
            trials_remaining=user_remaining,
            device_trials_used=device_used,
            device_trials_remaining=device_remaining,
            upgrade_url=upgrade_url,
        )

    effective_remaining = (
        min(user_remaining, device_remaining)
        if (device_hash and settings.ENFORCE_DEVICE_TRIAL_LIMIT)
        else user_remaining
    )
    machine_left = machine_remaining(db, machine_hash)
    effective_remaining = min(effective_remaining, machine_left)

    if effective_remaining > 0:
        return EntitlementOut(
            allowed=True,
            reason="trial_available",
            plan="free_trial",
            trials_limit=limit,
            trials_used=user.trials_used,
            trials_remaining=user_remaining,
            device_trials_used=device_used,
            device_trials_remaining=device_remaining,
            upgrade_url=upgrade_url,
        )

    if machine_left <= 0:
        reason = "machine_trial_exhausted"
    elif user_remaining > 0 and device_remaining <= 0:
        reason = "device_trial_exhausted"
    else:
        reason = "trial_exhausted"
    return EntitlementOut(
        allowed=False,
        reason=reason,
        plan="free_trial",
        trials_limit=limit,
        trials_used=user.trials_used,
        trials_remaining=user_remaining,
        device_trials_used=device_used,
        device_trials_remaining=device_remaining,
        upgrade_url=upgrade_url,
    )


def check(db: Session, user: User, fp: DeviceFingerprint) -> tuple[EntitlementOut, Device]:
    device = device_service.register_device(db, user, fp)
    machine_hash = device_service.fingerprint_to_machine_hash(fp)
    return build_entitlement(db, user, device.device_hash, machine_hash), device


def consume(
    db: Session,
    user: User,
    fp: DeviceFingerprint,
    *,
    action: str = "run",
    idempotency_key: str | None = None,
    meta: dict | None = None,
) -> ConsumeResponse:
    """Atomically spend one unit of entitlement.

    Subscribers are metered but never decremented. Free users decrement both the
    account counter and the device counter in the same transaction.
    """
    device = device_service.register_device(db, user, fp)
    device_hash = device.device_hash
    machine_hash = device_service.fingerprint_to_machine_hash(fp)

    if idempotency_key:
        existing = db.execute(
            select(UsageEvent).where(
                UsageEvent.user_id == user.id,
                UsageEvent.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ConsumeResponse(
                allowed=True,
                consumed=False,
                duplicate=True,
                granted_by=existing.granted_by,
                entitlement=build_entitlement(db, user, device_hash, machine_hash),
                usage_event_id=existing.id,
            )

    # Re-read the counters under a row lock so two concurrent requests can never
    # both spend the last remaining trial.
    locked_user = user
    if not settings.is_sqlite:
        locked_user = db.execute(
            select(User).where(User.id == user.id).with_for_update()
        ).scalar_one()
    ledger = device_service.get_ledger(db, device_hash, lock=True)
    machine_ledger = (
        device_service.get_ledger(db, machine_hash, lock=True)
        if machine_hash and settings.ENFORCE_MACHINE_TRIAL_LIMIT
        else None
    )

    entitlement = build_entitlement(db, locked_user, device_hash, machine_hash)
    if not entitlement.allowed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": _message_for(entitlement.reason),
                "entitlement": json.loads(entitlement.model_dump_json()),
            },
        )

    granted_by = "subscription" if entitlement.plan == "subscription" else "trial"
    counted = granted_by == "trial"

    if counted:
        locked_user.trials_used += 1
        ledger.trials_used += 1
        if machine_ledger is not None:
            machine_ledger.trials_used += 1

    event = UsageEvent(
        user_id=locked_user.id,
        device_id=device.id,
        device_hash=device_hash,
        action=action[:64],
        counted_against_trial=counted,
        granted_by=granted_by,
        idempotency_key=idempotency_key,
        meta=json.dumps(meta)[:4000] if meta else None,
    )
    db.add(event)

    try:
        db.flush()
    except IntegrityError:
        # Concurrent request with the same idempotency key won the race.
        db.rollback()
        existing = db.execute(
            select(UsageEvent).where(
                UsageEvent.user_id == user.id,
                UsageEvent.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return ConsumeResponse(
            allowed=True,
            consumed=False,
            duplicate=True,
            granted_by=existing.granted_by,
            entitlement=build_entitlement(db, user, device_hash, machine_hash),
            usage_event_id=existing.id,
        )

    return ConsumeResponse(
        allowed=True,
        consumed=counted,
        duplicate=False,
        granted_by=granted_by,
        entitlement=build_entitlement(db, locked_user, device_hash, machine_hash),
        usage_event_id=event.id,
    )


_MESSAGES = {
    "trial_exhausted": (
        "You have used all 5 free trials. Subscribe for $5/month to continue."
    ),
    "device_trial_exhausted": (
        "The 5 free trials for this device have already been used. "
        "Subscribe for $5/month to continue."
    ),
    "machine_trial_exhausted": (
        "This computer has used up its free trials. "
        "Subscribe for $5/month to continue."
    ),
    "email_verification_required": "Please verify your email address to continue.",
    "account_disabled": "This account has been disabled.",
    "device_blocked": "This device has been blocked.",
}


def _message_for(reason: str) -> str:
    return _MESSAGES.get(reason, "Access denied.")
