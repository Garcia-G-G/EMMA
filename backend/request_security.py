"""Browser request-integrity checks for cookie-authenticated mutations."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from backend.config import settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
SESSION_COOKIE_NAMES = ("__Host-emma_session", "emma_session")
PRODUCT_ORIGINS = frozenset(
    {"https://theemmafamily.com", "https://www.theemmafamily.com"}
)


def normalized_origin(value: str) -> str | None:
    """Return a canonical scheme/host/port origin, or None for invalid input."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
    except ValueError:
        return None


def browser_mutation_allowed(request: Request) -> bool:
    """Validate Origin for unsafe requests carrying Emma's session cookie."""
    if request.method.upper() in SAFE_METHODS:
        return True
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return True
    if not any(request.cookies.get(name) for name in SESSION_COOKIE_NAMES):
        return True

    expected = normalized_origin(settings.PUBLIC_URL)
    allowed = {origin for origin in PRODUCT_ORIGINS}
    if expected is not None:
        allowed.add(expected)

    supplied = normalized_origin(request.headers.get("origin", ""))
    if supplied is not None:
        return supplied in allowed

    # Browsers send Origin on unsafe cross-origin requests. Local test/dev
    # clients may omit it; production HTTPS fails closed.
    return not settings.PUBLIC_URL.lower().startswith("https://")
