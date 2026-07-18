"""EMMA-APP Part 2 — emma.ui contract checks.

The menubar/window BEHAVIOR needs on-device verification (headless can't render a
status item). These lock the pure contract: the module imports and registers its
Obj-C classes, every daemon state maps to a known icon, and the UI talks only to
loopback.
"""

from __future__ import annotations

import importlib

import pytest

ui = pytest.importorskip("emma.ui.__main__", reason="PyObjC/macOS only")


def test_module_registers_objc_classes() -> None:
    m = importlib.import_module("emma.ui.__main__")
    assert m.EmmaBar.__name__ == "EmmaBar"
    assert hasattr(m.EmmaBar, "setState_")  # the callAfter target exists


def test_every_state_bucket_has_an_icon() -> None:
    for bucket in set(ui._STATE_BUCKET.values()):
        assert bucket in ui._ICON_FOR_STATE, f"no icon for state bucket {bucket}"
    # the privacy + sleep states specifically must be distinguishable
    assert ui._ICON_FOR_STATE["muted"] == "mic.slash"
    assert ui._ICON_FOR_STATE["snoozing"] == "moon"


def test_talks_only_to_loopback() -> None:
    assert ui._HTTP_URL.startswith("http://127.0.0.1:")
    assert ui._WS_URL.startswith("ws://127.0.0.1:")
    # WS is HTTP port + 1 (3201 by default), the events bus
    assert ui._WS_URL.endswith("/events")


def test_unknown_state_falls_back_to_idle() -> None:
    assert ui._STATE_BUCKET.get("some_future_state", "idle") == "idle"
