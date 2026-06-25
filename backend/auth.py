"""OAuth (Google + GitHub) + cookie sessions (Prompt 31, Part B).

On a successful OAuth callback we upsert the user by email and set a signed,
HttpOnly, 30-day cookie. ``current_user`` reads that cookie everywhere else. The
provider token-exchange is the only piece that needs real client ids/secrets; the
upsert + cookie logic (``login_user`` / ``current_user``) is pure and unit-tested.
"""

from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from backend import db
from backend.config import settings
from backend.schemas import MeResponse

router = APIRouter()
_COOKIE = "emma_session"
_MAX_AGE = 30 * 86400
_serializer = URLSafeTimedSerializer(settings.SESSION_SECRET, salt="emma-session")

oauth = OAuth()
if settings.GOOGLE_OAUTH_CLIENT_ID:
    oauth.register(
        name="google",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
if settings.GITHUB_OAUTH_CLIENT_ID:
    oauth.register(
        name="github",
        client_id=settings.GITHUB_OAUTH_CLIENT_ID,
        client_secret=settings.GITHUB_OAUTH_CLIENT_SECRET,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user user:email"},
    )


def set_session_cookie(response: Response, uid: int) -> None:
    """Set the signed, HttpOnly, SameSite=Lax, Secure-on-HTTPS 30-day session cookie."""
    response.set_cookie(
        _COOKIE, _serializer.dumps({"uid": uid}),
        max_age=_MAX_AGE, httponly=True,
        secure=settings.PUBLIC_URL.lower().startswith("https"), samesite="lax",
    )


def login_user(response: Response, email: str, name: str, provider: str, provider_id: str) -> dict[str, Any]:
    """Upsert the user and set the signed session cookie. Returns the user row."""
    user = db.upsert_user(email=email, name=name, provider=provider, provider_id=provider_id)
    set_session_cookie(response, user["id"])
    return user


async def current_user(request: Request) -> dict[str, Any] | None:
    raw = request.cookies.get(_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer.loads(raw, max_age=_MAX_AGE)
    except (BadSignature, Exception):
        return None
    return db.get_user(int(data.get("uid", 0)))


async def require_user(request: Request) -> dict[str, Any]:
    user = await current_user(request)
    if user is None:
        raise HTTPException(401, "No autenticado.")
    return user


# ---- routes -----------------------------------------------------------------


@router.get("/auth/{provider}")
async def login(provider: str, request: Request) -> Any:
    if provider not in ("google", "github") or not getattr(oauth, provider, None):
        raise HTTPException(404, "Proveedor no configurado.")
    client = oauth.create_client(provider)
    redirect_uri = f"{settings.PUBLIC_URL}/auth/{provider}/callback"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/{provider}/callback")
async def callback(provider: str, request: Request) -> Any:
    client = oauth.create_client(provider)
    if client is None:
        raise HTTPException(404, "Proveedor no configurado.")
    token = await client.authorize_access_token(request)
    if provider == "google":
        info = token.get("userinfo") or await client.userinfo(token=token)
        email, name, pid = info["email"], info.get("name", ""), info.get("sub", "")
    else:  # github
        resp = await client.get("user", token=token)
        gh = resp.json()
        email = gh.get("email") or await _github_primary_email(client, token)
        name, pid = gh.get("name") or gh.get("login", ""), str(gh.get("id", ""))
    if not email:
        raise HTTPException(400, "No pude leer tu correo del proveedor.")
    response = RedirectResponse(url="/dashboard")
    login_user(response, email, name, provider, pid)
    return response


async def _github_primary_email(client: Any, token: Any) -> str:
    resp = await client.get("user/emails", token=token)
    for e in resp.json():
        if e.get("primary"):
            return str(e.get("email", ""))
    return ""


@router.get("/auth/logout")
async def logout() -> Any:
    response = RedirectResponse(url="/")
    response.delete_cookie(_COOKIE)
    return response


@router.get("/api/me", response_model=MeResponse)
async def me(request: Request) -> Any:
    user = await current_user(request)
    if user is None:
        return JSONResponse({"detail": "No autenticado."}, status_code=401)
    return MeResponse(**{k: user[k] for k in MeResponse.model_fields})
