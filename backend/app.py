"""Emma product backend — FastAPI app (Prompt 31).

Mounts the Realtime proxy, session gate, OAuth, and Stripe routers; serves the
public landing, the auth-gated dashboard, and a small login page. The OpenAI master
key lives only in this process (Part A's seam).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import auth, db, realtime_proxy, stripe_routes, wake_routes
from backend import session as session_mod
from backend.auth import current_user, require_user
from backend.config import PLAN_CAPS, settings

app = FastAPI(title="Emma")
# 24.6-D3: Secure cookie whenever we're served over HTTPS (prod). Local http
# dev (PUBLIC_URL=http://localhost) keeps it off so the session still works.
_HTTPS_ONLY = settings.PUBLIC_URL.lower().startswith("https")
app.add_middleware(
    SessionMiddleware, secret_key=settings.SESSION_SECRET, same_site="lax", https_only=_HTTPS_ONLY
)
app.include_router(session_mod.router)
app.include_router(realtime_proxy.router)
app.include_router(auth.router)
app.include_router(stripe_routes.router)
app.include_router(wake_routes.router)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

db.init_db()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
