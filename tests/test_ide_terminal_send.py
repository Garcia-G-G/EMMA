"""B18 (19.6): write into the IDE's integrated terminal (clipboard + paste)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.ide_actions as ia
from tools.base import ToolResult


@pytest.fixture()
def _wired(monkeypatch):
    """Stub the toggle + AppleScript runner, recording call order."""
    calls: list[str] = []

    async def fake_toggle(ide: str = "") -> ToolResult:
        calls.append("toggle")
        return ToolResult(True, {"app": ide}, "ok", False)

    async def fake_osascript(script: str, timeout_s: float = 5.0, on_error: str = ""):
        calls.append("osascript")
        fake_osascript.script = script  # type: ignore[attr-defined]
        return True, ""

    monkeypatch.setattr(ia, "toggle_ide_terminal", fake_toggle)
    monkeypatch.setattr(ia.macos, "osascript_or_friendly", fake_osascript)
    monkeypatch.setattr(ia.asyncio, "sleep", AsyncMock())  # no real 0.3s waits in tests
    return calls, fake_osascript


class TestIdeTerminalSend:
    @pytest.mark.asyncio
    async def test_dispatch_order_toggle_then_paste(self, _wired):
        calls, runner = _wired
        res = await ia.ide_terminal_send("ls", enter=True, ide="Cursor")
        assert res.success
        assert calls == ["toggle", "osascript"]
        assert 'keystroke "v" using {command down}' in runner.script
        assert "keystroke return" in runner.script
        assert 'set the clipboard to "ls"' in runner.script

    @pytest.mark.asyncio
    async def test_enter_false_omits_return(self, _wired):
        _calls, runner = _wired
        res = await ia.ide_terminal_send("npm test", enter=False, ide="Cursor")
        assert res.success
        assert "keystroke return" not in runner.script

    @pytest.mark.asyncio
    async def test_empty_text_asks(self, _wired):
        res = await ia.ide_terminal_send("", ide="Cursor")
        assert res.success is False

    @pytest.mark.asyncio
    async def test_quotes_are_escaped(self, _wired):
        _calls, runner = _wired
        await ia.ide_terminal_send('echo "hola"', enter=False, ide="Cursor")
        assert 'echo \\"hola\\"' in runner.script
