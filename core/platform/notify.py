"""System notification capability (Prompt 30). Tier-1-ish — every OS can notify."""

from __future__ import annotations

import sys
from typing import Protocol


class NotifyP(Protocol):
    def send(self, title: str, body: str) -> None:
        """Show a system notification. Fire-and-forget; never raises to the caller."""
        ...


if sys.platform == "darwin":
    from core.platform._mac.notify import MacNotify as _Impl
elif sys.platform == "win32":
    from core.platform._win.notify import WinNotify as _Impl
else:
    from core.platform._stub.notify import StubNotify as _Impl

_instance: NotifyP = _Impl()


def get() -> NotifyP:
    return _instance
