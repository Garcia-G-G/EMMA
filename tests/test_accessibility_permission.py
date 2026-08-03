"""The Accessibility / Screen Recording permissions screen vision needs.

Screen vision (core/screen_vision.py, 701 lines) never worked in production
because nothing ever asked macOS for Accessibility. A process appears in
System Settings → Privacy → Accessibility ONLY after calling
`AXIsProcessTrustedWithOptions` with the prompt option; Emma opened the pane
without calling it, so there was no row to toggle. And the probe that claimed
to check the permission (`check_accessibility`) actually shelled out to
osascript to ask System Events for a process list — an *Automation* grant —
so it reported "granted" while AX was stone dead.

These tests pin the distinction, because the whole failure was a category
error between two permissions that sound alike.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import patch

import pytest

from core import permissions


def _fake_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# --- the probe measures AX trust, not Automation ---------------------------


def test_check_accessibility_ax_reads_axisprocesstrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        _fake_module("ApplicationServices", AXIsProcessTrusted=lambda: True),
    )
    assert permissions.check_accessibility_ax() is True

    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        _fake_module("ApplicationServices", AXIsProcessTrusted=lambda: False),
    )
    assert permissions.check_accessibility_ax() is False


def test_check_accessibility_ax_never_shells_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original bug in one assertion: AX trust is not an osascript question."""

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("AX trust must not be probed with a subprocess")

    monkeypatch.setattr(permissions.subprocess, "run", boom)
    permissions.check_accessibility_ax()  # must not raise


def test_ax_probe_and_system_events_probe_are_independent() -> None:
    """They can disagree — and did, for the life of the feature."""
    with (
        patch("core.permissions.check_accessibility_ax", return_value=False),
        patch("core.permissions.check_system_events_automation", return_value=True),
    ):
        assert permissions.check_accessibility_ax() is False
        assert permissions.check_system_events_automation() is True


def test_old_probe_name_is_gone() -> None:
    """`check_accessibility` was a lie; nothing may resurrect the name."""
    assert not hasattr(permissions, "check_accessibility")
    assert hasattr(permissions, "check_system_events_automation")


def test_ax_trust_probe_degrades_to_false_without_the_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ApplicationServices", _fake_module("ApplicationServices"))
    assert permissions.check_accessibility_ax() is False


# --- the request actually creates the row ----------------------------------


def test_request_accessibility_trust_passes_the_prompt_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without kAXTrustedCheckOptionPrompt: True macOS creates no row at all."""
    seen: list[dict[str, Any]] = []

    def _with_options(opts: dict[str, Any]) -> bool:
        seen.append(opts)
        return False

    monkeypatch.setitem(
        sys.modules,
        "ApplicationServices",
        _fake_module(
            "ApplicationServices",
            AXIsProcessTrustedWithOptions=_with_options,
            kAXTrustedCheckOptionPrompt="AXTrustedCheckOptionPrompt",
        ),
    )
    permissions.request_accessibility_trust()
    assert seen == [{"AXTrustedCheckOptionPrompt": True}]


def test_request_screen_recording_uses_the_requesting_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CGPreflight* only reads; CGRequest* is what shows the alert."""
    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "Quartz",
        _fake_module(
            "Quartz",
            CGRequestScreenCaptureAccess=lambda: called.append("request") or True,
            CGPreflightScreenCaptureAccess=lambda: called.append("preflight") or True,
        ),
    )
    assert permissions.request_screen_recording() is True
    assert called == ["request"]


def test_manual_panes_carry_a_requester_for_the_two_that_have_one() -> None:
    by_pane = {p: req for p, _, req in permissions._MANUAL_PANES}
    assert by_pane["Accessibility"] is permissions.request_accessibility_trust
    assert by_pane["ScreenCapture"] is permissions.request_screen_recording
    # No request API exists for these two.
    assert by_pane["AllFiles"] is None
    assert by_pane["Calendars"] is None


# --- the functional smoke test ---------------------------------------------


def _patch_smoke(monkeypatch: pytest.MonkeyPatch, frontmost: Any, window: Any) -> None:
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        _fake_module(
            "AppKit",
            NSWorkspace=type(
                "W",
                (),
                {
                    "sharedWorkspace": staticmethod(
                        lambda: type(
                            "S", (), {"frontmostApplication": staticmethod(lambda: frontmost)}
                        )()
                    )
                },
            ),
        ),
    )
    from core import screen_vision

    monkeypatch.setattr(screen_vision, "frontmost_window", lambda: window)


def test_ax_smoke_detects_the_denial_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """A focused app with no readable focused window IS the TCC denial tell."""
    _patch_smoke(monkeypatch, frontmost=object(), window=None)
    assert permissions.ax_smoke() == (False, "ax_denied")


def test_ax_smoke_ok_when_the_window_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_smoke(monkeypatch, frontmost=object(), window=object())
    assert permissions.ax_smoke() == (True, "ok")


def test_ax_smoke_is_inconclusive_with_no_frontmost_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locked screen / login window — absence of evidence, not a denial."""
    _patch_smoke(monkeypatch, frontmost=None, window=None)
    assert permissions.ax_smoke() == (True, "no_frontmost_app")


def test_ax_smoke_never_reports_a_denial_on_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "AppKit", _fake_module("AppKit"))
    ok, reason = permissions.ax_smoke()
    assert ok is True
    assert reason.startswith("probe_error:")


# --- preflight surfaces it --------------------------------------------------


def test_preflight_logs_ax_denied_and_speaks_once() -> None:
    with (
        patch("core.permissions.check_microphone", return_value=True),
        patch("core.permissions.check_accessibility_ax", return_value=False),
        patch("core.permissions.ax_smoke", return_value=(False, "ax_denied")),
        patch("core.permissions.check_screen_recording", return_value=True),
        patch("core.permissions.check_system_events_automation", return_value=True),
        patch("core.permissions.check_automation", return_value=True),
        patch("core.permissions.check_calendar", return_value=True),
        patch("core.permissions._say") as say,
        patch("core.permissions._open_settings") as open_settings,
    ):
        assert permissions.preflight() is True  # not fatal
    assert say.call_count == 1
    assert "accesibilidad" in say.call_args[0][0].lower()
    assert "Accessibility" in [c.args[0] for c in open_settings.call_args_list]


def test_preflight_flags_trusted_but_dead() -> None:
    """A stale grant against a replaced binary reads trusted and reads nothing."""
    with (
        patch("core.permissions.check_microphone", return_value=True),
        patch("core.permissions.check_accessibility_ax", return_value=True),
        patch("core.permissions.ax_smoke", return_value=(False, "ax_denied")),
        patch("core.permissions.check_screen_recording", return_value=True),
        patch("core.permissions.check_system_events_automation", return_value=True),
        patch("core.permissions.check_automation", return_value=True),
        patch("core.permissions.check_calendar", return_value=True),
        patch("core.permissions._say") as say,
        patch("core.permissions._open_settings"),
    ):
        permissions.preflight()
    assert say.call_count == 1  # the smoke test alone is enough to raise it


def test_preflight_opens_the_screen_capture_pane_when_denied() -> None:
    with (
        patch("core.permissions.check_microphone", return_value=True),
        patch("core.permissions.check_accessibility_ax", return_value=True),
        patch("core.permissions.ax_smoke", return_value=(True, "ok")),
        patch("core.permissions.check_screen_recording", return_value=False),
        patch("core.permissions.check_system_events_automation", return_value=True),
        patch("core.permissions.check_automation", return_value=True),
        patch("core.permissions.check_calendar", return_value=True),
        patch("core.permissions._say"),
        patch("core.permissions._open_settings") as open_settings,
    ):
        permissions.preflight()
    assert "ScreenCapture" in [c.args[0] for c in open_settings.call_args_list]


# --- the screenshot fallback fails honestly --------------------------------


def test_capture_refuses_without_screen_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the grant screencapture writes a blank desktop PNG and exits 0,
    so the rc!=0 guard cannot catch it. Check the permission first instead."""
    from core import visual_screen

    monkeypatch.setattr(visual_screen, "_screen_recording_ok", lambda: False)

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not shell out to screencapture when denied")

    monkeypatch.setattr(visual_screen.subprocess, "run", boom)
    assert visual_screen._capture(None) is None


def test_capture_proceeds_when_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import visual_screen

    monkeypatch.setattr(visual_screen, "_screen_recording_ok", lambda: True)
    ran: list[list[str]] = []

    class _P:
        returncode = 0

    def _run(args: list[str], **k: Any) -> _P:
        ran.append(args)
        with open(args[-1], "wb") as f:
            f.write(b"PNGBYTES")
        return _P()

    monkeypatch.setattr(visual_screen.subprocess, "run", _run)
    assert visual_screen._capture(None) == b"PNGBYTES"
    assert ran and ran[0][0].endswith("screencapture")
