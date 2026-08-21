"""Stripe Checkout + Billing implementation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import stripe

from app.config import settings
from app.models import SubscriptionStatus, User
from app.services.payments.base import (
    CheckoutSession,
    NormalizedEvent,
    PaymentProvider,
    WebhookVerificationError,
)

_STATUS_MAP = {
    "incomplete": SubscriptionStatus.incomplete,
    "incomplete_expired": SubscriptionStatus.canceled,
    "trialing": SubscriptionStatus.trialing,
    "active": SubscriptionStatus.active,
    "past_due": SubscriptionStatus.past_due,
    "canceled": SubscriptionStatus.canceled,
    "unpaid": SubscriptionStatus.unpaid,
    "paused": SubscriptionStatus.canceled,
}

RELEVANT_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
    "invoice.paid",
}


class StripeProvider(PaymentProvider):
    name = "stripe"

    def __init__(self) -> None:
        if not settings.STRIPE_SECRET_KEY:
            raise RuntimeError("STRIPE_SECRET_KEY is not configured")
        stripe.api_key = settings.STRIPE_SECRET_KEY

    # -- checkout --------------------------------------------------------
    def create_checkout_session(
        self, user: User, *, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        price = settings.STRIPE_PRICE_ID
        line_item: dict[str, Any]
        if price:
            line_item = {"price": price, "quantity": 1}
        else:
            # No pre-created Price object: build the $5/month price inline.
            line_item = {
                "quantity": 1,
                "price_data": {
                    "currency": settings.PLAN_CURRENCY.lower(),
                    "unit_amount": settings.PLAN_PRICE_CENTS,
                    "recurring": {"interval": settings.PLAN_INTERVAL},
                    "product_data": {"name": f"{settings.APP_NAME} Pro"},
                },
            }

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[line_item],
            customer_email=user.email,
            client_reference_id=user.id,
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            # Echoed back on every subscription webhook, so we can always map an
            # event to a local user without a lookup table.
            subscription_data={"metadata": {"user_id": user.id}},
            metadata={"user_id": user.id},
            idempotency_key=f"checkout_{user.id}_{int(datetime.now(timezone.utc).timestamp()) // 60}",
        )
        return CheckoutSession(
            provider=self.name,
            checkout_url=session.url,
            session_id=session.id,
            expires_at=(
                datetime.fromtimestamp(session.expires_at, tz=timezone.utc)
                if getattr(session, "expires_at", None)
                else None
            ),
        )

    def create_portal_session(self, user: User, *, return_url: str) -> str | None:
        customer_id = _customer_id_for(user)
        if not customer_id:
            return None
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url
        )
        return session.url

    def cancel_subscription(self, subscription_id: str, *, at_period_end: bool = True) -> None:
        if at_period_end:
            stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
        else:
            stripe.Subscription.delete(subscription_id)

    # -- webhooks --------------------------------------------------------
    def parse_webhook(self, payload: bytes, headers: dict[str, str]) -> NormalizedEvent:
        signature = headers.get("stripe-signature")
        if not signature or not settings.STRIPE_WEBHOOK_SECRET:
            raise WebhookVerificationError("missing stripe signature or secret")
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, settings.STRIPE_WEBHOOK_SECRET
            )
        except Exception as exc:  # SignatureVerificationError / ValueError
            raise WebhookVerificationError(str(exc)) from exc

        obj = event["data"]["object"]
        etype = event["type"]

        normalised = NormalizedEvent(
            provider=self.name,
            event_id=event["id"],
            event_type=etype,
            raw=dict(event),
        )

        if etype == "checkout.session.completed":
            normalised.user_id = obj.get("client_reference_id") or (
                obj.get("metadata") or {}
            ).get("user_id")
            normalised.customer_id = obj.get("customer")
            normalised.subscription_id = obj.get("subscription")
            normalised.status = SubscriptionStatus.active
        elif etype.startswith("customer.subscription."):
            normalised.user_id = (obj.get("metadata") or {}).get("user_id")
            normalised.customer_id = obj.get("customer")
            normalised.subscription_id = obj.get("id")
            normalised.status = _STATUS_MAP.get(
                obj.get("status", ""), SubscriptionStatus.incomplete
            )
            if etype.endswith("deleted"):
                normalised.status = SubscriptionStatus.canceled
            period_end = obj.get("current_period_end") or (
                (obj.get("items", {}).get("data") or [{}])[0].get("current_period_end")
            )
            if period_end:
                normalised.current_period_end = datetime.fromtimestamp(
                    period_end, tz=timezone.utc
                )
            normalised.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        elif etype == "invoice.payment_failed":
            normalised.customer_id = obj.get("customer")
            normalised.subscription_id = obj.get("subscription")
            normalised.status = SubscriptionStatus.past_due
        elif etype == "invoice.paid":
            normalised.customer_id = obj.get("customer")
            normalised.subscription_id = obj.get("subscription")
            normalised.status = SubscriptionStatus.active
            period_end = (obj.get("lines", {}).get("data") or [{}])[0].get(
                "period", {}
            ).get("end")
            if period_end:
                normalised.current_period_end = datetime.fromtimestamp(
                    period_end, tz=timezone.utc
                )

        return normalised


def _customer_id_for(user: User) -> str | None:
    for sub in user.subscriptions:
        if sub.provider == "stripe" and sub.provider_customer_id:
            return sub.provider_customer_id
    return None
