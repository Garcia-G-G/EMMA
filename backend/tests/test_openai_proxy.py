"""CLIENT-INSTALL Phase 2A — /v1/* OpenAI HTTP proxy + token metering."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, openai_proxy
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


def _last_usage(user_id):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT * FROM usage_events WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
    finally:
        conn.close()


class _FakeResp:
    def __init__(self, status, data, ctype="application/json"):
        self.status_code = status
        self._data = data
        self.content = json.dumps(data).encode()
        self.headers = {"content-type": ctype}

    def json(self):
        return self._data


def _amock(val):
    async def f(*a, **k):
        return val
    return f


class _FakeStream:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b"".join(self._chunks)


class _DoneUpstream:
    """Realtime upstream that emits one response.done (with usage) then idles."""
    def __init__(self, *a, **k):
        self._q: asyncio.Queue = asyncio.Queue()
        self._sent = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        if not self._sent:
            self._sent = True
            await self._q.put(json.dumps({"type": "response.done", "response": {"usage": {
                "input_tokens": 40, "output_tokens": 12,
                "input_token_details": {"cached_tokens": 5, "audio_tokens": 30}}}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()


def test_missing_bearer_401():
    c = TestClient(app)
    r = c.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []})
    assert r.status_code == 401


def test_invalid_bearer_401():
    c = TestClient(app)
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer " + "z" * 40},
               json={"model": "gpt-4o-mini", "messages": []})
    assert r.status_code == 401


def test_http_nonstream_proxies_and_meters(monkeypatch):
    u, tok = _user_token()
    fake = _FakeResp(200, {"choices": [{"message": {"content": "hi"}}], "model": "gpt-4o-mini",
                           "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                     "prompt_tokens_details": {"cached_tokens": 10}}})
    monkeypatch.setattr(openai_proxy._CLIENT, "request", _amock(fake))
    c = TestClient(app)
    r = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {tok}"},
               json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["choices"]
    row = _last_usage(u["id"])
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50
    assert row["cached_tokens"] == 10 and row["kind"] == "http" and row["model"] == "gpt-4o-mini"


def test_http_stream_meters_from_last_frame(monkeypatch):
    u, tok = _user_token()
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"model":"gpt-4o-mini","usage":{"prompt_tokens":30,"completion_tokens":8}}\n\n',
        b'data: [DONE]\n\n',
    ]
    monkeypatch.setattr(openai_proxy._CLIENT, "stream", lambda *a, **k: _FakeStream(chunks))
    c = TestClient(app)
    with c.stream("POST", "/v1/chat/completions", headers={"Authorization": f"Bearer {tok}"},
                  json={"stream": True, "model": "gpt-4o-mini", "messages": []}) as r:
        body = b"".join(r.iter_bytes())
    assert b"hi" in body
    row = _last_usage(u["id"])
    assert row["input_tokens"] == 30 and row["output_tokens"] == 8 and row["kind"] == "http-stream"


def test_ws_realtime_meters_tokens(monkeypatch):
    u, tok = _user_token(plan="pro")
    monkeypatch.setattr("backend.realtime_proxy.websockets.connect", lambda *a, **k: _DoneUpstream())
    c = TestClient(app)
    with c.websocket_connect("/realtime", headers={"Authorization": f"Bearer {tok}"}) as ws:
        ws.send_text('{"type":"response.create"}')
        assert "response.done" in ws.receive_text()
    row = _last_usage(u["id"])
    assert row["kind"] == "realtime"
    assert row["input_tokens"] == 40 and row["output_tokens"] == 12
    assert row["cached_tokens"] == 5 and row["audio_tokens"] == 30
