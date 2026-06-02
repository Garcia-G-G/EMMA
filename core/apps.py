"""Resolve Garcia's preferred concrete app per category.

Order of resolution:
  1. config/dictionary.toml [apps] section (Garcia's explicit choice).
  2. actions.environment.detect_preferred(category) (auto-detected installed
     app), mapped from its shortlist *key* to the display name.
  3. None — caller decides whether to ask Garcia or fall back to open-by-name.

Category names accepted: "editor"/"ide"/"code", "browser", "terminal"/"shell",
"music". Returns the display name suitable for ``open -a "<name>"`` or
AppleScript ``tell application "<name>"``.
"""

from __future__ import annotations

import structlog

from actions import environment
from core import dictionary

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
    """Best display name for ``category``, or None if nothing is configured."""
    cat = _CATEGORY_ALIASES.get(category.strip().lower())
    if not cat:
        return None

    # 1. Dictionary [apps] — Garcia's explicit choice (already a display name).
    pick: str | None = dictionary.app_for(category) or dictionary.app_for(cat)
    if pick:
        log.debug("app_resolved", category=cat, via="dictionary", pick=pick)
        return pick

    # 2. detect_preferred — DetectionResult.app_name is a shortlist KEY, not a
    #    display name (there is no `.chosen` attribute), so map it.
    res = environment.detect_preferred(cat)
    pick = _display_for_key(cat, res.app_name)
    if pick:
        log.debug("app_resolved", category=cat, via="detect_preferred", pick=pick)
        return pick

    log.debug("app_unresolved", category=cat)
    return None


def resolve_or_raise(category: str, hint: str = "") -> str:
    pick = resolve(category)
    if pick:
        return pick
    raise RuntimeError(f"No tengo un {hint or category} configurado. Dime cuál usar o instala uno.")
