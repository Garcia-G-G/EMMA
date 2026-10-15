"""Server-rendered public and account pages, ES + EN (Step 40.1).

Each page is ``static/pages/<key>.html`` plus ``static/pages/<key>.json``
(``{"es": {...}, "en": {...}}``). The template uses ``{{t.<name>}}`` for its
strings, ``{{head}}`` for the shared <head> block, ``{{lang}}`` and
``{{i18n}}`` (the strings as JSON, for the page's script), ``{{path.<key>}}``
and ``{{oauth}}`` (sign-in buttons for the configured providers). Strings are authored
content and may hold markup; nothing user-supplied is rendered here.

The landing (``/``, ``/en/``) is served by the landing's nginx, which proxies
every path in ROUTES to this app, so all pages share one host and one cookie.
ROUTES must match design/nav.js and the landing's nginx.conf
(tests/test_design_sync.py checks both).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.auth import current_user, oauth

router = APIRouter()

SITE = "https://theemmafamily.com"
_DIR = Path(__file__).parent / "static" / "pages"

# key -> {lang: path}. "auth": page needs a session (else -> login with ?next=).
ROUTES: dict[str, dict[str, str]] = {
    "login": {"es": "/entrar", "en": "/en/login"},
    "signup": {"es": "/registro", "en": "/en/signup"},
    "forgot": {"es": "/olvide", "en": "/en/forgot"},
    "reset": {"es": "/restablecer", "en": "/en/reset"},
    "account": {"es": "/cuenta", "en": "/en/account"},
    "pricing": {"es": "/precios", "en": "/en/pricing"},
    "install": {"es": "/instalar", "en": "/en/install"},
    "privacy": {"es": "/privacidad", "en": "/en/privacy"},
    "terms": {"es": "/terminos", "en": "/en/terms"},
}
AUTH_REQUIRED = {"account"}

# Old routes -> new key, 301. (Pairing is gone: /pair lands on the account page.)
LEGACY = {
    "/login": "login",
    "/register": "signup",
    "/plans": "pricing",
    "/download": "install",
    "/dashboard": "account",
    "/pair": "account",
    "/privacy": "privacy",
    "/terms": "terms",
    "/auth/reset": "reset",
}

_VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _load(key: str) -> tuple[str, dict[str, dict[str, str]]]:
    # read per request: two small files, and an edited page shows up without a restart
    html = (_DIR / f"{key}.html").read_text(encoding="utf-8")
    strings = json.loads((_DIR / f"{key}.json").read_text(encoding="utf-8"))
    return html, strings


def _head(key: str, lang: str, t: dict[str, str]) -> str:
    paths = ROUTES[key]
    title = t.get("title", "Emma")
    desc = t.get("description", "")
    return "\n".join(
        [
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{title}</title>",
            f'<meta name="description" content="{desc}">',
            '<meta name="theme-color" content="#f5efe7">',
            f'<link rel="canonical" href="{SITE}{paths[lang]}">',
            f'<link rel="alternate" hreflang="es" href="{SITE}{paths["es"]}">',
            f'<link rel="alternate" hreflang="en" href="{SITE}{paths["en"]}">',
            f'<link rel="alternate" hreflang="x-default" href="{SITE}{paths["es"]}">',
            '<link rel="icon" href="/static/atom.svg" type="image/svg+xml">',
            '<link rel="icon" href="/static/favicon-32.png" sizes="32x32" type="image/png">',
            '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">',
            '<link rel="preload" href="/static/fonts/mona-sans.woff2" as="font" type="font/woff2" crossorigin>',
            '<link rel="stylesheet" href="/static/tokens.css">',
            '<link rel="stylesheet" href="/static/components.css">',
            f'<meta property="og:title" content="{title}">',
            f'<meta property="og:description" content="{desc}">',
            f'<meta property="og:url" content="{SITE}{paths[lang]}">',
            '<meta property="og:type" content="website">',
            f'<meta property="og:image" content="{SITE}/og.png">',
            '<script src="/static/nav.js" defer></script>',
        ]
    )


def render(key: str, lang: str) -> str:
    html, strings = _load(key)
    t = strings[lang]
    i18n = json.dumps(t, ensure_ascii=False).replace("</", "<\\/")

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "head":
            return _head(key, lang, t)
        if name == "lang":
            return lang
        if name == "i18n":
            return i18n
        if name == "oauth":  # buttons for the providers this server has keys for
            return "".join(
                f'<a class="btn btn-secondary btn-block" href="/auth/{p}">{t["oauth_" + p]}</a>'
                for p in ("google", "github")
                if getattr(oauth, p, None)
            )
        if name.startswith("t."):
            return t[name[2:]]
        if name.startswith("path."):  # {{path.login}}: that page in this language
            return ROUTES[name[5:]][lang]
        raise KeyError(f"unknown template variable {name!r} in {key}.html")

    return _VAR.sub(sub, html)


def _page(key: str, lang: str) -> Any:
    async def handler(request: Request) -> Any:
        if key in AUTH_REQUIRED and await current_user(request) is None:
            return RedirectResponse(f"{ROUTES['login'][lang]}?next={ROUTES[key][lang]}", 303)
        return HTMLResponse(render(key, lang), headers={"Cache-Control": "no-store"})

    handler.__name__ = f"page_{key}_{lang}"
    return handler


def _legacy(key: str) -> Any:
    async def handler(request: Request) -> Any:
        q = request.url.query
        return RedirectResponse(ROUTES[key]["es"] + (f"?{q}" if q else ""), 301)

    handler.__name__ = f"legacy_{key}"
    return handler


for _key, _paths in ROUTES.items():
    for _lang, _path in _paths.items():
        router.add_api_route(_path, _page(_key, _lang), methods=["GET"], include_in_schema=False)
for _old, _key in LEGACY.items():
    router.add_api_route(_old, _legacy(_key), methods=["GET"], include_in_schema=False)
