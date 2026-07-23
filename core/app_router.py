"""Dynamic app routing — the single source of truth for "which app" (22-B30).

The old model asked the dictionary ("preference") or a 24-hour cache.
Reality changes second-to-second: the user looks at Chrome → Chrome IS the
browser, even if Brave is the favorite. Resolution order, re-evaluated on
EVERY call:

    1. frontmost app, if it belongs to the category
    2. a running app from the category (preference breaks ties)
    3. the configured preference (dictionary / detect_preferred), if installed
    4. the category shortlist's first entry (Safari / Music / …)

Engine: ``NSWorkspace`` (pyobjc, already a dependency) — measured 0.7 ms for
frontmost and 2.9 ms for the full running list, in-process, no subprocess,
no Accessibility permission. The spec suggested System Events AppleScript;
that's a ~50 ms subprocess that would block the realtime audio loop, so
NSWorkspace wins. Matching is by BUNDLE ID (NSWorkspace reports VS Code as
localized "Code"; AppleScript needs "Visual Studio Code" — the shortlists
carry both, we return the canonical display name).

Tiny caches (200 ms frontmost / 1 s running) are spam guards within a tool
dispatch — never cross-call memory. Preferences are a tiebreaker, not the
answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from actions import environment
from core import dictionary

log = structlog.get_logger("emma.app_router")

_FRONTMOST_TTL_S = 0.2
_RUNNING_TTL_S = 1.0

# Caller-facing aliases → environment.Category (mirrors core/apps.py).
_ALIASES: dict[str, environment.Category] = {
    "editor": "ide",
    "ide": "ide",
    "code": "ide",
    "browser": "browser",
    "terminal": "terminal",
    "shell": "terminal",
    "music": "music",
}

_frontmost_cache: tuple[float, tuple[str, str] | None] = (0.0, None)
_running_cache: tuple[float, list[tuple[str, str]]] = (0.0, [])


@dataclass(frozen=True)
class RouteDecision:
    """Why the router picked what it picked — rides in ToolResult.data."""

    picked: str
    source: str  # "frontmost" | "running" | "preferred" | "fallback"
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"picked": self.picked, "source": self.source, "candidates": self.candidates}


def _workspace() -> Any | None:
    try:
        from AppKit import NSWorkspace  # in-process; ~100 ms import, once

        return NSWorkspace.sharedWorkspace()
    except Exception as exc:  # AppKit unavailable → degrade to preference path
        log.warning("nsworkspace_unavailable", error=str(exc))
        return None


def _frontmost_pair() -> tuple[str, str] | None:
    """(localized_name, bundle_id) of the frontmost app, briefly cached."""
    global _frontmost_cache
    now = time.monotonic()
    stamp, value = _frontmost_cache
    if now - stamp < _FRONTMOST_TTL_S:
        return value
    value = None
    ws = _workspace()
    if ws is not None:
        try:
            app = ws.frontmostApplication()
            if app is not None:
                value = (str(app.localizedName() or ""), str(app.bundleIdentifier() or ""))
        except Exception as exc:  # never let probing break routing
            log.warning("frontmost_probe_failed", error=str(exc))
    _frontmost_cache = (now, value)
    return value


def _running_pairs() -> list[tuple[str, str]]:
    """(localized_name, bundle_id) of every regular GUI app, briefly cached."""
    global _running_cache
    now = time.monotonic()
    stamp, value = _running_cache
    if now - stamp < _RUNNING_TTL_S:
        return value
    pairs: list[tuple[str, str]] = []
    ws = _workspace()
    if ws is not None:
        try:
            for app in ws.runningApplications():
                if app.activationPolicy() == 0:  # regular apps only (no agents)
                    pairs.append(
                        (str(app.localizedName() or ""), str(app.bundleIdentifier() or ""))
                    )
        except Exception as exc:
            log.warning("running_probe_failed", error=str(exc))
    _running_cache = (now, pairs)
    return pairs


def _shortlist(category: environment.Category) -> list[dict[str, Any]]:
    return environment.SHORTLISTS.get(category, [])


def _display_for_bundle(category: environment.Category, bundle: str) -> str | None:
    """Canonical display name for a bundle id — what `tell application` needs."""
    for entry in _shortlist(category):
        if entry.get("bundle") == bundle:
            apps = entry.get("apps") or []
            return apps[0] if apps else None
    return None


def frontmost() -> str | None:
    """Localized name of the frontmost application (200 ms cache)."""
    pair = _frontmost_pair()
    return pair[0] if pair else None


def running(category: str) -> list[str]:
    """Category shortlist apps currently running, in shortlist order."""
    cat = _ALIASES.get(category.strip().lower())
    if not cat:
        return []
    running_bundles = {b for _, b in _running_pairs() if b}
    out: list[str] = []
    for entry in _shortlist(cat):
        if entry.get("bundle") in running_bundles:
            apps = entry.get("apps") or []
            if apps:
                out.append(apps[0])
    return out


def _dictionary_preference(category: str, cat: environment.Category) -> str | None:
    """the user's configured pick (dictionary first, then detect_preferred)."""
    from core import apps as core_apps

    pick = dictionary.app_for(category) or dictionary.app_for(cat)
    if pick:
        return pick
    res = environment.detect_preferred(cat)
    return core_apps._display_for_key(cat, getattr(res, "app_name", None))


def inspect(category: str) -> RouteDecision:
    """Full routing decision: picked + why + who else was in the running."""
    cat = _ALIASES.get(category.strip().lower())
    if not cat:
        return RouteDecision(picked="", source="fallback", candidates=[])

    live = running(category)
    pref = _dictionary_preference(category, cat)

    # 1. Frontmost wins when it belongs to the category. the user is LOOKING
    #    at it — that's the app, preferences be damned.
    front = _frontmost_pair()
    if front is not None:
        display = _display_for_bundle(cat, front[1])
        if display:
            return RouteDecision(picked=display, source="frontmost", candidates=live)

    # 2. Something from the category is running → preference breaks ties,
    #    otherwise shortlist order decides.
    if live:
        picked = pref if pref in live else live[0]
        return RouteDecision(picked=picked, source="running", candidates=live)

    # 3. Nothing running → the configured preference (may need launching).
    if pref:
        return RouteDecision(picked=pref, source="preferred", candidates=[])

    # 4. Shortlist fallback (Safari / Music / … — the always-present entry).
    entries = _shortlist(cat)
    fallback = (entries[-1].get("apps") or ["Safari"])[0] if entries else "Safari"
    return RouteDecision(picked=fallback, source="fallback", candidates=[])


def preferred(category: str) -> str:
    """The app to use for ``category`` RIGHT NOW. Synchronous, ~1 ms."""
    decision = inspect(category)
    log.debug("app_routed", category=category, picked=decision.picked, source=decision.source)
    return decision.picked


def _installed_editor_displays() -> list[str]:
    """Display names of the installed shortlist editors, in shortlist order.
    (``detect_preferred`` reports installed entries by KEY; we map back to the
    canonical display name the rest of the stack uses.)"""
    avail = set(environment.detect_preferred("ide").available_alternatives)
    out: list[str] = []
    for entry in _shortlist("ide"):
        if entry.get("key") in avail:
            apps = entry.get("apps") or []
            if apps:
                out.append(apps[0])
    return out


def preferred_or_ask(category: str) -> tuple[str | None, list[str]]:
    """Like :func:`preferred`, but signals when the EDITOR pick is genuinely
    ambiguous and the user should be asked once (23.1-B41).

    Returns ``(picked, [])`` when the chain (frontmost → running → explicit
    preference) is confident, OR when only one editor is installed (not
    ambiguous — just use it). Returns ``(None, candidates)`` only when, for the
    editor category, nothing is frontmost, nothing is running, no explicit
    preference is set, AND more than one editor is installed.
    """
    cat = _ALIASES.get(category.strip().lower())
    if not cat:
        return "", []
    decision = inspect(category)
    if decision.source in ("frontmost", "running"):
        return decision.picked, []
    # An explicit preference (dictionary [apps.*] / env override) is a confident
    # answer — never ask over it.
    if dictionary.app_for(category) or dictionary.app_for(cat):
        return decision.picked, []
    # Only the editor pick is interactive; other categories keep silent fallback.
    if cat != "ide":
        return decision.picked, []
    installed = _installed_editor_displays()
    if len(installed) <= 1:
        return (installed[0] if installed else decision.picked), []
    return None, installed


def failure_data(category: str, wanted: str, reason: str) -> dict[str, Any]:
    """Structured OS-state failure payload for ToolResult.data (22-B33)."""
    decision = inspect(category)
    alternatives = [c for c in decision.candidates if c != wanted]
    if not alternatives:
        alternatives = [
            (e.get("apps") or [""])[0]
            for e in _shortlist(_ALIASES.get(category, "browser"))
            if (e.get("apps") or [""])[0] and (e.get("apps") or [""])[0] != wanted
        ]
    return {
        "failure_reason": reason,
        "wanted": wanted,
        "got": decision.picked if decision.picked != wanted else None,
        "alternatives": alternatives,
        "route_decision": decision.as_dict(),
    }
