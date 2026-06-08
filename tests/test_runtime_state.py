"""Unit tests for the cross-thread runtime-state gate (HOTFIX Layer B).

`core/runtime_state.py` carries the `bot_speaking` flag and the wake-listener
suppression decision shared between the asyncio loop (Pipecat speech frames)
and the PortAudio callback thread (wake-word model).
"""

from __future__ import annotations

import time

from core import runtime_state


def setup_function() -> None:
    runtime_state.force_clear()


def test_mark_started_sets_flag() -> None:
    runtime_state.mark_started()
    assert runtime_state.bot_speaking.is_set()


def test_mark_stopped_zero_clears_immediately() -> None:
    runtime_state.mark_started()
    runtime_state.mark_stopped(0)
    assert not runtime_state.bot_speaking.is_set()


def test_mark_stopped_tail_keeps_set_then_clears() -> None:
    runtime_state.mark_started()
    runtime_state.mark_stopped(120)  # 120 ms tail
    # Still set immediately after — the tail covers speaker decay.
    assert runtime_state.bot_speaking.is_set()
    time.sleep(0.25)
    assert not runtime_state.bot_speaking.is_set()


def test_mark_started_cancels_pending_tail() -> None:
    runtime_state.mark_started()
    runtime_state.mark_stopped(80)
    runtime_state.mark_started()  # re-assert before the tail fires
    time.sleep(0.15)
    assert runtime_state.bot_speaking.is_set()  # tail was cancelled


def test_force_clear_cancels_tail_and_clears() -> None:
    runtime_state.mark_started()
    runtime_state.mark_stopped(500)
    runtime_state.force_clear()
    assert not runtime_state.bot_speaking.is_set()
    time.sleep(0.05)
    assert not runtime_state.bot_speaking.is_set()


def test_suppress_wake_within_warmup() -> None:
    open_t = 100.0
    # 1.2 s warmup; "now" only 0.3 s after open -> suppressed.
    assert runtime_state.suppress_wake(open_t, 1.2, now=open_t + 0.3) is True


def test_suppress_wake_after_warmup_when_silent() -> None:
    open_t = 100.0
    runtime_state.force_clear()
    # Past warmup and Emma silent -> not suppressed.
    assert runtime_state.suppress_wake(open_t, 1.2, now=open_t + 2.0) is False


def test_suppress_wake_after_warmup_when_bot_speaking() -> None:
    open_t = 100.0
    runtime_state.mark_started()
    # Past warmup but Emma speaking (defense-in-depth) -> suppressed.
    assert runtime_state.suppress_wake(open_t, 1.2, now=open_t + 2.0) is True


def test_wake_predict_skipped_while_bot_speaking_then_runs() -> None:
    """Integration at the wake-callback seam: while Emma speaks the model is
    NOT predicted against; once she stops, prediction resumes."""
    open_t, warmup_s = 100.0, 1.2
    frame_t = open_t + 5.0  # well past the warmup window
    predict_calls: list[bytes] = []

    def maybe_predict() -> None:
        # Mirrors the gate in wake_word._cb: skip predict when suppressed.
        if runtime_state.suppress_wake(open_t, warmup_s, now=frame_t):
            return
        predict_calls.append(b"frame")

    runtime_state.mark_started()
    maybe_predict()
    assert predict_calls == []  # bot speaking -> model never sees the echo

    runtime_state.mark_stopped(0)  # Emma stops; tail cleared immediately
    maybe_predict()
    assert predict_calls == [b"frame"]  # real listening resumes
