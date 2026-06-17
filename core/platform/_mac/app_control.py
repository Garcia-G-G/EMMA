"""macOS app control (Prompt 30) — wraps NSWorkspace (app_router) + osascript open."""

from __future__ import annotations


class MacAppControl:
    def open_app(self, name: str) -> None:
        from actions import macos

        macos.open_app(name)

    def frontmost(self) -> str | None:
        from core import app_router

        return app_router.frontmost()

    def running(self, name: str) -> bool:
        from core import app_router

        return bool(app_router.running(name))
