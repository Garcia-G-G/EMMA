"""Orchestrator: wake word → Pipecat session → repeat.

Pipecat owns the streaming pipeline (mic → OpenAIRealtimeLLMService →
speaker, server-side VAD, native barge-in, function-call dispatch),
so the orchestrator's job collapses to wake → run_session → loop.

Memory wiring (short_term/reflection) is deferred to Prompt 14.
"""
from __future__ import annotations

import asyncio
import signal
import time
import uuid

import structlog

from config.settings import settings
from core import conversation, dev_state
from core.wake_word import listen_for_wake_word

log = structlog.get_logger("emma.orchestrator")

_shutdown = asyncio.Event()
_simulate_crash = False
_last_turn_id: str = ""


def enable_simulate_crash() -> None:
    """Called by emma/__main__.py when --simulate-crash is passed."""
    global _simulate_crash
    _simulate_crash = True


def last_context() -> dict[str, str]:
    """Snapshot used by the crash handler. Transcripts wire in Prompt 14."""
    return {"turn_id": _last_turn_id, "last_transcript": "", "response_text": ""}


def preflight() -> None:
    """No-op shim. Wake-word validation runs lazily in core.wake_word."""
    return


def _install_signals() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except NotImplementedError:
            pass


async def _one_session() -> None:
    """Wake → Pipecat session → return. One iteration of the main loop."""
    global _last_turn_id
    turn_id = uuid.uuid4().hex[:8]
    _last_turn_id = turn_id
    structlog.contextvars.bind_contextvars(turn_id=turn_id)
    try:
        t_start = time.monotonic()
        await listen_for_wake_word()
        t_wake = time.monotonic()
        log.info("wake_detected", elapsed_ms=int((t_wake - t_start) * 1000))
        # Let the ack tone fully decay before Pipecat opens the mic.
        # Without this, the beep leaks into the Realtime session's VAD
        # and fires spurious speech_started events (self-interruption loop).
        await asyncio.sleep(0.4)
        if _simulate_crash:
            raise RuntimeError("test crash (--simulate-crash)")
        try:
            await asyncio.wait_for(
                conversation.run_session(),
                timeout=float(settings.SESSION_MAX_S) + 30.0,
            )
        except asyncio.TimeoutError:
            log.info("session_timeout")
        log.info("session_close", duration_s=int(time.monotonic() - t_wake))
    finally:
        structlog.contextvars.unbind_contextvars("turn_id")


async def main_loop() -> None:
    """Entry point called by ``emma.__main__``."""
    _install_signals()
    log.info("waiting_for_wake")
    while not _shutdown.is_set():
        try:
            await _one_session()
        except asyncio.CancelledError:
            break
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
    try:
        from tools.browser import shutdown_browser

        await shutdown_browser()
    except Exception:
        pass
    log.info("shutdown_complete")


run = main_loop  # backward-compat: emma/__main__.py historically used run()
