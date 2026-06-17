"""macOS notification (Prompt 30) — wraps the existing osascript notifier."""

from __future__ import annotations


class MacNotify:
    def send(self, title: str, body: str) -> None:
        from actions import macos

        macos.notify(title, body)
