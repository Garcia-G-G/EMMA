"""Is Emma ready to serve a wake word? (VOICE-1 Part 0)

The cascade (Scribe STT -> brain -> ElevenLabs TTS) needs four things and
nothing else: a microphone, the wake model, the ElevenLabs key, and the brain's
embedder. OpenAI is not on the list — the Realtime path keeps its own
credential preflight behind ``VOICE_BACKEND=realtime``. OpenRouter is optional
until BRAIN-2 plugs a thinker in.

Every probe is local and cheap (a file stat, a field read, a device listing):
``wait_until_ready`` polls them while the daemon is parked. Nothing here opens
the mic, so a parked daemon never lights the recording indicator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from config.settings import settings

log = structlog.get_logger("emma.readiness")

REQUIREMENTS = ("mic", "wake_model", "elevenlabs_key", "embedder")

# What the user (or the onboarding window) is told to do for each missing piece.
FIXES = {
    "mic": "connect or enable a microphone",
    "wake_model": "re-run the installer (step 5: wake-word model)",
    "elevenlabs_key": "add your ElevenLabs API key in onboarding",
    "embedder": "re-run the installer (step 5.2), or: python -m emma.brain fetch-model",
}


@dataclass(frozen=True)
class Readiness:
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing


def has_mic() -> bool:
    """An input device exists. A device listing, not a stream: no TCC prompt, no indicator."""
    try:
        import sounddevice as sd

        return bool(sd.query_devices(kind="input"))
    except Exception:
        return False


def has_wake_model() -> bool:
    engine = (settings.WAKE_WORD_ENGINE or "sherpa").strip().lower()
    if engine != "sherpa":
        return True  # the other engines keep their own lazy guards
    return (Path(settings.SHERPA_KWS_MODEL_PATH).expanduser() / "tokens.txt").exists()


def has_elevenlabs_key() -> bool:
    return bool((settings.ELEVENLABS_API_KEY or "").strip())


def has_embedder() -> bool:
    from core.brain.embed import Embedder

    return Embedder().available()


_PROBES = {
    "mic": has_mic,
    "wake_model": has_wake_model,
    "elevenlabs_key": has_elevenlabs_key,
    "embedder": has_embedder,
}


def check() -> Readiness:
    return Readiness(tuple(name for name in REQUIREMENTS if not _PROBES[name]()))


async def refresh_elevenlabs_key() -> None:
    """Re-read the key from Keychain: onboarding writes it while the daemon is parked."""
    if has_elevenlabs_key():
        return
    from core.secrets import retrieve

    val = await retrieve("ELEVENLABS_API_KEY")
    if val:
        object.__setattr__(settings, "ELEVENLABS_API_KEY", val)
