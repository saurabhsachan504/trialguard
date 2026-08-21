from __future__ import annotations

import hashlib
import hmac
import json

from app.config import settings
from tests.conftest import activate_subscription, make_device, register

API = settings.API_PREFIX


def test_plan_is_five_dollars_monthly(client):
    plans = client.get(f"{API}/billing/plans").json()
    assert len(plans) == 1
    assert plans[0]["price_cents"] == 500
    assert plans[0]["currency"] == "USD"
    assert plans[0]["interval"] == "month"
    assert plans[0]["free_trials"] == 5


def test_checkout_requires_auth(client):
    assert client.post(f"{API}/billing/checkout", json={}).status_code == 401


def test_checkout_returns_a_url(client, device):
    _, headers, _ = register(client, device=device)
    res = client.post(f"{API}/billing/checkout", json={}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["provider"] == "mock"
    assert body["checkout_url"].startswith("http")


def test_cannot_checkout_twice_while_active(client, device):
    body, headers, _ = register(client, device=device)
    activate_subscription(client, headers)
    res = client.post(f"{API}/billing/checkout", json={}, headers=headers)
    assert res.status_code == 409


def test_subscription_status_endpoint(client, device):
    body, headers, _ = register(client, device=device)
    assert client.get(f"{API}/billing/subscription", headers=headers).json() is None

    activate_subscription(client, headers)
    sub = client.get(f"{API}/billing/subscription", headers=headers).json()
    assert sub["status"] == "active"
    assert sub["price_cents"] == 500
    assert sub["current_period_end"] is not None


def test_cancel_marks_end_of_period(client, device):
    body, headers, _ = register(client, device=device)
    activate_subscription(client, headers)

    res = client.post(f"{API}/billing/cancel", json={}, headers=headers)
    assert res.status_code == 200

    sub = client.get(f"{API}/billing/subscription", headers=headers).json()
    assert sub["cancel_at_period_end"] is True
    # Access continues until the period actually ends.
    ent = client.post(
        f"{API}/entitlement/check", json={"device": {"installation_id": "x" * 12}},
        headers=headers,
    ).json()
    assert ent["allowed"] is True


def test_expired_subscription_stops_granting_access(client, db, device):
    from datetime import datetime, timedelta, timezone

    from app.models import Subscription

    body, headers, _ = register(client, device=device)
    activate_subscription(client, headers)

    sub = db.query(Subscription).filter_by(user_id=body["user"]["id"]).one()
    sub.current_period_end = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["plan"] == "free_trial"


def test_mock_confirm_requires_auth_and_only_affects_the_caller(client, device):
    """The fake checkout must not be a free-subscription faucet."""
    victim, victim_headers, _ = register(client, email="victim@example.com", device=device)

    # No token at all.
    assert client.post(f"{API}/billing/mock/confirm", json={}).status_code == 401

    # A signed-in attacker cannot name someone else's user_id.
    _, attacker_headers, _ = register(
        client, email="attacker@example.com", device=make_device()
    )
    res = client.post(
        f"{API}/billing/mock/confirm",
        json={"user_id": victim["user"]["id"]},
        headers=attacker_headers,
    )
    assert res.status_code == 200
    # ...the subscription landed on the attacker, not the victim.
    assert client.get(f"{API}/billing/subscription", headers=victim_headers).json() is None


def test_mock_endpoints_disabled_without_a_secret(client, device, monkeypatch):
    from app.config import settings as live

    _, headers, _ = register(client, device=device)
    monkeypatch.setattr(live, "MOCK_BILLING_SECRET", "")

    assert (
        client.post(f"{API}/billing/mock/confirm", json={}, headers=headers).status_code
        == 404
    )
    assert (
        client.get(f"{API}/billing/mock/checkout?session_id=x&token=y").status_code == 404
    )


def test_mock_webhook_verifies_its_signature(client, device):
    from app.config import settings as live

    body, _, _ = register(client, email="mockwh@example.com", device=device)
    payload = json.dumps(
        {"id": "evt_1", "type": "subscription.updated", "user_id": body["user"]["id"]}
    ).encode()

    unsigned = client.post(
        f"{API}/webhooks/mock", content=payload, headers={"content-type": "application/json"}
    )
    assert unsigned.status_code == 400

    forged = client.post(
        f"{API}/webhooks/mock",
        content=payload,
        headers={"x-mock-signature": "deadbeef", "content-type": "application/json"},
    )
    assert forged.status_code == 400

    signature = hmac.new(
        live.MOCK_BILLING_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    signed = client.post(
        f"{API}/webhooks/mock",
        content=payload,
        headers={"x-mock-signature": signature, "content-type": "application/json"},
    )
    assert signed.status_code == 200


def test_webhook_rejects_bad_signature(client, monkeypatch):
    from app.config import settings as live

    monkeypatch.setattr(live, "PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setattr(live, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setattr(live, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(live, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(live, "RAZORPAY_PLAN_ID", "plan_123")

    from app.services import payments

    payments.get_provider.cache_clear()
    try:
        res = client.post(
            f"{API}/webhooks/razorpay",
            content=b'{"event":"subscription.activated"}',
            headers={"x-razorpay-signature": "deadbeef", "content-type": "application/json"},
        )
        assert res.status_code == 400
    finally:
        payments.get_provider.cache_clear()


def test_razorpay_webhook_activates_and_is_idempotent(client, device, monkeypatch):
    from app.config import settings as live

    body, headers, _ = register(client, email="rz@example.com", device=device)
    user_id = body["user"]["id"]

    monkeypatch.setattr(live, "PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setattr(live, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setattr(live, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(live, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(live, "RAZORPAY_PLAN_ID", "plan_123")

    from app.services import payments

    payments.get_provider.cache_clear()
    try:
        payload = json.dumps(
            {
                "event": "subscription.activated",
                "created_at": 1700000000,
                "payload": {
                    "subscription": {
                        "entity": {
                            "id": "sub_test_1",
                            "status": "active",
                            "customer_id": "cust_1",
                            "current_end": 1900000000,
                            "notes": {"user_id": user_id},
                        }
                    }
                },
            }
        ).encode()
        signature = hmac.new(b"whsec", payload, hashlib.sha256).hexdigest()
        hdrs = {
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": "evt_1",
            "content-type": "application/json",
        }

        first = client.post(f"{API}/webhooks/razorpay", content=payload, headers=hdrs)
        assert first.status_code == 200
        assert first.json()["detail"] == "ok"

        # Provider retries the same event - must not create a second subscription.
        second = client.post(f"{API}/webhooks/razorpay", content=payload, headers=hdrs)
        assert second.json()["detail"] == "duplicate ignored"
    finally:
        payments.get_provider.cache_clear()

    sub = client.get(f"{API}/billing/subscription", headers=headers).json()
    assert sub["status"] == "active"

    from app.models import Subscription

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["plan"] == "subscription"
