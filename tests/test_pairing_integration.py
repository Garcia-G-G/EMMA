"""PAIR-DEVICE-1 (Part H) — daemon-side device pairing (core/pairing.py).

No live backend: httpx is faked with a sequenced client, and the Keychain
(core.secrets) is mocked. Asserts the poll loop honors pending/slow_down and
persists the minted token to the Keychain BEFORE returning.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core import pairing


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def _seq_httpx(responses, capture=None):
    """AsyncClient whose successive .post() calls return the given responses."""
    seq = iter(responses)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if capture is not None:
                capture.append({"url": url, **kw})
            return next(seq)

    return _Client


def _pending():
    return _Resp(401, {"detail": {"error": "authorization_pending"}})


def _slow_down():
    return _Resp(429, {"detail": {"error": "slow_down"}})


def _ok(token="tok-abc123"):
    return _Resp(200, {"access_token": token, "user": {"email": "a@b.com"}})


@pytest.mark.asyncio
async def test_poll_pending_then_success_stores_token(monkeypatch):
    store = AsyncMock()
    monkeypatch.setattr(pairing.kc, "store", store)
    monkeypatch.setattr(pairing.httpx, "AsyncClient", _seq_httpx([_pending(), _ok("secret-xyz")]))

    data = await pairing.poll_until_authorized("dc", interval=0, expires_in=30)

    assert data is not None and data["access_token"] == "secret-xyz"
    # persisted to Keychain before returning, under the device-token label
    store.assert_awaited_once()
    assert store.await_args.args[0] == pairing._TOKEN_LABEL
    assert store.await_args.args[1] == "secret-xyz"


@pytest.mark.asyncio
async def test_poll_slow_down_backs_off_then_succeeds(monkeypatch):
    monkeypatch.setattr(pairing.kc, "store", AsyncMock())
    monkeypatch.setattr(pairing.httpx, "AsyncClient", _seq_httpx([_slow_down(), _ok()]))

    data = await pairing.poll_until_authorized("dc", interval=0, expires_in=30)
    assert data is not None and data["access_token"] == "tok-abc123"


@pytest.mark.asyncio
async def test_poll_access_denied_returns_none(monkeypatch):
    store = AsyncMock()
    monkeypatch.setattr(pairing.kc, "store", store)
    denied = _Resp(401, {"detail": {"error": "access_denied"}})
    monkeypatch.setattr(pairing.httpx, "AsyncClient", _seq_httpx([denied]))

    assert await pairing.poll_until_authorized("dc", interval=0, expires_in=30) is None
    store.assert_not_awaited()  # never store a token we didn't get


@pytest.mark.asyncio
async def test_authed_client_requires_pairing(monkeypatch):
    monkeypatch.setattr(pairing.kc, "retrieve", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError):
        await pairing.authed_client()


@pytest.mark.asyncio
async def test_is_paired_and_revoke(monkeypatch):
    monkeypatch.setattr(pairing.kc, "has", AsyncMock(return_value=True))
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(pairing.kc, "delete", delete)

    assert await pairing.is_paired() is True
    await pairing.revoke_local()
    delete.assert_awaited_once_with(pairing._TOKEN_LABEL)
