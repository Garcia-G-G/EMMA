"""Generic path stub (Prompt 30) — POSIX-ish home fallback."""

from __future__ import annotations

from pathlib import Path


class StubFs:
    def home_dir(self) -> Path:
        return Path.home()

    def data_dir(self) -> Path:
        return Path.home() / ".emma"

    def app_support_dir(self) -> Path:
        return Path.home() / ".local" / "share" / "Emma"

    def logs_dir(self) -> Path:
        return Path.home() / ".local" / "state" / "Emma" / "logs"
