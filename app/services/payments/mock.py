"""In-process fake provider so the whole flow is testable without API keys.

The "checkout page" is a local endpoint (POST /billing/mock/confirm) that flips
the subscription to active, mimicking what a real webhook would do.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import SubscriptionStatus, User
from app.security import sha256
from app.services.payments.base import (
    CheckoutSession,
    NormalizedEvent,
    PaymentProvider,
    WebhookVerificationError,
)


class MockProvider(PaymentProvider):
    name = "mock"

    def create_checkout_session(
        self, user: User, *, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        session_id = f"mock_cs_{sha256(user.id + str(datetime.now(timezone.utc)))[:24]}"
        url = (
            f"{settings.APP_BASE_URL}{settings.API_PREFIX}"
            f"/billing/mock/checkout?session_id={session_id}"
            f"&token={settings.MOCK_BILLING_SECRET}"
        )
        return CheckoutSession(
            provider=self.name,
            checkout_url=url,
            session_id=session_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def parse_webhook(self, payload: bytes, headers: dict[str, str]) -> NormalizedEvent:
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError("invalid payload") from exc

        # Verified exactly like a real provider: HMAC over the raw body with a
        # dedicated shared secret. Without MOCK_BILLING_SECRET set, or in
        # production, the endpoint accepts nothing at all.
        if settings.ENV == "prod" or not settings.MOCK_BILLING_SECRET:
            raise WebhookVerificationError("mock provider is disabled")

        expected = hmac.new(
            settings.MOCK_BILLING_SECRET.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-mock-signature", "")):
            raise WebhookVerificationError("signature mismatch")

        status_value = body.get("status", "active")
        period_end = body.get("current_period_end")
        return NormalizedEvent(
            provider=self.name,
            event_id=body.get("id", f"mock_evt_{sha256(payload.decode())[:16]}"),
            event_type=body.get("type", "subscription.updated"),
            user_id=body.get("user_id"),
            customer_id=body.get("customer_id"),
            subscription_id=body.get("subscription_id"),
            status=SubscriptionStatus(status_value),
            current_period_end=(
                datetime.fromtimestamp(period_end, tz=timezone.utc)
                if isinstance(period_end, (int, float))
                else datetime.now(timezone.utc) + timedelta(days=30)
            ),
            cancel_at_period_end=bool(body.get("cancel_at_period_end", False)),
            raw=body,
        )

    def create_portal_session(self, user: User, *, return_url: str) -> str | None:
        return f"{settings.APP_BASE_URL}{settings.API_PREFIX}/billing/mock/portal"
