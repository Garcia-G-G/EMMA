"""License activation for the BYO-key tier (LAUNCH-11 Part 4).

BYO-key sells a licence to the software, not metered usage, so the backend's
entire relationship with such a daemon is this one endpoint, called once.

What it deliberately does NOT do:

* It does not see the user's OpenAI key. Ever, in any form. That is the tier's
  product claim and the reason this endpoint takes a licence key and nothing
  else.
* It does not receive usage. No minutes, no turns, no model names, no
  heartbeat. A licensed daemon is a black box to us, on purpose.
* It does not fingerprint the machine. ``install_id`` is a random value the
  daemon generated for itself; it exists only so that re-activating the same
  install is free, and so activations_max means something. Reinstalling Emma or
  restoring a Mac from a backup must not burn a seat.

The daemon treats a network failure here as "licensed" — see core/license.py.
Refusing to start because our Hetzner box is down would convert an outage into
churn, and the source is public anyway.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend import db

router = APIRouter()

# The ladder. Monthly and annual expire; lifetime does not.
PLANS: dict[str, dict[str, Any]] = {
    "monthly": {"usd": 6.0, "days": 31, "label": "Emma Libre · mensual"},
    "annual": {"usd": 59.0, "days": 366, "label": "Emma Libre · anual"},
    "lifetime": {"usd": 249.0, "days": None, "label": "Emma Libre · de por vida"},
}


def expiry_for(plan: str, now: float | None = None) -> float | None:
    """When a freshly-issued licence of this plan lapses. None = lifetime."""
    days = PLANS.get(plan, {}).get("days")
    if days is None:
        return None
    return (now or time.time()) + days * 86400


def check(license_key: str, install_id: str, device_name: str | None) -> dict[str, Any]:
    """Pure activation logic. Returns the daemon's payload. Testable without HTTP."""
    key = (license_key or "").strip()
    install = (install_id or "").strip()
    if not key or not install:
        raise HTTPException(400, "license_key e install_id son obligatorios.")

    lic = db.get_license(key)
    if not lic:
        raise HTTPException(404, "No encontramos esa licencia.")
    if lic["status"] != "active":
        raise HTTPException(403, "Esa licencia ya no está activa.")
    if lic["expires_at"] is not None and float(lic["expires_at"]) < time.time():
        raise HTTPException(403, "Esa licencia expiró.")

    allowed, used = db.record_activation(int(lic["id"]), install, device_name)
    if not allowed:
        raise HTTPException(
            409,
            f"Esta licencia ya está activa en {used} equipos "
            f"(máximo {lic['activations_max']}).",
        )

    return {
        "valid": True,
        "plan": lic["plan"],
        "expires_at": lic["expires_at"],
        "activations_used": used,
        "activations_max": lic["activations_max"],
    }


@router.post("/api/license/activate")
async def activate(request: Request) -> dict[str, Any]:
    """License key in, valid/invalid out. No account required, no usage recorded."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido.") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON inválido.")

    return check(
        str(body.get("license_key", "")),
        str(body.get("install_id", "")),
        (str(body.get("device_name")) if body.get("device_name") else None),
    )
