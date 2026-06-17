"""Windows notes (Prompt 30) — Tier-3 stub. Real impl (OneNote COM) → 30.x."""

from __future__ import annotations

from core.platform.base import UnsupportedOnPlatform


class WinNotes:
    async def create(self, title: str, body: str, folder: str = "") -> None:
        raise UnsupportedOnPlatform("notas")
