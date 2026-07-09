"""DASHBOARD-CREDITS-2 — prepaid balance, reservations, bundles, auto-refill.

DB helpers + the dashboard_credits bridge + reservation wiring + the public API
surface (auth gating, free-tier guard). Stripe network calls are not exercised here
(no live keys) — the buy/confirm flows are covered by the prod smoke in
_planning/notes/DASHBOARD-CREDITS-VERIFY.md.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.app import app
from backend.config import settings


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "credits.db"))
    db.init_db()
    yield


# ---- balance basics ---------------------------------------------------------


def test_ensure_user_balance_idempotent(tmp_db):
    db.ensure_user_balance(1)
    db.ensure_user_balance(1)
    bal = db.get_user_balance(1)
    assert bal["extra_seconds"] == 0 and bal["auto_refill_enabled"] == 0


def test_add_seconds_to_balance(tmp_db):
    db.add_seconds_to_balance(1, 1800)
    assert db.get_user_balance(1)["extra_seconds"] == 1800
    db.add_seconds_to_balance(1, 1800)
    assert db.get_user_balance(1)["extra_seconds"] == 3600


def test_set_auto_refill(tmp_db):
    db.set_auto_refill(1, True, "regular")
    bal = db.get_user_balance(1)
    assert bal["auto_refill_enabled"] == 1 and bal["auto_refill_bundle_key"] == "regular"
    db.set_auto_refill(1, False)
    assert db.get_user_balance(1)["auto_refill_enabled"] == 0


# ---- reservations (two-phase, atomic) ---------------------------------------


def test_reservation_holds_and_finalizes(tmp_db):
    db.add_seconds_to_balance(1, 1800)
    ok, held = db.try_reserve_seconds(1, "s1", 600)
    assert ok and held == 600
    bal = db.get_user_balance(1)
    assert bal["extra_seconds"] == 1800 and bal["extra_held_seconds"] == 600
    db.finalize_reservation("s1", 300)  # used only 300 of 600
    bal = db.get_user_balance(1)
    assert bal["extra_seconds"] == 1500 and bal["extra_held_seconds"] == 0


def test_reservation_concurrent_no_double_spend(tmp_db):
    db.add_seconds_to_balance(1, 600)
    ok1, h1 = db.try_reserve_seconds(1, "s1", 500)
    ok2, h2 = db.try_reserve_seconds(1, "s2", 500)
    assert ok1 and h1 == 500
    assert ok2 and h2 == 100  # only 100 left after the first hold


def test_reservation_idempotent(tmp_db):
    db.add_seconds_to_balance(1, 1800)
    ok1, h1 = db.try_reserve_seconds(1, "same", 600)
    ok2, h2 = db.try_reserve_seconds(1, "same", 900)  # different amount, ignored
    assert ok1 and ok2 and h1 == h2 == 600


def test_reservation_no_balance(tmp_db):
    db.ensure_user_balance(1)  # zero extra
    ok, held = db.try_reserve_seconds(1, "s1", 600)
    assert not ok and held == 0


def test_finalize_unknown_session_is_noop(tmp_db):
    db.finalize_reservation("nope", 100)  # must not raise


# ---- refill audit -----------------------------------------------------------


def test_refill_history_newest_first(tmp_db):
    db.append_refill_event(1, "regular", 6000, 19.99, "pi_abc", "manual")
    db.append_refill_event(1, "starter", 1800, 9.99, "pi_def", "auto")
    rows = db.refill_history_for_user(1)
    assert len(rows) == 2 and rows[0]["bundle_key"] == "starter"


def test_seconds_used_last_n_days(tmp_db):
    now = time.time()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO usage_events(user_id, device_id, seconds, created_at) VALUES(?,?,?,?)",
            (1, 1, 300, now - 3600),
        )
        conn.execute(
            "INSERT INTO usage_events(user_id, device_id, seconds, created_at) VALUES(?,?,?,?)",
            (1, 1, 500, now - 10 * 86400),  # 10 days ago → outside the 7-day window
        )
        conn.commit()
    finally:
        conn.close()
    assert db.seconds_used_last_n_days(1, days=7) == 300


# ---- dashboard_credits bridge + reservation wiring --------------------------


def test_balance_left_seconds_bridge(tmp_db):
    from backend.dashboard_credits import balance_left_seconds

    db.add_seconds_to_balance(1, 3600)
    # baseline 3600s, used 1200s → 2400 baseline_left + 3600 extra = 6000
    assert balance_left_seconds(1, "pro", 3600, 1200) == 6000


def test_is_auto_refill_enabled_bridge(tmp_db):
    from backend.dashboard_credits import is_auto_refill_enabled

    assert is_auto_refill_enabled(1) is False
    db.set_auto_refill(1, True)
    assert is_auto_refill_enabled(1) is True


def test_reserve_and_finalize_session_wiring(tmp_db):
    from backend.dashboard_credits import finalize_session, reserve_for_session

    db.add_seconds_to_balance(1, 600)
    # baseline exhausted (0 left); a 500s session must reserve 500 of extra.
    reserve_for_session(1, "s1", session_max_s=500, baseline_left_s=0)
    assert db.get_user_balance(1)["extra_held_seconds"] == 500
    # used 300 → consume 300 from extra, release the rest.
    finalize_session("s1", seconds_used=300, baseline_left_at_start_s=0)
    bal = db.get_user_balance(1)
    assert bal["extra_seconds"] == 300 and bal["extra_held_seconds"] == 0


def test_reserve_session_within_baseline_holds_nothing(tmp_db):
    from backend.dashboard_credits import finalize_session, reserve_for_session

    db.add_seconds_to_balance(1, 600)
    reserve_for_session(1, "s2", session_max_s=100, baseline_left_s=200)  # fits baseline
    assert db.get_user_balance(1)["extra_held_seconds"] == 0
    finalize_session("s2", seconds_used=90, baseline_left_at_start_s=200)  # no extra used
    assert db.get_user_balance(1)["extra_seconds"] == 600  # untouched


# ---- API surface ------------------------------------------------------------


def _register(c, email="a@b.com"):
    c.post("/api/auth/register", json={"email": email, "password": "correcthorse9"})


def test_bundles_endpoint_lists_three_with_recommended(tmp_db):
    c = TestClient(app, follow_redirects=False)
    d = c.get("/api/bundles").json()
    assert len(d["bundles"]) == 3
    rec = [b for b in d["bundles"] if b["recommended"]]
    assert len(rec) == 1 and rec[0]["key"] == "regular"


def test_balance_requires_login(tmp_db):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/api/balance").status_code == 401


def test_balance_shape_when_authed(tmp_db):
    c = TestClient(app, follow_redirects=False)
    _register(c)
    d = c.get("/api/balance").json()
    for k in ("baseline_total_min", "extra_min", "total_left_min", "auto_refill_enabled"):
        assert k in d


def test_free_tier_cannot_buy_bundle(tmp_db):
    c = TestClient(app, follow_redirects=False)
    _register(c)  # default plan = free
    r = c.post("/api/bundles/buy", json={"bundle_key": "regular"})
    assert r.status_code == 403


def test_autorefill_requires_payment_method(tmp_db):
    c = TestClient(app, follow_redirects=False)
    _register(c)
    r = c.post("/api/autorefill", json={"enabled": True, "bundle_key": "regular"})
    assert r.status_code == 400  # no saved PM yet
    # disabling never needs a PM
    assert c.post("/api/autorefill", json={"enabled": False}).status_code == 200
