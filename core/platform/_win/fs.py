"""Windows path conventions (Prompt 30) — per-user roots, no admin needed."""

from __future__ import annotations

import os
from pathlib import Path


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def _roaming() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))


class WinFs:
    def home_dir(self) -> Path:
        return Path.home()

    def data_dir(self) -> Path:
        return _local() / "Emma"

    def app_support_dir(self) -> Path:
        return _roaming() / "Emma"

    def logs_dir(self) -> Path:
        return _local() / "Emma" / "Logs"
