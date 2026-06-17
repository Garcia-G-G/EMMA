"""Platform abstraction layer (Prompt 30).

The ONE place platform branching happens. Each capability module exposes a typing
Protocol + a ``get()`` factory that returns the right implementation for the current
OS, chosen once at import:

  - darwin → ``_mac``  (Tier 2: AppleScript / NSWorkspace / EventKit / AX)
  - win32  → ``_win``  (Tier 3: stubs today; real impls land in 30.x)
  - other  → ``_stub`` (raise :class:`UnsupportedOnPlatform`)

Tier-1 features (voice loop, Realtime, memory, web search, social, dictionary) never
touch this layer. Tier-2/3 tools call ``core.platform.<cap>.get()`` instead of
inlining ``osascript``/pyobjc, so a single switch decides behavior per OS and the
rest of the codebase stays platform-agnostic.

Rule: ``_mac`` modules may import pyobjc / ``actions.macos``; ``_win`` / ``_stub``
NEVER do, and nothing outside this package branches on ``sys.platform``.
"""

from __future__ import annotations

import sys

from core.platform.base import UnsupportedOnPlatform

__all__ = ["UnsupportedOnPlatform", "current_os"]


def current_os() -> str:
    return {"darwin": "mac", "win32": "windows"}.get(sys.platform, "other")
