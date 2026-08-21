"""Subscription checkout, status and the mock-provider test harness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Subscription, SubscriptionStatus, User
from app.schemas import (
    CheckoutRequest,
    CheckoutSessionOut,
    MessageOut,
    PlanOut,
    SubscriptionOut,
)
from app.security import constant_time_equals
from app.services import billing as billing_service
from app.services.payments import get_provider

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", response_model=list[PlanOut])
def list_plans():
    return [
        PlanOut(
            id="pro-monthly",
            name=f"{settings.APP_NAME} Pro",
            price_cents=settings.PLAN_PRICE_CENTS,
            currency=settings.PLAN_CURRENCY,
            interval=settings.PLAN_INTERVAL,
            description="Unlimited usage, billed monthly. Cancel any time.",
            free_trials=settings.FREE_TRIAL_LIMIT,
        )
    ]


@router.post("/checkout", response_model=CheckoutSessionOut)
def create_checkout(
    payload: CheckoutRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    provider = get_provider()

    from app.services.entitlements import active_subscription

    if active_subscription(db, user) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an active subscription.",
        )

    session = provider.create_checkout_session(
        user,
        success_url=payload.success_url or settings.BILLING_SUCCESS_URL,
        cancel_url=payload.cancel_url or settings.BILLING_CANCEL_URL,
    )
    sub = billing_service.start_checkout_record(db, user, provider.name)
    if provider.name == "razorpay":
        # Razorpay creates the subscription up front, so we already know its id.
        sub.provider_subscription_id = session.session_id
    db.commit()

    return CheckoutSessionOut(
        provider=session.provider,
        checkout_url=session.checkout_url,
        session_id=session.session_id,
        expires_at=session.expires_at,
    )


@router.get("/subscription", response_model=SubscriptionOut | None)
def get_subscription(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    sub = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    ).scalars().first()
    if sub is None:
        return None
    return SubscriptionOut.model_validate(sub)


@router.post("/portal", response_model=MessageOut)
def billing_portal(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    url = get_provider().create_portal_session(
        user, return_url=settings.BILLING_SUCCESS_URL
    )
    if not url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing portal available for this account.",
        )
    return MessageOut(detail=url)


@router.post("/cancel", response_model=MessageOut)
def cancel_subscription(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    from app.services.entitlements import active_subscription

    sub = active_subscription(db, user)
    if sub is None:
        raise HTTPException(status_code=404, detail="No active subscription.")
    if sub.provider_subscription_id:
        get_provider().cancel_subscription(
            sub.provider_subscription_id, at_period_end=True
        )
    sub.cancel_at_period_end = True
    db.commit()
    return MessageOut(
        detail="Subscription will end at the close of the current billing period."
    )


# ---------------------------------------------------------------------------
# Mock provider harness (dev/test only)
# ---------------------------------------------------------------------------
def _assert_mock_enabled() -> None:
    """The fake checkout may only ever exist outside production.

    It grants a paid subscription for free, so it is also gated on a shared
    secret - being non-prod is not on its own a good enough reason to expose it.
    """
    if (
        settings.PAYMENT_PROVIDER != "mock"
        or settings.ENV == "prod"
        or not settings.MOCK_BILLING_SECRET
    ):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/mock/checkout", response_class=HTMLResponse, include_in_schema=False)
def mock_checkout_page(session_id: str, token: str = ""):
    _assert_mock_enabled()
    if not constant_time_equals(token, settings.MOCK_BILLING_SECRET):
        raise HTTPException(status_code=404, detail="Not found")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mock checkout</title>
<style>body{{font-family:system-ui;margin:4rem auto;max-width:32rem;line-height:1.6}}
button{{padding:.75rem 1.25rem;font-size:1rem;border-radius:8px;border:0;
background:#111;color:#fff;cursor:pointer}}
input{{width:100%;padding:.5rem;font-family:monospace}}</style></head><body>
<h1>Mock checkout</h1>
<p>Subscribe to <b>{settings.APP_NAME} Pro</b> &mdash;
${settings.PLAN_PRICE_CENTS / 100:.2f}/{settings.PLAN_INTERVAL}</p>
<p><small>session {session_id}</small></p>
<p><label>Paste your access token<br><input id="tok" placeholder="eyJ..."></label></p>
<button onclick="pay()">Pay now</button>
<pre id="out"></pre>
<script>
async function pay() {{
  const r = await fetch('{settings.API_PREFIX}/billing/mock/confirm', {{
    method: 'POST',
    headers: {{
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + document.getElementById('tok').value.trim()
    }},
    body: JSON.stringify({{session_id: '{session_id}'}})
  }});
  document.getElementById('out').textContent = await r.text();
}}
</script></body></html>"""


@router.post("/mock/confirm", response_model=SubscriptionOut, include_in_schema=False)
def mock_confirm(
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stand-in for a real provider webhook: activates the caller's subscription.

    Requires a valid access token, so it can only ever affect the authenticated
    user - never an arbitrary user_id supplied in the body.
    """
    _assert_mock_enabled()
    session_id = str(body.get("session_id", "mock_sub"))[:64]

    sub = billing_service.start_checkout_record(db, user, "mock")
    sub.provider_subscription_id = f"sub_{session_id}"
    sub.provider_customer_id = f"cus_{user.id[:12]}"
    sub.status = SubscriptionStatus.active
    sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=30)
    db.commit()
    db.refresh(sub)
    return SubscriptionOut.model_validate(sub)
