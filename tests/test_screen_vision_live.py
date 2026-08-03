"""Live AX tests — the real API, no mocks. `pytest -m macos_live`

Why this file exists: `tests/test_screen_vision.py:53-55` has an **autouse**
fixture that monkeypatches `sv._attr`, the single seam every AX read goes
through. That is the right pattern for testing the tree logic, and those ~50
tests are worth keeping — but it also means they pass identically whether the
Accessibility permission is granted or the entire AX path is dead. It was dead
for the life of the project and the suite stayed green.

So: mock exactly one seam for the logic tests, and keep one file that mocks
nothing.

The acquisition entry points — the ones the mocked suite structurally cannot
reach, because they are what produce the elements the mock replaces — are
covered here: `frontmost_window`, `_focused_window`, `_current_screen_sync`,
`read_current_screen`, plus `_bounds`, `_is_secure` and `_resolve_focused_pane`.

Every skip is LOUD. A skip that reads "AX denied" is a finding, not a pass.
"""

from __future__ import annotations

import pytest

from core import permissions
from core import screen_vision as sv

pytestmark = pytest.mark.macos_live


def _require_ax() -> None:
    """Skip with a message that says what is broken, never a bare skip."""
    if not sv._AX_OK:
        pytest.skip("ApplicationServices unavailable — this test verifies nothing")
    if not permissions.check_accessibility_ax():
        pytest.skip(
            "AX DENIED — this test cannot verify anything. This process is not "
            "Accessibility-trusted, so every AX read returns None and screen "
            "vision answers 'No veo una ventana enfocada'. Grant it in System "
            "Settings → Privacy & Security → Accessibility, or run "
            "`python -m emma.permissions bootstrap`."
        )
    if sv._frontmost_app() is None:
        pytest.skip("no frontmost app (screen locked / login window) — inconclusive")


def test_process_is_ax_trusted() -> None:
    """The precondition for the whole feature, asserted rather than assumed."""
    if not sv._AX_OK:
        pytest.skip("ApplicationServices unavailable")
    assert permissions.check_accessibility_ax(), (
        "This process is NOT Accessibility-trusted. Screen vision cannot work. "
        "Nothing else in this file can tell you anything until this passes."
    )


def test_frontmost_window_returns_a_real_snapshot() -> None:
    """The top of the acquisition chain, against the live AX API."""
    _require_ax()
    snap = sv.frontmost_window()
    assert snap is not None, (
        "frontmost_window() returned None while an app is focused — the exact "
        "signature of a TCC denial (see permissions.ax_smoke)."
    )
    assert snap.app, "snapshot carries no app name"
    assert snap.role == sv.ROLE_WINDOW


def test_focused_window_element_resolves() -> None:
    """`_app_element` succeeds even when denied (it only mints a handle), so
    `_focused_window` is the first call that proves the permission."""
    _require_ax()
    app = sv._frontmost_app()
    app_elem = sv._app_element(app)
    assert app_elem is not None
    assert sv._focused_window(app_elem) is not None


def test_current_screen_sync_reads_the_live_window() -> None:
    _require_ax()
    read = sv._current_screen_sync()
    assert read is not None, "no ScreenRead from the live window"
    assert read.app
    assert read.structured, "structured block is empty"
    assert "App:" in read.structured


@pytest.mark.asyncio
async def test_read_current_screen_produces_text() -> None:
    _require_ax()
    text = await sv.read_current_screen()
    assert isinstance(text, str)
    assert text.strip(), "read_current_screen returned nothing for a live window"


def test_bounds_of_the_live_window_are_sane() -> None:
    _require_ax()
    fw = sv._frontmost_window_element()
    assert fw is not None
    _app, win = fw
    bounds = sv._bounds(win)
    if bounds is None:
        pytest.skip("this app does not expose AXPosition/AXSize")
    _x, _y, w, h = bounds
    assert w > 0 and h > 0, f"degenerate window bounds: {bounds}"


def test_is_secure_does_not_crash_on_live_elements() -> None:
    """Secure-field detection guards password redaction — it must hold up on
    real elements, not just synthetic ones."""
    _require_ax()
    fw = sv._frontmost_window_element()
    assert fw is not None
    _app, win = fw
    assert sv._is_secure(win) is False  # a window is never a secure field
    for child in sv._children(win)[:20]:
        assert isinstance(sv._is_secure(child), bool)


def test_resolve_focused_pane_against_a_live_window() -> None:
    _require_ax()
    resolved = sv._resolve_focused_pane()
    if resolved is None:
        pytest.skip("no focused element in the frontmost app right now")
    app, _focused, pane, win, ancestors = resolved
    assert app
    assert pane is not None and win is not None
    assert isinstance(ancestors, list)


def test_screen_recording_grant_is_real() -> None:
    """The look_at_screen fallback silently returns a blank desktop without it."""
    if not sv._AX_OK:
        pytest.skip("not a macOS session with the frameworks — verifies nothing")
    assert permissions.check_screen_recording(), (
        "Screen Recording NOT granted — `screencapture` returns a wallpaper-only "
        "image with rc=0, so look_at_screen OCRs nothing and blames the content."
    )


def test_ax_smoke_agrees_with_the_live_read() -> None:
    """The preflight probe must match reality, or it is another false green."""
    _require_ax()
    ok, reason = permissions.ax_smoke()
    assert ok and reason == "ok", f"ax_smoke says {reason} while AX reads fine"
