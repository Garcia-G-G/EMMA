"""LAUNCH-11 Part 4 — licence activation (backend side).

The endpoint's whole job: licence key in, valid/invalid out. What it must NOT
grow is anything else — usage, a heartbeat, a machine fingerprint, or the user's
OpenAI key. A BYO-key daemon is a black box to us on purpose, and this is the
one place that could quietly stop being true.
"""

from __future__ import annotations

import os
import tempfile
import time

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest
from fastapi import HTTPException

from backend import db
from backend.config import settings
from backend.license_routes import PLANS, check, expiry_for


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    yield


def _user(email="a@b.com"):
    db.create_local_user(email, "x")
    return db.get_user_by_email(email)["id"]


def _license(plan="lifetime", key="EMMA-1", activations_max=3, expires_at=None):
    uid = _user()
    db.create_license(uid, plan, key, expires_at, activations_max=activations_max)
    return key


# ---- the ladder -------------------------------------------------------------


def test_the_three_rungs():
    assert PLANS["monthly"]["usd"] == 6.0
    assert PLANS["annual"]["usd"] == 59.0
    assert PLANS["lifetime"]["usd"] == 249.0


def test_lifetime_never_expires():
    assert expiry_for("lifetime") is None
    assert expiry_for("monthly") > time.time()
    assert expiry_for("annual") > expiry_for("monthly")


# ---- activation -------------------------------------------------------------


def test_a_valid_licence_activates():
    key = _license()
    out = check(key, "install-1", "Mac de prueba")
    assert out["valid"] is True
    assert out["plan"] == "lifetime"
    assert out["activations_used"] == 1


def test_reactivating_the_same_install_is_free():
    """Reinstalling Emma, or restoring a Mac from a backup, must not burn a seat."""
    key = _license(activations_max=1)
    check(key, "install-1", None)
    again = check(key, "install-1", None)
    assert again["activations_used"] == 1


def test_activations_are_bounded():
    key = _license(activations_max=2)
    check(key, "install-1", None)
    check(key, "install-2", None)
    with pytest.raises(HTTPException) as ei:
        check(key, "install-3", None)
    assert ei.value.status_code == 409


def test_an_unknown_licence_is_rejected():
    with pytest.raises(HTTPException) as ei:
        check("NOPE", "install-1", None)
    assert ei.value.status_code == 404


def test_an_expired_licence_is_rejected():
    key = _license(plan="annual", expires_at=time.time() - 10)
    with pytest.raises(HTTPException) as ei:
        check(key, "install-1", None)
    assert ei.value.status_code == 403


def test_a_revoked_licence_is_rejected():
    key = _license()
    conn = db.connect()
    conn.execute("UPDATE licenses SET status='refunded' WHERE license_key=?", (key,))
    conn.commit()
    conn.close()
    with pytest.raises(HTTPException) as ei:
        check(key, "install-1", None)
    assert ei.value.status_code == 403


# ---- what must never appear -------------------------------------------------


def test_the_licence_tables_record_no_usage():
    """If a usage column ever appears here, the tier's promise has been broken."""
    conn = db.connect()
    try:
        for table in ("licenses", "license_activations"):
            cols = {r[1].lower() for r in conn.execute(f"PRAGMA table_info({table})")}
            for forbidden in ("seconds", "minutes", "tokens", "usage", "openai_api_key", "api_key"):
                assert not any(forbidden in c for c in cols), f"{table}.{forbidden}"
    finally:
        conn.close()


# ---- issued on purchase, by webhook -----------------------------------------


def test_a_license_purchase_issues_a_key_by_webhook_alone():
    """Same lesson as the bundle credit (LAUNCH-10 Part 4): the webhook is the
    source of truth, so a closed tab cannot cost someone what they paid for."""
    from backend.stripe_routes import handle_event

    uid = _user()
    out = handle_event({"type": "payment_intent.succeeded", "data": {"object": {
        "id": "pi_lic_1",
        "metadata": {"user_id": str(uid), "plan": "lifetime",
                     "purpose": "emma_license_purchase"},
    }}})
    assert out == "licensed"

    conn = db.connect()
    row = conn.execute("SELECT * FROM licenses WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    assert row is not None
    assert row["plan"] == "lifetime"
    assert row["expires_at"] is None
    assert row["license_key"].startswith("EMMA-")


def test_a_replayed_license_webhook_issues_one_key():
    from backend.stripe_routes import handle_event

    uid = _user()
    ev = {"type": "payment_intent.succeeded", "data": {"object": {
        "id": "pi_lic_2",
        "metadata": {"user_id": str(uid), "plan": "annual",
                     "purpose": "emma_license_purchase"},
    }}}
    handle_event(ev)
    handle_event(ev)

    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM licenses WHERE user_id=?", (uid,)).fetchone()["c"]
    conn.close()
    assert n == 1


def test_generated_keys_avoid_ambiguous_characters():
    """Someone reads this off a screen and types it into another Mac."""
    for _ in range(40):
        key = db.generate_license_key()
        body = key.replace("EMMA-", "").replace("-", "")
        assert not (set(body) & set("O0I1L"))
