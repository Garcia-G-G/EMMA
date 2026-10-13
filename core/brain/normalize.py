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
    words: tuple[str, ...] = ()  # the same tokens in display form (accents kept), 1:1


def normalize(utterance: str) -> Normalized:
    """Cut the wake run and vocatives out of the ORIGINAL string; never re-join tokens.

    Re-joining turned "example.com/portfolio" into "example. com/ portfolio" and
    "analytics-prod" into "analytics- prod" — values the slots must return intact.
    """
    raw = _SPACE.sub(" ", utterance or "").strip().lower()
    drop: list[tuple[int, int]] = []
    leading = True
    for m in _WORD.finditer(raw):
        p = m.group()
        is_word = p[0].isalnum() or p[0] == "_"
        f = fold(p)
        if is_word and ((leading and f in _WAKE_LEADING) or f in _VOCATIVE):
            drop.append(m.span())
            continue
        if is_word:
            leading = False
        elif leading:
            drop.append(m.span())  # punctuation between wake words ("Ay, Emma, …")
    kept, pos = [], 0
    for a, b in drop:
        kept.append(raw[pos:a])
        pos = b
    kept.append(raw[pos:])
    text = _SPACE.sub(" ", "".join(kept))
    text = re.sub(r"\s+([,.;:?!])", r"\1", text).strip(" ,.;:")
    words = tuple(w for w in _WORD.findall(text) if w[0].isalnum() or w[0] == "_")
    tokens = tuple(fold(w) for w in words)
    return Normalized(
        original=utterance, text=text, folded=fold(text), tokens=tokens, words=words
    )


_ES = frozenset({"el", "la", "los", "las", "de", "que", "en", "un", "una", "mi", "mis", "por",
                 "con", "para", "es", "abre", "pon", "dime", "como", "esta", "este"})
_EN = frozenset({"the", "a", "an", "of", "to", "in", "on", "my", "for", "with", "is", "open",
                 "play", "what", "how", "this", "that", "please", "and"})


def lang_hint(tokens: tuple[str, ...]) -> str:
    """'es' | 'en' by which stopword set matches more; ties go to 'es' (Emma's default)."""
    es = sum(t in _ES for t in tokens)
    en = sum(t in _EN for t in tokens)
    return "en" if en > es else "es"
