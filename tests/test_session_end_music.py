"""Tests for closing the session after playback tools (Spotify glitch fix).

Music plays out the same speakers as Emma's voice; with the mic open the
Realtime VAD hears the music as user speech and loops. The fix: playback tools
set ToolResult.ends_session=True, and the session closes after Emma's spoken
confirmation so the mic stops fighting the music.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.base import ToolResult
from tools.music import pause, play_track


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# --- ToolResult signal -----------------------------------------------------


def test_toolresult_ends_session_defaults_false():
    assert ToolResult(True, None, "x").ends_session is False


# --- music tools set the signal correctly ----------------------------------


def test_play_track_ends_session():
    fake = MagicMock()
    fake.search.return_value = {
        "tracks": {"items": [{"uri": "spotify:track:1", "name": "Song", "artists": [{"name": "Artist"}]}]}
    }
    with patch("tools.music._spotify", return_value=fake):
        r = play_track("a song")
    assert r.success
    assert r.ends_session is True
    fake.start_playback.assert_called_once()


def test_play_track_apple_music_fallback_ends_session():
    with patch("tools.music._spotify", return_value=None), patch("tools.music.macos.run_applescript"):
        r = play_track("a song")
    assert r.success
    assert r.ends_session is True


def test_pause_keeps_session_open():
    fake = MagicMock()
    with patch("tools.music._spotify", return_value=fake):
        r = pause()
    assert r.success
    assert r.ends_session is False


# --- SessionControl + EndSessionWatcher ------------------------------------


def test_session_control_end_now_cancels_task_once():
    from core import conversation as conv

    ctl = conv.SessionControl()
    task = MagicMock()
    task.cancel = AsyncMock()
    ctl.set_task(task)
    ctl._end_requested = True

    asyncio.run(ctl.end_now("test"))
    task.cancel.assert_awaited_once()
    assert ctl.end_requested is False

    # second call is a no-op (guarded)
    asyncio.run(ctl.end_now("test"))
    task.cancel.assert_awaited_once()


def test_session_control_end_now_noop_when_not_requested():
    from core import conversation as conv

    ctl = conv.SessionControl()
    task = MagicMock()
    task.cancel = AsyncMock()
    ctl.set_task(task)
    asyncio.run(ctl.end_now("test"))
    task.cancel.assert_not_awaited()


def test_end_watcher_ends_on_bot_stopped_when_requested():
    from pipecat.frames.frames import BotStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    from core import conversation as conv

    ctl = conv.SessionControl()
    task = MagicMock()
    task.cancel = AsyncMock()
    ctl.set_task(task)
    ctl._end_requested = True
    watcher = conv.EndSessionWatcher(ctl)
    watcher.push_frame = AsyncMock()

    async def run():
        with patch.object(conv.FrameProcessor, "process_frame", new=AsyncMock()):
            await watcher.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    asyncio.run(run())
    task.cancel.assert_awaited_once()


def test_end_watcher_ignores_when_not_requested():
    from pipecat.frames.frames import BotStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    from core import conversation as conv

    ctl = conv.SessionControl()
    task = MagicMock()
    task.cancel = AsyncMock()
    ctl.set_task(task)
    watcher = conv.EndSessionWatcher(ctl)
    watcher.push_frame = AsyncMock()

    async def run():
        with patch.object(conv.FrameProcessor, "process_frame", new=AsyncMock()):
            await watcher.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    asyncio.run(run())
    task.cancel.assert_not_awaited()


# --- function handler honors ends_session ----------------------------------


def _consume(coro):
    coro.close()
    return MagicMock()


class _Params:
    def __init__(self, name):
        self.function_name = name
        self.arguments = {"query": "x"}

    async def result_callback(self, payload):
        self.payload = payload


def test_handler_requests_end_for_playback_result():
    from core import conversation as conv

    ctl = conv.SessionControl()
    handler = conv._make_function_handler(ctl)

    async def fake_dispatch(name, args):
        return ToolResult(True, None, "Reproduciendo X.", False, ends_session=True)

    with (
        patch("core.conversation.dispatch", new=fake_dispatch),
        patch("core.conversation.asyncio.create_task", side_effect=_consume),
    ):
        asyncio.run(handler(_Params("play_track")))
    assert ctl.end_requested is True


def test_handler_does_not_request_end_for_normal_result():
    from core import conversation as conv

    ctl = conv.SessionControl()
    handler = conv._make_function_handler(ctl)

    async def fake_dispatch(name, args):
        return ToolResult(True, None, "Listo.", False)

    with patch("core.conversation.dispatch", new=fake_dispatch):
        asyncio.run(handler(_Params("get_time")))
    assert ctl.end_requested is False
