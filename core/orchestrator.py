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
import re
import signal
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import structlog

from config.settings import settings
from core import dev_state
from core.audio import capture_until_silence, play_audio_stream
from core.llm import Message, PendingConfirmation, converse
from core.stt import transcribe
from core.tts import say_fallback, speak
from core.wake_word import listen_for_wake_word
from tools.registry import dispatch

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
    ppn = Path(settings.WAKE_WORD_PATH).expanduser()
    if not ppn.exists():
        raise SystemExit(
            f"Wake word file not found at {ppn}. "
            "Generate it in the Picovoice console (see README) and try again."
        )


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
        return
    if not answer:
        msg = "OK, cancelado." if spoken_lang == "es" else "OK, cancelled."
        await _say_or_speak(msg, spoken_lang)
        history.append(Message(role="assistant", content=msg))
        return

    log.info("confirmation_granted", tool=pending.tool_name)
    result = await dispatch(pending.tool_name, {**pending.args, "confirmed": True})
    await _say_or_speak(result.user_message, spoken_lang)
    history.append(Message(role="assistant", content=result.user_message))


async def _one_turn(
    history: list[Message], last_lang: Literal["es", "en"]
) -> Literal["es", "en"]:
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

        audio_pcm = await capture_until_silence()
        t_vad = time.monotonic()
        log.info("vad_done", elapsed_ms=int((t_vad - t_wake) * 1000), bytes=len(audio_pcm))
        if not audio_pcm:
            return last_lang

        transcript = await transcribe(audio_pcm)
        t_stt = time.monotonic()
        log.info("stt_done", elapsed_ms=int((t_stt - t_vad) * 1000), lang=transcript.language)
        _last_transcript = transcript.text
        if not transcript.text.strip():
            return last_lang

        spoken_lang: Literal["es", "en"] = (
            last_lang if transcript.language == "other" else transcript.language
        )

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

        await play_audio_stream(play_with_marker())

        reply_text = "".join(full_response).strip()
        _last_response = reply_text

        if "tts_first_byte" not in stages:
            fallback_text = reply_text or _OFFLINE_MSG[spoken_lang]
            log.warning("tts_silent_falling_back_to_say", offline=not reply_text)
            say_fallback(fallback_text, spoken_lang)

        history.append(Message(role="user", content=transcript.text))
        if reply_text:
            history.append(Message(role="assistant", content=reply_text))

        for p in pending:
            await _handle_confirmation(p, spoken_lang, history)

        cap = HISTORY_TURNS * 2
        if len(history) > cap:
            del history[: len(history) - cap]
        return spoken_lang
    finally:
        structlog.contextvars.unbind_contextvars("turn_id")


async def run() -> None:
    _install_signals()
    log.info("Listening for wake word")
    history: list[Message] = []
    last_lang: Literal["es", "en"] = "en"
    while not _shutdown.is_set():
        try:
            last_lang = await _one_turn(history, last_lang)
        except asyncio.CancelledError:
            break
        except RuntimeError as exc:
            if "test crash" in str(exc):
                raise  # let it bubble to the crash handler
            log.error("turn_failed", error=str(exc))
            await asyncio.sleep(0.5)
        except Exception as exc:
            log.error("turn_failed", error=str(exc))
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
