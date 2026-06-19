"""Visual screen reading (screenshot → on-device OCR). Capture + Vision are
mocked so these run with no display and never touch the real frameworks."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.visual_screen_tool as vst
from core import visual_screen as vis


def test_read_screen_sync_assembles_record(monkeypatch) -> None:
    monkeypatch.setattr(vis, "_frontmost_window_id", lambda: 42)
    monkeypatch.setattr(vis, "_capture", lambda wid: b"PNGDATA")
    monkeypatch.setattr(vis, "_ocr", lambda png: ["Hola mundo", "segunda línea"])
    monkeypatch.setattr(vis, "_app_name", lambda: "Preview")
    r = vis._read_screen_sync()
    assert r is not None
    assert r.app == "Preview" and r.scope == "window" and r.line_count == 2
    assert "Hola mundo" in r.text


def test_read_screen_sync_full_screen_when_no_window(monkeypatch) -> None:
    monkeypatch.setattr(vis, "_frontmost_window_id", lambda: None)
    monkeypatch.setattr(vis, "_capture", lambda wid: b"PNG")
    monkeypatch.setattr(vis, "_ocr", lambda png: ["x"])
    monkeypatch.setattr(vis, "_app_name", lambda: "X")
    assert vis._read_screen_sync().scope == "screen"


def test_read_screen_sync_none_when_capture_fails(monkeypatch) -> None:
    monkeypatch.setattr(vis, "_frontmost_window_id", lambda: None)
    monkeypatch.setattr(vis, "_capture", lambda wid: None)  # e.g. no Screen Recording perm
    assert vis._read_screen_sync() is None


@pytest.mark.asyncio
async def test_look_at_screen_returns_text(monkeypatch) -> None:
    monkeypatch.setattr(vis, "read_screen",
                        AsyncMock(return_value=vis.VisualRead("Safari", "Octopus es un cefalópodo", 1, "window")))
    res = await vst.look_at_screen()
    assert res.success and "cefalópodo" in res.user_message
    assert res.data["scope"] == "window"


@pytest.mark.asyncio
async def test_look_at_screen_summarizes_with_question(monkeypatch) -> None:
    monkeypatch.setattr(vis, "read_screen",
                        AsyncMock(return_value=vis.VisualRead("Safari", "long article text", 50, "window")))
    monkeypatch.setattr(vst, "_summarize", AsyncMock(return_value="Trata de pulpos."))
    res = await vst.look_at_screen("¿de qué trata?")
    assert res.success and res.user_message == "Trata de pulpos."


@pytest.mark.asyncio
async def test_look_at_screen_degrades_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(vis, "read_screen", AsyncMock(return_value=None))
    res = await vst.look_at_screen()
    assert not res.success and "Grabación de pantalla" in res.user_message


@pytest.mark.asyncio
async def test_look_at_screen_handles_no_text(monkeypatch) -> None:
    monkeypatch.setattr(vis, "read_screen",
                        AsyncMock(return_value=vis.VisualRead("Game", "", 0, "screen")))
    res = await vst.look_at_screen()
    assert res.success and "no encontré texto" in res.user_message


def test_capture_deletes_temp_file(monkeypatch, tmp_path) -> None:
    # Simulate screencapture writing a file, then assert _capture removes it.
    made = tmp_path / "shot.png"

    def fake_mktemp(suffix=""):
        made.write_bytes(b"PNGBYTES")
        return str(made)

    class _R:
        returncode = 0

    monkeypatch.setattr(vis.tempfile, "mktemp", fake_mktemp)
    monkeypatch.setattr(vis.subprocess, "run", lambda *a, **k: _R())
    data = vis._capture(None)
    assert data == b"PNGBYTES"
    assert not made.exists()  # screenshot never left on disk
