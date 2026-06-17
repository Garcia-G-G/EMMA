"""macOS path conventions (Prompt 30). The ONLY place ~/Library/... lives."""

from __future__ import annotations

from pathlib import Path


class MacFs:
    def home_dir(self) -> Path:
        return Path.home()

    def data_dir(self) -> Path:
        return Path.home() / ".emma"

    def app_support_dir(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Emma"

    def logs_dir(self) -> Path:
        return Path.home() / "Library" / "Logs" / "Emma"
