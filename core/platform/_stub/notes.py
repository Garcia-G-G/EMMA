"""Generic notes stub (Prompt 30)."""

from __future__ import annotations

from core.platform.base import UnsupportedOnPlatform


class StubNotes:
    async def create(self, title: str, body: str, folder: str = "") -> None:
        raise UnsupportedOnPlatform("notas")
