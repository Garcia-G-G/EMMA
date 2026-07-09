"""DASHBOARD-CREDITS-2 — bridge between the balance/bundle data and the rest of
the backend.

- `balance_left_seconds` / `trigger_auto_refill` / `is_auto_refill_enabled` are the
  three functions ABUSE-PROTECTION-2's realtime_proxy imports for CAPA 5 (balance
  cut) and the warning ticker.
- `reserve_for_session` / `finalize_session` hold+consume EXTRA seconds around a
  live session so concurrent sessions can't double-spend the prepaid balance.

Raw sqlite3 via db.py; zero SQLAlchemy.
"""

from __future__ import annotations

import logging

import stripe

from backend import db
from backend.config import BUNDLES, settings

log = logging.getLogger("emma.dashboard_credits")
stripe.api_key = settings.STRIPE_SECRET_KEY


# ---- bridge: called by realtime_proxy -------------------------------------------


def balance_left_seconds(user_id: int, plan: str, monthly_cap_s: int, used_month: int) -> int:
    """Total seconds available = (baseline remaining) + (prepaid extra). CAPA 5."""
    baseline_left = max(0, monthly_cap_s - used_month)
    bal = db.get_user_balance(user_id)
    extra = int(bal["extra_seconds"] or 0) if bal else 0
    return baseline_left + extra


def is_auto_refill_enabled(user_id: int) -> bool:
    bal = db.get_user_balance(user_id)
    return bool(bal and bal["auto_refill_enabled"])


async def trigger_auto_refill(user_id: int, plan: str) -> bool:
    """Balance hit zero. If the user opted into auto-refill AND has a saved payment
    method, charge the configured bundle off-session and credit it. Returns True only
    if they now have minutes; False on no-opt-in / no-PM / decline / Stripe error —
    the caller then closes the WS."""
    bal = db.get_user_balance(user_id)
    if not bal or not bal["auto_refill_enabled"]:
        return False
    if not bal["default_payment_method"]:
        log.warning("auto_refill user=%s no payment method saved", user_id)
        return False

    bundle_key = bal["auto_refill_bundle_key"] or "regular"
    b = BUNDLES.get(bundle_key)
    if not b:
        return False

    conn = db.connect()
    try:
        user_row = conn.execute(
            "SELECT stripe_customer_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not user_row or not user_row["stripe_customer_id"]:
        return False

    try:
        pi = stripe.PaymentIntent.create(
            amount=int(b["usd"] * 100),
            currency="usd",
            customer=user_row["stripe_customer_id"],
            payment_method=bal["default_payment_method"],
            off_session=True,
            confirm=True,
            metadata={"user_id": str(user_id), "bundle_key": bundle_key,
                      "purpose": "emma_auto_refill"},
        )
    except stripe.error.CardError as e:
        err_code = getattr(getattr(e, "error", None), "code", "") or ""
        log.warning("auto_refill user=%s card declined code=%s", user_id, err_code)
        db.append_refill_event(user_id, bundle_key, 0, b["usd"], None, "auto", "failed")
        return False
    except Exception as e:  # network / API error
        log.warning("auto_refill user=%s stripe error=%s", user_id, e)
        return False

    if pi.status != "succeeded":
        # SCA / requires_action — needs a client-side confirm we can't do off-session.
        db.append_refill_event(user_id, bundle_key, 0, b["usd"], pi.id, "auto", "requires_action")
        return False

    db.add_seconds_to_balance(user_id, int(b["seconds"]))
    db.append_refill_event(user_id, bundle_key, int(b["seconds"]), b["usd"], pi.id, "auto", "succeeded")
    log.info("auto_refill user=%s bundle=%s ok pi=%s", user_id, bundle_key, pi.id)
    return True


# ---- reservation lifecycle: called by realtime_proxy on start/end ---------------


def reserve_for_session(
    user_id: int, session_id: str, session_max_s: int, baseline_left_s: int
) -> None:
    """Hold the EXTRA seconds a session might use beyond its baseline. Baseline is
    free-flow within its monthly cap (already gated in CAPA 5); only the overflow is
    reserved so concurrent sessions can't double-spend the prepaid balance. No-op if
    the session fits inside the baseline."""
    if session_max_s <= baseline_left_s:
        return
    extra_needed = session_max_s - baseline_left_s
    ok, _held = db.try_reserve_seconds(user_id, session_id, extra_needed)
    if not ok:
        log.info("reserve failed user=%s session=%s extra=%s", user_id, session_id, extra_needed)


def finalize_session(session_id: str, seconds_used: int, baseline_left_at_start_s: int) -> None:
    """Session ended: consume from EXTRA only the seconds spent beyond the baseline
    that was available at start. Releases the hold + refunds the rest. Idempotent."""
    extra_used = max(0, seconds_used - baseline_left_at_start_s)
    db.finalize_reservation(session_id, extra_used)
