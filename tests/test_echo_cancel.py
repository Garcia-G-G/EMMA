"""Unit tests for reference-based echo suppression (HOTFIX Layer C).

The echo gate keeps a ring of recently-played OUTPUT samples and, while Emma
speaks, cross-correlates incoming mic audio against it. High coherence ⇒ the
"speech" is Emma's own echo ⇒ drop it before it reaches the RMS barge-in path.
"""

from __future__ import annotations

import numpy as np

from core.echo_gate import EchoGateFilter, normalized_xcorr_peak

_SR = 24000


def _sig(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(-8000, 8000, size=n).astype(np.float64)


def test_xcorr_identical_is_high() -> None:
    ref = _sig(6000, 1)
    win = ref[-2400:].copy()  # exact copy at lag 0
    assert normalized_xcorr_peak(win, ref, max_lag=3600, lag_stride=240) > 0.95


def test_xcorr_unrelated_is_low() -> None:
    ref = _sig(6000, 1)
    win = _sig(2400, 999)  # independent noise
    assert normalized_xcorr_peak(win, ref, max_lag=3600, lag_stride=240) < 0.3


def test_xcorr_detects_lagged_copy() -> None:
    ref = _sig(6000, 2)
    lag = 1920  # 80 ms at 24 kHz
    win = ref[-2400 - lag : -lag].copy()  # copy delayed by 80 ms
    peak = normalized_xcorr_peak(win, ref, max_lag=3600, lag_stride=240)
    assert peak > 0.95


def test_filter_suppresses_echo_while_speaking() -> None:
    import asyncio

    g = EchoGateFilter(echo_cancel=True, echo_corr_threshold=0.35)
    asyncio.run(g.start(_SR))
    g.set_bot_speaking(True)
    echo = _sig(2400, 3).astype(np.int16)
    g.push_reference(echo.tobytes())  # what Emma just played
    out = asyncio.run(g.filter(echo.tobytes()))  # the mic hears the same thing
    assert out == b"\x00" * len(echo.tobytes())  # suppressed as echo


def test_filter_passes_unrelated_speech_while_speaking() -> None:
    import asyncio

    g = EchoGateFilter(echo_cancel=True, echo_corr_threshold=0.35, barge_in_rms=2000.0)
    asyncio.run(g.start(_SR))
    g.set_bot_speaking(True)
    played = _sig(2400, 4).astype(np.int16)
    g.push_reference(played.tobytes())
    # Loud, unrelated voice (different seed) — not echo, should pass the corr gate.
    voice = (_sig(2400, 77) * 3).astype(np.int16)
    out = asyncio.run(g.filter(voice.tobytes()))
    assert out != b"\x00" * len(voice.tobytes())  # not suppressed as echo
