"""Playback-accurate barge-in truncation (EMMA-OBVIOUS-3).

Unit-tests the played-audio clock and the two OpenAIRealtimeLLMService overrides
in isolation. NOTE: the end-to-end <400ms barge-in latency and "no trailing
audio" are audio-device behaviors — they must be measured on-device (a real mic
+ speaker); they cannot be asserted headless. These tests lock the LOGIC that
feeds them: the truncation point math and the trailing-delta drop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import realtime_playback as rp
from core.realtime_playback import PlaybackClock, TruncateAccurateRealtimeLLMService


def test_playback_clock_counts_played_ms() -> None:
    clock = PlaybackClock(rate=24000)
    assert clock.played_ms() == 0
    # 24000 bytes = 12000 samples @ 24kHz = 500 ms
    clock.add_played(24000)
    assert clock.played_ms() == 500


def test_playback_clock_subtracts_device_latency() -> None:
    clock = PlaybackClock(rate=24000)
    clock.add_played(24000)  # 500 ms written to the device
    clock.set_device_latency_s(0.1)  # 100 ms still in the DAC buffer, not yet heard
    # 500 ms written minus 100 ms buffered = 400 ms actually played
    assert clock.played_ms() == 400
    # latency can never drive it negative
    clock.set_device_latency_s(10.0)
    assert clock.played_ms() == 0


def test_playback_clock_reset_zeros_the_turn() -> None:
    clock = PlaybackClock(rate=24000)
    clock.add_played(48000)
    clock.reset()
    assert clock.played_ms() == 0


def _svc_no_init(clock: PlaybackClock) -> TruncateAccurateRealtimeLLMService:
    """Build the service without pipecat's heavy __init__ (no network/config)."""
    svc = TruncateAccurateRealtimeLLMService.__new__(TruncateAccurateRealtimeLLMService)
    svc._playback_clock = clock
    svc._truncated_items = set()
    return svc


def test_truncate_uses_played_position_not_wall_clock() -> None:
    clock = PlaybackClock(rate=24000)
    clock.add_played(24000)  # 500 ms actually played
    svc = _svc_no_init(clock)
    # pipecat received 1000 ms of audio (48000 bytes) but only 500 ms played
    svc._current_audio_response = SimpleNamespace(item_id="item_A", content_index=0, total_size=48000)
    sent = {}

    async def fake_send(ev):
        sent["ev"] = ev

    svc.send_client_event = fake_send  # type: ignore[method-assign]
    asyncio.run(svc._truncate_current_audio_response())
    # truncate at the PLAYED point (500 ms), not the received 1000 ms
    assert sent["ev"].audio_end_ms == 500
    assert sent["ev"].item_id == "item_A"
    assert "item_A" in svc._truncated_items  # future deltas for it are now muted
    assert svc._current_audio_response is None


def test_truncate_clamps_to_received_bytes() -> None:
    clock = PlaybackClock(rate=24000)
    clock.add_played(96000)  # 2000 ms "played" — more than was received
    svc = _svc_no_init(clock)
    svc._current_audio_response = SimpleNamespace(item_id="item_B", content_index=0, total_size=24000)
    sent = {}
    svc.send_client_event = AsyncMock(side_effect=lambda ev: sent.update(ev=ev))  # type: ignore[method-assign]
    asyncio.run(svc._truncate_current_audio_response())
    # can't truncate past what the API actually sent (500 ms)
    assert sent["ev"].audio_end_ms == 500


def test_truncate_noop_without_current_response() -> None:
    svc = _svc_no_init(PlaybackClock())
    svc._current_audio_response = None
    svc.send_client_event = AsyncMock()  # type: ignore[method-assign]
    asyncio.run(svc._truncate_current_audio_response())
    svc.send_client_event.assert_not_awaited()


def test_delta_for_truncated_item_is_dropped() -> None:
    svc = _svc_no_init(PlaybackClock())
    svc._current_audio_response = None
    svc._truncated_items.add("gone")
    with patch.object(rp.OpenAIRealtimeLLMService, "_handle_evt_audio_delta", new=AsyncMock()) as sup:
        asyncio.run(svc._handle_evt_audio_delta(SimpleNamespace(item_id="gone", delta="x")))
        sup.assert_not_awaited()  # trailing audio for a cut item never reaches the pipeline


def test_first_delta_resets_clock_and_forwards() -> None:
    clock = PlaybackClock(rate=24000)
    clock.add_played(24000)  # stale bytes from a prior turn
    svc = _svc_no_init(clock)
    svc._current_audio_response = None  # first delta of a new turn
    with patch.object(rp.OpenAIRealtimeLLMService, "_handle_evt_audio_delta", new=AsyncMock()) as sup:
        asyncio.run(svc._handle_evt_audio_delta(SimpleNamespace(item_id="new", delta="x")))
        sup.assert_awaited_once()  # forwarded to pipecat's real handler
    assert clock.played_ms() == 0  # clock was reset for the new turn
