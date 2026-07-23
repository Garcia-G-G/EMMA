"""Voice-triggered social posting (Prompt 26).

Two paths, both confirmation-gated because the content is public:
  * URL-scheme / web composer — opens the platform prefilled; the user taps Send.
    No credentials, works today. (X web intent, WhatsApp wa.me, LinkedIn feed.)
  * API / webhook — fully unattended, only when a credential is in Keychain.
    (Discord per-channel webhook; X /2/tweets if an OAuth2 user token is saved.)

Verified 2026: X has no native macOS app since Aug 2024 (twitter:// is dead on
the Mac) → the web intent is the reliable path; X API POST needs an OAuth2
USER-context token (app Bearer is forbidden) on a paid/approved account, so it's
opt-in. LinkedIn can't reliably prefill post text via URL → we open the composer
AND copy the text to the clipboard. URL templates live in
config/app_capabilities.toml; credentials route through core/secrets.py (Keychain),
never .env or memory.db (SECURITY.md).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
import structlog

from config.settings import settings
from core import app_capabilities, dictionary, redaction, secrets, x_oauth
from tools.app_url_tool import _fill_template, _open
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.social")

_X_MAX = 280
_DISCORD_MAX = 2000
_X_REAUTH = "Corre `python -m emma.setup` para configurar X (o `--only x` si solo quieres ese)."

# B50.1: single-flight refresh. Two posts racing on an expired token (a background
# task + a foreground voice turn) would otherwise each POST /2/oauth2/token; the
# second spends the one-time refresh token again → invalid_grant, breaking the
# chain until the user re-runs setup. The lock serializes; the waiter then re-reads
# the now-fresh token instead of refreshing again.
_X_REFRESH_LOCK = asyncio.Lock()

# B50.3: curly quotes the Realtime LLM occasionally emits around URLs break the
# link. Normalize ONLY inside URL tokens so styled prose quotes survive.
# (escapes, not literals, so the linter doesn't flag ambiguous Unicode)
_CURLY = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
_URL_RE = re.compile(r"https?://\S+")


def _normalize_urls(text: str) -> str:
    return _URL_RE.sub(lambda m: m.group(0).translate(_CURLY), text)


def _is_malformed_token_error(body: dict[str, Any]) -> bool:
    """X returns OAuth ``invalid_request`` for a malformed bearer — a refresh
    can't fix that. ``invalid_token`` / plain expiry IS refreshable. Default to
    refreshable when the body is unrecognized (don't strand a recoverable 401)."""
    blob = json.dumps(body or {}).lower()
    return "invalid_request" in blob or "invalid-request" in blob


def _rate_limit_result(headers: dict[str, Any]) -> ToolResult:
    """B50.2: a 429 surfaces the ``x-rate-limit-reset`` (epoch) as a wait time."""
    reset = headers.get("x-rate-limit-reset") if headers else None
    suffix = " Intenta en un rato."
    if reset:
        try:
            mins = max(1, round((float(reset) - time.time()) / 60))
            suffix = f" Intenta en ~{mins} min."
        except (ValueError, TypeError):
            pass
    return ToolResult(False, None, f"X me limitó el ritmo de publicaciones.{suffix}", False)


async def _refresh_x_token(stale: str | None = None) -> str | None:
    """Mint a fresh access token from the stored refresh token, persisting it.
    None if there's no refresh token / client id or the refresh call fails.

    Single-flight (B50.1): callers pass the token that just failed as ``stale``.
    After taking the lock we re-read the stored token; if it already changed,
    a concurrent caller refreshed while we waited — reuse it rather than spend
    the refresh token a second time."""
    async with _X_REFRESH_LOCK:
        if stale is not None:
            current = await secrets.retrieve("X_ACCESS_TOKEN")
            if current and current != stale:
                return current
        refresh = await secrets.retrieve("X_REFRESH_TOKEN")
        if not refresh or not settings.X_CLIENT_ID:
            return None
        return await _do_refresh(refresh)


async def _do_refresh(refresh: str) -> str | None:
    try:
        tokens = await x_oauth.refresh_access_token(settings.X_CLIENT_ID, refresh)
    except Exception as exc:
        log.warning("x_refresh_failed", error=str(exc))
        return None
    access: str | None = tokens.get("access_token")
    if not access:
        return None
    await secrets.store("X_ACCESS_TOKEN", access, kind="oauth_token")
    if tokens.get("refresh_token"):  # X may or may not rotate — keep what it gives
        await secrets.store("X_REFRESH_TOKEN", tokens["refresh_token"], kind="oauth_token")
    expires_at = int(time.time()) + int(tokens.get("expires_in", 7200))
    await secrets.store("X_TOKEN_EXPIRES_AT", str(expires_at), kind="oauth_meta")
    log.info("x_token_refreshed")
    return access


async def _valid_x_token() -> str | None:
    """A usable X access token, refreshing proactively when it's within 60s of
    expiry. None if X isn't set up yet (run emma.x_setup)."""
    token = await secrets.retrieve("X_ACCESS_TOKEN")
    if not token:
        return None
    expires_at = await secrets.retrieve("X_TOKEN_EXPIRES_AT")
    expired = False
    if expires_at:
        try:
            expired = time.time() > float(expires_at) - 60
        except ValueError:
            expired = False
    if expired:
        return await _refresh_x_token(token) or token
    return token


async def _x_post(token: str, text: str) -> tuple[int, dict[str, Any], dict[str, Any]]:
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
        resp = await client.post(
            "https://api.x.com/2/tweets",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": text},
        )
    try:
        body = dict(resp.json())
    except Exception:
        body = {}
    # Lowercase header keys so the 429 reset lookup is case-insensitive regardless
    # of what httpx (or a test mock) hands back. Defensive: mocks may omit headers.
    headers = {str(k).lower(): v for k, v in (getattr(resp, "headers", {}) or {}).items()}
    return resp.status_code, body, headers


def _composer_url(app: str, kind: str, **values: str) -> str | None:
    """Fill an app's web_fallback (preferred) or resource_url template, or None."""
    caps = app_capabilities.caps_for(app)
    if not caps:
        return None
    template = caps.web_fallback.get(kind) or caps.resource_url.get(kind)
    if not template:
        return None
    url, missing = _fill_template(template, values)
    return url if missing is None else None


async def _copy_clipboard(text: str) -> bool:
    """Put `text` on the macOS clipboard via pbcopy. True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy", stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=5.0)
        return proc.returncode == 0
    except Exception as exc:
        log.warning("clipboard_copy_failed", error=str(exc))
        return False


def _webhook_label(channel: str) -> str:
    return "discord_webhook_" + re.sub(r"[^a-z0-9]+", "_", channel.strip().lower()).strip("_")


# ---------------------------------------------------------------------------
# X / Twitter
# ---------------------------------------------------------------------------


@tool(destructive=True)
async def post_to_x(text: str, confirmed: bool = False) -> ToolResult:
    """Publica un tweet/post en X (Twitter). SIEMPRE confirma antes de enviar.

    Úsalo cuando the user diga "Emma, tuitea: <texto>" / "publica en X: <texto>".
    Si hay un token de X guardado, lo publica por API; si no, abre el composer
    de X prellenado y the user le da Post.
    """
    text = (text or "").strip()
    if not text:
        return ToolResult(False, None, "¿Qué quieres que publique en X?", False)

    # B50.3: normalize curly quotes inside URLs, then refuse to publish anything
    # that carries a secret (API key, card, token — including inside a URL query
    # like ?api_key=…). `contains_secret` is precise: it ignores phone numbers and
    # long plain words/hashtags, so normal tweets aren't blocked. We refuse rather
    # than auto-redact-and-post — the user must see it and rewrite.
    text = _normalize_urls(text)
    if redaction.contains_secret(text):
        return ToolResult(
            False,
            {"blocked": "sensitive"},
            "No voy a publicar esto: parece traer un dato sensible (una clave o número). "
            "Reescríbelo sin esa información.",
            False,
        )

    truncated = len(text) > _X_MAX
    if truncated:
        text = text[: _X_MAX - 1].rstrip() + "…"

    if not confirmed:
        warn = " (lo recorté a 280 caracteres)" if truncated else ""
        return ToolResult(
            True,
            {"text": text, "truncated": truncated},
            f"Voy a publicar en X{warn}: «{text}». ¿Lo confirmo?",
            requires_confirmation=True,
        )

    token = await _valid_x_token()
    if token is None:
        # Not authorized yet. The API is the supported path (26.1); only open the
        # unauthenticated composer if the user explicitly re-enabled that fallback.
        if settings.X_USE_COMPOSER_FALLBACK:
            return await _open_x_composer(text)
        return ToolResult(
            False,
            {"needs_setup": True},
            f"No tengo permiso para publicar en X. {_X_REAUTH}",
            False,
        )

    try:
        status, body, headers = await _x_post(token, text)
        # B50.2: only an expired/invalid_token 401 is refreshable. invalid_request
        # (malformed bearer) and 403 (missing scope) are NOT — refreshing there
        # just burns the token. Exactly one refresh + retry, never a loop.
        if status == 401 and not _is_malformed_token_error(body):
            fresh = await _refresh_x_token(token)
            if fresh:
                status, body, headers = await _x_post(fresh, text)
    except Exception as exc:
        log.warning("x_api_error", error=str(exc))
        return ToolResult(False, None, f"No pude publicar en X: {exc}", False)

    if status == 201:
        tweet_id = (body.get("data") or {}).get("id", "")
        return ToolResult(
            True, {"via": "api", "tweet_id": tweet_id}, "Listo, publiqué en X.", False
        )
    if status == 429:
        return _rate_limit_result(headers)
    if status == 403:
        # Missing scope (tweet.write not granted at OAuth time) — re-auth needed.
        return ToolResult(
            False, None, f"X no me da permiso para publicar (falta el alcance). {_X_REAUTH}", False
        )
    if status == 401:
        return ToolResult(
            False, None, f"Mi sesión con X no es válida o expiró. {_X_REAUTH}", False
        )
    log.warning("x_api_failed", status=status)
    return ToolResult(False, None, f"X rechazó la publicación ({status}).", False)


async def _open_x_composer(text: str) -> ToolResult:
    """Legacy web-intent composer (OFF by default after 26.1)."""
    url = _composer_url("x", "post", text=text)
    if not url:
        return ToolResult(False, None, "No pude armar el composer de X.", False)
    try:
        await _open(url)
    except Exception as exc:
        return ToolResult(False, None, f"No pude abrir X: {exc}", False)
    return ToolResult(
        True, {"via": "composer", "url": url}, "Te abrí X con el texto listo — dale Post.", False
    )


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


@tool(destructive=True)
async def post_to_linkedin(text: str, confirmed: bool = False) -> ToolResult:
    """Abre el composer de LinkedIn con tu texto y lo copia al portapapeles.

    Úsalo cuando the user diga "Emma, publica en LinkedIn: <texto>". LinkedIn no
    deja prellenar texto por URL de forma confiable, así que abrimos el composer
    y copiamos el texto para que the user lo pegue (Cmd+V) y publique.
    """
    text = (text or "").strip()
    if not text:
        return ToolResult(False, None, "¿Qué quieres publicar en LinkedIn?", False)
    if not confirmed:
        return ToolResult(
            True,
            {"text": text},
            f"Voy a abrir LinkedIn con: «{text}». ¿Lo confirmo?",
            requires_confirmation=True,
        )
    copied = await _copy_clipboard(text)
    url = _composer_url("linkedin", "post", text=text)
    if not url:
        return ToolResult(False, None, "No pude armar el composer de LinkedIn.", False)
    try:
        await _open(url)
    except Exception as exc:
        return ToolResult(False, None, f"No pude abrir LinkedIn: {exc}", False)
    tail = " Copié el texto, pégalo con Cmd+V si no aparece." if copied else ""
    return ToolResult(
        True,
        {"via": "composer", "copied": copied, "url": url},
        f"Te abrí el composer de LinkedIn.{tail} Revisa y publica.",
        False,
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


@tool(destructive=True)
async def send_to_discord(channel: str, text: str, confirmed: bool = False) -> ToolResult:
    """Manda un mensaje a un canal de Discord vía webhook. SIEMPRE confirma.

    Úsalo cuando the user diga "Emma, manda en Discord al canal X: <texto>".
    La primera vez por canal, the user copia la URL del webhook (Ajustes del canal
    → Integraciones → Webhooks) y la guarda: "Emma, recuerda mi webhook de
    Discord del canal X". Sin webhook, te abro el canal para escribir a mano.
    """
    channel = (channel or "").strip()
    text = (text or "").strip()
    if not channel or not text:
        return ToolResult(False, None, "Necesito el canal y el mensaje.", False)
    if len(text) > _DISCORD_MAX:
        text = text[: _DISCORD_MAX - 1].rstrip() + "…"

    webhook = await secrets.retrieve(_webhook_label(channel))
    if not confirmed:
        how = "" if webhook else " (no tengo webhook de ese canal, te diré cómo configurarlo)"
        return ToolResult(
            True,
            {"channel": channel, "text": text, "has_webhook": bool(webhook)},
            f"Voy a mandar al canal {channel} de Discord{how}: «{text}». ¿Confirmo?",
            requires_confirmation=True,
        )

    if not webhook:
        return ToolResult(
            False,
            {"channel": channel, "needs_webhook": True},
            f"No tengo el webhook del canal {channel}. En Discord: Ajustes del canal → "
            "Integraciones → Webhooks → copia la URL, y dime "
            f"«recuerda mi webhook de Discord del canal {channel}».",
            False,
        )
    try:
        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT_S) as client:
            resp = await client.post(webhook, json={"content": text})
        if resp.status_code in (200, 204):
            return ToolResult(True, {"via": "webhook"}, f"Listo, mandé al canal {channel}.", False)
        if resp.status_code == 429:
            return ToolResult(
                False, None, "Discord me limitó el ritmo. Intenta en un momento.", False
            )
        log.warning("discord_webhook_failed", status=resp.status_code)
        return ToolResult(False, None, f"Discord rechazó el mensaje ({resp.status_code}).", False)
    except Exception as exc:
        return ToolResult(False, None, f"No pude mandar a Discord: {exc}", False)


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


def _resolve_phone(to: str) -> tuple[str | None, str]:
    """(digits-only phone, display name). Resolves a contact name/alias via the
    dictionary, or treats `to` as a literal number. (None, "") if unknown."""
    contact = dictionary.find_contact(to)
    if contact and contact.phone:
        return re.sub(r"\D", "", contact.phone), contact.name or to
    digits = re.sub(r"\D", "", to)
    if len(digits) >= 8:  # looks like a real international number
        return digits, to
    return None, ""


@tool(destructive=True)
async def send_whatsapp(to: str, text: str, confirmed: bool = False) -> ToolResult:
    """Abre WhatsApp con un mensaje prellenado para un contacto. SIEMPRE confirma.

    Úsalo cuando the user diga "Emma, mándale en WhatsApp a Juan: <texto>".
    `to` puede ser un nombre de contacto (lo resuelvo en tu directorio) o un
    número con código de país. the user le da Enviar.
    """
    to = (to or "").strip()
    text = (text or "").strip()
    if not to or not text:
        return ToolResult(False, None, "¿A quién le mando y qué digo?", False)

    phone, name = _resolve_phone(to)
    if not phone:
        return ToolResult(
            False,
            {"unknown_contact": to},
            f"No encontré a {to} en tus contactos ni un número válido. "
            f"Dime su número con código de país y dilo: «recuerda el contacto {to}».",
            False,
        )
    if not confirmed:
        return ToolResult(
            True,
            {"to": name, "phone": phone, "text": text},
            f"Voy a mandarle a {name} por WhatsApp: «{text}». ¿Confirmo?",
            requires_confirmation=True,
        )
    url = _composer_url("whatsapp", "message", phone=phone, text=text)
    if not url:
        return ToolResult(False, None, "No pude armar el mensaje de WhatsApp.", False)
    try:
        await _open(url)
    except Exception as exc:
        return ToolResult(False, None, f"No pude abrir WhatsApp: {exc}", False)
    return ToolResult(
        True, {"via": "wa.me", "to": name}, f"Te abrí WhatsApp con {name} — dale Enviar.", False
    )
