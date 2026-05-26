"""Main loop: wake word → Realtime session → idle close → repeat.

Phase 13 collapsed STT + LLM + TTS into a single ``core.realtime``
WebSocket session, so the orchestrator's job shrinks dramatically:

1. ``listen_for_wake_word()`` blocks until "Hey Mycroft".
2. We open a Realtime session, prefix the system prompt with whatever
   long-term memory has on Garcia, and run three coroutines in
   parallel until idle:
   - mic-to-session (24 kHz mono int16 → ``input_audio_buffer.append``)
   - session-to-speakers (``response.audio.delta`` → output stream)
   - event loop (function calls, transcripts, native barge-in)
3. Idle (no user/assistant activity for ``IDLE_TIMEOUT_S``) → close
   the session and loop back to wake-word listening.

Phase 03 memory wiring lives here: the user-transcript handler
appends to the short-term log, the assistant-transcript handler does
too and fires the background reflection task. ``runtime.set_spoken_lang``
is updated from a cheap es/en heuristic on the user transcript so
``tools/preferences.py`` and ``tools/dev.py`` can keep speaking
bilingual progress.
"""
from __future__ import annotations

import asyncio
import signal
import time
import uuid
from pathlib import Path

import structlog

from config.settings import settings
from core import dev_state, realtime, runtime
from core.wake_word import listen_for_wake_word
from memory import long_term as memory_lt
from memory import reflection as memory_reflection
from memory import short_term as memory_st

log = structlog.get_logger("emma.orchestrator")

_shutdown = asyncio.Event()
_simulate_crash = False

# Exposed to the crash handler.
_last_turn_id: str = ""
_last_transcript: str = ""
_last_response: str = ""


def enable_simulate_crash() -> None:
    """Called by emma/__main__.py when --simulate-crash is passed."""
    global _simulate_crash
    _simulate_crash = True


def last_context() -> dict[str, str]:
    """Snapshot used by the crash handler."""
    return {
        "turn_id": _last_turn_id,
        "last_transcript": _last_transcript,
        "response_text": _last_response,
    }


def preflight() -> None:
    """Wake-word validation runs lazily in :func:`core.wake_word._get_model`
    so it accepts either a custom .onnx path or a built-in model name."""
    return


def _install_signals() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except NotImplementedError:
            pass


def _check_path() -> None:
    """Early validation that the wake-word path looks plausible."""
    wake_word_path = Path(settings.WAKE_WORD_PATH).expanduser()
    if not wake_word_path.exists() and not any(
        c in settings.WAKE_WORD_PATH for c in {"alexa", "hey_mycroft", "hey_jarvis"}
    ):
        # Lazy-load in core.wake_word will surface this; we just log up front.
        log.warning(
            "wake_word_path_missing",
            path=str(wake_word_path),
            hint="train .onnx via README's Colab or use a built-in name",
        )


async def _one_session() -> None:
    """Wake → Realtime session → idle close. One iteration of the run loop."""
    global _last_turn_id, _last_transcript, _last_response

    turn_id = uuid.uuid4().hex[:8]
    _last_turn_id = turn_id
    _last_transcript = ""
    _last_response = ""
    structlog.contextvars.bind_contextvars(turn_id=turn_id)

    try:
        t_start = time.monotonic()
        await listen_for_wake_word()
        t_wake = time.monotonic()
        log.info("wake", elapsed_ms=int((t_wake - t_start) * 1000))

        if _simulate_crash:
            raise RuntimeError("test crash (--simulate-crash)")

        # Phase 03 memory priming - top-N highest-confidence facts get
        # injected into the Realtime session's persistent instructions.
        try:
            priming = await memory_lt.priming_block()
        except Exception as exc:
            log.warning("memory_priming_failed", error=str(exc))
            priming = ""

        session = await realtime.connect(memory_priming=priming)
        t_connected = time.monotonic()
        log.info("realtime_connected_ms", elapsed_ms=int((t_connected - t_wake) * 1000))

        first_audio_logged = False
        last_user: list[str] = []
        last_assistant: list[str] = []

        async def on_user(text: str) -> None:
            _set_last_transcript(text)
            last_user.append(text)
            # Update runtime spoken_lang so preferences / dev tools keep
            # speaking bilingually.
            lang = realtime.detect_es_en(text)
            runtime.set_spoken_lang(lang)  # type: ignore[arg-type]

        async def on_assistant(text: str) -> None:
            _set_last_response(text)
            last_assistant.append(text)
            # Memory: append turn + fire-and-forget reflection.
            if last_user:
                user_text = " ".join(last_user)
                memory_st.append_turn(user_text, text)
                try:
                    memory_reflection.schedule_reflection(memory_st.last_turns(4))
                except Exception as exc:
                    log.warning("reflection_schedule_failed", error=str(exc))
                # Reset the per-turn accumulator so the next assistant
                # transcript pairs with the next user transcript only.
                last_user.clear()

        # Wrap the audio-out task so we can log time-to-first-byte.
        async def play_with_first_audio_marker() -> None:
            nonlocal first_audio_logged
            # Peek the audio queue: the first non-empty chunk triggers the log.
            original_get = session.audio_out_queue.get

            async def get_and_mark() -> bytes:
                chunk = await original_get()
                nonlocal first_audio_logged
                if chunk and not first_audio_logged:
                    first_audio_logged = True
                    elapsed_ms = int((time.monotonic() - t_wake) * 1000)
                    log.info("wake_to_first_audio_ms", elapsed_ms=elapsed_ms)
                return chunk

            session.audio_out_queue.get = get_and_mark  # type: ignore[assignment]
            await realtime.play_session_audio(session)

        mic_task = asyncio.create_task(realtime.mic_to_session(session))
        play_task = asyncio.create_task(play_with_first_audio_marker())
        loop_task = asyncio.create_task(
            realtime.run_event_loop(
                session,
                on_user_transcript=on_user,
                on_assistant_transcript=on_assistant,
            )
        )
        idle_task = asyncio.create_task(_idle_watcher(session))

        done, pending = await asyncio.wait(
            {mic_task, play_task, loop_task, idle_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await session.close()

        session_ms = int((time.monotonic() - t_wake) * 1000)
        log.info(
            "realtime_session_close",
            duration_ms=session_ms,
            user_utterances=len(last_assistant),
        )
    finally:
        structlog.contextvars.unbind_contextvars("turn_id")


async def _idle_watcher(session: realtime.RealtimeSession) -> None:
    """Close the session when no activity has been seen for IDLE_TIMEOUT_S."""
    timeout = float(settings.IDLE_TIMEOUT_S)
    while not session.closed:
        await asyncio.sleep(min(2.0, timeout / 2))
        if time.monotonic() - session.last_activity > timeout:
            log.info("idle_close", since_activity_s=int(time.monotonic() - session.last_activity))
            await session.close()
            return


def _set_last_transcript(text: str) -> None:
    global _last_transcript
    _last_transcript = text


def _set_last_response(text: str) -> None:
    global _last_response
    _last_response = text


async def run() -> None:
    _install_signals()
    _check_path()
    log.info("Listening for wake word")
    while not _shutdown.is_set():
        try:
            await _one_session()
        except asyncio.CancelledError:
            break
        except RuntimeError as exc:
            if "test crash" in str(exc):
                raise  # bubble to crash handler
            log.error("session_failed", error=str(exc))
            await asyncio.sleep(0.5)
        except Exception as exc:
            log.error("session_failed", error=str(exc))
            await asyncio.sleep(0.5)
        if dev_state.shutdown_requested.is_set():
            log.info("dev_mode_exit")
            break

    # Best-effort browser cleanup on shutdown.
    try:
        from tools.browser import shutdown_browser

        await shutdown_browser()
    except Exception:
        pass
    log.info("shutdown_complete")
