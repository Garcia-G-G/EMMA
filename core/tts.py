"""One spoken-line helper for code outside a voice session.

:func:`say_fallback` speaks a short line in Emma's own voice through
:mod:`core.voice` (cached clip, else ElevenLabs TTS), without blocking the
caller. The macOS ``say`` command is no longer used (VOICE-1). The name is
historical; ``actions/environment.py`` uses it to narrate long installs.
"""

from __future__ import annotations

from typing import Literal

import structlog

log = structlog.get_logger("emma.tts")

SpokenLang = Literal["es", "en"]


def say_fallback(text: str, language: SpokenLang) -> None:
    """Speak ``text`` in Emma's voice on a background thread (never blocks).

    Used by :mod:`actions.environment` to narrate long brew installs.
    """
    if not text.strip():
        return
    del language  # one voice for both languages (VOICE-1): no per-language `say` voice
    import threading

    from core import voice

    # Non-blocking contract: synthesis on a cache miss is a network call.
    threading.Thread(target=voice.speak_sync, args=(text,), daemon=True).start()
