"""Wake-word listener must cancel instantly on shutdown.

The PortAudio stream stop/close can block on the CoreAudio HAL mutex; if that
ran inline during cancellation it would hang Ctrl+C / SIGTERM. The listener now
closes the stream in a daemon thread on cancellation and re-raises promptly.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.wake_word as ww


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_close_stream_background_aborts_and_closes():
    stream = MagicMock()
    ww._close_stream_background(stream)
    # runs in a daemon thread; give it a moment
    time.sleep(0.2)
    stream.abort.assert_called_once()
    stream.close.assert_called_once()


def test_listen_cancellation_is_prompt_and_reraises():
    fake_stream = MagicMock()
    fake_model = MagicMock()

    with (
        patch.object(ww, "_get_model", new=AsyncMock(return_value=fake_model)),
        patch.object(ww.sd, "RawInputStream", return_value=fake_stream),
        patch.object(ww, "play_wake_chime"),
    ):

        async def run():
            task = asyncio.create_task(ww.listen_for_wake_word())
            await asyncio.sleep(0.1)  # let it reach detected.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                # must finish well under the timeout (no HAL-mutex hang)
                await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(run())

    fake_stream.start.assert_called_once()
    # background close should have aborted the stream (not a synchronous stop)
    time.sleep(0.2)
    assert fake_stream.abort.called
    fake_stream.stop.assert_not_called()  # cancellation path skips the blocking stop()
