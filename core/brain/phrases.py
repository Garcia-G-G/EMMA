"""Closed phrase lists, both languages. Neutral Spanish + English; no dialect.

Used by the injection guard (a confirmation must be a spoken yes AFTER the
question) and by the brain's confirmation/clarification turns (Part 6). Closed
by design: an open-ended "sounds like agreement" classifier is exactly what an
attacker's text would be written to satisfy.
"""

from __future__ import annotations

from core.brain.normalize import normalize

YES: frozenset[str] = frozenset(
    {
        "si", "sí", "claro", "dale", "hazlo", "adelante", "confirmo", "confirmado",
        "correcto", "de acuerdo", "va", "ok", "okay", "esta bien", "está bien",
        "por favor", "yes", "yeah", "yep", "sure", "do it", "go ahead", "confirm",
        "confirmed", "correct", "please do",
    }
)
NO: frozenset[str] = frozenset(
    {
        "no", "nel", "mejor no", "para nada", "nope", "nah", "don't", "do not",
        "no thanks", "no gracias",
    }
)
CANCEL: frozenset[str] = frozenset(
    {"cancela", "cancelar", "cancelalo", "cancélalo", "déjalo", "dejalo", "cancel", "stop", "never mind", "nevermind"}
)
# Clears the session's injection taint (BRAIN-1 Part 2): the user tells Emma to
# drop what she just read. Deliberately not "olvídalo", which is a cancel.
FORGET: frozenset[str] = frozenset(
    {"olvida eso", "olvídate de eso", "olvida lo que leiste", "olvida lo que leíste",
     "forget that", "forget about that", "forget what you read"}
)

# VOICE-1 Part 3: right after Emma ACTED, these mean she got it wrong — the
# production wrong-rate signal. Opening phrase of a short utterance only
# ("abre Safari otra vez" is a request, "otra vez" alone is a retry).
CORRECTION: frozenset[str] = frozenset(
    {"eso no", "no eso no", "así no", "no era eso", "eso no era", "eso no es", "está mal",
     "esta mal", "mal", "te equivocaste", "deshaz eso", "deshazlo", "otra vez", "de nuevo",
     "inténtalo de nuevo", "intentalo de nuevo",
     "not that", "no not that", "that's wrong", "thats wrong", "wrong", "undo", "undo that",
     "again", "try again", "that's not it", "thats not it"}
)

_MAX_ANSWER_TOKENS = 6  # a yes/no answer is short; a long sentence is a new request


def _contains(tokens: tuple[str, ...], phrases: frozenset[str]) -> bool:
    joined = " " + " ".join(tokens) + " "
    for p in phrases:
        pt = normalize(p).tokens
        if pt and (" " + " ".join(pt) + " ") in joined:
            return True
    return False


def _starts_with(tokens: tuple[str, ...], phrases: frozenset[str]) -> bool:
    for p in phrases:
        pt = normalize(p).tokens
        if pt and tokens[: len(pt)] == pt:
            return True
    return False


def is_yes(text: str) -> bool:
    """A short answer that OPENS with an affirmative and carries no negation or cancel.

    Opening matters: "borra la nota compras, sí" is a request that happens to
    end in "sí" — if its transcript lands late, after Emma's question, it must
    not count as the answer.
    """
    toks = normalize(text).tokens
    if not toks or len(toks) > _MAX_ANSWER_TOKENS:
        return False
    return _starts_with(toks, YES) and not _contains(toks, NO) and not _contains(toks, CANCEL)


def is_no(text: str) -> bool:
    toks = normalize(text).tokens
    if not toks or len(toks) > _MAX_ANSWER_TOKENS:
        return False
    if _starts_with(toks, NO) or _starts_with(toks, CANCEL):
        return True  # "no, por favor" is a no
    return (_contains(toks, NO) or _contains(toks, CANCEL)) and not _contains(toks, YES)


def is_forget(text: str) -> bool:
    return _contains(normalize(text).tokens, FORGET)


def is_correction(text: str) -> bool:
    """A short utterance that OPENS with a correction, a no, or a cancel.

    Only meaningful right after an executed turn (the processor checks that):
    there, "no" or "cancela" is the user saying the action was wrong.
    """
    toks = normalize(text).tokens
    if not toks or len(toks) > _MAX_ANSWER_TOKENS:
        return False
    return _starts_with(toks, CORRECTION) or _starts_with(toks, NO) or _starts_with(toks, CANCEL)
