"""Prompt 26.1-C: post_to_x token auto-refresh + 401 retry."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from tools import social_tool as s


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {"data": {"id": "1"}}

    def json(self):
        return self._body


def _seq_httpx(statuses, capture=None):
    """An AsyncClient whose successive .post() calls return the given statuses."""
    seq = iter(statuses)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if capture is not None:
                capture.setdefault("headers", []).append(kw.get("headers", {}))
            return _Resp(next(seq))

    return lambda *a, **k: _Client()


@pytest.fixture
def x_ready(monkeypatch):
    """X is set up: a stored access token + client id, store() is a no-op."""
    monkeypatch.setattr(s.settings, "X_CLIENT_ID", "CID")
    monkeypatch.setattr(s.secrets, "store", AsyncMock())
    return monkeypatch


def _retrieve(values: dict):
    async def _r(label):
        return values.get(label)

    return _r


class TestRefreshTiming:
    @pytest.mark.asyncio
    async def test_future_expiry_no_refresh(self, x_ready):
        x_ready.setattr(
            s.secrets,
            "retrieve",
            _retrieve({"X_ACCESS_TOKEN": "AT", "X_TOKEN_EXPIRES_AT": str(time.time() + 3600)}),
        )
        refresh = AsyncMock()
        x_ready.setattr(s, "_refresh_x_token", refresh)
        x_ready.setattr(s.httpx, "AsyncClient", _seq_httpx([201]))
        r = await s.post_to_x("hola", confirmed=True)
        assert r.success and r.data["via"] == "api"
        refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_past_expiry_triggers_refresh(self, x_ready):
        x_ready.setattr(
            s.secrets,
            "retrieve",
            _retrieve(
                {
                    "X_ACCESS_TOKEN": "OLD",
                    "X_TOKEN_EXPIRES_AT": str(time.time() - 10),
                    "X_REFRESH_TOKEN": "RT",
                }
            ),
        )
        x_ready.setattr(
            s.x_oauth,
            "refresh_access_token",
            AsyncMock(return_value={"access_token": "NEW", "expires_in": 7200}),
        )
        cap: dict = {}
        x_ready.setattr(s.httpx, "AsyncClient", _seq_httpx([201], cap))
        r = await s.post_to_x("hola", confirmed=True)
        assert r.success
        assert cap["headers"][0]["Authorization"] == "Bearer NEW"  # used the refreshed token


class TestRetryOn401:
    @pytest.mark.asyncio
    async def test_401_then_refresh_then_success(self, x_ready):
        x_ready.setattr(
            s.secrets,
            "retrieve",
            _retrieve(
                {
                    "X_ACCESS_TOKEN": "AT",
                    "X_TOKEN_EXPIRES_AT": str(time.time() + 3600),
                    "X_REFRESH_TOKEN": "RT",
                }
            ),
        )
        x_ready.setattr(
            s.x_oauth, "refresh_access_token", AsyncMock(return_value={"access_token": "NEW2"})
        )
        x_ready.setattr(s.httpx, "AsyncClient", _seq_httpx([401, 201]))
        r = await s.post_to_x("hola", confirmed=True)
        assert r.success and r.data["via"] == "api"

    @pytest.mark.asyncio
    async def test_401_twice_prompts_reauth(self, x_ready):
        x_ready.setattr(
            s.secrets,
            "retrieve",
            _retrieve(
                {
                    "X_ACCESS_TOKEN": "AT",
                    "X_TOKEN_EXPIRES_AT": str(time.time() + 3600),
                    "X_REFRESH_TOKEN": "RT",
                }
            ),
        )
        x_ready.setattr(
            s.x_oauth, "refresh_access_token", AsyncMock(return_value={"access_token": "NEW"})
        )
        x_ready.setattr(s.httpx, "AsyncClient", _seq_httpx([401, 401]))
        r = await s.post_to_x("hola", confirmed=True)
        assert not r.success and "emma.x_setup" in r.user_message


class TestNotSetUp:
    @pytest.mark.asyncio
    async def test_no_token_prompts_setup(self, monkeypatch):
        monkeypatch.setattr(s.secrets, "retrieve", _retrieve({}))
        monkeypatch.setattr(s, "_open", AsyncMock())
        r = await s.post_to_x("hola", confirmed=True)
        assert not r.success and r.data["needs_setup"]
