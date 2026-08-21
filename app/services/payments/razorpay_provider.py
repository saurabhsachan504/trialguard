"""Razorpay Subscriptions implementation (useful when billing from India)."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import razorpay

from app.config import settings
from app.models import SubscriptionStatus, User
from app.services.payments.base import (
    CheckoutSession,
    NormalizedEvent,
    PaymentProvider,
    WebhookVerificationError,
)

_STATUS_MAP = {
    "created": SubscriptionStatus.incomplete,
    "authenticated": SubscriptionStatus.incomplete,
    "pending": SubscriptionStatus.past_due,
    "halted": SubscriptionStatus.unpaid,
    "active": SubscriptionStatus.active,
    "paused": SubscriptionStatus.canceled,
    "cancelled": SubscriptionStatus.canceled,
    "completed": SubscriptionStatus.canceled,
    "expired": SubscriptionStatus.canceled,
}


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def __init__(self) -> None:
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not configured")
        if not settings.RAZORPAY_PLAN_ID:
            raise RuntimeError(
                "RAZORPAY_PLAN_ID is not configured - create a $5/month plan in the "
                "Razorpay dashboard and put its plan_xxx id here"
            )
        self.client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )

    def create_checkout_session(
        self, user: User, *, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        subscription = self.client.subscription.create(
            {
                "plan_id": settings.RAZORPAY_PLAN_ID,
                "customer_notify": 1,
                "total_count": 120,  # up to 10 years of monthly cycles
                "notes": {"user_id": user.id, "email": user.email},
            }
        )
        return CheckoutSession(
            provider=self.name,
            checkout_url=subscription["short_url"],
            session_id=subscription["id"],
        )

    def cancel_subscription(self, subscription_id: str, *, at_period_end: bool = True) -> None:
        self.client.subscription.cancel(
            subscription_id, {"cancel_at_cycle_end": 1 if at_period_end else 0}
        )

    def parse_webhook(self, payload: bytes, headers: dict[str, str]) -> NormalizedEvent:
        signature = headers.get("x-razorpay-signature")
        secret = settings.RAZORPAY_WEBHOOK_SECRET
        if not signature or not secret:
            raise WebhookVerificationError("missing razorpay signature or secret")

        expected = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("signature mismatch")

        body = json.loads(payload.decode("utf-8"))
        etype = body.get("event", "")
        entity = (
            body.get("payload", {}).get("subscription", {}).get("entity", {})
        )

        normalised = NormalizedEvent(
            provider=self.name,
            event_id=str(
                headers.get("x-razorpay-event-id")
                or f"{etype}:{entity.get('id')}:{body.get('created_at')}"
            ),
            event_type=etype,
            user_id=(entity.get("notes") or {}).get("user_id"),
            customer_id=entity.get("customer_id"),
            subscription_id=entity.get("id"),
            raw=body,
        )

        if entity.get("status"):
            normalised.status = _STATUS_MAP.get(
                entity["status"], SubscriptionStatus.incomplete
            )
        if entity.get("current_end"):
            normalised.current_period_end = datetime.fromtimestamp(
                entity["current_end"], tz=timezone.utc
            )
        return normalised
