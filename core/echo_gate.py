"""Echo gate filter for Pipecat's LocalAudioTransport.

On a MacBook with built-in mic + speakers, the mic picks up everything
the speakers emit. The Realtime API's server-side VAD interprets the
echo as user speech and fires ``speech_started``, which truncates
Emma's response after 1-2 words (a self-interruption loop).

While the bot is speaking, this filter suppresses mic audio UNLESS
the input energy exceeds a threshold — a close-range human voice is
significantly louder than speaker echo reflected back into the mic.
This preserves barge-in: Garcia can interrupt Emma by speaking up,
but quiet echo from the speakers is silenced.

After the bot stops speaking, a short tail period continues the gate
to let residual echo decay.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import numpy as np
import structlog
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterControlFrame

log = structlog.get_logger("emma.echo_gate")

# Garcia's buffered over-the-opener speech is capped at 2 s of 24 kHz mono
# int16 — enough for "Emma, espera" without replaying a whole monologue.
_OPENER_BUFFER_CAP_BYTES = 2 * 24_000 * 2


def normalized_xcorr_peak(
    in_win: np.ndarray, ref: np.ndarray, max_lag: int, lag_stride: int
) -> float:
    """Peak normalized cross-correlation of ``in_win`` against ``ref`` (Layer C).

    Slides the input window back across ``ref`` by ``0..max_lag`` samples in
    ``lag_stride`` steps (the speaker→mic latency is unknown, so we search a
    lag range and take the peak). Each position is a mean-subtracted, unit-
    normalized dot product (Pearson) — 1.0 means the mic frame IS the played
    audio (echo); ~0 means unrelated (real speech). Returns ``max |corr|``.

    Cost: ``(max_lag/lag_stride)`` dot products of length ``len(in_win)`` —
    a few dozen x 2400 multiplies, well under the 5 ms/frame budget.
    """
    w = in_win.size
    if w == 0 or ref.size < w:
        return 0.0
    iw = in_win - in_win.mean()
    iw_norm = float(np.sqrt(np.dot(iw, iw)))
    if iw_norm == 0.0:
        return 0.0
    best = 0.0
    capped_max_lag = min(max_lag, ref.size - w)
    stride = max(1, lag_stride)
    lag = 0
    while lag <= capped_max_lag:
        end = ref.size - lag
        seg = ref[end - w : end]
        seg = seg - seg.mean()
        seg_norm = float(np.sqrt(np.dot(seg, seg)))
        if seg_norm > 0.0:
            corr = abs(float(np.dot(iw, seg)) / (iw_norm * seg_norm))
            if corr > best:
                best = corr
        lag += stride
    return best


class SpeechPhase:
    """Tracks Emma's speech phase: ``opener`` | ``body`` | None (22-B32).

    The OPENER is the first utterance after a session opens (or resumes via
    a continuation seed) — it must never be cut, or Emma stalls mid-greeting
    and the conversation derails. It graduates to BODY when EITHER ≥ 8 words
    have been spoken OR 1.5 s have elapsed (the OR matters: word count alone
    would block forever on short greetings). Everything after the opener
    utterance is body and fully interruptible. A new pipeline session
    constructs a fresh instance, which IS the reset.
    """

    OPENER_MAX_S = 1.5
    OPENER_MAX_WORDS = 8

    def __init__(self) -> None:
        self._speaking = False
        self._opener_done = False
        self._opener_started_at = 0.0
        self._opener_words = 0

    def on_bot_started(self) -> None:
        self._speaking = True
        if not self._opener_done and self._opener_started_at == 0.0:
            self._opener_started_at = time.monotonic()

    def on_bot_text(self, text: str) -> None:
        if not self._opener_done:
            self._opener_words += len(text.split())

    def on_bot_stopped(self) -> None:
        self._speaking = False
        self._opener_done = True  # the opener is at most ONE utterance

    def current(self) -> str | None:
        if not self._speaking:
            return None
        if self._opener_done:
            return "body"
        if self._opener_words >= self.OPENER_MAX_WORDS:
            return "body"
        if time.monotonic() - self._opener_started_at >= self.OPENER_MAX_S:
            return "body"
        return "opener"


class EchoGateFilter(BaseAudioFilter):
    """Suppresses echo while allowing loud barge-in speech."""

    def __init__(
        self,
        tail_ms: int = 500,
        barge_in_rms: float = 2000.0,
        phase_provider: Callable[[], str | None] | None = None,
        barge_in_rms_window: float = 0.0,
        window_ms: int = 250,
        echo_cancel: bool = False,
        echo_ref_buffer_ms: int = 250,
        echo_corr_window_ms: int = 100,
        echo_corr_threshold: float = 0.35,
        echo_corr_max_lag_ms: int = 150,
        echo_corr_lag_stride_ms: int = 10,
    ):
        self._tail_s = tail_ms / 1000.0
        self._barge_in_rms = barge_in_rms
        self._bot_speaking = False
        self._bot_stopped_at: float = 0.0
        self._sample_rate = 24000
        self._gate_logged = False
        # 22-B32: while phase_provider() == "opener" the gate is FULLY closed
        # (no barge-in), but loud speech is buffered and released afterwards.
        self._phase_provider = phase_provider
        self._opener_buffer: list[bytes] = []
        self._opener_buffer_bytes = 0
        # 22.1-B38: rolling-window barge-in. Normal voice rarely spikes the
        # single-frame threshold but SUSTAINS moderate RMS — the window mean
        # catches it. 0.0 disables (legacy single-frame behavior).
        self._window_rms = barge_in_rms_window
        self._window_s = window_ms / 1000.0
        self._rms_history: list[tuple[float, float]] = []  # (monotonic_ts, rms)
        # Layer C: reference-based echo suppression. The ref ring holds recent
        # OUTPUT samples (pushed from the pipeline output side); the input ring
        # holds the most recent mic samples. Sizes are set in start() from the
        # real sample rate. Counts/`_echo_active` drive transition-only logging.
        self._echo_cancel = echo_cancel
        self._echo_ref_buffer_ms = echo_ref_buffer_ms
        self._echo_corr_window_ms = echo_corr_window_ms
        self._echo_corr_threshold = echo_corr_threshold
        self._echo_corr_max_lag_ms = echo_corr_max_lag_ms
        self._echo_corr_lag_stride_ms = echo_corr_lag_stride_ms
        self._ref_buf = np.zeros(0, dtype=np.float64)
        self._in_buf = np.zeros(0, dtype=np.float64)
        self._ref_cap = 0
        self._in_cap = 0
        self._corr_max_lag = 0
        self._corr_lag_stride = 1
        self._echo_active = False
        self._echo_suppressed_count = 0
        self._echo_passed_count = 0

    async def start(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        spms = sample_rate / 1000.0  # samples per ms
        self._ref_cap = int(self._echo_ref_buffer_ms * spms)
        self._in_cap = int(self._echo_corr_window_ms * spms)
        self._corr_max_lag = int(self._echo_corr_max_lag_ms * spms)
        self._corr_lag_stride = max(1, int(self._echo_corr_lag_stride_ms * spms))

    async def stop(self) -> None:
        pass

    async def process_frame(self, frame: FilterControlFrame) -> None:
        pass

    def push_reference(self, audio: bytes) -> None:
        """Feed an OUTPUT (played) frame into the echo reference ring.

        Called from the pipeline output side (EchoGateProcessor on
        OutputAudioRawFrame) because Pipecat's BaseAudioFilter has no
        output hook of its own.
        """
        if not self._echo_cancel or not audio:
            return
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return
        buf = np.concatenate((self._ref_buf, samples))
        if self._ref_cap and buf.size > self._ref_cap:
            buf = buf[-self._ref_cap :]
        self._ref_buf = buf

    def _push_input(self, samples_f: np.ndarray) -> None:
        buf = np.concatenate((self._in_buf, samples_f))
        if self._in_cap and buf.size > self._in_cap:
            buf = buf[-self._in_cap :]
        self._in_buf = buf

    def _is_echo(self) -> bool:
        """True if the buffered mic window correlates with recent output."""
        if not self._echo_cancel:
            return False
        win, ref = self._in_buf, self._ref_buf
        if self._in_cap == 0 or win.size < self._in_cap or ref.size < win.size:
            return False
        peak = normalized_xcorr_peak(win, ref, self._corr_max_lag, self._corr_lag_stride)
        is_echo = peak >= self._echo_corr_threshold
        if is_echo:
            self._echo_suppressed_count += 1
        else:
            self._echo_passed_count += 1
        # Log only on transitions (+ running counts) — never per-frame at INFO.
        if is_echo != self._echo_active:
            self._echo_active = is_echo
            log.debug(
                "echo_suppressed",
                echo_suppressed=is_echo,
                corr=round(peak, 3),
                suppressed=self._echo_suppressed_count,
                passed=self._echo_passed_count,
            )
        return is_echo

    def set_bot_speaking(self, speaking: bool) -> None:
        if speaking and not self._bot_speaking:
            self._bot_speaking = True
            self._gate_logged = False
            log.debug("echo_gate_on")
        elif not speaking and self._bot_speaking:
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()
            log.debug("echo_gate_tail_start", tail_ms=int(self._tail_s * 1000))

    def _is_gating(self) -> bool:
        if self._bot_speaking:
            return True
        if self._bot_stopped_at == 0.0:
            return False
        elapsed = time.monotonic() - self._bot_stopped_at
        if elapsed < self._tail_s:
            return True
        if not self._gate_logged:
            self._gate_logged = True
            log.debug("echo_gate_off", elapsed_ms=int(elapsed * 1000))
        return False

    def _rms(self, audio: bytes) -> float:
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return 0.0
        return math.sqrt(float(np.mean(samples * samples)))

    def _release_opener_buffer(self) -> bytes:
        if not self._opener_buffer:
            return b""
        released = b"".join(self._opener_buffer)
        self._opener_buffer.clear()
        self._opener_buffer_bytes = 0
        log.debug("opener_buffer_released", bytes=len(released))
        return released

    async def filter(self, audio: bytes) -> bytes:
        # 35.1: tap the raw mic frame for speaker ID. No-op unless resemblyzer is
        # installed (one cached bool check per frame), so this never affects timing.
        from core import speaker

        speaker.feed_audio(audio, self._sample_rate)
        phase = self._phase_provider() if self._phase_provider else None

        # Opener phase (22-B32): the gate is FULLY closed — Emma always
        # finishes her first sentence. Garcia's loud speech is BUFFERED, not
        # dropped, and dispatched right after the opener ends.
        if phase == "opener":
            rms = self._rms(audio)
            if (
                rms >= self._barge_in_rms
                and self._opener_buffer_bytes + len(audio) <= _OPENER_BUFFER_CAP_BYTES
            ):
                self._opener_buffer.append(audio)
                self._opener_buffer_bytes += len(audio)
            return b"\x00" * len(audio)

        released = self._release_opener_buffer()

        # Keep the correlation input window warm regardless of gate state, so
        # it's already full the moment Emma starts speaking (Layer C).
        if self._echo_cancel and audio:
            self._push_input(np.frombuffer(audio, dtype=np.int16).astype(np.float64))

        if not self._is_gating():
            self._rms_history.clear()  # gate open → no need to track
            return released + audio
        # Reference-based echo suppression (Layer C): if the mic frame
        # correlates with what Emma just played, it's her echo — drop it
        # BEFORE the RMS barge-in path can mistake it for real speech.
        if self._echo_cancel and self._is_echo():
            return released + b"\x00" * len(audio)
        rms = self._rms(audio)
        if rms == 0.0:
            return released + b"\x00" * len(audio)
        # Spike shortcut: a deliberate loud "¡Emma!" cuts her instantly.
        if rms >= self._barge_in_rms:
            log.debug("echo_gate_barge_in", rms=int(rms), signal="spike")
            return released + audio
        # Rolling window (22.1-B38): sustained normal-volume voice over
        # ~250 ms barges in even though no single frame spikes. Requires the
        # window to be at least 60% full so one moderate frame can't trigger.
        if self._window_rms > 0:
            now = time.monotonic()
            self._rms_history = [(t, r) for t, r in self._rms_history if now - t <= self._window_s]
            self._rms_history.append((now, rms))
            if self._rms_history and (now - self._rms_history[0][0]) >= self._window_s * 0.6:
                mean = sum(r for _, r in self._rms_history) / len(self._rms_history)
                if mean >= self._window_rms:
                    # No history clear: while the voice SUSTAINS, frames keep
                    # passing (clearing would stutter the barge-in on/off).
                    log.debug("echo_gate_barge_in", rms=int(mean), signal="window")
                    return released + audio
        return released + b"\x00" * len(audio)
