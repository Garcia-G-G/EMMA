"""Playback-accurate barge-in truncation for the OpenAI Realtime session.

pipecat 1.2.1 truncates a barged-in assistant turn at
``min(wall_clock_elapsed_since_first_delta, bytes_received_as_ms)``. The
wall-clock term overcounts by the whole output buffer depth (the base-output
queue + PortAudio's ring buffer), so the model's transcript records MORE audio
than the speaker actually played — and Emma later references words the user
never heard. pipecat also lets audio deltas already in flight keep playing
~300 ms past the interruption, because after a truncate ``_current_audio_response``
is None and a late delta simply starts a fresh response.

This module fixes both, entirely Emma-side (no vendoring of pipecat):

- ``PlaybackClock`` — a shared counter of audio bytes actually WRITTEN to the
  output device (post-queue), minus the device's own DAC-buffer latency. That
  is far closer to true speaker position than the LLM-side wall clock, because
  it excludes the base-output queue depth.
- ``PlaybackTrackingLocalAudioTransport`` — a ``LocalAudioTransport`` whose
  output feeds that clock on every frame it hands to PortAudio.
- ``TruncateAccurateRealtimeLLMService`` — an ``OpenAIRealtimeLLMService`` that
  truncates at the clock's played position and DROPS trailing deltas for an
  item it already truncated (no audio past the interruption).

Coupling note: this overrides pipecat PRIVATE members
(``_truncate_current_audio_response``, ``_handle_evt_audio_delta``,
``_current_audio_response``, ``_calculate_audio_duration_ms``). Pinned to the
shape of pipecat 1.2.1. Each override degrades to ``super()`` / stock behavior
if pipecat's internals change, so an upgrade fails safe (stock truncation), not
with a crash. Sample-exact played ms would additionally require PortAudio
callback mode (a real fork of the transport); the blocking-mode + output-latency
estimate here is the accurate-enough version that needs no such rewrite.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import structlog
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.local.audio import (
    LocalAudioOutputTransport,
    LocalAudioTransport,
    LocalAudioTransportParams,
)

log = structlog.get_logger("emma.realtime_playback")

_BYTES_PER_SAMPLE = 2  # 16-bit PCM
_OPENAI_RATE = 24000  # Realtime output PCM rate (matches pipecat's OPENAI_SAMPLE_RATE)


class PlaybackClock:
    """Bytes of assistant audio actually handed to the speaker since turn start.

    Written by the output transport (one increment per device write, which in
    PyAudio blocking mode returns only once PortAudio accepts the bytes), read by
    the LLM service at truncation time. Thread-safe: the transport writes from an
    executor thread, the LLM reads from the event loop.
    """

    def __init__(self, rate: int = _OPENAI_RATE) -> None:
        self._rate = rate
        self._played_bytes = 0
        self._latency_bytes = 0
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Start a new assistant turn — zero the played counter."""
        with self._lock:
            self._played_bytes = 0

    def set_device_latency_s(self, latency_s: float) -> None:
        """Record the DAC-buffer latency: bytes written but not yet clocked out."""
        with self._lock:
            samples = max(0, int(latency_s * self._rate))
            self._latency_bytes = samples * _BYTES_PER_SAMPLE

    def add_played(self, nbytes: int) -> None:
        with self._lock:
            self._played_bytes += max(0, int(nbytes))

    def played_ms(self) -> int:
        """Milliseconds of audio the user has actually heard this turn (>= 0)."""
        with self._lock:
            eff = max(0, self._played_bytes - self._latency_bytes)
        return int((eff / _BYTES_PER_SAMPLE) / self._rate * 1000)


class _PlaybackTrackingOutput(LocalAudioOutputTransport):
    """LocalAudioOutputTransport that reports each written frame to a PlaybackClock."""

    def __init__(self, py_audio: Any, params: LocalAudioTransportParams, clock: PlaybackClock) -> None:
        super().__init__(py_audio, params)
        # NOT self._clock — pipecat's BaseOutputTransport already owns self._clock
        # (a BaseClock it uses for frame timing); clobbering it breaks the pipeline.
        self._playback_clock = clock
        self._latency_read = False

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        ok = await super().write_audio_frame(frame)
        if ok:
            if not self._latency_read and self._out_stream is not None:
                # Read the device's output latency once the stream exists, so
                # played_ms can subtract the DAC buffer that hasn't sounded yet.
                with contextlib.suppress(Exception):
                    self._playback_clock.set_device_latency_s(
                        float(self._out_stream.get_output_latency())
                    )
                self._latency_read = True
            self._playback_clock.add_played(len(frame.audio))
        return ok


class PlaybackTrackingLocalAudioTransport(LocalAudioTransport):
    """LocalAudioTransport whose output() feeds a shared PlaybackClock."""

    def __init__(self, params: LocalAudioTransportParams, clock: PlaybackClock) -> None:
        super().__init__(params)
        self._playback_clock = clock

    def output(self) -> Any:
        if not self._output:
            self._output = _PlaybackTrackingOutput(self._pyaudio, self._params, self._playback_clock)
        return self._output


class TruncateAccurateRealtimeLLMService(OpenAIRealtimeLLMService):
    """OpenAIRealtimeLLMService with playback-accurate barge-in truncation.

    Truncates the barged-in item at the played position from ``playback_clock``
    (clamped to bytes actually received) instead of the wall-clock heuristic, and
    drops any trailing deltas for an item it already truncated so no audio plays
    past the interruption.
    """

    def __init__(self, *args: Any, playback_clock: PlaybackClock, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # NOT self._clock — the base service already owns a BaseClock by that name.
        self._playback_clock = playback_clock
        self._truncated_items: set[str] = set()

    async def _handle_evt_audio_delta(self, evt: Any) -> None:
        item_id = getattr(evt, "item_id", None)
        if item_id is not None and item_id in self._truncated_items:
            # Late delta for an item we already cut — drop it (no trailing speech).
            return
        # First delta of a new assistant turn → reset the played clock so its
        # counter is aligned with this item, matching pipecat's per-turn start_time.
        if getattr(self, "_current_audio_response", None) is None:
            self._playback_clock.reset()
        await super()._handle_evt_audio_delta(evt)  # type: ignore[no-untyped-call]

    async def _truncate_current_audio_response(self) -> None:
        current = getattr(self, "_current_audio_response", None)
        if current is None:
            return
        # If pipecat's internal shape drifted, fail safe to stock behavior.
        if not (hasattr(current, "item_id") and hasattr(current, "total_size")):
            await super()._truncate_current_audio_response()  # type: ignore[no-untyped-call]
            return
        played_ms = self._playback_clock.played_ms()
        self._truncated_items.add(current.item_id)
        if len(self._truncated_items) > 256:  # bound the set over a long session
            self._truncated_items = set(list(self._truncated_items)[-128:])
        self._current_audio_response = None
        try:
            audio_duration_ms = self._calculate_audio_duration_ms(current.total_size)
            truncate_ms = max(0, min(played_ms, audio_duration_ms))
            await self.send_client_event(
                events.ConversationItemTruncateEvent(
                    item_id=current.item_id,
                    content_index=current.content_index,
                    audio_end_ms=truncate_ms,
                )
            )
            log.debug(
                "barge_in_truncate",
                played_ms=played_ms,
                received_ms=audio_duration_ms,
                truncate_ms=truncate_ms,
            )
        except Exception as exc:  # non-fatal: let the session continue
            log.warning("barge_in_truncate_failed", error=str(exc))
