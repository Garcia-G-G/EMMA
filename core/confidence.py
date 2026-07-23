"""Low-confidence transcript heuristics (21-B28).

Logprobs would be the right signal, but they can't reach us today:
OpenAI Realtime supports ``include=["item.input_audio_transcription.logprobs"]``
and Pipecat's GA ``SessionProperties`` even has the ``include`` field — yet
the typed server event (``ConversationItemInputAudioTranscriptionCompleted``,
pydantic with default ``extra="ignore"``) DROPS the logprobs payload during
parsing, so it never reaches the ``TranscriptionFrame``. Verified against
pipecat 1.2.1 source. Until that's upstreamed, confidence is inferred:

- **Echo**: ≥ 80 % token overlap with Emma's own last utterance → the mic
  re-heard her (the V13 "See" artifact family).
- **Suspicious fragment**: ≤ 2 tokens that are NOT a known confirmation
  word / number — noise blips, clipped speech.

Deferred (documented, not guessed at): out-of-vocab proper-noun detection
needs a frequency lexicon; logprob threshold calibration needs logprobs.
"""

from __future__ import annotations

import unicodedata

# Words that legitimately arrive as 1-2 token turns — never flag these.
_CONFIRM_WORDS = frozenset(
    {
        "sí",
        "si",
        "no",
        "ok",
        "okay",
        "dale",
        "claro",
        "hazlo",
        "cancela",
        "cancelar",
        "bueno",
        "va",
        "sale",
        "yes",
        "sure",
        "cancel",
        "nope",
        "yep",
        "gracias",
        "thanks",
        "adiós",
        "bye",
        "para",
        "stop",
        "espera",
        "wait",
    }
)

_ECHO_OVERLAP = 0.8


def _tokens(s: str) -> list[str]:
    out = []
    for raw in s.lower().split():
        t = "".join(
            ch for ch in unicodedata.normalize("NFC", raw) if ch.isalnum() or ch in "áéíóúüñ"
        )
        if t:
            out.append(t)
    return out


def is_low_confidence(text: str, last_bot_text: str = "") -> bool:
    """True when the transcript smells like noise/echo rather than the user."""
    tokens = _tokens(text)
    if not tokens:
        return True

    # Echo of Emma's own voice leaking back through the mic.
    if last_bot_text and len(tokens) >= 3:
        bot = set(_tokens(last_bot_text))
        if bot:
            overlap = sum(1 for t in tokens if t in bot) / len(tokens)
            if overlap >= _ECHO_OVERLAP:
                return True

    # Tiny fragments that aren't a recognizable confirmation/number.
    return len(tokens) <= 2 and not all(t in _CONFIRM_WORDS or t.isdigit() for t in tokens)
