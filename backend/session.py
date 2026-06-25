"""Session tokens, captcha, rate limit, cost guard (Prompt 31, A3 / A4).

`/api/session/start` is the gate: captcha → rate limit → budget → signed JWT. The
JWT (5-min TTL) is the ONLY thing the browser gets; it carries the server-decided
``max_seconds`` so the 2-min cap can't be tampered with client-side.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response

from backend import db
from backend.auth import current_user
from backend.config import PLAN_CAPS, settings
from backend.netutil import client_ip as _client_ip
from backend.schemas import SessionStartRequest, SessionStartResponse

router = APIRouter()


async def verify_captcha(token: str, ip: str) -> bool:
    """Validate a Turnstile token. If no secret is configured (dev), allow."""
    if not settings.captcha_enabled:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                settings.TURNSTILE_VERIFY_URL,
                data={"secret": settings.CLOUDFLARE_TURNSTILE_SECRET, "response": token, "remoteip": ip},
            )
        return bool(r.json().get("success"))
    except Exception:
        return False


def issue_token(sid: str, kind: str, max_seconds: int, user_id: int | None,
                cost_cap_cents: int | None = None) -> str:
    now = int(time.time())
    payload = {
        "sid": sid, "kind": kind, "max_seconds": max_seconds, "user_id": user_id,
        "cost_cap_cents": cost_cap_cents,  # signed → the WS can't be tricked into a bigger cap
        "iat": now, "exp": now + settings.SESSION_TOKEN_TTL_S,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    """Decode + verify a session_token. Raises jwt exceptions on tamper/expiry."""
    return dict(jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"]))


@router.post("/api/session/start", response_model=SessionStartResponse)
async def session_start(body: SessionStartRequest, request: Request) -> SessionStartResponse:
    ip = _client_ip(request)

    # A4 — hard budget stop.
    if db.month_cost_usd() >= settings.MONTHLY_BUDGET_USD:
        raise HTTPException(503, "Emma está descansando, vuelve mañana.")

    user = await current_user(request)

    if not await verify_captcha(body.captcha_token, ip):
        raise HTTPException(400, "Verificación anti-bot fallida. Recarga e intenta de nuevo.")

    if user is None:
        # Unauth demo: 1 session / IP / 24h.
        if db.demo_count_24h(ip) >= 1:
            raise HTTPException(429, "Ya usaste tu demo de hoy. Inicia sesión para más.")
        db.record_demo_hit(ip)
        max_seconds = settings.DEMO_SESSION_SECONDS
        sid = db.create_session(None)
        return SessionStartResponse(
            session_token=issue_token(sid, "demo", max_seconds, None),
            realtime_url=f"/realtime?token={issue_token(sid, 'demo', max_seconds, None)}",
            max_seconds=max_seconds,
        )

    caps = PLAN_CAPS.get(user["plan"], PLAN_CAPS["free"])
    # LANDING-27 cap schema: enforce the per-user daily-seconds ceiling.
    daily = int(caps["daily_seconds"])
    if daily and db.user_seconds_today(user["id"]) >= daily:
        raise HTTPException(429, "Llegaste a tu límite diario. Sube de plan para más tiempo.")
    max_seconds = int(caps["session_seconds"])
    sid = db.create_session(user["id"])
    tok = issue_token(sid, "user", max_seconds, user["id"])
    return SessionStartResponse(session_token=tok, realtime_url=f"/realtime?token={tok}", max_seconds=max_seconds)


@router.post("/api/session/end")
async def session_end(request: Request, response: Response) -> dict[str, str]:
    # The proxy is the source of truth for usage; this is the client's "I'm done"
    # signal. Accounting is finalized in realtime_proxy on socket close.
    return {"status": "ok"}
