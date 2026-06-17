"""App control (Prompt 30): open / frontmost / running. Tier-2 (mac) vs Tier-3 (win)."""

from __future__ import annotations

import sys
from typing import Protocol


class AppControlP(Protocol):
    def open_app(self, name: str) -> None: ...
    def frontmost(self) -> str | None: ...
    def running(self, name: str) -> bool: ...


if sys.platform == "darwin":
    from core.platform._mac.app_control import MacAppControl as _Impl
elif sys.platform == "win32":
    from core.platform._win.app_control import WinAppControl as _Impl
else:
    from core.platform._stub.app_control import StubAppControl as _Impl

_instance: AppControlP = _Impl()


def get() -> AppControlP:
    return _instance
