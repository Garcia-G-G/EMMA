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
from backend.config import settings
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


def test_payment_failed_is_recognized_not_swallowed():
    assert handle_event({"type": "invoice.payment_failed", "data": {"object": {}}}) == "payment_failed"


def test_unknown_event_is_ignored():
    assert handle_event({"type": "customer.updated", "data": {"object": {}}}) == "ignored"


def test_completed_without_uid_is_safe_noop():
    # neither metadata.user_id nor client_reference_id → must not raise
    out = handle_event({"type": "checkout.session.completed", "data": {"object": {"customer": "c"}}})
    assert out == "upgraded:pro"  # returns cleanly even though no row was updated
