"""Filesystem path conventions (Prompt 30).

macOS uses ~/Library/...; Windows uses %LOCALAPPDATA%/%APPDATA%. This is the only
place those roots are decided — never hard-code ~/Library outside ``_mac/fs.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol


class FsP(Protocol):
    def home_dir(self) -> Path: ...
    def data_dir(self) -> Path: ...          # ~/.emma   |  %LOCALAPPDATA%\Emma
    def app_support_dir(self) -> Path: ...    # ~/Library/Application Support/Emma | %APPDATA%\Emma
    def logs_dir(self) -> Path: ...           # ~/Library/Logs/Emma | %LOCALAPPDATA%\Emma\Logs


if sys.platform == "darwin":
    from core.platform._mac.fs import MacFs as _Impl
elif sys.platform == "win32":
    from core.platform._win.fs import WinFs as _Impl
else:
    from core.platform._stub.fs import StubFs as _Impl

_instance: FsP = _Impl()


def get() -> FsP:
    return _instance
