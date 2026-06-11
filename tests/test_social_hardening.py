"""B50 — post_to_x hardening: single-flight refresh, 401/403/429 disambiguation,
tweet-content secret-leak guard, URL curly-quote normalization."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from tools import social_tool as s


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {"data": {"id": "1"}}
        self.headers = headers or {}

    def json(self):
        return self._body


def _seq_httpx(responses):
    seq = iter(responses)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            r = next(seq)
            return r if isinstance(r, _Resp) else _Resp(r)

    return lambda *a, **k: _Client()


def _ready(monkeypatch, store, expires_offset=3600):
    """X is set up with a token whose expiry is `expires_offset` s away."""
    store.setdefault("X_ACCESS_TOKEN", "AT")
    store.setdefault("X_TOKEN_EXPIRES_AT", str(time.time() + expires_offset))
    store.setdefault("X_REFRESH_TOKEN", "RT")

    async def _ret(label):
        return store.get(label)

    monkeypatch.setattr(s.secrets, "retrieve", _ret)
    monkeypatch.setattr(s.secrets, "store", AsyncMock())
    monkeypatch.setattr(s.settings, "X_CLIENT_ID", "CID")


# ---- B50.1 single-flight refresh --------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refresh_is_single_flight(monkeypatch):
    """Two callers hitting a stale token at once must POST /oauth2/token ONCE —
    a double refresh spends the one-time refresh token twice → invalid_grant."""
    store = {"X_ACCESS_TOKEN": "a0", "X_REFRESH_TOKEN": "r0",
             "X_TOKEN_EXPIRES_AT": str(time.time() - 10)}

    async def _ret(label):
        return store.get(label)

    async def _store(label, value, kind="secret"):
        store[label] = value

    monkeypatch.setattr(s.secrets, "retrieve", _ret)
    monkeypatch.setattr(s.secrets, "store", _store)
    monkeypatch.setattr(s.settings, "X_CLIENT_ID", "CID")

    calls = {"n": 0}

    async def _refresh(cid, rt):
        calls["n"] += 1
        await asyncio.sleep(0)  # yield so the second caller contends for the lock
        return {"access_token": "a1", "refresh_token": "r1", "expires_in": 7200}

    monkeypatch.setattr(s.x_oauth, "refresh_access_token", _refresh)

    results = await asyncio.gather(s._refresh_x_token("a0"), s._refresh_x_token("a0"))
    assert calls["n"] == 1
    assert results == ["a1", "a1"]  # both callers end up with the fresh token


# ---- B50.2 disambiguation ---------------------------------------------------


@pytest.mark.asyncio
async def test_403_scope_does_not_refresh(monkeypatch):
    """403 = missing tweet.write scope; refreshing can't grant scope, so DON'T."""
    _ready(monkeypatch, {})
    refresh = AsyncMock()
    monkeypatch.setattr(s, "_refresh_x_token", refresh)
    monkeypatch.setattr(s.httpx, "AsyncClient", _seq_httpx([_Resp(403, {"title": "Forbidden"})]))
    r = await s.post_to_x("hola", confirmed=True)
    assert not r.success
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_401_invalid_request_does_not_refresh(monkeypatch):
    """401 invalid_request = malformed bearer; a refresh won't fix it."""
    _ready(monkeypatch, {})
    refresh = AsyncMock()
    monkeypatch.setattr(s, "_refresh_x_token", refresh)
    monkeypatch.setattr(
        s.httpx, "AsyncClient", _seq_httpx([_Resp(401, {"error": "invalid_request"})])
    )
    r = await s.post_to_x("hola", confirmed=True)
    assert not r.success
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_401_invalid_token_refreshes_and_retries(monkeypatch):
    """401 with no invalid_request marker = expired/invalid_token → refresh+retry once."""
    _ready(monkeypatch, {})
    monkeypatch.setattr(s, "_refresh_x_token", AsyncMock(return_value="NEW"))
    monkeypatch.setattr(s.httpx, "AsyncClient", _seq_httpx([_Resp(401), _Resp(201)]))
    r = await s.post_to_x("hola", confirmed=True)
    assert r.success and r.data["via"] == "api"


@pytest.mark.asyncio
async def test_429_reports_reset_minutes(monkeypatch):
    """429 surfaces the x-rate-limit-reset header as a wait time; no refresh."""
    _ready(monkeypatch, {})
    reset = str(int(time.time()) + 120)
    monkeypatch.setattr(
        s.httpx, "AsyncClient",
        _seq_httpx([_Resp(429, {}, {"x-rate-limit-reset": reset})]),
    )
    r = await s.post_to_x("hola", confirmed=True)
    assert not r.success
    assert "min" in r.user_message.lower()


# ---- B50.3 content guard ----------------------------------------------------


@pytest.mark.asyncio
async def test_tweet_with_secret_is_refused(monkeypatch):
    """A tweet carrying an API-key-shaped secret is refused BEFORE any POST."""
    _ready(monkeypatch, {})
    posted = _seq_httpx([_Resp(201)])
    monkeypatch.setattr(s.httpx, "AsyncClient", posted)
    r = await s.post_to_x("mi llave es re_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", confirmed=True)
    assert not r.success
    assert r.data.get("blocked") == "sensitive"


@pytest.mark.asyncio
async def test_normal_tweet_is_not_refused(monkeypatch):
    _ready(monkeypatch, {})
    monkeypatch.setattr(s.httpx, "AsyncClient", _seq_httpx([_Resp(201)]))
    r = await s.post_to_x("buenos días a todos, feliz lunes", confirmed=True)
    assert r.success


@pytest.mark.asyncio
async def test_long_hashtag_is_not_refused(monkeypatch):
    """A long hashtag is pure letters, not a secret — must NOT be blocked
    (the old full-redaction guard false-positived on any 32+ char run)."""
    _ready(monkeypatch, {})
    monkeypatch.setattr(s.httpx, "AsyncClient", _seq_httpx([_Resp(201)]))
    r = await s.post_to_x("vamos #SuperLargoHashtagDeLanzamientoParaEmmaHoy", confirmed=True)
    assert r.success


@pytest.mark.asyncio
async def test_phone_number_is_not_refused(monkeypatch):
    """Sharing a phone number is a legitimate tweet; a phone is not a secret."""
    _ready(monkeypatch, {})
    monkeypatch.setattr(s.httpx, "AsyncClient", _seq_httpx([_Resp(201)]))
    r = await s.post_to_x("llámame al +52 81 1234 5678", confirmed=True)
    assert r.success


@pytest.mark.asyncio
async def test_secret_in_url_query_is_refused(monkeypatch):
    """A key hidden in a URL query param must still be caught — the guard scans
    the whole text, not a URL-stripped version (closes the query-param leak)."""
    _ready(monkeypatch, {})
    monkeypatch.setattr(s.httpx, "AsyncClient", _seq_httpx([_Resp(201)]))
    r = await s.post_to_x(
        "mira https://dash.example.com/?api_key=re_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        confirmed=True,
    )
    assert not r.success
    assert r.data.get("blocked") == "sensitive"


@pytest.mark.asyncio
async def test_secret_tweet_never_reaches_the_network(monkeypatch):
    """Privacy invariant: a secret-bearing tweet must never call _x_post — the
    guard refuses before any network I/O."""
    _ready(monkeypatch, {})
    called = {"n": 0}

    async def _spy(*a, **k):
        called["n"] += 1
        return 201, {"data": {"id": "1"}}, {}

    monkeypatch.setattr(s, "_x_post", _spy)
    r = await s.post_to_x("la clave es re_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", confirmed=True)
    assert not r.success
    assert called["n"] == 0


def test_curly_quotes_in_urls_are_normalized():
    """Realtime LLM sometimes wraps URLs in smart quotes — convert them inside the
    URL so the link isn't broken; prose quotes are left untouched."""
    out = s._normalize_urls("mira https://x.com/“path” y dime “qué tal”")
    assert 'https://x.com/"path"' in out  # curly → straight inside the URL token
    assert "“qué tal”" in out  # prose quotes preserved
