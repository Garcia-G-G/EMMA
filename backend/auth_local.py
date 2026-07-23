"""Email/password auth (LANDING-27, Part B). Complements the OAuth flow in auth.py.

Register / login / logout / password reset, with a per-IP login rate limit
(5 failures / 5 min). Passwords are PBKDF2-SHA256 (backend/passwords.py — stdlib,
no new dep). Sessions reuse the same signed HttpOnly cookie as the OAuth flow.

Email-verification choice (Part B4): we allow login IMMEDIATELY (email_verified
stays 0) and defer verification to first *demo* use rather than block sign-in —
less friction for discovery, and the demo's per-plan caps + rate limits already
bound abuse. Verification-on-demo can be added later without touching this flow.
"""

from __future__ import annotations

import re
import secrets
import time
from collections import defaultdict, deque

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from backend import db
from backend.auth import clear_session_cookie, current_user, set_session_cookie
from backend.config import settings
from backend.netutil import client_ip as _client_ip
from backend.passwords import (
    hash_password,
    password_needs_rehash,
    password_problem,
    verify_password,
)

log = structlog.get_logger("emma.auth")
router = APIRouter()
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# In-process login-failure tracker: ip → recent failure timestamps. Single backend
# instance, never persisted/logged. (At scale, move to Redis.)
_LOGIN_FAILS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=16))
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_S = 300
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def _too_many_fails(ip: str, now: float | None = None) -> bool:
    now = now or time.time()
    fails = _LOGIN_FAILS[ip]
    while fails and fails[0] < now - _LOGIN_WINDOW_S:
        fails.popleft()
    return len(fails) >= _LOGIN_MAX_FAILS


def _record_fail(ip: str) -> None:
    _LOGIN_FAILS[ip].append(time.time())


class _EmailModel(BaseModel):
    @field_validator("email", check_fields=False)
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _EMAIL_RE.match(v) or len(v) > 254:
            raise ValueError("Correo inválido.")
        return v


class Credentials(_EmailModel):
    email: str
    password: str
    name: str = ""  # optional; used by /register (login ignores it)


class ResetRequest(_EmailModel):
    email: str


class ResetConfirm(BaseModel):
    token: str
    new_password: str


def _public_user(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u.get("name"), "plan": u.get("plan", "free")}


@router.post("/api/auth/register")
async def register(body: Credentials, response: Response) -> dict:
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    user = db.create_local_user(
        str(body.email).lower(), hash_password(body.password), name=(body.name or "").strip()[:80])
    if user is None:
        raise HTTPException(409, "Ya existe una cuenta con ese correo.")
    set_session_cookie(response, user["id"])  # auto-login after register
    log.info("user_registered", uid=user["id"])
    return _public_user(user)


@router.post("/api/auth/login")
async def login(body: Credentials, request: Request, response: Response) -> dict:
    ip = _client_ip(request)
    if _too_many_fails(ip):
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    user = db.get_user_by_email(str(body.email).lower())
    # Same error + a real hash check on the miss path → no user-enumeration timing leak.
    stored_hash = user.get("password_hash") if user else None
    ok = verify_password(body.password, stored_hash or _DUMMY_PASSWORD_HASH)
    ok = bool(user and stored_hash and ok)
    if not ok:
        _record_fail(ip)
        raise HTTPException(401, "Correo o contraseña incorrectos.")
    if password_needs_rehash(user["password_hash"]):
        db.set_password(user["id"], hash_password(body.password))
    set_session_cookie(response, user["id"])
    return _public_user(user)


@router.post("/api/auth/logout")
async def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/api/auth/reset-request")
async def reset_request(body: ResetRequest, request: Request) -> dict:
    # Always 200 — never reveal whether the email exists (enumeration guard). But
    # throttle per-IP so this can't be used to email-bomb a victim, drain the Resend
    # quota, or churn a victim's in-flight reset token (shares the login bucket).
    ip = _client_ip(request)
    if _too_many_fails(ip):
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    _record_fail(ip)
    token = secrets.token_urlsafe(32)
    if db.set_reset_token(str(body.email).lower(), token, time.time() + 3600):
        link = f"{settings.PUBLIC_URL}/auth/reset?token={token}"
        await _send_reset_email(str(body.email), link)
    return {"ok": True}


@router.post("/api/auth/reset-confirm")
async def reset_confirm(body: ResetConfirm, response: Response) -> dict:
    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(400, problem)
    user = db.user_by_reset_token(body.token)
    if user is None:
        raise HTTPException(400, "El enlace de recuperación es inválido o expiró.")
    db.set_password(user["id"], hash_password(body.new_password))
    set_session_cookie(response, user["id"])  # log them in on success
    return {"ok": True}


async def _send_reset_email(email: str, link: str) -> None:
    """Send the reset link via Resend if configured; otherwise log (dev no-op)."""
    key = getattr(settings, "RESEND_API_KEY", "")
    if not key:
        log.info("reset_email_skipped", reason="no RESEND_API_KEY")
        return
    import httpx
    with __import__("contextlib").suppress(Exception):
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}"},
                json={"from": "Emma <noreply@theemmafamily.com>", "to": [email],
                      "subject": "Restablece tu contraseña de Emma",
                      "html": f'<p>Para restablecer tu contraseña, abre este enlace '
                              f'(válido 1 hora):</p><p><a href="{link}">{link}</a></p>'},
            )


@router.get("/api/auth/whoami")
async def whoami(request: Request) -> dict:
    user = await current_user(request)
    return {"authenticated": user is not None,
            "user": _public_user(user) if user else None}
