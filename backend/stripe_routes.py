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
from backend.config import BUNDLES, settings
from backend.schemas import CheckoutRequest, CheckoutResponse

router = APIRouter()

stripe.api_key = settings.STRIPE_SECRET_KEY

_PRICE = {"pro": settings.STRIPE_PRICE_PRO,
          "power": settings.STRIPE_PRICE_POWER or settings.STRIPE_PRICE_TEAM,
          "team": settings.STRIPE_PRICE_TEAM}  # LANDING-27: pro/power (team = legacy)

# Subscription statuses that mean "this person is not entitled any more".
# past_due is deliberately absent: Stripe is still retrying, and invoice.
# payment_failed handles the end of that cycle.
_DEAD_SUB_STATUSES = frozenset({"canceled", "unpaid", "incomplete_expired"})

# PaymentIntent purposes that credit a bundle, mapped to the refill_events
# `trigger` they are recorded under. Both must credit — see handle_event.
_CREDITING_PURPOSES = {"emma_auto_refill": "auto", "emma_bundle_purchase": "first_purchase"}

# LAUNCH-11 Part 4: a BYO-key purchase buys a LICENCE, not seconds. Same webhook,
# a different fulfilment — and, like the bundle credit, the webhook is the source
# of truth so a closed tab cannot cost someone the thing they paid for.
_LICENSE_PURPOSE = "emma_license_purchase"


def _issue_license(meta: dict[str, Any], pi_id: str) -> str:
    from backend import license_routes

    user_id = int(meta.get("user_id", 0) or 0)
    plan = str(meta.get("plan", ""))
    if not user_id or plan not in license_routes.PLANS:
        return "ignored"
    existing = db.license_for_payment_intent(pi_id)
    if existing:
        return "licensed"  # idempotent: the webhook may arrive more than once
    key = db.generate_license_key()
    db.create_license(user_id, plan, key, license_routes.expiry_for(plan), pi_id)
    return "licensed"


def _plan_for_subscription(obj: dict[str, Any]) -> str | None:
    """Reverse the price id on a subscription back to our plan name.

    Read from `settings` at call time rather than the module-level `_PRICE`, so a
    price id configured after import (and every test) resolves correctly.
    """
    try:
        price_id = obj["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None
    for plan, configured in (
        ("pro", settings.STRIPE_PRICE_PRO),
        ("power", settings.STRIPE_PRICE_POWER),
        ("team", settings.STRIPE_PRICE_TEAM),
    ):
        if configured and configured == price_id:
            return plan
    return None


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

    # LAUNCH-10 Part 4: a plan change made through the Stripe billing portal
    # never reached the DB — the user upgraded, paid, and kept the old caps.
    if etype == "customer.subscription.updated":
        cust = obj.get("customer")
        if not cust:
            return "ignored"
        status = str(obj.get("status", ""))
        if status in _DEAD_SUB_STATUSES:
            db.set_plan_by_customer(cust, "free")
            return f"downgraded:free:{status}"
        plan = _plan_for_subscription(obj)
        if plan:
            db.set_plan_by_customer(cust, plan)
            return f"updated:{plan}"
        return f"subscription_updated:{status}"

    # This used to `return "payment_failed"` and do nothing, so a past_due
    # subscriber kept full access through Stripe's entire dunning cycle and out
    # the other side. `plan` is the entitlement (PLAN_CAPS), so the plan is what
    # has to move — but only once Stripe has stopped retrying. A card that fails
    # today and clears tomorrow must not cost anyone their subscription.
    if etype == "invoice.payment_failed":
        cust = obj.get("customer")
        if obj.get("next_payment_attempt") is not None:
            return "payment_failed:retrying"
        if not cust:
            return "payment_failed:no_customer"  # nothing to act on; never silent
        db.set_plan_by_customer(cust, "free")
        return "downgraded:free:dunning_exhausted"

    # DASHBOARD-CREDITS-2 + LAUNCH-10 Part 4: the webhook is the SOURCE OF TRUTH
    # for every bundle credit — auto-refill and first purchase alike.
    #
    # It used to gate on purpose == "emma_auto_refill" only. A first purchase
    # carries "emma_bundle_purchase" (set at credits_routes.py buy time), so it
    # fell straight through to `return "credited"` having credited nothing, and
    # the ONLY path that credited it was /api/bundles/confirm, fired from the
    # browser. Close the tab, lose Wi-Fi, or fail the 3DS redirect and the
    # customer was charged and received nothing, with no server-side recovery.
    # /confirm is now a latency optimization, not the mechanism.
    if etype == "payment_intent.succeeded":
        meta = obj.get("metadata") or {}
        purpose = meta.get("purpose", "")
        if purpose == _LICENSE_PURPOSE:
            return _issue_license(meta, str(obj.get("id") or ""))
        if purpose in _CREDITING_PURPOSES:
            user_id = int(meta.get("user_id", 0) or 0)
            bundle_key = meta.get("bundle_key", "")
            b = BUNDLES.get(bundle_key)
            pi_id = obj.get("id")
            if user_id and b and pi_id:
                # Idempotent by DB constraint, not by check-then-write: whichever
                # of the webhook and /confirm arrives second gets False here.
                db.credit_bundle_once(
                    user_id, bundle_key, int(b["seconds"]), float(b["usd"]),
                    pi_id, _CREDITING_PURPOSES[purpose],
                )
        return "credited"

    if etype == "payment_intent.payment_failed":
        meta = obj.get("metadata") or {}
        if meta.get("purpose") in ("emma_auto_refill", "emma_bundle_purchase"):
            user_id = int(meta.get("user_id", 0) or 0)
            b = BUNDLES.get(meta.get("bundle_key", ""))
            if user_id and b:
                db.append_refill_event(
                    user_id, meta.get("bundle_key", ""), 0, b["usd"],
                    obj.get("id"), meta.get("purpose", "unknown"), "failed",
                )
        return "refill_failed"

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
