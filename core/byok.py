"""Bring-your-own-key: validate a user's OpenAI key and put it in Keychain.

The product claim of the BYO-key tier is that **the backend never sees the key**
— not hashed, not truncated, not in an error report. That is what makes Emma a
piece of software rather than a reseller of someone else's API access, and it is
why the validation call in this module goes straight to ``api.openai.com``.

Two rules this module exists to enforce:

**Write-only.** ``store`` puts the key in Keychain and nothing here reads it
back. There is no getter, no "just for the settings UI" accessor, and no control
command that returns it. The UI is given :func:`masked` — last four characters,
enough to recognise which key is installed and useless to anyone else.

**Validated before accepted.** A wrong key that is discovered at the first wake
word is a support ticket; a wrong key discovered while the user is still looking
at the field they typed it into is a typo they fix in five seconds. The check is
the cheapest authenticated call OpenAI offers.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger("emma.byok")

# Hard-coded, not read from settings.OPENAI_BASE_URL. In managed mode that
# setting points at the Emma proxy, and validating a user's personal key through
# our own backend is the one thing this tier promises never to do.
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"

_MIN_LEN = 20


def looks_well_formed(key: str) -> bool:
    """Cheap shape check before spending a network round trip on it."""
    key = (key or "").strip()
    return key.startswith("sk-") and len(key) >= _MIN_LEN and " " not in key


def masked(key: str) -> str:
    """``sk-…a1b2`` — for recognition only. Never the value, never reversible."""
    key = (key or "").strip()
    if len(key) < 8:
        return "sk-…"
    return f"sk-…{key[-4:]}"


async def validate(key: str, *, timeout_s: float = 12.0) -> tuple[bool, str]:
    """Ask OpenAI directly whether this key works. Returns (ok, message_es).

    Never raises, and never routes through the Emma backend. A network failure
    is reported as such rather than as a bad key — telling someone their key is
    invalid because their wifi dropped is worse than saying nothing.
    """
    key = (key or "").strip()
    if not looks_well_formed(key):
        return False, "Esa key no tiene el formato correcto (empieza con sk-)."

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(
                _OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {key}"}
            )
    except Exception as exc:
        # Deliberately does not include the exception's text: httpx puts the
        # request URL in it, and the Authorization header has been known to
        # appear in proxy errors.
        log.warning("byok_validate_network_error", error_type=type(exc).__name__)
        return False, "No pude conectarme a OpenAI. Revisa tu conexión e intenta de nuevo."

    if r.status_code == 200:
        return True, "Key verificada."
    if r.status_code in (401, 403):
        return False, "OpenAI rechazó esa key. Revísala y vuelve a intentar."
    if r.status_code == 429:
        # A key with no credit still authenticates; the user should know now.
        return False, "Esa key es válida pero no tiene cuota disponible en OpenAI."
    log.warning("byok_validate_unexpected_status", status=r.status_code)
    return False, f"OpenAI respondió {r.status_code}. Intenta de nuevo en un momento."


async def store(key: str) -> None:
    """Put the key in Keychain under the frozen service, and nowhere else.

    Not memory.db, not install.json, not tasks.jsonl, not a log line. The label
    is the same one Settings reads back at boot, so the daemon picks it up on its
    next resolution without any other plumbing.
    """
    from config.settings import invalidate_mode_cache
    from core import secrets

    await secrets.store("OPENAI_API_KEY", key.strip(), kind="secret")
    # The tier just changed under a running daemon: unconfigured -> byok.
    invalidate_mode_cache()
    log.info("byok_key_stored", key=masked(key))  # masked, and redaction runs after


async def clear() -> None:
    """Remove the key. Used when switching to the managed tier."""
    from config.settings import invalidate_mode_cache
    from core import secrets

    await secrets.delete("OPENAI_API_KEY")
    invalidate_mode_cache()
    log.info("byok_key_cleared")


async def installed_hint() -> str | None:
    """``sk-…a1b2`` if a key is installed, else None.

    The ONE thing the UI may learn about the stored key. It reads Keychain to
    build the mask and returns nothing else — no getter is exposed above this.
    """
    from core import secrets

    value = await secrets.retrieve("OPENAI_API_KEY")
    return masked(value) if value else None
