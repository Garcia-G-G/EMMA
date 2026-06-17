"""Windows app control (Prompt 30) — Tier-3 stub. Real impl (UI Automation) → 30.x."""

from __future__ import annotations

from core.platform.base import UnsupportedOnPlatform


class WinAppControl:
    def open_app(self, name: str) -> None:
        raise UnsupportedOnPlatform("abrir apps")

    def frontmost(self) -> str | None:
        raise UnsupportedOnPlatform("ver la app activa")

    def running(self, name: str) -> bool:
        raise UnsupportedOnPlatform("ver apps abiertas")
