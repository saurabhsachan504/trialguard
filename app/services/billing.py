"""Applies normalised payment events to local subscription state."""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Subscription, SubscriptionStatus, User, WebhookEvent
from app.services.payments.base import NormalizedEvent

logger = logging.getLogger("trialguard.billing")


def already_processed(db: Session, provider: str, event_id: str) -> bool:
    return (
        db.execute(
            select(WebhookEvent).where(
                WebhookEvent.provider == provider, WebhookEvent.event_id == event_id
            )
        ).scalar_one_or_none()
        is not None
    )


def record_event(db: Session, event: NormalizedEvent) -> bool:
    """Store the event id. Returns False if it was already recorded."""
    record = WebhookEvent(
        provider=event.provider,
        event_id=event.event_id,
        event_type=event.event_type,
        payload=json.dumps(event.raw, default=str)[:100_000],
    )
    db.add(record)
    try:
        db.flush()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _find_user(db: Session, event: NormalizedEvent) -> User | None:
    if event.user_id:
        user = db.get(User, event.user_id)
        if user:
            return user
    if event.subscription_id:
        sub = db.execute(
            select(Subscription).where(
                Subscription.provider == event.provider,
                Subscription.provider_subscription_id == event.subscription_id,
            )
        ).scalar_one_or_none()
        if sub:
            return db.get(User, sub.user_id)
    if event.customer_id:
        sub = db.execute(
            select(Subscription).where(
                Subscription.provider == event.provider,
                Subscription.provider_customer_id == event.customer_id,
            )
        ).scalar_one_or_none()
        if sub:
            return db.get(User, sub.user_id)
    return None


def apply_event(db: Session, event: NormalizedEvent) -> Subscription | None:
    """Create or update the local Subscription row from a webhook event."""
    user = _find_user(db, event)
    if user is None:
        logger.warning(
            "Webhook %s/%s could not be mapped to a user", event.provider, event.event_id
        )
        return None

    sub: Subscription | None = None
    if event.subscription_id:
        sub = db.execute(
            select(Subscription).where(
                Subscription.provider == event.provider,
                Subscription.provider_subscription_id == event.subscription_id,
            )
        ).scalar_one_or_none()
    if sub is None:
        # Fall back to an in-flight row created when checkout started.
        sub = db.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.provider == event.provider,
                Subscription.provider_subscription_id.is_(None),
            )
            .order_by(Subscription.created_at.desc())
        ).scalar_one_or_none()

    if sub is None:
        sub = Subscription(
            user_id=user.id,
            provider=event.provider,
            price_cents=settings.PLAN_PRICE_CENTS,
            currency=settings.PLAN_CURRENCY,
        )
        db.add(sub)

    if event.customer_id:
        sub.provider_customer_id = event.customer_id
    if event.subscription_id:
        sub.provider_subscription_id = event.subscription_id
    if event.status is not None:
        sub.status = event.status
        if event.status == SubscriptionStatus.canceled:
            sub.canceled_at = sub.canceled_at or event.current_period_end
    if event.current_period_end is not None:
        sub.current_period_end = event.current_period_end
    sub.cancel_at_period_end = event.cancel_at_period_end

    db.flush()
    return sub


def start_checkout_record(db: Session, user: User, provider: str) -> Subscription:
    """Placeholder row so a webhook that arrives before we store ids still lands."""
    existing = db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.provider == provider,
            Subscription.provider_subscription_id.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    sub = Subscription(
        user_id=user.id,
        provider=provider,
        status=SubscriptionStatus.incomplete,
        price_cents=settings.PLAN_PRICE_CENTS,
        currency=settings.PLAN_CURRENCY,
    )
    db.add(sub)
    db.flush()
    return sub
