"""B49.2 — gap-filling unit tests for core.echo_gate.

NOTE: echo_gate is already heavily unit-tested elsewhere —
test_speech_phase.py (phase machine + opener buffer/drain) and
test_operational_hardening.py::TestRollingBargeIn (window/spike/quiet). To avoid
duplicating harness coverage (a 24.1 anti-pattern) this file adds ONLY the
behaviors those suites miss: window dip-tolerance, the just-below-threshold
boundary, the exact 8-word graduation, and the bare _rms() helper math.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from core.echo_gate import EchoGateFilter, SpeechPhase


@pytest.fixture(autouse=True)
def _restore_current_event_loop():
    """`_run` uses asyncio.run(), which closes its loop and clears the current
    one. Some legacy suites (e.g. test_registry) still call the deprecated
    asyncio.get_event_loop(); leave a live loop so test ORDER can't break them.
    The real fix belongs in those legacy suites — flagged in the 24.1 report."""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _run(coro):
    return asyncio.run(coro)


def _chunk(level: int, samples: int = 480) -> bytes:
    return (np.ones(samples, dtype=np.int16) * level).tobytes()


def _gate():
    return EchoGateFilter(
        tail_ms=600, barge_in_rms=18_000.0, barge_in_rms_window=6_000.0, window_ms=250
    )


def test_rolling_window_tolerates_a_brief_dip(monkeypatch) -> None:
    """Sustained loud-enough voice with ONE quiet dip frame still barges in —
    a momentary low frame must not reset the window decision. (TestRollingBargeIn
    only covers a perfectly constant level.)"""
    import core.echo_gate as eg

    g = _gate()
    g.set_bot_speaking(True)
    clock = {"t": 10.0}
    monkeypatch.setattr(eg.time, "monotonic", lambda: clock["t"])
    levels = [8000] * 5 + [300] + [8000] * 9  # one dip mid-stream
    out = b""
    for lvl in levels:
        clock["t"] += 0.02
        out = _run(g.filter(_chunk(lvl)))
    assert out == _chunk(8000)  # window mean stays above 6000 despite the dip


def test_sustained_just_below_threshold_stays_silenced(monkeypatch) -> None:
    """REGRESSION (self-interruption loop, 15.x): echo that is loud-ish but
    still under the deliberate-speech threshold must be silenced, or the
    server VAD mistakes it for the user and truncates Emma. 5900 < 6000 mean
    and < 18000 spike → gated to zeros."""
    import core.echo_gate as eg

    g = _gate()
    g.set_bot_speaking(True)
    clock = {"t": 10.0}
    monkeypatch.setattr(eg.time, "monotonic", lambda: clock["t"])
    out = b""
    for _ in range(20):
        clock["t"] += 0.02
        out = _run(g.filter(_chunk(5900)))
    assert out == b"\x00" * len(_chunk(5900))


def test_opener_graduates_at_exactly_eight_words() -> None:
    """OPENER_MAX_WORDS is an inclusive bound: exactly 8 words → body.
    (test_speech_phase covers 9 words; the boundary itself was untested.)"""
    p = SpeechPhase()
    p.on_bot_started()
    p.on_bot_text("uno dos tres cuatro cinco seis siete ocho")  # exactly 8
    assert p.current() == "body"


def test_rms_of_constant_signal_equals_amplitude() -> None:
    """The rolling-window math (B38) leans on _rms == amplitude for a constant
    signal. Exercise the helper directly so its contract is pinned."""
    g = _gate()
    assert g._rms(_chunk(8000)) == 8000.0
    assert g._rms(b"") == 0.0
