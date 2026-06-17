"""Personal notes (Prompt 30): Tier-2 capability — Apple Notes (mac) vs OneNote (win, 30.x)."""

from __future__ import annotations

import sys
from typing import Protocol


class NotesP(Protocol):
    async def create(self, title: str, body: str, folder: str = "") -> None:
        """Create a note. Raises UnsupportedOnPlatform where there's no impl yet."""
        ...


if sys.platform == "darwin":
    from core.platform._mac.notes import MacNotes as _Impl
elif sys.platform == "win32":
    from core.platform._win.notes import WinNotes as _Impl
else:
    from core.platform._stub.notes import StubNotes as _Impl

_instance: NotesP = _Impl()


def get() -> NotesP:
    return _instance
