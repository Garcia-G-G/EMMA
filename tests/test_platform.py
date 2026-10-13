"""Prompt 30 — platform abstraction layer: factory dispatch + graceful stubs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core import platform
from core.platform import UnsupportedOnPlatform, app_control, fs, notes, notify

# The factory follows the host, so these three only hold on macOS. The Windows
# release job runs this file on windows-latest.
mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="asserts the macOS host")


@mac_only
def test_current_os_is_mac_here() -> None:
    assert platform.current_os() == "mac"


@mac_only
def test_factory_returns_mac_impls() -> None:
    assert type(notify.get()).__name__ == "MacNotify"
    assert type(fs.get()).__name__ == "MacFs"
    assert type(app_control.get()).__name__ == "MacAppControl"
    assert type(notes.get()).__name__ == "MacNotes"


@mac_only
def test_mac_fs_paths() -> None:
    f = fs.get()
    assert f.data_dir() == Path.home() / ".emma"
    assert "Library" in str(f.logs_dir())


# ---- the impl classes are stdlib-only → importable + testable on any OS --------


def test_win_fs_uses_localappdata(monkeypatch) -> None:
    from core.platform._win.fs import WinFs

    monkeypatch.setenv("LOCALAPPDATA", "/C/Users/g/AppData/Local")
    monkeypatch.setenv("APPDATA", "/C/Users/g/AppData/Roaming")
    w = WinFs()
    # Compare path parts, not a "/"-joined string: on Windows str() uses "\\".
    assert w.data_dir().parts[-3:] == ("AppData", "Local", "Emma")
    assert w.app_support_dir().parts[-3:] == ("AppData", "Roaming", "Emma")


@pytest.mark.asyncio
async def test_stub_and_win_notes_raise_unsupported() -> None:
    from core.platform._stub.notes import StubNotes
    from core.platform._win.notes import WinNotes

    for impl in (StubNotes(), WinNotes()):
        with pytest.raises(UnsupportedOnPlatform) as ei:
            await impl.create("t", "b")
        assert ei.value.capability == "notas"


def test_win_app_control_raises_unsupported() -> None:
    from core.platform._win.app_control import WinAppControl

    with pytest.raises(UnsupportedOnPlatform):
        WinAppControl().open_app("Notes")


def test_win_and_stub_never_import_pyobjc() -> None:
    # The cardinal rule: cross-platform impls must not pull in macOS frameworks.
    for mod in ("_win.fs", "_win.notes", "_win.app_control", "_stub.notes", "_stub.fs"):
        src = Path(f"core/platform/{mod.replace('.', '/')}.py").read_text()
        assert "objc" not in src and "actions.macos" not in src and "AppKit" not in src


def test_notify_send_does_not_raise() -> None:
    # fire-and-forget contract — must never raise to the caller on any platform
    from core.platform._win.notify import WinNotify

    WinNotify().send("t", "b")  # no exception
    assert sys.platform  # sanity
