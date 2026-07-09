"""Emma product backend — FastAPI app (Prompt 31).

Mounts the Realtime proxy, session gate, OAuth, and Stripe routers; serves the
public landing, the auth-gated dashboard, and a small login page. The OpenAI master
key lives only in this process (Part A's seam).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import (
    account_routes,
    admin_routes,
    auth,
    auth_local,
    credits_routes,
    db,
    demo_session,
    device_pairing,
    openai_proxy,
    realtime_proxy,
    stripe_routes,
)

# wake_routes disabled in Fly deploy — depends on daemon's core.background / config.settings.
# Re-enable after vendoring those modules into backend/ (follow-up prompt 16.3.1).
from backend import session as session_mod
from backend.auth import current_user, require_admin, require_user
from backend.config import assert_secure_secrets, settings

assert_secure_secrets()  # fail loud if a prod (HTTPS) host is still on the dev signing keys

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


@app.middleware("http")
async def _fresh_static(request: Request, call_next):
    """Serve /static (tokens.css etc.) with no-cache so CSS/JS fixes land immediately
    instead of being stuck behind Cloudflare's 4h asset cache."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

# 25.0.1-B1: Turnstile is optional. verify_captcha() already allows-all when no
# secret is configured; log that posture once at startup so it's never a surprise.
if not settings.CLOUDFLARE_TURNSTILE_SECRET:
    structlog.get_logger("emma.backend").info(
        "turnstile_not_configured", note="demo gated on IP rate-limit + cost cap only")
app.include_router(session_mod.router)
app.include_router(realtime_proxy.router)
app.include_router(openai_proxy.router)  # CLIENT-INSTALL Phase 2A: /v1/* managed HTTP proxy
app.include_router(auth.router)
app.include_router(auth_local.router)
app.include_router(account_routes.router)
app.include_router(stripe_routes.router)
app.include_router(credits_routes.router)  # DASHBOARD-CREDITS-2: balance/bundles/auto-refill
# app.include_router(wake_routes.router)  # disabled in Fly deploy
app.include_router(demo_session.router)
app.include_router(admin_routes.router)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

db.init_db()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


_FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<g fill="none" stroke="#a04a16" stroke-width="1.3">'
            '<ellipse cx="12" cy="12" rx="10" ry="3.6"/>'
            '<ellipse cx="12" cy="12" rx="10" ry="3.6" transform="rotate(60 12 12)"/>'
            '<ellipse cx="12" cy="12" rx="10" ry="3.6" transform="rotate(120 12 12)"/></g></svg>')


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(content=_FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/plans")
async def api_plans() -> dict[str, Any]:
    """Public pricing structure the plans page renders (LANDING-27). Prices are
    presentational; the binding caps live server-side in PLAN_CAPS."""
    return {
        "currency": "usd",
        "plans": [
            {"id": "pro", "name": "Pro", "monthly": 19, "yearly": 190,
             "session_min": 5, "daily_min": 10, "monthly_min": 60,
             "features": ["Instalación .pkg", "Minutos incluidos + excedente medido",
                          "Soporte por correo (48h)"]},
            {"id": "power", "name": "Power", "monthly": 49, "yearly": 490,
             "session_min": 15, "daily_min": 30, "monthly_min": 200,
             "features": ["Todo lo de Pro", "Actualizaciones prioritarias",
                          "Soporte 24h + chat", "Acceso anticipado"]},
        ],
        "overage_per_min_usd": 0.30,
    }


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


# LANDING-27.post: the lock-screen CTAs (_landing/index.html:482,486,490) link to
# /login + /plans + /download. Previously only /login existed; the other two 404'd.
# Register is rendered server-side so signup flows from the landing don't bounce
# through /login first. Each page is a minimal HTML shell that pulls live data
# from existing JSON endpoints (/api/plans, /api/account, etc).
@app.get("/plans", response_class=HTMLResponse)
async def plans_page() -> str:
    return (_STATIC / "plans.html").read_text(encoding="utf-8")


@app.get("/download", response_class=HTMLResponse)
async def download_page() -> str:
    return (_STATIC / "download.html").read_text(encoding="utf-8")


@app.get("/register", response_class=HTMLResponse)
async def register_page() -> str:
    return (_STATIC / "register.html").read_text(encoding="utf-8")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page() -> str:
    return (_STATIC / "privacy.html").read_text(encoding="utf-8")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page() -> str:
    return (_STATIC / "terms.html").read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    if await current_user(request) is None:
        return RedirectResponse("/login")
    # The page is served static (no Jinja), so inject the Stripe *publishable* key
    # (safe to expose) by substituting the placeholder. Bundle purchases need it.
    html = (_STATIC / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("{{ stripe_pk }}", settings.STRIPE_PUBLISHABLE_KEY)
    return HTMLResponse(html)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> Any:
    try:
        await require_admin(request)  # gate the PAGE, not just the API
    except HTTPException:
        return RedirectResponse("/login")
    return HTMLResponse((_STATIC / "admin.html").read_text(encoding="utf-8"))


# ---- PAIR-DEVICE-1: RFC 8628 device pairing (daemon ↔ web). Kept in one block. ----
# /code + /token are daemon-facing (no cookie — the daemon has no session yet).
# /authorize + /devices are web-facing (cookie auth via require_user).


@app.post("/api/device/code")
async def device_code() -> Any:
    return device_pairing.issue_device_code()


@app.post("/api/device/token")
async def device_token(request: Request) -> Any:
    body = await request.json()
    dc = body.get("device_code", "")
    if not dc:
        raise HTTPException(400, "device_code required")
    result = device_pairing.exchange_device_code(dc, request.client.host if request.client else None)
    status = result.pop("_status", 200)
    if status != 200:
        raise HTTPException(status, detail=result)
    return result


@app.post("/api/device/authorize")
async def device_authorize(request: Request) -> Any:
    user = await require_user(request)
    body = await request.json()
    try:
        device_pairing.authorize_user_code(
            body.get("user_code", ""), user["id"], body.get("device_name", "Emma device"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@app.get("/api/devices")
async def list_devices_route(request: Request) -> Any:
    user = await require_user(request)
    return device_pairing.list_devices(user["id"])


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: int, request: Request) -> Any:
    user = await require_user(request)
    if not device_pairing.revoke_token(device_id, user["id"]):
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.get("/pair", response_class=HTMLResponse)
async def pair_page(request: Request) -> Any:
    if await current_user(request) is None:
        return RedirectResponse("/login?next=/pair")
    return HTMLResponse((_STATIC / "pair.html").read_text(encoding="utf-8"))


@app.get("/api/dashboard")
async def api_dashboard(request: Request) -> Any:
    user = await require_user(request)
    seconds = round(float(user["monthly_seconds_used"] or 0), 1)
    return JSONResponse({
        "user": {"email": user["email"], "name": user["name"], "plan": user["plan"]},
        "usage": {"sessions": user["monthly_session_count"],
                  "seconds": seconds, "minutes": round(seconds / 60, 1)},
        "subscription": {"plan": user["plan"], "active": bool(user.get("stripe_customer_id"))},
        "recent": db.recent_sessions(user["id"]),
        "downloads": {"mac": settings.DOWNLOAD_PKG_URL, "win": settings.DOWNLOAD_MSI_URL},
    })
