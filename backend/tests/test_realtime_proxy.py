"""CLIENT-INSTALL-PIPELINE Phase 1 — managed-voice realtime proxy + metering.

The device (Bearer) path of /realtime: auth, plan-cap enforcement, frame proxying
to a mocked OpenAI upstream, and per-session metering into usage_events.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import db, metering
from backend import device_pairing as dp
from backend.app import app
from backend.config import settings


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    yield


def _user_token(email="a@b.com", plan="pro"):
    u = db.create_local_user(email, "x" * 60)
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, u["id"]))
        conn.commit()
    finally:
        conn.close()
    info = dp.issue_device_code()
    dp.authorize_user_code(info["user_code"], u["id"], "Test Mac")
    tok = dp.exchange_device_code(info["device_code"], None)["access_token"]
    return u, tok


class _FakeUpstream:
    """Stand-in for the OpenAI realtime socket: echoes one frame per sent frame."""
    def __init__(self, *a, **k):
        self._q: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        await self._q.put('{"type":"echo"}')

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._q.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


def test_invalid_token_closes_4401():
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei, \
            c.websocket_connect("/realtime", headers={"Authorization": "Bearer " + "z" * 40}) as ws:
        ws.receive_text()
    assert ei.value.code == 4401


def test_over_monthly_cap_closes_4402():
    u, tok = _user_token(plan="pro")  # pro managed cap = 3600s
    metering.record_usage(u["id"], 1, 4000)  # over cap
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei, \
            c.websocket_connect("/realtime", headers={"Authorization": f"Bearer {tok}"}) as ws:
        ws.receive_text()
    assert ei.value.code == 4402


def test_valid_session_proxies_both_ways_and_meters(monkeypatch):
    u, tok = _user_token(plan="pro")
    monkeypatch.setattr("backend.realtime_proxy.websockets.connect", lambda *a, **k: _FakeUpstream())
    calls = []
    monkeypatch.setattr("backend.realtime_proxy.metering.record_usage",
                        lambda uid, did, sec=0, **kw: calls.append((uid, did, sec)))
    c = TestClient(app)
    with c.websocket_connect("/realtime", headers={"Authorization": f"Bearer {tok}"}) as ws:
        ws.send_text('{"type":"input_audio_buffer.append"}')  # daemon → backend → (fake) OpenAI
        assert '"echo"' in ws.receive_text()                   # (fake) OpenAI → backend → daemon
    assert len(calls) == 1 and calls[0][0] == u["id"]          # metered on close


def test_record_usage_inserts_row_and_bumps_counters():
    u = db.create_local_user("m@b.com", "x" * 60)
    metering.record_usage(u["id"], 1, 10)
    metering.record_usage(u["id"], 1, 0)  # zero is a no-op
    conn = db.connect()
    try:
        rows = conn.execute("SELECT seconds FROM usage_events WHERE user_id=?", (u["id"],)).fetchall()
        usr = conn.execute(
            "SELECT monthly_seconds_used, monthly_session_count FROM users WHERE id=?", (u["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert [r["seconds"] for r in rows] == [10]  # the 0 was skipped
    assert usr["monthly_seconds_used"] == 10 and usr["monthly_session_count"] == 1
    assert metering.minutes_used_this_month(u["id"]) == pytest.approx(10 / 60)
