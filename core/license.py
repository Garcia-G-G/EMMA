"""BYO-key licensing on the daemon side (LAUNCH-11 Part 4).

Three rules, in priority order, and the third is the one that usually gets
sacrificed by accident:

1. **Activate once, then work offline.** The activation call happens when the
   user enters their licence key and never again on the critical path. Emma
   starting must not require the network — "works on a plane" is half the point
   of BYO-key.
2. **Fail open.** A daemon that cannot reach the licence server keeps working.
   The alternative converts a Hetzner outage into churn, and it punishes exactly
   the honest users who paid.
3. **This is not DRM.** The source is public; anyone determined removes this
   file in five minutes. That is true of every indie Mac app and it is not the
   business risk. The HMAC below is INTEGRITY, not protection: it catches a
   truncated write and a casual hand-edit of the cache, and it is not intended
   to survive an adversary who has the source in front of them. Adding a real
   signature scheme would mean a new install-time dependency for a check that,
   by design, must not be load-bearing.

The activation call is also the ONLY contact a BYO-key daemon ever has with the
Emma backend. No usage, no heartbeat, no telemetry — see backend/license_routes.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("emma.license")

_BACKEND = "https://api.theemmafamily.com"
_INSTALL_LABEL = "install_id"

# How stale a cached activation may get before we quietly re-check in the
# background. Long, because a re-check must never be something the user waits on.
_RECHECK_AFTER_S = 14 * 86400


def _home() -> Path:
    return Path(os.environ.get("EMMA_HOME") or (Path.home() / ".emma")).expanduser()


def _cache_path() -> Path:
    return _home() / "license.json"


def _integrity_key() -> bytes:
    """Local key for the cache HMAC. Derived, not secret — see the module note."""
    return hashlib.sha256(f"emma-license-v1:{_home()}".encode()).digest()


def _sign(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_integrity_key(), blob, hashlib.sha256).hexdigest()


async def install_id() -> str:
    """A random, stable, opaque id for this install. NOT a machine fingerprint.

    It exists so re-activating the same install is free and activations_max means
    something. It carries nothing about the hardware, the user, or the network,
    and it lives in Keychain beside everything else Emma keeps.
    """
    import secrets as pysecrets

    from core import secrets

    existing = await secrets.retrieve(_INSTALL_LABEL)
    if existing:
        return existing
    fresh = pysecrets.token_urlsafe(16)
    await secrets.store(_INSTALL_LABEL, fresh, kind="install_id")
    return fresh


def cached() -> dict[str, Any] | None:
    """The stored activation, or None if absent/corrupt. Never raises, never
    touches the network."""
    path = _cache_path()
    try:
        if not path.is_file():
            return None
        blob = json.loads(path.read_text())
        payload, sig = blob.get("payload"), blob.get("sig")
        if not isinstance(payload, dict) or not isinstance(sig, str):
            return None
        if not hmac.compare_digest(sig, _sign(payload)):
            log.warning("license_cache_integrity_failed")
            return None
        return payload
    except Exception as exc:
        log.debug("license_cache_unreadable", error=str(exc))
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"payload": payload, "sig": _sign(payload)}))
    except Exception as exc:
        log.warning("license_cache_unwritable", error=str(exc))


def status() -> dict[str, Any]:
    """What this daemon believes about its licence. Local only, instant.

    ``licensed`` is False ONLY when the server explicitly said so and we cached
    that. Absent cache, unreadable cache, expired-but-never-rechecked — all of
    those are treated as licensed, because none of them is evidence of anything
    except that we have not been able to ask.
    """
    payload = cached()
    if payload is None:
        return {"licensed": True, "reason": "unknown", "plan": None}
    if not payload.get("valid", True):
        return {"licensed": False, "reason": "revoked", "plan": payload.get("plan")}
    expires = payload.get("expires_at")
    if expires is not None and float(expires) < time.time():
        # Expired by the cached date. Still licensed: the user may have renewed,
        # and we are not going to hold Emma hostage over a stale local file.
        return {"licensed": True, "reason": "expired_pending_recheck", "plan": payload.get("plan")}
    return {"licensed": True, "reason": "active", "plan": payload.get("plan")}


def needs_recheck() -> bool:
    payload = cached()
    if payload is None:
        return False
    return (time.time() - float(payload.get("checked_at", 0))) > _RECHECK_AFTER_S


async def activate(license_key: str, *, timeout_s: float = 12.0) -> tuple[bool, str]:
    """One call to the licence server. Returns (ok, message_es).

    A NETWORK failure returns ok=True: it is not evidence the licence is bad, and
    the daemon must not refuse to work because we are unreachable. An explicit
    rejection from the server is cached and honoured.
    """
    key = (license_key or "").strip()
    if not key:
        return False, "Escribe tu clave de licencia."

    import httpx

    try:
        iid = await install_id()
    except Exception as exc:
        log.warning("license_install_id_failed", error_type=type(exc).__name__)
        return True, "No pude leer el Keychain; Emma seguirá funcionando."

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                f"{_BACKEND}/api/license/activate",
                json={"license_key": key, "install_id": iid, "device_name": _device_name()},
            )
    except Exception as exc:
        log.warning("license_activate_network_error", error_type=type(exc).__name__)
        return True, "No pude verificar la licencia ahora. Emma funciona; lo reintento después."

    if r.status_code == 200:
        payload = dict(r.json())
        payload["checked_at"] = time.time()
        _write_cache(payload)
        return True, "Licencia activada."

    detail = ""
    with contextlib.suppress(Exception):
        detail = str(r.json().get("detail", ""))
    if r.status_code in (403, 404, 409):
        _write_cache({"valid": False, "reason": detail or "rejected", "checked_at": time.time()})
        return False, detail or "No pudimos activar esa licencia."
    # 5xx and anything unexpected: our problem, not theirs.
    log.warning("license_activate_unexpected_status", status=r.status_code)
    return True, "El servidor de licencias no respondió bien. Emma funciona; lo reintento después."


def _device_name() -> str:
    import platform

    return platform.node() or "Mac"


def clear() -> None:
    """Forget the cached activation (used when switching tiers)."""
    with contextlib.suppress(Exception):
        _cache_path().unlink()
