"""Cheap, cached probes for "can this tool actually run on this machine?".

Every registered tool costs ~60 input tokens on **every turn** (measured
against gpt-realtime-2, see ``_planning/notes/LAUNCH-1-VERIFY.md``). A tool
that cannot possibly succeed — no API key, no CLI on PATH, no browser binary
— is pure cost with zero capability, so ``tools.registry.openai_tool_specs()``
filters it out of the session payload.

Two rules govern everything in here:

1. **No slow I/O.** These run on every ``build_pipeline`` (once per wake
   word). No subprocesses, no network, no app launches. Filesystem probes are
   memoized behind a short TTL so a burst of sessions costs one ``stat``.
2. **Dynamic, never a static list.** A predicate reads live state. Add a
   ``GITHUB_TOKEN`` or ``brew install brightness`` and the tools come back —
   filesystem probes within ``_TTL_S``, settings-backed ones at the next
   daemon start (pydantic-settings reads ``.env`` once per process).

A tool stays available whenever we cannot cheaply prove otherwise: a probe
that errors returns True. Under-filtering costs tokens; over-filtering
silently removes a capability, which is the failure mode this module exists
to prevent.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from config.settings import settings

# Filesystem probes are re-checked at most this often. Long enough that a
# rapid-fire series of sessions costs a single stat, short enough that
# installing a CLI shows up without restarting the daemon.
_TTL_S = 300.0

_cache: dict[str, tuple[float, bool]] = {}


def _cached(key: str, probe: Callable[[], bool]) -> bool:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and (now - hit[0]) < _TTL_S:
        return hit[1]
    try:
        value = bool(probe())
    except Exception:
        value = True  # never let a broken probe remove a working tool
    _cache[key] = (now, value)
    return value


def reset_cache() -> None:
    """Drop every memoized probe (tests, and ``reload_tools``)."""
    _cache.clear()


def has_binary(name: str) -> bool:
    """True if ``name`` resolves on PATH. Memoized — ``which`` stats every PATH entry."""
    return _cached(f"bin:{name}", lambda: shutil.which(name) is not None)


def has_path(path: str | Path) -> bool:
    """True if ``path`` exists on disk. Memoized."""
    p = Path(path)
    return _cached(f"path:{p}", p.exists)


def has_app(app_name: str) -> bool:
    """True if ``<app_name>.app`` is installed in either Applications dir."""
    return _cached(
        f"app:{app_name}",
        lambda: Path(f"/Applications/{app_name}.app").exists()
        or (Path.home() / "Applications" / f"{app_name}.app").exists(),
    )


def has_chromium() -> bool:
    """True if Playwright's Chromium **binary** is downloaded.

    The ``playwright`` Python package is a hard dependency, but the browser
    itself is a separate ~150 MB download that ``_landing/install.sh`` never
    performs — so on a clean install ``tools/browser.py`` cannot launch. Probe
    the download cache directly rather than calling into Playwright, which
    would start a driver subprocess.
    """

    def _probe() -> bool:
        for root in (
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        ):
            if root.is_dir() and any(
                c.name.startswith("chromium-") and c.is_dir() for c in root.iterdir()
            ):
                return True
        return False

    return _cached("chromium", _probe)


def has_web_search() -> bool:
    """True if a web-search backend is configured (``tools/web.py:search_results``)."""
    return bool(settings.BRAVE_API_KEY or settings.TAVILY_API_KEY)
