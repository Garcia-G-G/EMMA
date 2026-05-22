"""Main loop: wake -> capture -> STT -> LLM (with tools) -> TTS -> playback.

Adds in phase 04:
- ``turn_id`` UUID bound to structlog contextvars for every turn.
- Polls ``dev_state.shutdown_requested`` between turns and exits cleanly.
- ``_simulate_crash`` flag (set by ``--simulate-crash``) raises after the
  first wake word so the crash handler runs through its real code path.
- Falls back to ``tts.say_fallback`` when ElevenLabs produces no audio.
- Exposes ``last_context()`` so the crash handler can include the
  in-flight transcript and response text in its report.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import signal
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import structlog

from config.settings import settings
from core import dev_state, runtime
from core.audio import capture_until_silence, listen_for_speech, play_audio_stream
from core.llm import Message, PendingConfirmation, converse
from core.stt import transcribe
from core.tts import say_fallback, speak
from core.wake_word import listen_for_wake_word
from tools.registry import dispatch, get_tool

log = structlog.get_logger("emma.orchestrator")

HISTORY_TURNS = 10
_shutdown = asyncio.Event()
_simulate_crash = False

# Surfaces in-flight context to the crash handler.
_last_turn_id: str = ""
_last_transcript: str = ""
_last_response: str = ""

_YES_WORDS = {
    "sí", "si", "yes", "yeah", "yep", "yup", "claro", "ok", "okay",
    "dale", "adelante", "sure", "confirmo", "correcto",
}
_NO_WORDS = {"no", "nope", "cancela", "cancelar", "cancel", "stop", "alto"}
_YN_RE = re.compile(r"\b(" + "|".join(_YES_WORDS | _NO_WORDS) + r")\b", re.IGNORECASE)

_OFFLINE_MSG = {
    "es": "Estoy sin conexión a internet, no puedo pensar ni hablar bien ahorita.",
    "en": "I'm offline right now, I can't think or speak properly.",
}


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
    """Wake-word validation is now done lazily in core.wake_word._get_model()
    so it can accept either a custom .onnx path or a built-in model name."""
    return


def _install_signals() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except NotImplementedError:
            pass


def _classify_yes_no(text: str) -> bool | None:
    m = _YN_RE.search(text.lower())
    if not m:
        return None
    return m.group(1).lower() in _YES_WORDS


async def _await_yes_no() -> bool | None:
    audio_pcm = await capture_until_silence()
    if not audio_pcm:
        return None
    transcript = await transcribe(audio_pcm)
    if not transcript.text.strip():
        return None
    return _classify_yes_no(transcript.text)


async def _speak_text(text: str, spoken_lang: Literal["es", "en"]) -> bool:
    """Speak `text` via ElevenLabs; returns True if any audio was produced."""
    spoke = False

    async def _one() -> AsyncIterator[str]:
        yield text

    async def _track() -> AsyncIterator[bytes]:
        nonlocal spoke
        async for chunk in speak(_one(), spoken_lang):
            spoke = True
            yield chunk

    await play_audio_stream(_track())
    return spoke


async def _say_or_speak(text: str, spoken_lang: Literal["es", "en"]) -> None:
    """Speak via ElevenLabs, falling back to `say` if no audio bytes flowed."""
    if not text.strip():
        return
    spoke = await _speak_text(text, spoken_lang)
    if not spoke:
        log.warning("tts_fallback_to_say", chars=len(text))
        say_fallback(text, spoken_lang)


def _tool_accepts_cancelled(tool_name: str) -> bool:
    """True when the tool's signature opts into cancellation handling."""
    entry = get_tool(tool_name)
    if entry is None:
        return False
    try:
        sig = inspect.signature(entry.fn)
    except (TypeError, ValueError):
        return False
    return "cancelled" in sig.parameters


async def _handle_confirmation(
    pending: PendingConfirmation,
    spoken_lang: Literal["es", "en"],
    history: list[Message],
) -> None:
    answer = await _await_yes_no()

    if answer is None:
        msg = "No te entendí, cancelo." if spoken_lang == "es" else "Didn't catch that, cancelling."
        await _say_or_speak(msg, spoken_lang)
        history.append(Message(role="assistant", content=msg))
        log.info("confirmation_skipped_no_match", tool=pending.tool_name)
        if _tool_accepts_cancelled(pending.tool_name):
            result = await dispatch(pending.tool_name, {**pending.args, "cancelled": True})
            if result.user_message and result.user_message != msg:
                await _say_or_speak(result.user_message, spoken_lang)
                history.append(Message(role="assistant", content=result.user_message))
        else:
            log.info("confirmation_tool_no_cancel_handler", tool=pending.tool_name)
        return

    if not answer:
        msg = "OK, cancelado." if spoken_lang == "es" else "OK, cancelled."
        await _say_or_speak(msg, spoken_lang)
        history.append(Message(role="assistant", content=msg))
        log.info("confirmation_declined", tool=pending.tool_name)
        if _tool_accepts_cancelled(pending.tool_name):
            result = await dispatch(pending.tool_name, {**pending.args, "cancelled": True})
            if result.user_message and result.user_message != msg:
                await _say_or_speak(result.user_message, spoken_lang)
                history.append(Message(role="assistant", content=result.user_message))
        else:
            log.info("confirmation_tool_no_cancel_handler", tool=pending.tool_name)
        return

    log.info("confirmation_granted", tool=pending.tool_name)
    result = await dispatch(pending.tool_name, {**pending.args, "confirmed": True})
    await _say_or_speak(result.user_message, spoken_lang)
    history.append(Message(role="assistant", content=result.user_message))


async def _race_playback_with_barge_in(
    play_gen: AsyncIterator[bytes],
    stages: dict[str, float],
    t_wake: float,
) -> bool:
    """Run playback concurrently with a mic-listener. Returns True if the
    user barged in (interrupting Emma); False if playback finished
    normally.
    """
    playback_task = asyncio.create_task(play_audio_stream(play_gen))
    interrupt_task = asyncio.create_task(
        listen_for_speech(
            rms_threshold=settings.BARGE_IN_RMS,
            consecutive_frames_required=settings.BARGE_IN_FRAMES,
            blanking_ms=settings.BARGE_IN_BLANKING_MS,
        )
    )
    log.info(
        "barge_in_armed",
        rms=settings.BARGE_IN_RMS,
        frames=settings.BARGE_IN_FRAMES,
        blanking_ms=settings.BARGE_IN_BLANKING_MS,
    )
    barge_in_start = time.monotonic()

    try:
        done, _pending_tasks = await asyncio.wait(
            {playback_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        for t in (playback_task, interrupt_task):
            t.cancel()
        raise

    if interrupt_task in done:
        # Barge-in fired.
        elapsed_ms = int((time.monotonic() - barge_in_start) * 1000)
        log.info("barge_in_detected", elapsed_ms=elapsed_ms)
        try:
            interrupt_task.result()
        except Exception:
            pass
        playback_task.cancel()
        try:
            await playback_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await play_gen.aclose()
        except Exception:
            pass
        return True

    # Playback finished cleanly. Tear down the listener.
    interrupt_task.cancel()
    try:
        await interrupt_task
    except (asyncio.CancelledError, Exception):
        pass
    # Surface any playback exception (other than cancel).
    try:
        playback_task.result()
    except asyncio.CancelledError:
        pass
    return False


async def _one_turn(
    history: list[Message],
    last_lang: Literal["es", "en"],
    skip_wake: bool = False,
) -> tuple[Literal["es", "en"], bool]:
    """Run one full turn. Returns (spoken_lang, barge_in_pending).

    When ``skip_wake`` is true, the wake-word listen is skipped - we
    came here directly from a barge-in and the user is already
    mid-utterance. When ``barge_in_pending`` is true, the caller should
    skip the wake-word on the *next* call.
    """
    global _last_turn_id, _last_transcript, _last_response

    turn_id = uuid.uuid4().hex[:8]
    _last_turn_id = turn_id
    _last_transcript = ""
    _last_response = ""
    structlog.contextvars.bind_contextvars(turn_id=turn_id)

    try:
        t_start = time.monotonic()
        if skip_wake:
            log.info("barge_in_continuation")
            t_wake = t_start
        else:
            await listen_for_wake_word()
            t_wake = time.monotonic()
            log.info("wake", elapsed_ms=int((t_wake - t_start) * 1000))

        if _simulate_crash:
            raise RuntimeError("test crash (--simulate-crash)")

        audio_pcm = await capture_until_silence()
        t_vad = time.monotonic()
        log.info("vad_done", elapsed_ms=int((t_vad - t_wake) * 1000), bytes=len(audio_pcm))
        if not audio_pcm:
            return last_lang, False

        transcript = await transcribe(audio_pcm)
        t_stt = time.monotonic()
        log.info("stt_done", elapsed_ms=int((t_stt - t_vad) * 1000), lang=transcript.language)
        _last_transcript = transcript.text
        if not transcript.text.strip():
            # Distinguish "you didn't say anything" from "STT failed on real
            # audio". 16000 bytes = ~500 ms of 16 kHz int16 mono - below that
            # we treat as no real utterance (mic noise, false wake).
            # Skip the apology when we're in a barge-in continuation: the
            # first ~200 ms of speech were already lost between listen and
            # capture streams, and what we have is often just the tail of
            # the user's first syllable - not a real STT failure.
            if len(audio_pcm) > 16000 and not skip_wake:
                log.warning("stt_empty_after_audio", bytes=len(audio_pcm))
                msg = (
                    "Perdón, tuve un problema entendiéndote. Repítelo, por favor."
                    if last_lang == "es"
                    else "Sorry, I had trouble catching that. Could you say it again?"
                )
                await _say_or_speak(msg, last_lang)
            elif skip_wake:
                log.info("stt_empty_after_barge_in", bytes=len(audio_pcm))
            return last_lang, False

        spoken_lang: Literal["es", "en"] = (
            last_lang if transcript.language == "other" else transcript.language
        )
        runtime.set_spoken_lang(spoken_lang)

        stages: dict[str, float] = {}
        full_response: list[str] = []
        pending: list[PendingConfirmation] = []

        async def llm_with_marker() -> AsyncIterator[str]:
            async for piece in converse(transcript, history, spoken_lang, pending):
                if "llm_first_token" not in stages:
                    stages["llm_first_token"] = time.monotonic()
                    log.info(
                        "llm_first_token",
                        elapsed_ms=int((stages["llm_first_token"] - t_stt) * 1000),
                    )
                full_response.append(piece)
                yield piece

        async def tts_with_marker() -> AsyncIterator[bytes]:
            async for chunk in speak(llm_with_marker(), spoken_lang):
                if "tts_first_byte" not in stages:
                    stages["tts_first_byte"] = time.monotonic()
                    base = stages.get("llm_first_token", t_stt)
                    log.info(
                        "tts_first_byte",
                        elapsed_ms=int((stages["tts_first_byte"] - base) * 1000),
                    )
                yield chunk

        async def play_with_marker() -> AsyncIterator[bytes]:
            async for chunk in tts_with_marker():
                if "playback_start" not in stages:
                    stages["playback_start"] = time.monotonic()
                    log.info(
                        "playback_start",
                        since_wake_ms=int((stages["playback_start"] - t_wake) * 1000),
                    )
                yield chunk

        barge_in = False
        play_gen = play_with_marker()
        if settings.BARGE_IN_ENABLED:
            barge_in = await _race_playback_with_barge_in(play_gen, stages, t_wake)
        else:
            await play_audio_stream(play_gen)

        reply_text = "".join(full_response).strip()
        _last_response = reply_text

        if "tts_first_byte" not in stages and not barge_in:
            # No audio bytes flowed and we weren't interrupted -> TTS/network
            # is broken. Fall back to system `say`.
            fallback_text = reply_text or _OFFLINE_MSG[spoken_lang]
            log.warning("tts_silent_falling_back_to_say", offline=not reply_text)
            say_fallback(fallback_text, spoken_lang)

        history.append(Message(role="user", content=transcript.text))
        if barge_in:
            # Drop the partial assistant reply: the LLM never finished a
            # complete thought and feeding it back would confuse future turns.
            log.info("barge_in_dropping_partial_reply", chars=len(reply_text))
            # Also drop pending confirmations; user is starting a new request.
            if pending:
                log.info("barge_in_dropping_pending", count=len(pending))
                pending.clear()
        elif reply_text:
            history.append(Message(role="assistant", content=reply_text))

        for p in pending:
            await _handle_confirmation(p, spoken_lang, history)

        cap = HISTORY_TURNS * 2
        if len(history) > cap:
            del history[: len(history) - cap]
        return spoken_lang, barge_in
    finally:
        structlog.contextvars.unbind_contextvars("turn_id")


async def run() -> None:
    _install_signals()
    log.info("Listening for wake word")
    history: list[Message] = []
    last_lang: Literal["es", "en"] = "en"
    skip_wake = False
    while not _shutdown.is_set():
        try:
            last_lang, barge_in = await _one_turn(history, last_lang, skip_wake=skip_wake)
            skip_wake = barge_in
        except asyncio.CancelledError:
            break
        except RuntimeError as exc:
            if "test crash" in str(exc):
                raise  # let it bubble to the crash handler
            log.error("turn_failed", error=str(exc))
            await asyncio.sleep(0.5)
            skip_wake = False
        except Exception as exc:
            log.error("turn_failed", error=str(exc))
            await asyncio.sleep(0.5)
            skip_wake = False
        if dev_state.shutdown_requested.is_set():
            log.info("dev_mode_exit")
            break

    try:
        from tools.browser import shutdown_browser

        await shutdown_browser()
    except Exception:
        pass
    log.info("shutdown_complete")
