"""Provider-agnostic payment interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models import SubscriptionStatus, User


@dataclass(slots=True)
class CheckoutSession:
    provider: str
    checkout_url: str
    session_id: str
    expires_at: datetime | None = None


@dataclass(slots=True)
class NormalizedEvent:
    """A webhook payload flattened into the fields we actually act on."""

    provider: str
    event_id: str
    event_type: str
    user_id: str | None = None
    customer_id: str | None = None
    subscription_id: str | None = None
    status: SubscriptionStatus | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class WebhookVerificationError(Exception):
    """Raised when a webhook signature does not verify."""


class PaymentProvider(ABC):
    name: str = "base"

    @abstractmethod
    def create_checkout_session(
        self, user: User, *, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        """Start a hosted subscription checkout for $5/month."""

    @abstractmethod
    def parse_webhook(self, payload: bytes, headers: dict[str, str]) -> NormalizedEvent:
        """Verify the signature and normalise the event. Raises on bad signature."""

    def create_portal_session(self, user: User, *, return_url: str) -> str | None:
        """URL where the customer can manage/cancel their subscription."""
        return None

    def cancel_subscription(self, subscription_id: str, *, at_period_end: bool = True) -> None:
        return None
