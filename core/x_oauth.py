"""X / Twitter OAuth 2.0 PKCE flow — stdlib + httpx, no new dependency (26.1).

X requires an OAuth 2.0 *user-context* token to post (app Bearer is forbidden);
the public-client PKCE flow (RFC 7636) mints one without a client secret. This
module is the reusable plumbing; ``emma/x_setup.py`` drives the one-time consent
and ``tools/social_tool.py`` calls :func:`refresh_access_token` on expiry.

Endpoints (X OAuth 2.0 user-context docs):
  authorize  https://x.com/i/oauth2/authorize
  token      https://api.x.com/2/oauth2/token   (form-encoded; client_id in body)

Refs: RFC 7636 (PKCE) · docs.x.com/resources/fundamentals/authentication/oauth-2-0.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import secrets as _secrets
import subprocess
import time
import urllib.parse
from typing import Any

import httpx
import structlog

from config.settings import settings
from core import secrets

log = structlog.get_logger("emma.x_oauth")

_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
_TOKEN_URL = "https://api.x.com/2/oauth2/token"


def make_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge). Verifier is 43-128 URL-safe chars;
    challenge is base64url(SHA256(verifier)) with no padding (RFC 7636 S256)."""
    verifier = _secrets.token_urlsafe(64)  # ~86 chars, within the 43-128 bound
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_state() -> str:
    """An unguessable CSRF state for the authorize round-trip."""
    return _secrets.token_urlsafe(24)


def build_authorize_url(
    client_id: str, redirect_uri: str, scope: str, code_challenge: str, state: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def _token_request(data: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        resp = await client.post(
            _TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    resp.raise_for_status()
    return dict(resp.json())


async def exchange_code(
    client_id: str, code: str, code_verifier: str, redirect_uri: str
) -> dict[str, Any]:
    """Authorization code → token dict {access_token, refresh_token, expires_in, scope}."""
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    )


async def refresh_access_token(client_id: str, refresh_token: str) -> dict[str, Any]:
    """Refresh token → a fresh token dict. X may or may not rotate the refresh
    token (both behaviors are spec) — callers keep the old one if none is returned."""
    return await _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    )


def run_callback_server(
    expected_state: str, port: int = 8723, timeout_s: int = 300
) -> dict[str, str]:
    """Block on a one-shot localhost server for the OAuth redirect.

    Returns ``{"code": ..., "state": ...}`` once X redirects to
    ``/callback?code=&state=``. The ``state`` is checked against
    ``expected_state`` (CSRF) — a mismatch never yields a code. Raises
    ``TimeoutError`` if Garcia doesn't authorize within ``timeout_s``.
    """
    result: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _page(self, status: int, msg: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body style='font-family:sans-serif;text-align:center;"
                f"margin-top:18%'><h2>{msg}</h2></body></html>".encode()
            )

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != urllib.parse.urlparse(settings.X_REDIRECT_URI).path:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            if not code or state != expected_state:
                result["error"] = "state_mismatch" if code else (qs.get("error") or ["no_code"])[0]
                self._page(400, "Algo salió mal con la autorización. Cierra y reintenta.")
                return
            result["code"] = code
            result["state"] = state
            self._page(200, "¡Listo! Ya puedes cerrar esta pestaña y volver a la Terminal.")

        def log_message(self, *args: Any) -> None:  # silence stdlib request logging
            return

    server = http.server.HTTPServer(("localhost", port), _Handler)
    deadline = time.monotonic() + timeout_s
    try:
        while "code" not in result and "error" not in result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no OAuth redirect received before timeout")
            server.timeout = remaining
            server.handle_request()  # one request (or favicon, etc.) then loop
    finally:
        server.server_close()
    if "code" not in result:
        raise ValueError(f"authorization failed: {result.get('error', 'unknown')}")
    return result


# ---- setup-time entry points (consumed by emma.setup, 26.2) ---------------


async def _store_tokens(tokens: dict[str, Any]) -> None:
    access = tokens.get("access_token")
    if not access:
        raise ValueError("X did not return an access_token")
    await secrets.store("X_ACCESS_TOKEN", access, kind="oauth_token")
    if tokens.get("refresh_token"):
        await secrets.store("X_REFRESH_TOKEN", tokens["refresh_token"], kind="oauth_token")
    expires_at = int(time.time()) + int(tokens.get("expires_in", 7200))
    await secrets.store("X_TOKEN_EXPIRES_AT", str(expires_at), kind="oauth_meta")


async def run_pkce_setup(non_interactive: bool = False) -> bool:
    """Run the browser PKCE consent and persist the tokens. True on success.

    Pure auth flow — no user-decision logic (the orchestrator owns "do you use
    X?"). Returns False when X_CLIENT_ID is unset or we're non-interactive (the
    flow needs a browser), so the caller can message accordingly.
    """
    if not settings.X_CLIENT_ID or non_interactive:
        return False
    verifier, challenge = make_pkce_pair()
    state = make_state()
    url = build_authorize_url(
        settings.X_CLIENT_ID, settings.X_REDIRECT_URI, settings.X_SCOPES, challenge, state
    )
    port = urllib.parse.urlparse(settings.X_REDIRECT_URI).port or 8723
    print("Abriendo X.com para que autorices a Emma…")
    print(f"(Si no se abre solo, pega esto en tu navegador:\n  {url}\n)")
    subprocess.run(["open", url], check=False)
    try:
        cb = await asyncio.to_thread(run_callback_server, state, port)
        tokens = await exchange_code(
            settings.X_CLIENT_ID, cb["code"], verifier, settings.X_REDIRECT_URI
        )
        await _store_tokens(tokens)
    except Exception as exc:
        log.warning("x_pkce_setup_failed", error=str(exc))
        return False
    return True


async def token_status() -> str:
    """'valid' | 'expired' | 'missing' — reads Keychain, never prompts."""
    access = await secrets.retrieve("X_ACCESS_TOKEN")
    if not access:
        return "missing"
    expires_at = await secrets.retrieve("X_TOKEN_EXPIRES_AT")
    if expires_at:
        try:
            if time.time() > float(expires_at):
                return "expired"  # a refresh on next post will try to renew
        except ValueError:
            pass
    return "valid"
