"""Compat shim post Realtime-API migration (Prompt 13).

Whisper-based STT was removed in Phase 13. The Realtime session
transcribes the user's audio server-side and emits the text as a
``conversation.item.input_audio_transcription.completed`` event;
:mod:`core.realtime` consumes that event directly.

What remains here is the :class:`Transcript` dataclass, which the
acceptance runner imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LangCode = Literal["es", "en", "other"]


@dataclass(frozen=True)
class Transcript:
    text: str
    language: LangCode
