"""Emma product backend — FastAPI app (Prompt 31).

Mounts the Realtime proxy, session gate, OAuth, and Stripe routers; serves the
public landing, the auth-gated dashboard, and a small login page. The OpenAI master
key lives only in this process (Part A's seam).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import auth, db, demo_session, realtime_proxy, stripe_routes

# wake_routes disabled in Fly deploy — depends on daemon's core.background / config.settings.
# Re-enable after vendoring those modules into backend/ (follow-up prompt 16.3.1).
from backend import session as session_mod
from backend.auth import current_user, require_user
from backend.config import PLAN_CAPS, settings

app = FastAPI(title="Emma")
# 25.0.1-B2: the landing only ever lives on the apex + www; the backend at
# api.theemmafamily.com must reject cross-origin calls from anywhere else.
# A closed allowlist — never ["*"]. Local dev origins are added when not HTTPS.
_CORS_ORIGINS = ["https://theemmafamily.com", "https://www.theemmafamily.com"]
if not settings.PUBLIC_URL.lower().startswith("https"):
    _CORS_ORIGINS += ["http://localhost:8000", "http://127.0.0.1:8000"]
app.add_middleware(
    CORSMiddleware, allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"], allow_credentials=True,
)
# 24.6-D3: Secure cookie whenever we're served over HTTPS (prod). Local http
# dev (PUBLIC_URL=http://localhost) keeps it off so the session still works.
_HTTPS_ONLY = settings.PUBLIC_URL.lower().startswith("https")
app.add_middleware(
    SessionMiddleware, secret_key=settings.SESSION_SECRET, same_site="lax", https_only=_HTTPS_ONLY
)

# 25.0.1-B1: Turnstile is optional. verify_captcha() already allows-all when no
# secret is configured; log that posture once at startup so it's never a surprise.
if not settings.CLOUDFLARE_TURNSTILE_SECRET:
    structlog.get_logger("emma.backend").info(
        "turnstile_not_configured", note="demo gated on IP rate-limit + cost cap only")
app.include_router(session_mod.router)
app.include_router(realtime_proxy.router)
app.include_router(auth.router)
app.include_router(stripe_routes.router)
# app.include_router(wake_routes.router)  # disabled in Fly deploy
app.include_router(demo_session.router)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

db.init_db()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/demo/config")
async def demo_config() -> dict[str, Any]:
    """Public config the landing's demo needs — the Turnstile SITE key (public) and
    session length. No secrets (the Turnstile SECRET + salt stay server-side)."""
    return {
        "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
        "duration_seconds": settings.DEMO_TALK_SECONDS,
        "warning_at_seconds": settings.DEMO_WARNING_SECONDS,
    }


@app.get("/", response_class=HTMLResponse)
async def landing() -> str:
    return (_STATIC / "landing.html").read_text(encoding="utf-8")


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return (_STATIC / "login.html").read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    if await current_user(request) is None:
        return RedirectResponse("/login")
    return HTMLResponse((_STATIC / "dashboard.html").read_text(encoding="utf-8"))


@app.get("/api/dashboard")
async def api_dashboard(request: Request) -> Any:
    user = await require_user(request)
    caps = PLAN_CAPS.get(user["plan"], PLAN_CAPS["free"])
    return JSONResponse({
        "user": {"email": user["email"], "name": user["name"], "plan": user["plan"]},
        "usage": {"sessions": user["monthly_session_count"], "seconds": round(user["monthly_seconds_used"], 1)},
        "caps": caps,
        "recent": db.recent_sessions(user["id"]),
        "downloads": {"mac": settings.DOWNLOAD_PKG_URL, "win": settings.DOWNLOAD_MSI_URL},
    })
