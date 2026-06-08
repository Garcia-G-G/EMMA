"""Prompt 26.1-A: X OAuth 2.0 PKCE primitives + the localhost callback server."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core import x_oauth


class _FakeTokenResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _fake_async_client(body, capture=None):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if capture is not None:
                capture["url"] = url
                capture.update(kw)
            return _FakeTokenResp(body)

    return lambda *a, **k: _Client()


class TestPkce:
    def test_pair_is_rfc_compliant(self, monkeypatch):
        monkeypatch.setattr(x_oauth._secrets, "token_urlsafe", lambda n: "VERIFIER_FIXED_VALUE")
        verifier, challenge = x_oauth.make_pkce_pair()
        assert verifier == "VERIFIER_FIXED_VALUE"
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected and "=" not in challenge

    def test_authorize_url(self):
        url = x_oauth.build_authorize_url(
            "CID", "http://localhost:8723/callback", "tweet.write", "CHAL", "STATE"
        )
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert url.startswith("https://x.com/i/oauth2/authorize?")
        assert q["response_type"] == ["code"] and q["client_id"] == ["CID"]
        assert q["code_challenge"] == ["CHAL"] and q["code_challenge_method"] == ["S256"]
        assert q["state"] == ["STATE"]


class TestTokenCalls:
    def test_exchange_code(self, monkeypatch):
        cap = {}
        monkeypatch.setattr(
            x_oauth.httpx,
            "AsyncClient",
            _fake_async_client(
                {"access_token": "AT", "refresh_token": "RT", "expires_in": 7200}, cap
            ),
        )
        out = asyncio.run(
            x_oauth.exchange_code("CID", "CODE", "VERIFIER", "http://localhost:8723/callback")
        )
        assert out["access_token"] == "AT"
        assert cap["data"]["grant_type"] == "authorization_code"
        assert cap["data"]["code_verifier"] == "VERIFIER"

    def test_refresh(self, monkeypatch):
        cap = {}
        monkeypatch.setattr(
            x_oauth.httpx, "AsyncClient", _fake_async_client({"access_token": "AT2"}, cap)
        )
        out = asyncio.run(x_oauth.refresh_access_token("CID", "RT"))
        assert out["access_token"] == "AT2"
        assert cap["data"]["grant_type"] == "refresh_token"
        assert cap["data"]["refresh_token"] == "RT"


class TestCallbackServer:
    def _serve(self, state, port):
        out = {}

        def run():
            try:
                out["res"] = x_oauth.run_callback_server(state, port=port, timeout_s=5)
            except Exception as exc:
                out["err"] = exc

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.25)  # let the socket bind
        return out, t

    def test_returns_code_on_matching_state(self):
        out, t = self._serve("STATE123", 8731)
        urllib.request.urlopen(
            "http://localhost:8731/callback?code=abc&state=STATE123", timeout=3
        ).read()
        t.join(6)
        assert out.get("res", {}).get("code") == "abc"

    def test_rejects_state_mismatch(self):
        out, t = self._serve("GOOD", 8732)
        with contextlib.suppress(urllib.error.HTTPError):  # server returns 400 by design
            urllib.request.urlopen(
                "http://localhost:8732/callback?code=abc&state=BAD", timeout=3
            ).read()
        t.join(6)
        assert "err" in out  # raised — a bad state never yields a code
