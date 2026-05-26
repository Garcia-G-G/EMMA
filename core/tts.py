"""Compat shim post Realtime-API migration (Prompt 13).

ElevenLabs-driven streaming TTS is gone; :mod:`core.realtime` emits
PCM audio directly from the Realtime session. The only piece left
here is :func:`say_fallback`, which other in-tree code (notably
``actions/environment.py`` for brew-install progress voice) still
calls to speak short messages via the macOS ``say`` command when the
network path is unavailable.

Kept as a small utility rather than moved because the constraint for
Phase 13 was "do not touch actions/*.py".
"""
from __future__ import annotations

import subprocess
from typing import Literal

import structlog

log = structlog.get_logger("emma.tts")

SpokenLang = Literal["es", "en"]


def say_fallback(text: str, language: SpokenLang) -> None:
    """Non-blocking spoken fallback via the macOS ``say`` command.

    Used by:

    - :mod:`actions.environment` to narrate long brew installs.
    - Any future offline-degradation path that wants speech without
      depending on the Realtime WebSocket.
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
