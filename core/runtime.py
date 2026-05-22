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
