"""Voice-mode driver for the acceptance runner (19.7-VAH3).

Per scenario: synthesize (cached) → spawn a fresh Emma subprocess in test
mode → wait for ``waiting_for_wake`` → play the wake-prefixed WAV at her →
tail the subprocess's structlog JSON until the turn completes → collect
wake latency, STT transcript, tool calls (with args via the test-mode
``tool_args_test`` hook), capability gaps, and spoken text.

Subprocess isolation is deliberate: Emma owns her own event loop + audio
threads; the harness only reads her JSON log stream. ``follows`` scenarios
reuse the SAME subprocess/session (the confirmation flow needs the pending
state) — everything else gets a fresh daemon.

Event-name reality vs the 19.7 spec (documented deviations):
- wake event is ``wake_detected`` (not ``wake_word_detected``).
- gaps surface as ``capability_gap_recorded``.
- STT/bot text come from the test taps (``stt_user_test`` /
  ``bot_text_test``) — the production TranscriptCollector never receives
  those frames (the known reflection gap, root-caused during 19.7).
- turn-end = quiet period after at least one ``bot_text_test`` (a turn can
  hold several bot utterances: preamble → tool → answer).
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.acceptance.audio_gen import synthesize, wake_clip
from tests.acceptance.audio_play import PlaybackError, play

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_WAKE_WAIT_S = 10.0  # spec: max wait for waiting_for_wake
_WAKE_DETECT_S = 14.0  # playback start → wake_detected deadline
_SESSION_SETTLE_S = 1.5  # conversation_start → mic actually streaming
_TURN_SILENCE_S = 20.0  # no interesting event for this long → turn over
_SCENARIO_CAP_S = 90.0  # hard ceiling per scenario chain segment
_DRAIN_S = 1.5  # grace after turn-end to catch trailing tool logs

_INTERESTING = {
    "wake_detected",
    "tool_started",
    "tool_args_test",
    "tool_completed",
    "tool_failed",
    "tool_timed_out",
    "capability_gap_recorded",
    "transcript_corrected",
    "stt_user_test",  # user STT, from the test tap between input and LLM
    "bot_text_test",  # assistant text, flushed per bot utterance
    "conversation_start",
    "session_close",
    "conversation_end",
    "echo_gate_on",  # bot started speaking — holds the quiet-window open
    "echo_gate_off",  # bot (and its 600ms tail) finished
}

_AFTER_BOT_SILENCE_S = 8.0  # bot already answered + this much quiet → turn over


@dataclass
class VoiceExtras:
    """Everything voice mode knows beyond the text-mode TurnResult."""

    audio_path: str = ""
    wake_detected: bool = False
    wake_latency_ms: int = -1
    transcript: str = ""
    capability_gaps: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    error: str = ""
    chars_synthesized: int = 0  # 0 when served from cache


class EmmaProcess:
    """A test-mode Emma daemon whose JSON log stream we consume."""

    def __init__(self, input_device: str) -> None:
        self._input_device = input_device
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.events: list[dict[str, Any]] = []  # everything interesting, archived

    def start(self) -> None:
        env = {
            **os.environ,
            "EMMA_TEST_MODE": "true",
            "EMMA_TEST_INPUT_DEVICE": self._input_device,
            # Keep the run hermetic: no proactive speech or dashboard noise.
            "PROACTIVE_ENABLED": "false",
            "EMMA_DASHBOARD": "0",
        }
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "emma", "--debug", "--test"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump, name="emma-log-pump", daemon=True).start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue  # pipecat/loguru/non-JSON noise
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload["_t"] = time.monotonic()  # arrival stamp for latencies
            self._queue.put(payload)

    def wait_for(self, event_name: str, timeout: float) -> dict[str, Any] | None:
        """Consume events (archiving all) until ``event_name`` or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ev = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            self._archive(ev)
            if ev.get("event") == event_name:
                return ev

    def next_event(self, timeout: float) -> dict[str, Any] | None:
        try:
            ev = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        self._archive(ev)
        return ev

    def _archive(self, ev: dict[str, Any]) -> None:
        if ev.get("event") in _INTERESTING:
            self.events.append(ev)

    def stop(self) -> int | None:
        if self._proc is None:
            return None
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        return self._proc.returncode


def _collect_turn(proc: EmmaProcess, t_play: float) -> tuple[list[dict[str, Any]], str]:
    """Tail events until the turn ends. Returns (events_this_turn, end_reason)."""
    start_idx = len(proc.events)
    deadline = time.monotonic() + _SCENARIO_CAP_S
    last_interesting = time.monotonic()
    bot_answered = False
    bot_speaking = False
    end_reason = "cap"
    while time.monotonic() < deadline:
        ev = proc.next_event(timeout=1.0)
        now = time.monotonic()
        if ev is None:
            # While Emma is mid-utterance no events arrive at all — a long
            # answer must NOT trip the quiet windows (real bug: V56's reply
            # was cut after the preamble). The echo gate holds them open.
            if bot_speaking:
                continue
            # A turn can hold several bot utterances (preamble → tool →
            # answer), so "first bot text" isn't the end — quiet after one is.
            if bot_answered and now - last_interesting > _AFTER_BOT_SILENCE_S:
                end_reason = "turn_complete"
                break
            if now - last_interesting > _TURN_SILENCE_S:
                end_reason = "silence"
                break
            continue
        name = ev.get("event", "")
        if name in _INTERESTING:
            last_interesting = now
        if name == "echo_gate_on":
            bot_speaking = True
        elif name == "echo_gate_off":
            bot_speaking = False
        elif name == "bot_text_test":
            bot_answered = True
        elif name in ("session_close", "conversation_end"):
            end_reason = "session_close"
            break
    return proc.events[start_idx:], end_reason


def _merge_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tool_started/args/completed/failed streams → one record per call."""
    args_by_name: dict[str, dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    for ev in events:
        name = ev.get("event")
        tool = str(ev.get("name", ""))
        if name == "tool_args_test":
            try:
                args_by_name[tool] = json.loads(str(ev.get("args", "{}")))
            except json.JSONDecodeError:
                args_by_name[tool] = {}
        elif name == "tool_completed":
            calls.append(
                {
                    "name": tool,
                    "args": args_by_name.get(tool, {}),
                    "success": bool(ev.get("success", False)),
                    "elapsed_ms": int(ev.get("elapsed_ms", 0)),
                }
            )
        elif name in ("tool_failed", "tool_timed_out"):
            calls.append(
                {
                    "name": tool,
                    "args": args_by_name.get(tool, {}),
                    "success": False,
                    "elapsed_ms": int(ev.get("elapsed_ms", 0)),
                }
            )
    return calls


def _extract(events: list[dict[str, Any]], t_play: float) -> tuple[dict[str, Any], VoiceExtras]:
    """(turn_summary, extras) from one turn's archived events."""
    extras = VoiceExtras()
    user_parts: list[str] = []
    assistant_parts: list[str] = []
    for ev in events:
        name = ev.get("event")
        if name == "wake_detected":
            extras.wake_detected = True
            extras.wake_latency_ms = int((float(ev["_t"]) - t_play) * 1000)
        elif name == "stt_user_test":
            user_parts.append(str(ev.get("text", "")))
        elif name == "bot_text_test":
            assistant_parts.append(str(ev.get("text", "")))
        elif name == "capability_gap_recorded":
            extras.capability_gaps.append(
                {k: v for k, v in ev.items() if k not in ("_t", "timestamp", "level")}
            )
    extras.transcript = " ".join(p for p in user_parts if p).strip()
    stamps = [float(ev["_t"]) for ev in events if "_t" in ev]
    summary = {
        "tool_calls": _merge_tool_calls(events),
        "spoken_text": " ".join(p for p in assistant_parts if p).strip(),
        "turn_ms": int((max(stamps) - t_play) * 1000) if stamps else 0,
    }
    return summary, extras


def check_voice_extras(scenario: dict[str, Any], extras: VoiceExtras) -> list[str]:
    """The two voice-only assertions (19.7-VAH3.3); both opt-in per scenario."""
    issues: list[str] = []
    pattern = scenario.get("expected_transcript_pattern")
    if pattern and not re.search(pattern, extras.transcript, re.IGNORECASE):
        issues.append(f"STT transcript {extras.transcript!r} did not match /{pattern}/i")
    if scenario.get("expected_no_capability_gaps") and any(
        not g.get("success", True) for g in extras.capability_gaps
    ):
        issues.append(f"capability gaps recorded: {extras.capability_gaps}")
    return issues


def run_voice_scenario(
    scenario: dict[str, Any],
    *,
    input_device: str,
    output_device: str,
    proc: EmmaProcess | None = None,
) -> tuple[dict[str, Any], VoiceExtras, EmmaProcess]:
    """Drive one scenario. Pass ``proc`` to continue a ``follows`` chain in
    the same session (no wake clip on continuations).

    Cadence mirrors a human: play the EN wake clip → wait for the session to
    actually open (``conversation_start``) + a short settle → play the bare
    utterance. A concatenated single clip loses its tail to the chime +
    Realtime connect (~1s) — measured, not theoretical.
    """
    extras = VoiceExtras()
    continuing = proc is not None
    text = scenario["utterance"]
    lang = scenario.get("language", "es")

    from tests.acceptance.audio_gen import cache_path

    if not cache_path(text, lang, scenario.get("voice_id")).exists():
        extras.chars_synthesized = len(text)
    wav = synthesize(text, lang, scenario.get("voice_id"), scenario_id=scenario["id"])
    extras.audio_path = str(wav)

    t_play = time.monotonic()
    if proc is None:
        proc = EmmaProcess(input_device)
        proc.start()
        if proc.wait_for("waiting_for_wake", _WAKE_WAIT_S) is None:
            extras.error = f"daemon never reached waiting_for_wake in {_WAKE_WAIT_S:.0f}s"
            extras.exit_code = proc.stop()
            return {"tool_calls": [], "spoken_text": ""}, extras, proc
        try:
            t_play = time.monotonic()
            play(wake_clip(), output_device)
        except PlaybackError as exc:
            extras.error = str(exc)
            extras.exit_code = proc.stop()
            return {"tool_calls": [], "spoken_text": ""}, extras, proc
        if proc.wait_for("conversation_start", _WAKE_DETECT_S) is None:
            extras.error = "wake word was not detected from the played audio"
            extras.exit_code = proc.stop()
            return {"tool_calls": [], "spoken_text": ""}, extras, proc
        time.sleep(_SESSION_SETTLE_S)  # let the mic actually start streaming

    try:
        t_utt = time.monotonic()
        duration_s = play(wav, output_device)
    except PlaybackError as exc:
        extras.error = str(exc)
        extras.exit_code = proc.stop()
        return {"tool_calls": [], "spoken_text": ""}, extras, proc

    # Turn time counts from the END of the utterance audio — what a human
    # perceives as "Emma's response time", not the harness's playback time.
    t_turn_base = t_utt + duration_s
    events, _end_reason = _collect_turn(proc, t_turn_base)
    summary, extracted = _extract(events, t_turn_base)
    extracted.audio_path = extras.audio_path
    extracted.chars_synthesized = extras.chars_synthesized
    if continuing:
        # continuation turns ride an already-woken session
        extracted.wake_detected = True
        extracted.wake_latency_ms = 0
    else:
        # the wake event was consumed while waiting for conversation_start
        wake_ev = next((e for e in proc.events if e.get("event") == "wake_detected"), None)
        if wake_ev is not None:
            extracted.wake_detected = True
            extracted.wake_latency_ms = int((float(wake_ev["_t"]) - t_play) * 1000)
    return summary, extracted, proc
