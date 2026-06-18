"""Speaker-ID voice tools (Prompt 35.1): enroll / who / forget.

Enrollment is always explicit + voice-confirmed (never background auto-enroll). The
embedding is computed locally and stored in memory.db; no raw audio is kept.
"""

from __future__ import annotations

import structlog

from core import speaker
from memory import voice_profiles
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.speaker")

_ENROLL_SECONDS = 5
_SR = 16000


def _record(seconds: int) -> object:
    import numpy as np
    import sounddevice as sd

    rec = sd.rec(int(seconds * _SR), samplerate=_SR, channels=1, dtype="int16")
    sd.wait()
    return np.asarray(rec, dtype=np.int16).reshape(-1)


@tool(destructive=True)
async def enroll_my_voice(name: str = "garcia", confirmed: bool = False) -> ToolResult:
    """Aprende tu voz para reconocerte ("Emma, esta es mi voz").

    Graba 5 segundos y guarda tu huella de voz como `name` (por defecto «garcia»).
    Re-enrolar mejora el perfil. Pide confirmación antes de grabar.
    """
    name = (name or "garcia").strip().lower() or "garcia"
    if not speaker.enabled():
        return ToolResult(
            False, None,
            "El reconocimiento de voz no está instalado. Actívalo con «pip install -e .[speaker]».",
            False,
        )
    if not confirmed:
        return ToolResult(
            True, {"name": name},
            f"Voy a guardar tu voz como «{name}». Grabaré 5 segundos. ¿Confirmas?",
            requires_confirmation=True,
        )
    try:
        samples = _record(_ENROLL_SECONDS)
        embedding = voice_profiles.embed_audio(samples, _SR)  # type: ignore[arg-type]
    except voice_profiles.SpeakerIDUnavailable:
        return ToolResult(False, None, "El reconocimiento de voz no está disponible.", False)
    except Exception as exc:
        log.error("enroll_failed", error=str(exc))
        return ToolResult(False, None, "No pude grabar tu voz. Intenta de nuevo.", False)
    await voice_profiles.enroll(name, embedding)
    speaker.set_active(name)  # the person who just enrolled is the active speaker
    return ToolResult(True, {"name": name}, f"Listo, guardé tu voz como «{name}».", False)


@tool()
async def who_is_speaking() -> ToolResult:
    """Dice a quién reconoce Emma en la voz actual ("¿quién está hablando?", "¿soy yo?")."""
    if (await voice_profiles.list_profiles()) == []:
        return ToolResult(True, {"speaker": None},
                          "No tengo ninguna voz enrollada todavía. Di «Emma, esta es mi voz».", False)
    name = speaker.active()
    if name:
        return ToolResult(True, {"speaker": name}, f"Te reconozco como {name}.", False)
    return ToolResult(True, {"speaker": None}, "No reconozco la voz que está hablando.", False)


@tool(destructive=True)
async def forget_my_voice(name: str, confirmed: bool = False) -> ToolResult:
    """Olvida un perfil de voz guardado ("olvida la voz de X"). Confirma antes de borrar."""
    name = (name or "").strip().lower()
    if not name:
        return ToolResult(False, None, "¿De quién olvido la voz?", False)
    if not confirmed:
        return ToolResult(True, {"name": name},
                          f"¿Borro el perfil de voz de «{name}»?", requires_confirmation=True)
    deleted = await voice_profiles.delete_profile(name)
    if not deleted:
        return ToolResult(False, None, f"No tenía un perfil de voz para «{name}».", False)
    if speaker.active() == name:
        speaker.set_active(None)
    return ToolResult(True, {"name": name}, f"Listo, olvidé la voz de «{name}».", False)
