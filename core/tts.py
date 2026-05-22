"""ElevenLabs streaming text-to-speech.

Strategy (phase A): buffer LLM tokens until a sentence boundary (or
``settings.TTS_FIRST_CHUNK_MIN_CHARS`` chars on the very first chunk to
get audio flowing sooner), then synthesize that piece. We request
``pcm_16000`` so playback is a passthrough to sounddevice without an
MP3 decoder.

Model, latency mode, output format, and first-chunk minimum are all
configurable via :mod:`config.settings`. Defaults (``eleven_flash_v2_5``
+ latency_mode=1 + 120-char first chunk) are tuned for natural prosody
at ~250 ms time-to-first-byte.

Phase B (not yet implemented): replace the per-sentence buffering with
a real streaming session - one HTTP connection, text fed in as it
arrives from the LLM, audio coming out continuously. Tracked as
TODO(phase-08-B).
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import AsyncIterator, Iterable
from typing import Literal

import structlog
from elevenlabs import ElevenLabs

from config.settings import settings
from core import audio as _audio

log = structlog.get_logger("emma.tts")

SpokenLang = Literal["es", "en"]

_SENTENCE_END = re.compile(r"[.!?¡¿\n]")
_FMT_RATE_RE = re.compile(r"^(?:pcm|mp3|ulaw)_(\d+)")

_client: ElevenLabs | None = None


def _parse_output_format_rate(fmt: str) -> int | None:
    m = _FMT_RATE_RE.match(fmt)
    return int(m.group(1)) if m else None


# Module-load sanity check: if the user changed ELEVENLABS_OUTPUT_FORMAT
# in .env without also bumping core.audio.SAMPLE_RATE, playback speed
# will be wrong. Warn loudly.
_fmt_rate = _parse_output_format_rate(settings.ELEVENLABS_OUTPUT_FORMAT)
if _fmt_rate is not None and _fmt_rate != _audio.SAMPLE_RATE:
    log.warning(
        "tts_sample_rate_mismatch",
        output_format=settings.ELEVENLABS_OUTPUT_FORMAT,
        format_rate=_fmt_rate,
        audio_sample_rate=_audio.SAMPLE_RATE,
        hint="playback will be at the wrong speed; align core.audio.SAMPLE_RATE",
    )


def _get_client() -> ElevenLabs:
    global _client
    if _client is None:
        _client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
    return _client


def _voice_id_for(language: SpokenLang) -> str:
    return (
        settings.ELEVENLABS_VOICE_ID_ES
        if language == "es"
        else settings.ELEVENLABS_VOICE_ID_EN
    )


def _split_off_sentence(buf: str) -> tuple[str, str] | None:
    match = _SENTENCE_END.search(buf)
    if not match:
        return None
    cut = match.end()
    return buf[:cut].strip(), buf[cut:]


async def _synthesize_chunk(text: str, voice_id: str) -> list[bytes]:
    def _run() -> list[bytes]:
        result: Iterable[bytes] = _get_client().text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id=settings.ELEVENLABS_MODEL_ID,
            output_format=settings.ELEVENLABS_OUTPUT_FORMAT,
            optimize_streaming_latency=settings.ELEVENLABS_LATENCY_MODE,
        )
        return [b for b in result if b]

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=settings.API_TIMEOUT_S)
    except Exception as exc:
        log.error("tts_failed", error=str(exc), chars=len(text))
        return []


async def speak(
    text_stream: AsyncIterator[str],
    language: SpokenLang,
) -> AsyncIterator[bytes]:
    """Pipe incoming text into ElevenLabs and yield streamed PCM audio.

    Cancellation-aware: on barge-in the consumer cancels playback, which
    propagates here. We log ``tts_cancelled`` and re-raise. The in-flight
    ``_synthesize_chunk`` HTTP call may finish to completion (it runs in
    a worker thread we can't cancel), wasting at most one sentence of
    ElevenLabs tokens. Switching to a true streaming session (phase-08-B)
    would tighten this.
    """
    voice_id = _voice_id_for(language)
    buf = ""
    first_flushed = False
    first_chunk_min = settings.TTS_FIRST_CHUNK_MIN_CHARS

    try:
        async for piece in text_stream:
            buf += piece
            while True:
                split = _split_off_sentence(buf)
                if split is None:
                    if not first_flushed and len(buf) >= first_chunk_min:
                        chunk, buf = buf.strip(), ""
                        first_flushed = True
                        if chunk:
                            for b in await _synthesize_chunk(chunk, voice_id):
                                yield b
                    break
                chunk, buf = split
                if chunk:
                    first_flushed = True
                    for b in await _synthesize_chunk(chunk, voice_id):
                        yield b

        tail = buf.strip()
        if tail:
            for b in await _synthesize_chunk(tail, voice_id):
                yield b
    except (asyncio.CancelledError, GeneratorExit):
        log.info(
            "tts_cancelled",
            chars_buffered=len(buf),
            first_flushed=first_flushed,
        )
        raise


def say_fallback(text: str, language: SpokenLang) -> None:
    """Non-blocking spoken fallback via the macOS `say` command.

    Used by the orchestrator when ElevenLabs produces no audio (offline,
    rate-limited, broken auth, etc.) and by the crash handler.
    """
    if not text.strip():
        return
    voice = "Mónica" if language == "es" else "Samantha"
    try:
        subprocess.Popen(
            ["say", "-v", voice, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.error("say_fallback_failed", error=str(exc))
