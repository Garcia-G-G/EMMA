"""Speech-to-text via the OpenAI audio.transcriptions API.

The model is configurable via :data:`config.settings.STT_MODEL`. Three
choices are wired:

- ``gpt-4o-mini-transcribe`` (default) - fast, dramatically more
  accurate than whisper-1 on Spanish and slang. Does not support
  ``verbose_json``, so the API does not return a ``language`` field;
  we leave language as ``"other"`` and let the orchestrator fall back
  to the prior turn's language.
- ``gpt-4o-transcribe`` - best quality, slower / more expensive. Same
  limitation as the mini model.
- ``whisper-1`` - legacy fallback. Only model that supports
  ``verbose_json`` with automatic language detection.
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
    """Map Whisper's `language` field to "es" | "en" | "other".

    Whisper `verbose_json` returns full language names ("spanish",
    "english", "french", ...), not 2-letter ISO codes. We accept both
    forms plus the common Spanish autonyms.
    """
    if not raw:
        return "other"
    s = raw.strip().lower()
    if s in {"es", "spa", "spanish", "español", "espanol", "castellano"}:
        return "es"
    if s in {"en", "eng", "english"}:
        return "en"
    # Fall back to ISO 2-letter prefix only when not a known full name.
    head = s[:2]
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

    model = settings.STT_MODEL
    # Only `whisper-1` supports `verbose_json` (which carries the
    # `language` field). The gpt-4o-* transcribe models accept `json`
    # only; we get higher accuracy at the cost of language detection,
    # which the orchestrator covers by propagating `last_lang`.
    response_format = "verbose_json" if model == "whisper-1" else "json"

    create_kwargs: dict[str, object] = {
        "model": model,
        "file": file_obj,
        "response_format": response_format,
    }
    # Bias the model toward listed proper nouns / slang. Same kwarg name
    # on whisper-1 and the gpt-4o-* transcribe models.
    if settings.WHISPER_PROMPT:
        create_kwargs["prompt"] = settings.WHISPER_PROMPT

    try:
        result = await asyncio.wait_for(
            _get_client().audio.transcriptions.create(**create_kwargs),
            timeout=settings.API_TIMEOUT_S,
        )
    except Exception as exc:
        log.error("stt_failed", error=str(exc), model=model)
        return Transcript("", "other")

    text = _scrub(str(getattr(result, "text", "") or ""))
    # gpt-4o-* transcribe responses don't include `language`; getattr
    # returns None, which _normalize_lang maps to "other" - orchestrator
    # then falls back to the prior turn's language.
    lang = _normalize_lang(getattr(result, "language", None))
    log.debug("stt_result", text=text, language=lang, model=model)
    return Transcript(text, lang)
