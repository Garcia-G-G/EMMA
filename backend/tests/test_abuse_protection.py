"""ABUSE-PROTECTION-2 — the 7 defense layers.

Covers the pure/unit-testable pieces: db.py flags + hash-chained audit + usage
aggregates, and the in-memory ConnectionManager (Capas 2/3/7). The realtime_proxy
wiring + warning ticker are exercised by the prod smoke steps in
_planning/notes/ABUSE-PROTECTION-VERIFY.md (they need a live OpenAI upstream).
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, realtime_proxy
from backend.app import app
from backend.config import settings
from backend.connection_manager import ConnectionManager


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh sqlite DB per test: point DATABASE_URL at a tmp file, init the schema."""
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "abuse.db"))
    db.init_db()
    yield


# ---- db.py: user flags ------------------------------------------------------


def test_user_flags_lifecycle(tmp_db):
    assert db.get_user_flags(1) is None
    db.ensure_user_flags(1)
    flags = db.get_user_flags(1)
    assert flags is not None and flags["disabled"] == 0

    db.set_user_disabled(1, "abuse")
    f = db.get_user_flags(1)
    assert f["disabled"] == 1 and f["disabled_reason"] == "abuse" and f["disabled_at"]

    db.clear_user_disabled(1)
    f = db.get_user_flags(1)
    assert f["disabled"] == 0 and f["disabled_reason"] is None


def test_set_user_disabled_is_idempotent(tmp_db):
    db.set_user_disabled(5, "first")
    db.set_user_disabled(5, "second")
    assert db.get_user_flags(5)["disabled"] == 1


def test_throttle_and_anomaly_score(tmp_db):
    until = time.time() + 3600
    db.set_user_throttle(9, until)
    db.update_anomaly_score(9, 4.7)
    f = db.get_user_flags(9)
    assert abs(f["throttle_until"] - until) < 0.01
    assert abs(f["anomaly_score"] - 4.7) < 0.001


# ---- db.py: hash-chained audit trail ----------------------------------------


def test_audit_hash_chain(tmp_db):
    h1 = db.append_status_event(1, "disable", actor_id=99, reason="test1")
    h2 = db.append_status_event(1, "enable", actor_id=99, reason="test2")
    h3 = db.append_status_event(2, "throttle", actor_id=None, reason="anomaly")
    assert len({h1, h2, h3}) == 3  # all distinct

    events = db.status_events_for_user(1)
    assert len(events) == 2  # only user 1's rows

    ok, broken = db.verify_audit_chain()
    assert ok is True and broken is None


def test_audit_chain_detects_tampering(tmp_db):
    db.append_status_event(1, "disable", actor_id=99, reason="real")
    db.append_status_event(1, "enable", actor_id=99, reason="real")
    # Tamper: mutate a reason in place (append-only rule violated on purpose).
    conn = db.connect()
    try:
        conn.execute("UPDATE user_status_events SET reason='forged' WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    ok, broken = db.verify_audit_chain()
    assert ok is False and broken == 1


# ---- db.py: usage aggregates ------------------------------------------------


def _seed_usage(user_id: int, seconds_list, when: float | None = None):
    conn = db.connect()
    try:
        for s in seconds_list:
            conn.execute(
                "INSERT INTO usage_events(user_id, device_id, seconds, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, 1, s, when if when is not None else time.time()),
            )
        conn.commit()
    finally:
        conn.close()


def test_seconds_used_today(tmp_db):
    _seed_usage(1, [60, 90, 90])
    assert db.seconds_used_today(1) == 240
    # A row from two days ago must NOT count toward today.
    _seed_usage(1, [999], when=time.time() - 2 * 86400)
    assert db.seconds_used_today(1) == 240


def test_recent_session_seconds_baseline(tmp_db):
    _seed_usage(2, [60, 60, 60, 60, 3600])
    baseline = db.recent_session_seconds(2, limit=14)
    assert sorted(baseline) == [60, 60, 60, 60, 3600]


def test_revoke_all_device_tokens(tmp_db):
    conn = db.connect()
    try:
        now = time.time()
        for i in range(3):
            conn.execute(
                "INSERT INTO device_tokens(user_id, token_hash, device_name, created_at) "
                "VALUES(?,?,?,?)",
                (7, f"hash{i}", "Emma device", now),
            )
        conn.commit()
    finally:
        conn.close()
    assert db.revoke_all_device_tokens_for_user(7) == 3
    # Second call is a no-op (already revoked).
    assert db.revoke_all_device_tokens_for_user(7) == 0


# ---- ConnectionManager: Capas 2 + 3 + 7 -------------------------------------


@pytest.mark.asyncio
async def test_connection_manager_concurrent_cap():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 1, "reconnect_min_seconds": 0}
    ok, _ = await mgr.can_connect(42, cap)
    assert ok
    await mgr.register(42, "ws1", object())
    ok, why = await mgr.can_connect(42, cap)
    assert not ok and why == "concurrent_limit"


@pytest.mark.asyncio
async def test_connection_manager_rate_limit():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 5, "reconnect_min_seconds": 5}
    await mgr.register(42, "ws1", object())
    ok, why = await mgr.can_connect(42, cap)
    assert not ok and why == "rate_limit"


@pytest.mark.asyncio
async def test_connection_manager_unregister_frees_slot():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 1, "reconnect_min_seconds": 0}
    await mgr.register(1, "a", object())
    await mgr.unregister(1, "a")
    ok, _ = await mgr.can_connect(1, cap)
    assert ok


@pytest.mark.asyncio
async def test_disable_and_cut():
    mgr = ConnectionManager()

    class FakeWS:
        def __init__(self):
            self.closed = False

        async def close(self, **k):
            self.closed = True

    ws_a, ws_b = FakeWS(), FakeWS()
    await mgr.register(7, "a", ws_a)
    await mgr.register(7, "b", ws_b)
    cut = await mgr.disable_and_cut(7)
    assert cut == 2
    assert ws_a.closed and ws_b.closed
    assert 7 in mgr.disabled_users
    # A disabled user cannot reconnect until enabled.
    ok, why = await mgr.can_connect(7, {"concurrent_sessions": 5})
    assert not ok and why == "disabled"
    await mgr.enable(7)
    assert 7 not in mgr.disabled_users


# ---- try_register: atomic admission (the TOCTOU fix) ------------------------


@pytest.mark.asyncio
async def test_try_register_enforces_concurrent_cap():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 1, "reconnect_min_seconds": 0}
    ok1, _ = await mgr.try_register(1, "a", object(), cap)
    ok2, why2 = await mgr.try_register(1, "b", object(), cap)
    assert ok1 and not ok2 and why2 == "concurrent_limit"


@pytest.mark.asyncio
async def test_try_register_race_only_one_wins():
    # 10 simultaneous connects for one user with a cap of 1 → exactly one slot.
    # (A check-then-register would let several through across await points.)
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 1, "reconnect_min_seconds": 0}
    results = await asyncio.gather(
        *[mgr.try_register(1, f"w{i}", object(), cap) for i in range(10)]
    )
    assert sum(1 for ok, _ in results if ok) == 1


@pytest.mark.asyncio
async def test_try_register_power_tier_allows_two():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 2, "reconnect_min_seconds": 0}
    assert (await mgr.try_register(1, "a", object(), cap))[0]
    assert (await mgr.try_register(1, "b", object(), cap))[0]
    ok3, why3 = await mgr.try_register(1, "c", object(), cap)
    assert not ok3 and why3 == "concurrent_limit"


@pytest.mark.asyncio
async def test_try_register_rate_window_blocks_fourth():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 99, "reconnect_min_seconds": 0}
    for i in range(3):
        ok, _ = await mgr.try_register(1, f"w{i}", object(), cap)
        assert ok
    ok4, why4 = await mgr.try_register(1, "w3", object(), cap)
    assert not ok4 and why4 == "rate_limit_window"


@pytest.mark.asyncio
async def test_try_register_reconnect_min_blocks_rapid():
    mgr = ConnectionManager()
    cap = {"concurrent_sessions": 99, "reconnect_min_seconds": 100}
    await mgr.try_register(1, "a", object(), cap)
    ok, why = await mgr.try_register(1, "b", object(), cap)
    assert not ok and why == "rate_limit"


@pytest.mark.asyncio
async def test_try_register_rejects_disabled_user():
    mgr = ConnectionManager()
    await mgr.disable_and_cut(1)
    ok, why = await mgr.try_register(1, "a", object(), {"concurrent_sessions": 99})
    assert not ok and why == "disabled"


# ---- anomaly detector: realtime_proxy._update_anomaly_score -----------------


def test_anomaly_throttles_and_audits_at_4sigma(tmp_db):
    _seed_usage(3, [60] * 14)  # tight baseline
    realtime_proxy._update_anomaly_score(3, 3600)  # a wildly long session
    f = db.get_user_flags(3)
    assert f and f["throttle_until"] and f["throttle_until"] > time.time()
    events = db.status_events_for_user(3)
    assert len(events) == 1
    assert events[0]["action"] == "throttle" and events[0]["actor_id"] is None
    assert "z=" in (events[0]["reason"] or "")
    ok, broken = db.verify_audit_chain()
    assert ok is True and broken is None


def test_anomaly_noop_without_baseline(tmp_db):
    _seed_usage(3, [60, 60, 60])  # <4 samples → no scoring
    realtime_proxy._update_anomaly_score(3, 3600)
    assert db.get_user_flags(3) is None
    assert db.status_events_for_user(3) == []


def test_anomaly_normal_session_no_throttle(tmp_db):
    _seed_usage(3, [60, 62, 58, 61, 59])
    realtime_proxy._update_anomaly_score(3, 60)  # z ~ 0
    f = db.get_user_flags(3)
    assert f is not None and not f["throttle_until"]
    assert db.status_events_for_user(3) == []


# ---- warning ticker: realtime_proxy._warning_ticker -------------------------


async def _run_ticker(monkeypatch, cap, *, day_used, month_used, iterations=3, auto_refill=False):
    injected: list[str] = []

    async def fake_inject(_ws, text):
        injected.append(text)

    n = {"i": 0}

    async def fake_sleep(_seconds):
        n["i"] += 1
        if n["i"] > iterations:
            raise asyncio.CancelledError

    monkeypatch.setattr(realtime_proxy, "_inject_assistant_message", fake_inject)
    monkeypatch.setattr(realtime_proxy.db, "seconds_used_today", lambda uid: day_used)
    monkeypatch.setattr(realtime_proxy.metering, "seconds_used_this_month", lambda uid: month_used)
    monkeypatch.setattr(realtime_proxy, "_is_auto_refill_enabled", lambda uid: auto_refill)
    monkeypatch.setattr(realtime_proxy.asyncio, "sleep", fake_sleep)
    with contextlib.suppress(asyncio.CancelledError):
        await realtime_proxy._warning_ticker(object(), 1, cap)
    return injected


@pytest.mark.asyncio
async def test_warning_ticker_fires_each_threshold_once(monkeypatch):
    cap = {"daily_seconds": 600, "monthly_seconds": 3600}
    injected = await _run_ticker(monkeypatch, cap, day_used=999, month_used=9999)
    # 3 loop iterations but the `given` set dedups → exactly one daily + one monthly.
    assert len(injected) == 2
    assert any("minutos" in t for t in injected)
    assert any("auto-recarga" in t or "90%" in t for t in injected)


@pytest.mark.asyncio
async def test_warning_ticker_monthly_autorefill_message(monkeypatch):
    cap = {"daily_seconds": 0, "monthly_seconds": 3600}  # daily off; only monthly
    injected = await _run_ticker(monkeypatch, cap, day_used=0, month_used=9999, auto_refill=True)
    assert len(injected) == 1 and "recargo" in injected[0]


@pytest.mark.asyncio
async def test_warning_ticker_free_tier_silent(monkeypatch):
    cap = {"daily_seconds": 0, "monthly_seconds": 0}
    injected = await _run_ticker(monkeypatch, cap, day_used=999, month_used=999)
    assert injected == []


# ---- admin kill-switch endpoints -------------------------------------------


def _admin_client(monkeypatch, admin_email="boss@x.com"):
    c = TestClient(app, follow_redirects=False)
    c.post("/api/auth/register", json={"email": admin_email, "password": "correcthorse9"})
    monkeypatch.setattr(settings, "ADMIN_EMAILS", admin_email)
    return c


def test_admin_disable_requires_admin(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
    c = TestClient(app, follow_redirects=False)
    assert c.post("/api/admin/disable-user", json={"email": "x@y.com"}).status_code == 401
    c.post("/api/auth/register", json={"email": "user@x.com", "password": "correcthorse9"})
    assert c.post("/api/admin/disable-user", json={"email": "x@y.com"}).status_code == 403


def test_admin_disable_side_effects(tmp_db, monkeypatch):
    c = _admin_client(monkeypatch)
    target = db.create_local_user("target@x.com", "x" * 60)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO device_tokens(user_id, token_hash, device_name, created_at) VALUES(?,?,?,?)",
            (target["id"], "hh", "Mac", time.time()),
        )
        conn.commit()
    finally:
        conn.close()

    r = c.post("/api/admin/disable-user", json={"email": "target@x.com", "reason": "abuse"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["devices_revoked"] == 1

    f = db.get_user_flags(target["id"])
    assert f["disabled"] == 1 and f["disabled_reason"] == "abuse"
    events = db.status_events_for_user(target["id"])
    assert len(events) == 1 and events[0]["action"] == "disable"
    assert events[0]["actor_id"] == db.get_user_by_email("boss@x.com")["id"]
    ok, _ = db.verify_audit_chain()
    assert ok
    # tokens actually revoked
    assert db.revoke_all_device_tokens_for_user(target["id"]) == 0

    assert c.post("/api/admin/disable-user", json={"email": "nope@x.com"}).status_code == 404
    assert c.post("/api/admin/disable-user", json={"reason": "x"}).status_code == 400


def test_admin_bad_json_returns_400(tmp_db, monkeypatch):
    c = _admin_client(monkeypatch)
    r = c.post("/api/admin/disable-user", content="not json",
               headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_admin_enable_writes_audit(tmp_db, monkeypatch):
    c = _admin_client(monkeypatch)
    target = db.create_local_user("t2@x.com", "x" * 60)
    db.set_user_disabled(target["id"], "abuse")
    r = c.post("/api/admin/enable-user", json={"email": "t2@x.com"})
    assert r.status_code == 200
    assert db.get_user_flags(target["id"])["disabled"] == 0
    assert "enable" in [e["action"] for e in db.status_events_for_user(target["id"])]
    ok, _ = db.verify_audit_chain()
    assert ok
