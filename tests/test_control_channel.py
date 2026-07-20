"""EMMA-APP Part 3 — UI->daemon control channel (server side + Parar).

The WS transport + menu clicks need on-device verification, but the dispatch
logic (which closes the mute->unmute hole) and the stop-speech helper are pure
and tested here.
"""

from __future__ import annotations

import pytest

from core import conversation, orchestrator
from dashboard import server


@pytest.fixture(autouse=True)
def _reset():
    orchestrator._muted = False
    orchestrator._snooze_until = 0.0
    conversation._active_task = None
    yield
    orchestrator._muted = False
    orchestrator._snooze_until = 0.0
    conversation._active_task = None


class _FakeReq:
    def __init__(self, origin: str | None) -> None:
        self.headers = {"Origin": origin} if origin else {}


class _FakeWS:
    def __init__(self, origin: str | None) -> None:
        self.request = _FakeReq(origin)


def test_origin_check_blocks_cross_site_hijack() -> None:
    # A malicious page opening ws://127.0.0.1/control must NOT be able to unmute
    # or shut down Emma. WS ignores same-origin policy, so we check Origin ourselves.
    assert server._origin_ok(_FakeWS(None)) is True  # native app (no Origin)
    assert server._origin_ok(_FakeWS("http://127.0.0.1:3200")) is True  # own dashboard
    assert server._origin_ok(_FakeWS("http://localhost:3200")) is True
    assert server._origin_ok(_FakeWS("https://evil.example")) is False  # foreign page
    assert server._origin_ok(_FakeWS("http://127.0.0.1:9999")) is False  # wrong port


@pytest.mark.asyncio
async def test_unmute_reaches_orchestrator() -> None:
    orchestrator.mute_mic()
    assert orchestrator.is_muted() is True
    res = await server.dispatch_control({"cmd": "unmute"})
    # the click flipped the real daemon flag — the way back voice can't give
    assert res["ok"] is True and res["muted"] is False
    assert orchestrator.is_muted() is False


@pytest.mark.asyncio
async def test_mute_reaches_orchestrator() -> None:
    res = await server.dispatch_control({"cmd": "mute"})
    assert res["ok"] is True and res["muted"] is True
    assert orchestrator.is_muted() is True


@pytest.mark.asyncio
async def test_snooze_uses_given_minutes() -> None:
    res = await server.dispatch_control({"cmd": "snooze", "minutes": 15})
    assert res["ok"] is True
    assert 800 < res["snooze_remaining_s"] <= 900  # ~15 min pending


@pytest.mark.asyncio
async def test_status_reports_state_without_side_effects() -> None:
    res = await server.dispatch_control({"cmd": "status"})
    assert res["ok"] is True and res["cmd"] == "status"
    assert res["muted"] is False and res["snooze_remaining_s"] == 0


@pytest.mark.asyncio
async def test_unknown_command_is_rejected() -> None:
    res = await server.dispatch_control({"cmd": "rm -rf"})
    assert res["ok"] is False and "unknown" in res["error"]


@pytest.mark.asyncio
async def test_stop_dispatches_to_stop_active_speech(monkeypatch) -> None:
    called = {}

    async def fake_stop() -> bool:
        called["stop"] = True
        return True

    monkeypatch.setattr(conversation, "stop_active_speech", fake_stop)
    res = await server.dispatch_control({"cmd": "stop"})
    assert res["ok"] is True and called.get("stop") is True


@pytest.mark.asyncio
async def test_stop_active_speech_noop_without_session() -> None:
    conversation._active_task = None
    assert await conversation.stop_active_speech() is False


@pytest.mark.asyncio
async def test_stop_active_speech_queues_interruption() -> None:
    class _FakeTask:
        def __init__(self) -> None:
            self.frames: list = []

        async def queue_frame(self, frame) -> None:
            self.frames.append(frame)

    task = _FakeTask()
    conversation._active_task = task
    assert await conversation.stop_active_speech() is True
    assert len(task.frames) == 1  # one interruption frame queued onto the live task
