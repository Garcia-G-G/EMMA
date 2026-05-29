"""Orchestrator: wake word → Pipecat session → repeat.

Pipecat owns the streaming pipeline (mic → OpenAIRealtimeLLMService →
speaker, server-side VAD, native barge-in, function-call dispatch),
so the orchestrator's job collapses to wake → run_session → loop.

Memory priming is wired into the session system prompt (reads from
long-term store on each session start). Reflection (automatic fact
extraction from transcripts) requires Pipecat transcript event
hooks — not yet implemented.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import structlog

from config.settings import settings
from core import conversation, dev_state, events_bus
from core.wake_word import listen_for_wake_word

log = structlog.get_logger("emma.orchestrator")

_shutdown = asyncio.Event()
_simulate_crash = False
_last_turn_id: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def enable_simulate_crash() -> None:
    """Called by emma/__main__.py when --simulate-crash is passed."""
    global _simulate_crash
    _simulate_crash = True


def request_shutdown() -> None:
    """Ask the main loop to stop after the current session.

    Called by emma.__main__'s SIGINT/SIGTERM handler IN ADDITION to cancelling
    the orchestrator task. Cancellation alone is not enough: while a Pipecat
    session is active, ``runner.run`` swallows the CancelledError and returns
    normally (the same path as an idle timeout), so the loop would otherwise
    spin back to wake-word listening. This flag makes it exit instead.
    """
    _shutdown.set()


def last_context() -> dict[str, str]:
    """Snapshot used by the crash handler. Transcripts wire in Prompt 14."""
    return {"turn_id": _last_turn_id, "last_transcript": "", "response_text": ""}


def preflight() -> None:
    """No-op shim. Wake-word validation runs lazily in core.wake_word."""
    return


async def _one_session() -> None:
    """Wake → Pipecat session → return. One iteration of the main loop."""
    global _last_turn_id
    turn_id = uuid.uuid4().hex[:8]
    _last_turn_id = turn_id
    structlog.contextvars.bind_contextvars(turn_id=turn_id)
    try:
        events_bus.publish("state", state="waiting_for_wake")
        t_start = time.monotonic()
        await listen_for_wake_word()
        t_wake = time.monotonic()
        log.info("wake_detected", elapsed_ms=int((t_wake - t_start) * 1000))
        events_bus.publish("wake_detected")
        events_bus.publish("session_started", id=turn_id, ts=_now_iso())
        events_bus.publish("state", state="listening")
        # Let the ack tone fully decay before Pipecat opens the mic.
        # Without this, the beep leaks into the Realtime session's VAD
        # and fires spurious speech_started events (self-interruption loop).
        await asyncio.sleep(0.8)
        if _simulate_crash:
            raise RuntimeError("test crash (--simulate-crash)")
        try:
            await asyncio.wait_for(
                conversation.run_session(),
                timeout=float(settings.SESSION_MAX_S) + 30.0,
            )
        except TimeoutError:
            log.info("session_timeout")
        duration_s = round(time.monotonic() - t_wake, 1)
        log.info("session_close", duration_s=int(duration_s))
        events_bus.publish("session_ended", id=turn_id, duration_s=duration_s)
        events_bus.publish("state", state="waiting_for_wake")
    finally:
        structlog.contextvars.unbind_contextvars("turn_id")


async def main_loop() -> None:
    """Entry point called by ``emma.__main__``.

    Signal handling lives in ``emma.__main__``, which cancels this task on
    SIGINT/SIGTERM. We must let ``CancelledError`` propagate (cooperative
    shutdown) rather than swallow it, and run cleanup in ``finally``. A
    ``SystemExit`` (terminal auth error raised by ``run_session``) likewise
    propagates so ``__main__`` can exit non-zero. Only ordinary
    ``RuntimeError``/``Exception`` per-session faults are caught and retried.
    """
    log.info("waiting_for_wake")
    try:
        while not _shutdown.is_set():
            try:
                await _one_session()
            except RuntimeError as exc:
                if "test crash" in str(exc):
                    raise
                log.error("session_error", error=str(exc))
                await asyncio.sleep(0.5)
            except Exception as exc:
                log.error("session_error", error=str(exc))
                await asyncio.sleep(0.5)
            if dev_state.shutdown_requested.is_set():
                log.info("dev_mode_exit")
                break
    finally:
        try:
            from tools.browser import shutdown_browser

            await shutdown_browser()
        except Exception:
            pass
        log.info("shutdown_complete")


run = main_loop  # backward-compat: emma/__main__.py historically used run()
