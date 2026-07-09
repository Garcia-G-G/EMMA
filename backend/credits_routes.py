"""DASHBOARD-CREDITS-2 — balance, bundles, auto-refill, refill history.

All endpoints are cookie-authed via require_user (401 for anon). The bundle catalog
is server-side truth (config.BUNDLES): the client posts a bundle_key, never an
amount. Raw sqlite3 via db.py.
"""

from __future__ import annotations

from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request

from backend import db, metering
from backend.auth import require_user
from backend.config import BUNDLES, bundle_per_min_usd, plan_caps, settings

router = APIRouter(tags=["credits"])
stripe.api_key = settings.STRIPE_SECRET_KEY

_PAID_PLANS = ("pro", "power", "team")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


@router.get("/api/balance")
async def get_balance(request: Request) -> dict[str, Any]:
    user = await require_user(request)
    plan = user.get("plan", "free")
    cap = plan_caps(plan)
    baseline_total_s = int(cap.get("monthly_seconds", 0) or 0)
    used_month_s = metering.seconds_used_this_month(user["id"])
    baseline_left_s = max(0, baseline_total_s - used_month_s)

    bal = db.get_user_balance(user["id"])
    extra_s = int(bal["extra_seconds"] or 0) if bal else 0
    held_s = int(bal["extra_held_seconds"] or 0) if bal else 0

    last_7d_used = db.seconds_used_last_n_days(user["id"], days=7)
    avg_per_day_s = max(1, last_7d_used // 7)
    total_left_s = baseline_left_s + extra_s - held_s
    runway_days = round(total_left_s / avg_per_day_s, 1) if last_7d_used > 0 else None

    return {
        "plan": plan,
        "baseline_total_min": round(baseline_total_s / 60, 1),
        "baseline_left_min": round(baseline_left_s / 60, 1),
        "extra_min": round(extra_s / 60, 1),
        "held_min": round(held_s / 60, 1),
        "total_left_min": round(total_left_s / 60, 1),
        "used_this_month_min": round(used_month_s / 60, 1),
        "runway_days_est": runway_days,
        "auto_refill_enabled": bool(bal and bal["auto_refill_enabled"]),
        "auto_refill_bundle": (bal and bal["auto_refill_bundle_key"]) or "regular",
        "has_payment_method": bool(bal and bal["default_payment_method"]),
    }


@router.get("/api/bundles")
async def list_bundles() -> dict[str, Any]:
    return {
        "bundles": [
            {"key": k, "seconds": v["seconds"], "usd": v["usd"],
             "minutes": v["seconds"] // 60,
             "label_es": v["label_es"], "label_en": v["label_en"],
             "per_min_usd": bundle_per_min_usd(k),
             "recommended": v.get("recommended", False)}
            for k, v in BUNDLES.items()
        ]
    }


@router.post("/api/bundles/buy")
async def buy_bundle(request: Request) -> dict[str, Any]:
    user = await require_user(request)
    if user.get("plan", "free") not in _PAID_PLANS:
        raise HTTPException(403, "los bundles requieren un plan de pago (Pro o Power)")
    body = await _json_body(request)
    key = (body.get("bundle_key") or "").strip()
    b = BUNDLES.get(key)
    if not b:
        raise HTTPException(400, "unknown bundle")

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT stripe_customer_id, email FROM users WHERE id=?", (user["id"],)
        ).fetchone()
    finally:
        conn.close()
    customer_id = row["stripe_customer_id"] if row else None
    if not customer_id:
        c = stripe.Customer.create(
            email=(row["email"] if row else user.get("email")),
            metadata={"user_id": str(user["id"])},
        )
        customer_id = c.id
        conn = db.connect()
        try:
            conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (customer_id, user["id"]))
            conn.commit()
        finally:
            conn.close()

    # First purchase on-session with setup_future_usage=off_session: charge NOW and
    # save the payment method for later auto-refills, in one intent.
    intent = stripe.PaymentIntent.create(
        amount=int(b["usd"] * 100),
        currency="usd",
        customer=customer_id,
        setup_future_usage="off_session",
        automatic_payment_methods={"enabled": True},
        metadata={"user_id": str(user["id"]), "bundle_key": key,
                  "purpose": "emma_bundle_purchase"},
    )
    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "bundle_key": key,
        "amount_usd": b["usd"],
        "seconds": b["seconds"],
    }


@router.post("/api/bundles/confirm")
async def confirm_bundle(request: Request) -> dict[str, Any]:
    """Called after the client confirms the PaymentIntent (Stripe.js). Verifies status
    server-side and credits the balance. Idempotent — a retry of the same
    payment_intent_id no-ops (guarded by the refill_events row)."""
    user = await require_user(request)
    body = await _json_body(request)
    pi_id = (body.get("payment_intent_id") or "").strip()
    if not pi_id:
        raise HTTPException(400, "payment_intent_id required")

    conn = db.connect()
    try:
        existing = conn.execute(
            "SELECT id FROM refill_events WHERE stripe_payment_intent=?", (pi_id,)
        ).fetchone()
    finally:
        conn.close()
    if existing:
        return {"ok": True, "already_credited": True}

    pi = stripe.PaymentIntent.retrieve(pi_id)
    if pi.status != "succeeded":
        raise HTTPException(402, f"payment status: {pi.status}")
    # Guard: the intent must belong to this user (metadata was set server-side at buy).
    if str(pi.metadata.get("user_id", "")) != str(user["id"]):
        raise HTTPException(403, "payment does not belong to this account")

    bundle_key = pi.metadata.get("bundle_key", "")
    b = BUNDLES.get(bundle_key)
    if not b:
        raise HTTPException(500, "unknown bundle in payment metadata")

    if pi.payment_method:
        db.set_default_payment_method(user["id"], pi.payment_method)

    db.add_seconds_to_balance(user["id"], int(b["seconds"]))
    db.append_refill_event(
        user["id"], bundle_key, int(b["seconds"]), b["usd"], pi.id, "first_purchase", "succeeded"
    )
    return {"ok": True, "seconds_added": int(b["seconds"])}


@router.post("/api/autorefill")
async def toggle_auto_refill(request: Request) -> dict[str, Any]:
    user = await require_user(request)
    body = await _json_body(request)
    enabled = bool(body.get("enabled", False))
    bundle_key = (body.get("bundle_key") or "regular").strip()
    if bundle_key not in BUNDLES:
        raise HTTPException(400, "unknown bundle")
    if enabled:
        bal = db.get_user_balance(user["id"])
        if not bal or not bal["default_payment_method"]:
            raise HTTPException(400, "no payment method — buy a bundle first")
    db.set_auto_refill(user["id"], enabled, bundle_key)
    return {"ok": True, "enabled": enabled, "bundle_key": bundle_key}


@router.get("/api/refills")
async def refill_history(request: Request) -> dict[str, Any]:
    user = await require_user(request)
    return {"refills": db.refill_history_for_user(user["id"], limit=30)}
