"""Lightweight, dependency-free affect detection for emotion-aware tone (Prompt 35).

The Realtime API is audio-to-audio, so Emma already *hears* the user's emotional
tone — the always-on attunement directive in the system prompt is what drives the
live behaviour. This module is the cheap text-side companion: it reads the last
user transcript with a small Spanish/English cue lexicon and maps it to a spoken
style hint, used for cross-session continuity, telemetry, and explicit steering.

No ML, no audio. Heavy speaker-ID / ambient-classifier work (pyannote, YAMNet) is
deferred — see the Prompt 35 follow-up.
"""

from __future__ import annotations

from typing import Literal

Affect = Literal["neutral", "frustrated", "sad", "excited", "urgent", "tired"]

# Substring cues (already lowercased). Stems keep ES inflections in one entry
# ("agotad" -> agotado/agotada). Entries are chosen specific enough to avoid
# matching unrelated words ("solo"/"mal" deliberately omitted — too broad).
_LEX: dict[Affect, tuple[str, ...]] = {
    "urgent": ("urgente", "apúrate", "apurate", "de inmediato", "ahora mismo",
               "cuanto antes", "es para ya", "urgent", "hurry", "asap", "right now"),
    "frustrated": ("harto", "harta", "enojad", "molest", "frustrad", "no funciona",
                   "no sirve", "no jala", "odio esto", "fastidi", "qué fastidio",
                   "frustrat", "annoyed", "is broken", "this is broken", "i hate", "stupid"),
    "sad": ("triste", "deprim", "desanim", "llor", "me siento mal", "bajón",
            "sad", "lonely", "depress", "crying", "unhappy", "down today"),
    "tired": ("cansad", "agotad", "cansancio", "con sueño", "rendid", "sin energía",
              "exhaust", "tired", "sleepy", "drained", "worn out"),
    "excited": ("emocion", "feliz", "genial", "increíble", "increible", "contentí",
                "qué bien", "excelente", "me encanta", "excited", "awesome",
                "amazing", "so happy", "yay", "love it"),
}

# Tie-break precedence: urgency/frustration steer tone the most.
_PRECEDENCE: tuple[Affect, ...] = ("urgent", "frustrated", "sad", "tired", "excited")

_STYLE: dict[str, str] = {
    "neutral": "",
    "frustrated": (
        "El usuario suena frustrado. Responde con calma, en una sola frase breve, "
        "ve directo a la solución y evita rodeos o disculpas largas."
    ),
    "sad": (
        "El usuario suena decaído. Usa un tono cálido y empático, sin prisa, "
        "y ofrece apoyo antes que datos."
    ),
    "excited": (
        "El usuario suena entusiasmado. Acompaña su energía con un tono alegre "
        "y cercano, sin exagerar."
    ),
    "urgent": (
        "El usuario tiene prisa. Sé telegráfica: responde lo esencial en una frase, "
        "sin preámbulo."
    ),
    "tired": (
        "El usuario suena cansado. Sé breve y suave, baja el ritmo y no le des "
        "tareas extra."
    ),
}


def detect_affect(text: str) -> Affect:
    """Best-effort emotional read of a user utterance. Defaults to ``neutral``."""
    t = (text or "").lower()
    if not t.strip():
        return "neutral"
    scores: dict[Affect, int] = {}
    for affect, cues in _LEX.items():
        hits = sum(1 for cue in cues if cue in t)
        if hits:
            scores[affect] = hits
    if not scores:
        return "neutral"
    best = max(scores.values())
    for affect in _PRECEDENCE:  # precedence breaks ties deterministically
        if scores.get(affect) == best:
            return affect
    return "neutral"


def style_hint(affect: str) -> str:
    """Spanish tone directive for an affect label. Empty for neutral/unknown."""
    return _STYLE.get(affect, "")
