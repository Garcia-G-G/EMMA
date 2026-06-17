"""Generic app-control stub (Prompt 30)."""

from __future__ import annotations

from core.platform.base import UnsupportedOnPlatform


class StubAppControl:
    def open_app(self, name: str) -> None:
        raise UnsupportedOnPlatform("abrir apps")

    def frontmost(self) -> str | None:
        raise UnsupportedOnPlatform("ver la app activa")

    def running(self, name: str) -> bool:
        raise UnsupportedOnPlatform("ver apps abiertas")
