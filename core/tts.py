"""ElevenLabs streaming text-to-speech.

Strategy: buffer LLM tokens until a sentence boundary (or 60 chars on the
very first chunk to start audio sooner), then synthesize that piece as a
streaming PCM call. We request `pcm_16000` so playback is a passthrough
to sounddevice without an MP3 decoder.

`optimize_streaming_latency=3` saves ~150 ms time-to-first-byte at the
cost of slightly less expressive prosody. Voice-assistant feel beats
fidelity here; if the voice ever sounds noticeably synthetic, lower to 2.
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

log = structlog.get_logger("emma.tts")

SpokenLang = Literal["es", "en"]

_LATENCY_MODE = 3
_MODEL_ID = "eleven_multilingual_v2"
_OUTPUT_FORMAT = "pcm_16000"
_FIRST_CHUNK_MIN_CHARS = 60

_SENTENCE_END = re.compile(r"[.!?¡¿\n]")

_client: ElevenLabs | None = None


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
            model_id=_MODEL_ID,
            output_format=_OUTPUT_FORMAT,
            optimize_streaming_latency=_LATENCY_MODE,
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
    """Pipe incoming text into ElevenLabs and yield streamed PCM audio."""
    voice_id = _voice_id_for(language)
    buf = ""
    first_flushed = False

    async for piece in text_stream:
        buf += piece
        while True:
            split = _split_off_sentence(buf)
            if split is None:
                if not first_flushed and len(buf) >= _FIRST_CHUNK_MIN_CHARS:
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
