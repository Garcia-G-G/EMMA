"""core/brain/normalize: what survives from an STT transcript."""

from __future__ import annotations

from core.brain.normalize import normalize


def test_quoted_words_survive_stt_punctuation() -> None:
    """VOICE-1: Scribe wrote "mi carpeta 'Home'" and the quoted word vanished,
    taking a safety refusal with it. Outer apostrophes are quotes; inner ones stay."""
    from core.brain.gate import pre_retrieval

    n = normalize("\"Emma, borra mi carpeta 'Home' completa.\"")
    assert n.tokens == ("borra", "mi", "carpeta", "home", "completa")
    assert pre_retrieval(n) == ("refuse", "safety:destroy_home")
    assert normalize("\u2018Spotify\u2019 ábrelo").tokens == ("spotify", "abrelo")
    assert normalize("don't open it").tokens[0] == "don't"
