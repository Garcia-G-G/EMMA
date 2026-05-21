"""Speech-to-text via OpenAI Whisper (whisper-1).

We send a single utterance worth of audio per call. The API auto-detects
language; we normalize to "es" | "en" | "other".
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Literal

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core.audio import pcm_to_wav_bytes

log = structlog.get_logger("emma.stt")

LangCode = Literal["es", "en", "other"]

# Whisper occasionally returns these on silence or noise. Treat as empty.
_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        "Subtítulos por la comunidad de Amara.org",
        "Subtítulos realizados por la comunidad de Amara.org",
        "Subtitles by the Amara.org community",
        "Thanks for watching!",
        "Thank you for watching!",
        "¡Gracias por ver el video!",
        "you",
        ".",
        "",
    }
)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


@dataclass(frozen=True)
class Transcript:
    text: str
    language: LangCode


def _normalize_lang(raw: str | None) -> LangCode:
    if not raw:
        return "other"
    head = raw.strip().lower()[:2]
    if head == "es":
        return "es"
    if head == "en":
        return "en"
    return "other"


def _scrub(text: str) -> str:
    cleaned = text.strip()
    if cleaned in _HALLUCINATIONS:
        return ""
    return cleaned


async def transcribe(audio_pcm: bytes) -> Transcript:
    """Transcribe PCM audio. Returns empty transcript on failure or silence."""
    if not audio_pcm:
        return Transcript("", "other")

    wav = pcm_to_wav_bytes(audio_pcm)
    file_obj = io.BytesIO(wav)
    file_obj.name = "speech.wav"

    try:
        result = await asyncio.wait_for(
            _get_client().audio.transcriptions.create(
                model="whisper-1",
                file=file_obj,
                response_format="verbose_json",
            ),
            timeout=settings.API_TIMEOUT_S,
        )
    except Exception as exc:
        log.error("stt_failed", error=str(exc))
        return Transcript("", "other")

    text = _scrub(str(getattr(result, "text", "") or ""))
    lang = _normalize_lang(getattr(result, "language", None))
    log.debug("stt_result", text=text, language=lang)
    return Transcript(text, lang)
