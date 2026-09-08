"""PAIR-DEVICE-1 — daemon half of RFC 8628 device pairing.

Mirror of backend/device_pairing.py. The device access token is Secret-tier:
stored ONLY in the macOS Keychain (via core.secrets), never on disk or in logs.
Adapted to core.secrets' async, single-label API (store/retrieve/delete/has under
the fixed com.garcia.emma service) — not the sync service+account shape.
"""
from __future__ import annotations

import asyncio
import contextlib
import platform
import socket
import time
from typing import Any

import httpx
import structlog

from core import dictionary
from core import secrets as kc

log = structlog.get_logger("emma.pairing")

_TOKEN_LABEL = "device_token"          # Keychain account under com.garcia.emma
_BACKEND = "https://api.theemmafamily.com"
_USER_AGENT = "Emma-daemon/PAIR-DEVICE-1"


async def is_paired() -> bool:
    return await kc.has(_TOKEN_LABEL)


async def stored_token() -> str | None:
    return await kc.retrieve(_TOKEN_LABEL)


# Phase 2B: sync accessor for the device bearer. Keychain reads are async, but the
# OpenAI SDK clients are built synchronously inside running event loops (where a
# sync Keychain read can't block) — so the token is cached at pairing time and read
# from memory here. Cold-cache fallback uses the sync Keychain read (works pre-loop).
_token_cache: str | None = None


def _invalidate_mode() -> None:
    """Tell settings to re-resolve the tier. Pairing state is one of the two
    things mode() reads, so every mutation of it has to say so (LAUNCH-11)."""
    from config.settings import invalidate_mode_cache

    invalidate_mode_cache()


def cached_token() -> str | None:
    global _token_cache
    if _token_cache:
        return _token_cache
    tok = kc.retrieve_sync(_TOKEN_LABEL)  # None inside a running loop
    if tok:
        _token_cache = tok
    return _token_cache


async def load_token_cache() -> str | None:
    """Populate the sync token cache from Keychain. Called once at pairing/boot."""
    global _token_cache
    _token_cache = await stored_token()
    _invalidate_mode()
    return _token_cache


async def start_pairing() -> dict[str, Any]:
    """Step 1 — fetch a user_code; returned so the orchestrator can speak it."""
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(f"{_BACKEND}/api/device/code", headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        info: dict[str, Any] = r.json()
        return info


async def poll_once(device_code: str) -> tuple[str, dict[str, Any] | None]:
    """One non-blocking exchange attempt. Returns (status, data).

    Status is one of ``paired`` | ``pending`` | ``slow_down`` | ``expired`` |
    ``denied`` | ``network_error``. The app's onboarding drives its own loop off
    this so it can render progress and offer a retry, instead of disappearing
    into ``poll_until_authorized``'s blocking sleep for up to 10 minutes.

    On success the token is persisted to Keychain BEFORE returning, so a crash
    between here and the caller cannot lose a pairing the server already spent.
    """
    async with httpx.AsyncClient(timeout=15.0) as c:
        try:
            r = await c.post(f"{_BACKEND}/api/device/token",
                             json={"device_code": device_code},
                             headers={"User-Agent": _USER_AGENT})
        except httpx.HTTPError as e:
            log.warning("pairing_poll_network_error", error=str(e))
            return ("network_error", None)
        if r.status_code == 200:
            data: dict[str, Any] = r.json()
            await kc.store(_TOKEN_LABEL, data["access_token"], kind="device_token")
            # Persist the paired account's name so the system prompt can address
            # the real user by name instead of the shipped default. This is why
            # a fresh install no longer calls every stranger "the user".
            user = data.get("user") or {}
            name = (user.get("name") or "").strip()
            if name:
                with contextlib.suppress(Exception):
                    dictionary.set_user_field("display_name", name)
            global _token_cache
            _token_cache = data["access_token"]  # the wake loop reads this synchronously
            _invalidate_mode()  # unconfigured -> managed, right now
            log.info("device_paired", user=user.get("email"))
            return ("paired", data)
        try:
            err = (r.json().get("detail") or {}).get("error")
        except Exception:
            err = None
        if err == "slow_down":
            return ("slow_down", None)
        if err in ("expired_token", "access_denied"):
            log.warning("pairing_aborted", reason=err)
            return ("expired" if err == "expired_token" else "denied", None)
        return ("pending", None)


async def poll_until_authorized(device_code: str, interval: int, expires_in: int) -> dict[str, Any] | None:
    """Step 3 — poll until the user authorizes on the web, or the code expires.

    The blocking variant, kept for the daemon's own path. Both share
    ``poll_once`` so the persistence and error mapping cannot drift.
    """
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        status, data = await poll_once(device_code)
        if status == "paired":
            return data
        if status == "slow_down":
            interval += 2
        elif status in ("expired", "denied"):
            return None
    return None


async def revoke_local() -> None:
    """Clear the token from Keychain (does NOT revoke server-side; the user does that)."""
    await kc.delete(_TOKEN_LABEL)
    global _token_cache
    _token_cache = None
    _invalidate_mode()  # managed -> unconfigured (or byok, if a key is present)


def device_name() -> str:
    """Human-friendly name for the dashboard's device list (display only). The
    hostname can carry personal info — fine; it's the user's own account UI, never
    public (CLAUDE.md public-copy rules govern the marketing site, not this)."""
    try:
        n = socket.gethostname()
        return n.replace(".local", "").replace("-", " ")
    except Exception:
        return platform.node() or "Mac"


async def authed_client() -> httpx.AsyncClient:
    """The single chokepoint for authenticated daemon → backend calls, so the token
    never leaks into logs from ad-hoc httpx usage elsewhere."""
    token = await stored_token()
    if not token:
        raise RuntimeError("daemon not paired — call start_pairing() first")
    return httpx.AsyncClient(
        base_url=_BACKEND, timeout=15.0,
        headers={"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT})
