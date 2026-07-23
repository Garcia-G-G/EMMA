"""Resolve the user's preferred concrete app per category.

Order of resolution:
  1. config/dictionary.toml [apps] section (the user's explicit choice).
  2. actions.environment.detect_preferred(category) (auto-detected installed
     app), mapped from its shortlist *key* to the display name.
  3. None — caller decides whether to ask the user or fall back to open-by-name.

Category names accepted: "editor"/"ide"/"code", "browser", "terminal"/"shell",
"music". Returns the display name suitable for ``open -a "<name>"`` or
AppleScript ``tell application "<name>"``.
"""

from __future__ import annotations

import structlog

from actions import environment

log = structlog.get_logger("emma.apps")

# Caller-facing aliases → the environment.Category used by detect_preferred.
_CATEGORY_ALIASES: dict[str, environment.Category] = {
    "editor": "ide",
    "ide": "ide",
    "code": "ide",
    "browser": "browser",
    "terminal": "terminal",
    "shell": "terminal",
    "music": "music",
}


def _display_for_key(cat: environment.Category, key: str | None) -> str | None:
    """Map a detection *key* (e.g. 'cursor') to its display name ('Cursor')."""
    if not key:
        return None
    for entry in environment.SHORTLISTS.get(cat, []):
        if entry.get("key") == key:
            apps = entry.get("apps") or []
            return apps[0] if apps else None
    return None


def resolve(category: str) -> str | None:
    """DEPRECATED thin wrapper over :func:`core.app_router.preferred` (22-B30).

    Routing is DYNAMIC now: frontmost → running → preference → fallback,
    re-evaluated on every call. The old static order (dictionary → 24h
    detection cache) made preferences the answer; reality changed
    second-to-second and the answer didn't — that's the Brave/Chrome and
    Spotify/Music bug family. Preferences are a tiebreaker inside the
    router. Kept with the old signature so the existing call sites
    (`resolve("browser") or "Safari"`, …) work unchanged; the trailing
    fallbacks are dead code now (the router always answers).

    For "which app do I DEFAULT to?" voice queries use
    ``actions.environment.detect_preferred`` — that's the configured
    answer, deliberately not the live one.
    """
    cat = _CATEGORY_ALIASES.get(category.strip().lower())
    if not cat:
        return None
    from core import app_router

    return app_router.preferred(category)


def resolve_or_raise(category: str, hint: str = "") -> str:
    pick = resolve(category)
    if pick:
        return pick
    raise RuntimeError(f"No tengo un {hint or category} configurado. Dime cuál usar o instala uno.")
