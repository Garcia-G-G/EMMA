"""Account management + downloads (LANDING-27, Parts D + E).

- /api/downloads/* : the installer .pkg + changelog (login-gated for telemetry).
- /api/me/*        : change password / email, delete account (GDPR soft delete).

All reuse the cookie session from auth.py. No new deps.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from backend import db
from backend.auth import current_user, require_user
from backend.config import plan_caps, settings
from backend.passwords import hash_password, password_problem, verify_password

log = structlog.get_logger("emma.account")
router = APIRouter()

_CHANGELOG = [
    {
        "version": "0.1.0",
        "date": "2026-06",
        "notes": "Primer preview: voz, memoria local, control de apps, visión de pantalla.",
    },
]


# ---- D2: downloads ----------------------------------------------------------


@router.get("/api/downloads/latest")
async def download_latest(request: Request) -> Any:
    """Redirect a logged-in user to the current .pkg (login-gated for telemetry)."""
    await require_user(request)
    url = settings.DOWNLOAD_PKG_URL
    if not url:
        raise HTTPException(503, "La descarga aún no está disponible.")
    return RedirectResponse(url, status_code=302)


@router.get("/api/downloads/changelog")
async def download_changelog() -> dict[str, Any]:
    return {"versions": _CHANGELOG}


# ---- E2: account ------------------------------------------------------------


def _me_payload(user: dict[str, Any]) -> dict[str, Any]:
    caps = plan_caps(user.get("plan"))
    used_today_s = db.user_seconds_today(user["id"])
    daily_s = int(caps["daily_seconds"])
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user.get("name"),
        "plan": user.get("plan", "free"),
        "email_verified": bool(user.get("email_verified")),
        "usage": {
            "today_min": round(used_today_s / 60, 1),
            "daily_cap_min": round(daily_s / 60, 1) if daily_s else None,
            "remaining_min": round(max(0, daily_s - used_today_s) / 60, 1) if daily_s else None,
            "month_min": round(float(user.get("monthly_seconds_used") or 0) / 60, 1),
        },
        "has_subscription": bool(user.get("stripe_customer_id")),
    }


@router.get("/api/account")
async def account(request: Request) -> dict[str, Any]:
    """Richer than /api/me — plan + usage for the dashboard."""
    return _me_payload(await require_user(request))


class PasswordChange(BaseModel):
    current_password: str = ""
    new_password: str


@router.post("/api/me/password")
async def change_password(body: PasswordChange, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    # If they already have a password, require the current one (OAuth users may not).
    if user.get("password_hash") and not verify_password(
        body.current_password, user["password_hash"]
    ):
        raise HTTPException(403, "La contraseña actual no es correcta.")
    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    db.set_password(user["id"], hash_password(body.new_password))
    return {"ok": True}


class EmailChange(BaseModel):
    email: str


@router.post("/api/me/email")
async def change_email(body: EmailChange, request: Request) -> dict[str, Any]:
    user = await require_user(request)
    import re

    new = (body.email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", new):
        raise HTTPException(400, "Correo inválido.")
    if not db.set_email(user["id"], new):
        raise HTTPException(409, "Ese correo ya está en uso.")
    return {"ok": True, "email": new, "email_verified": False}


@router.delete("/api/me")
async def delete_account(request: Request, response: Response) -> dict[str, Any]:
    user = await require_user(request)
    db.soft_delete_user(user["id"])  # anonymize now; hard purge is a later job
    response.delete_cookie("emma_session")
    log.info("account_deleted", uid=user["id"])
    return {"ok": True}


@router.get("/api/me/full")
async def me_full(request: Request) -> Any:
    user = await current_user(request)
    if user is None:
        raise HTTPException(401, "No autenticado.")
    return _me_payload(user)
