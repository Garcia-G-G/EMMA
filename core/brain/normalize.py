"""The one place transcript text is normalized for the brain.

Bilingual by construction — no language detection, no regional rules. Accents
are folded for MATCHING only; ``Normalized.text`` keeps them for display and for
any value that is handed back to the user or a tool.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from core.tool_selection import _QUERY_STOP

# Wake-phrase openers. The live wake set is _QUERY_STOP; the extra forms are how
# STT has actually rendered "Hey Emma" at the start of a turn in the labelled
# logs ("AYEMA ABRE MI GITCHUB", "AIMA ABRE SPOTIFY", "Ay, Emma, …", "ey emma").
# Stripped only as a LEADING run, so "hola" inside a dictated text survives.
_WAKE_LEADING = _QUERY_STOP | frozenset({"ey", "ay", "ayema", "aima", "heyemma", "oyeemma"})
_VOCATIVE = frozenset({"emma"})  # "…, Emma." anywhere is address, never content

_WORD = re.compile(r"[\w'\u2019]+|[^\w\s]", re.UNICODE)  # \u2019: typographic apostrophe
_SPACE = re.compile(r"\s+")


def fold(s: str) -> str:
    """Lowercase + strip combining accents (á→a, ñ→n). For comparison only."""
    decomposed = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True)
class Normalized:
    original: str
    text: str  # lowercase, wake/vocative removed, whitespace collapsed; accents kept
    folded: str  # ``text`` with accents folded — what matchers compare
    tokens: tuple[str, ...]  # word tokens of ``folded``, punctuation dropped


def normalize(utterance: str) -> Normalized:
    raw = _SPACE.sub(" ", utterance or "").strip()
    pieces = _WORD.findall(raw.lower())
    out: list[str] = []
    leading = True
    for p in pieces:
        is_word = p[0].isalnum() or p[0] == "_"
        f = fold(p)
        if is_word and leading and f in _WAKE_LEADING:
            continue
        if is_word and f in _VOCATIVE:
            continue
        if is_word:
            leading = False
        elif leading:
            continue  # punctuation between wake words ("Ay, Emma, …")
        out.append(p)
    text = _detok(out)
    folded = fold(text)
    tokens = tuple(t for t in _WORD.findall(folded) if t[0].isalnum() or t[0] == "_")
    return Normalized(original=utterance, text=text, folded=folded, tokens=tokens)


def _detok(pieces: list[str]) -> str:
    s = ""
    for p in pieces:
        if not s or (not (p[0].isalnum() or p[0] == "_") and p not in "¿¡("):
            s += p
        else:
            s += " " + p
    return _SPACE.sub(" ", s).strip(" ,.;:")
