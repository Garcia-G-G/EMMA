"""Per-turn runtime context shared between the orchestrator and tools.

Tools that need to speak in the user's language (install progress
messages, first-run guidance, etc.) read ``get_spoken_lang()`` instead
of taking a ``language`` argument that the LLM would have to remember
to fill in.
"""

from __future__ import annotations

from typing import Literal

SpokenLang = Literal["es", "en"]

_current: SpokenLang = "es"


def set_spoken_lang(lang: SpokenLang) -> None:
    global _current
    _current = lang


def get_spoken_lang() -> SpokenLang:
    return _current


# ---- Conversational style hint (Prompt 35) ---------------------------------
# A short Spanish directive ("habla con calma…") set either by auto-detected
# affect (core/affect.py) or explicitly by the user (tools/tone_tool.py). It is
# appended to the system prompt when the next Pipecat session is built, so tone
# carries across turns/sessions. Empty string = no override (Emma's default).
_style_hint: str = ""


def set_style_hint(hint: str) -> None:
    global _style_hint
    _style_hint = hint or ""


def get_style_hint() -> str:
    return _style_hint
