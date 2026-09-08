"""Stripe handle_event — the branches test_backend.py doesn't cover.

handle_event is pure (DB side effects only), so these run with no Stripe client:
the metadata fallbacks (client_reference_id, default plan) and the payment_failed /
unknown event types are exactly where a real prod payload would expose a bug.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest

from backend import db
from backend.config import BUNDLES, settings
from backend.stripe_routes import handle_event


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    yield


def _user(email="a@b.com"):
    db.create_local_user(email, "x")
    return db.get_user_by_email(email)["id"]


def test_completed_uses_metadata_plan_and_customer():
    uid = _user()
    handle_event({"type": "checkout.session.completed", "data": {"object": {
        "metadata": {"user_id": str(uid), "plan": "power"}, "customer": "cus_123"}}})
    row = db.get_user(uid)
    assert row["plan"] == "power" and row["stripe_customer_id"] == "cus_123"


def test_completed_falls_back_to_client_reference_id_and_default_plan():
    uid = _user()
    # no metadata.user_id, no metadata.plan → client_reference_id + default "pro"
    out = handle_event({"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": str(uid), "customer": "cus_9"}}})
    assert out == "upgraded:pro"
    assert db.get_user(uid)["plan"] == "pro"


def test_subscription_deleted_downgrades_by_customer():
    uid = _user()
    db.set_plan(uid, "power", "cus_xyz")
    out = handle_event({"type": "customer.subscription.deleted",
                        "data": {"object": {"customer": "cus_xyz"}}})
    assert out == "downgraded:free"
    assert db.get_user(uid)["plan"] == "free"


def test_payment_failed_without_a_customer_is_a_safe_noop():
    # LAUNCH-10 Part 4 changed this branch from a bare string to a real
    # entitlement change. With no customer on the payload there is nothing to
    # act on — but say so, rather than reporting a downgrade that never happened.
    out = handle_event({"type": "invoice.payment_failed", "data": {"object": {}}})
    assert out == "payment_failed:no_customer"


def test_unknown_event_is_ignored():
    assert handle_event({"type": "customer.updated", "data": {"object": {}}}) == "ignored"


def test_completed_without_uid_is_safe_noop():
    # neither metadata.user_id nor client_reference_id → must not raise
    out = handle_event({"type": "checkout.session.completed", "data": {"object": {"customer": "c"}}})
    assert out == "upgraded:pro"  # returns cleanly even though no row was updated


# --- LAUNCH-10 Part 4: the webhook is the source of truth for bundles --------
#
# payment_intent.succeeded credited only `emma_auto_refill`. A FIRST bundle
# purchase carries `emma_bundle_purchase` (set in credits_routes at buy time),
# so it fell straight through to `return "credited"` having credited nothing.
# The only path that credited it was /api/bundles/confirm, fired from the
# browser: close the tab, drop Wi-Fi, or fail the 3DS redirect and the customer
# was charged and received nothing, with no server-side recovery.


def _pi_event(uid, pi_id="pi_1", purpose="emma_bundle_purchase", bundle="regular"):
    return {"type": "payment_intent.succeeded", "data": {"object": {
        "id": pi_id,
        "metadata": {"user_id": str(uid), "bundle_key": bundle, "purpose": purpose},
    }}}


def _seconds(uid):
    bal = db.get_user_balance(uid)
    return int(bal["extra_seconds"]) if bal else 0


def test_bundle_purchase_is_credited_by_webhook_alone():
    """The tab is closed. Nothing but the webhook arrives."""
    uid = _user()
    out = handle_event(_pi_event(uid))
    assert out == "credited"
    assert _seconds(uid) == int(BUNDLES["regular"]["seconds"])


def test_bundle_purchase_credit_is_idempotent():
    """The webhook and /api/bundles/confirm both fire. Credit lands once."""
    uid = _user()
    handle_event(_pi_event(uid))
    handle_event(_pi_event(uid))
    assert _seconds(uid) == int(BUNDLES["regular"]["seconds"])


def test_auto_refill_is_still_credited():
    uid = _user()
    handle_event(_pi_event(uid, pi_id="pi_auto", purpose="emma_auto_refill"))
    assert _seconds(uid) == int(BUNDLES["regular"]["seconds"])


def test_unknown_bundle_key_credits_nothing():
    uid = _user()
    handle_event(_pi_event(uid, bundle="not-a-bundle"))
    assert _seconds(uid) == 0


def test_missing_user_id_credits_nothing():
    ev = _pi_event(0)
    ev["data"]["object"]["metadata"]["user_id"] = ""
    assert handle_event(ev) == "credited"


# --- the idempotency guard is now a DB constraint, not a check-then-write ----


def test_credit_bundle_once_returns_false_on_a_replay():
    uid = _user()
    b = BUNDLES["regular"]
    first = db.credit_bundle_once(uid, "regular", int(b["seconds"]), b["usd"], "pi_x", "webhook")
    second = db.credit_bundle_once(uid, "regular", int(b["seconds"]), b["usd"], "pi_x", "webhook")
    assert first is True and second is False
    assert _seconds(uid) == int(b["seconds"])


def test_duplicate_payment_intent_is_rejected_by_a_unique_index():
    """The old guard was SELECT-then-INSERT across two connections: a concurrent
    webhook and browser callback could both read 'not credited' and both write."""
    import sqlite3

    uid = _user()
    db.append_refill_event(uid, "regular", 100, 5.0, "pi_dup", "auto", "succeeded")
    with pytest.raises(sqlite3.IntegrityError):
        db.append_refill_event(uid, "regular", 100, 5.0, "pi_dup", "manual", "succeeded")


def test_many_rows_may_still_have_a_null_payment_intent():
    """A declined card logs a failure row with no intent id (dashboard_credits).
    The index is partial for exactly this reason."""
    uid = _user()
    db.append_refill_event(uid, "regular", 0, 5.0, None, "auto", "failed")
    db.append_refill_event(uid, "regular", 0, 5.0, None, "auto", "failed")
    assert len(db.refill_history_for_user(uid)) == 2


# --- subscription lifecycle -------------------------------------------------


def test_subscription_updated_applies_a_plan_change(monkeypatch):
    """A plan change through the Stripe billing portal never reached the DB."""
    monkeypatch.setattr(settings, "STRIPE_PRICE_POWER", "price_power")
    uid = _user()
    db.set_plan(uid, "pro", "cus_sub")
    out = handle_event({"type": "customer.subscription.updated", "data": {"object": {
        "customer": "cus_sub", "status": "active",
        "items": {"data": [{"price": {"id": "price_power"}}]}}}})
    assert out == "updated:power"
    assert db.get_user(uid)["plan"] == "power"


def test_subscription_updated_to_a_dead_status_downgrades():
    uid = _user()
    db.set_plan(uid, "power", "cus_dead")
    out = handle_event({"type": "customer.subscription.updated", "data": {"object": {
        "customer": "cus_dead", "status": "unpaid"}}})
    assert out == "downgraded:free:unpaid"
    assert db.get_user(uid)["plan"] == "free"


def test_payment_failed_mid_dunning_keeps_access():
    """Stripe will retry. A card that fails once and clears tomorrow must not
    cost the subscriber their plan."""
    uid = _user()
    db.set_plan(uid, "pro", "cus_dun")
    out = handle_event({"type": "invoice.payment_failed", "data": {"object": {
        "customer": "cus_dun", "next_payment_attempt": 1770000000}}})
    assert out == "payment_failed:retrying"
    assert db.get_user(uid)["plan"] == "pro"


def test_payment_failed_with_dunning_exhausted_downgrades():
    """No next attempt means Stripe is done. PLAN_CAPS is the entitlement, so
    the plan is what has to move — returning a string let a past_due subscriber
    keep full access through the whole cycle and out the other side."""
    uid = _user()
    db.set_plan(uid, "pro", "cus_done")
    out = handle_event({"type": "invoice.payment_failed", "data": {"object": {
        "customer": "cus_done", "next_payment_attempt": None}}})
    assert out == "downgraded:free:dunning_exhausted"
    assert db.get_user(uid)["plan"] == "free"
