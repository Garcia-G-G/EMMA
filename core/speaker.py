"""Active-speaker runtime state + the destructive-tool gate (Prompt 35.1).

The conversation loop feeds the user's mic audio here (``feed_audio``) and, at the
end of a user turn, calls ``on_turn_end`` to embed the clip and identify the speaker
against the enrolled profiles. ``active()`` is then read by the destructive-tool gate.

Degrade rules (so the daemon never locks itself out):
  - resemblyzer not installed → ``enabled()`` is False → the tap is a no-op, the gate
    is off, every turn is treated as the user (one INFO log per lifetime).
  - no enrolled profile → the gate is off (fresh install can still enroll).
"""

from __future__ import annotations

import importlib.util

import numpy as np
import structlog

from config.settings import settings
from memory import voice_profiles

log = structlog.get_logger("emma.speaker")

_active: str | None = None
_buffer: list[np.ndarray] = []
_buffer_sr = 24000
_BUFFER_MAX = 24000 * 3  # keep ~last 3 s of mic audio
_resemblyzer_ok: bool | None = None
_warned = False


def _available() -> bool:
    global _resemblyzer_ok
    if _resemblyzer_ok is None:
        _resemblyzer_ok = importlib.util.find_spec("resemblyzer") is not None
    return _resemblyzer_ok


def enabled() -> bool:
    return _available()


def active() -> str | None:
    return _active


def set_active(name: str | None) -> None:
    global _active
    _active = name


def reset() -> None:
    """New session: clear the buffer + the identified speaker."""
    global _active, _buffer
    _active = None
    _buffer = []


def should_gate() -> bool:
    """Gate destructive tools only when we CAN identify (resemblyzer) AND a profile
    is enrolled — so a fresh install never locks itself out, and a daemon without
    resemblyzer treats every turn as the user."""
    if not (settings.SPEAKER_GATE_DESTRUCTIVE and _available()):
        return False
    try:
        return voice_profiles.count_sync() > 0
    except Exception:
        return False


def feed_audio(audio: bytes, sample_rate: int = 24000) -> None:
    """Tap a mic frame (int16 PCM) into the rolling buffer. No-op when disabled."""
    if not _available() or not audio:
        return
    global _buffer, _buffer_sr
    _buffer_sr = sample_rate
    _buffer.append(np.frombuffer(audio, dtype=np.int16))
    total = sum(len(b) for b in _buffer)
    while total > _BUFFER_MAX and len(_buffer) > 1:
        total -= len(_buffer.pop(0))


async def identify_now() -> str | None:
    """Embed the buffered mic clip, identify the speaker, and update ``active()``.

    Called lazily (e.g. right before a destructive-tool decision) so we never need a
    fragile per-frame "user stopped speaking" hook. Returns the speaker name or None.
    Degrades to a no-op (keeps the current ``active()``) when disabled or buffer-empty.
    """
    global _warned
    if not _available() or not _buffer:
        return _active
    samples = np.concatenate(_buffer).astype(np.float32) / 32768.0
    try:
        emb = voice_profiles.embed_audio(samples, _buffer_sr)
    except voice_profiles.SpeakerIDUnavailable:
        if not _warned:
            log.info("speaker_id_disabled", hint="install resemblyzer ([speaker] extra) to enable")
            _warned = True
        return _active  # treat as the user
    try:
        name = await voice_profiles.identify(emb)
    except Exception as exc:
        log.warning("speaker_identify_failed", error=str(exc))
        return _active
    set_active(name)
    if name:
        await voice_profiles.mark_seen(name)
    log.debug("speaker_identified", speaker=name or "guest")
    return name
