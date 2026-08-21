"""Pluggable payment providers.

Set PAYMENT_PROVIDER=mock|stripe|razorpay. The rest of the application only
talks to the PaymentProvider interface, so switching processors later does not
touch the entitlement logic.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.services.payments.base import PaymentProvider


@lru_cache
def get_provider() -> PaymentProvider:
    name = settings.PAYMENT_PROVIDER
    if name == "stripe":
        from app.services.payments.stripe_provider import StripeProvider

        return StripeProvider()
    if name == "razorpay":
        from app.services.payments.razorpay_provider import RazorpayProvider

        return RazorpayProvider()
    from app.services.payments.mock import MockProvider

    return MockProvider()


__all__ = ["PaymentProvider", "get_provider"]
