"""harden_local_files must lock ~/.emma down without breaking what runs from it.

It runs on every daemon boot. It used to set every file to 0600, including
~/.emma/EmmaDaemon.app/Contents/MacOS/emma-daemon — the daemon's own
executable. The daemon booted once, stripped its own +x, and every launchd
respawn after that failed to exec (exit 78, nothing logged): the restart loop.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import permissions


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".emma").mkdir(mode=0o755)
    return tmp_path / ".emma"


def _mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def test_executables_stay_executable(home: Path) -> None:
    exe = home / "EmmaDaemon.app" / "Contents" / "MacOS" / "emma-daemon"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"\xcf\xfa\xed\xfe")
    os.chmod(exe, 0o755)

    permissions.harden_local_files()

    assert _mode(exe) == 0o700  # owner can still exec; group/other lose everything


def test_data_files_lose_group_and_other(home: Path) -> None:
    db = home / "memory.db"
    db.write_text("x")
    os.chmod(db, 0o644)

    permissions.harden_local_files()

    assert _mode(db) == 0o600
    assert _mode(home) == 0o700


def test_idempotent(home: Path) -> None:
    exe = home / "bin-tool"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    permissions.harden_local_files()
    assert permissions.harden_local_files() == {"dirs": 0, "files": 0}
    assert _mode(exe) == 0o700
