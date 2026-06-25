"""Stripe checkout + webhooks (Prompt 31, Part C).

Checkout creates a subscription session for the chosen plan; the webhook is the
source of truth for plan changes (checkout completed → upgrade; subscription
deleted → downgrade). The webhook's event handling (``handle_event``) is pure and
unit-tested; signature verification wraps it.
"""

from __future__ import annotations

from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request

from backend import db
from backend.auth import require_user
from backend.config import settings
from backend.schemas import CheckoutRequest, CheckoutResponse

router = APIRouter()

stripe.api_key = settings.STRIPE_SECRET_KEY

_PRICE = {"pro": settings.STRIPE_PRICE_PRO,
          "power": settings.STRIPE_PRICE_POWER or settings.STRIPE_PRICE_TEAM,
          "team": settings.STRIPE_PRICE_TEAM}  # LANDING-27: pro/power (team = legacy)


@router.post("/api/billing/checkout", response_model=CheckoutResponse)
async def checkout(body: CheckoutRequest, request: Request) -> CheckoutResponse:
    user = await require_user(request)
    price = _PRICE.get(body.plan)
    if not price:
        raise HTTPException(400, "Plan no válido.")
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(503, "Pagos no configurados todavía.")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price, "quantity": 1}],
        customer_email=user["email"],
        client_reference_id=str(user["id"]),
        metadata={"user_id": str(user["id"]), "plan": body.plan},
        success_url=f"{settings.PUBLIC_URL}/dashboard?upgrade=success",
        cancel_url=f"{settings.PUBLIC_URL}/dashboard",
    )
    return CheckoutResponse(url=session.url)


@router.post("/api/billing/portal")
async def portal(request: Request) -> dict[str, str]:
    user = await require_user(request)
    if not user.get("stripe_customer_id"):
        raise HTTPException(400, "Aún no tienes una suscripción.")
    sess = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"], return_url=f"{settings.PUBLIC_URL}/dashboard"
    )
    return {"url": sess.url}


def handle_event(event: dict[str, Any]) -> str:
    """Apply a Stripe webhook event to our DB. Returns a short status. Pure/testable."""
    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    if etype == "checkout.session.completed":
        uid = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
        plan = (obj.get("metadata") or {}).get("plan", "pro")
        if uid:
            db.set_plan(int(uid), plan, obj.get("customer"))
        return f"upgraded:{plan}"
    if etype == "customer.subscription.deleted":
        cust = obj.get("customer")
        if cust:
            db.set_plan_by_customer(cust, "free")
        return "downgraded:free"
    if etype == "invoice.payment_failed":
        return "payment_failed"
    return "ignored"


@router.post("/api/billing/webhook")
async def webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook no configurado.")
    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Firma inválida.") from None
    return {"status": handle_event(dict(event))}
