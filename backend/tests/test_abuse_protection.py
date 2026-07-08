"""ABUSE-PROTECTION-2 — the 7 defense layers.

Covers the pure/unit-testable pieces: db.py flags + hash-chained audit + usage
aggregates, and the in-memory ConnectionManager (Capas 2/3/7). The realtime_proxy
wiring + warning ticker are exercised by the prod smoke steps in
_planning/notes/ABUSE-PROTECTION-VERIFY.md (they need a live OpenAI upstream).
"""

from __future__ import annotations

import time

import pytest

from backend import db
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
