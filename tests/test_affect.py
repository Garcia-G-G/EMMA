"""Prompt 35 — text-based affect detection + Spanish style hints (no ML)."""

from __future__ import annotations

import core.affect as af


def test_detect_frustrated_es() -> None:
    assert af.detect_affect("estoy harto, esto no funciona") == "frustrated"


def test_detect_sad_es() -> None:
    assert af.detect_affect("me siento triste y desanimado hoy") == "sad"


def test_detect_excited_es() -> None:
    assert af.detect_affect("qué emoción, estoy feliz") == "excited"


def test_detect_urgent_es() -> None:
    assert af.detect_affect("apúrate que es urgente") == "urgent"


def test_detect_tired_es() -> None:
    assert af.detect_affect("estoy agotado, qué cansancio") == "tired"


def test_detect_frustrated_en() -> None:
    assert af.detect_affect("i'm so frustrated this is broken") == "frustrated"


def test_neutral_when_no_signal() -> None:
    assert af.detect_affect("qué hora es") == "neutral"


def test_neutral_on_empty() -> None:
    assert af.detect_affect("") == "neutral"


def test_urgent_outranks_frustrated_on_tie() -> None:
    # one cue each -> precedence decides
    assert af.detect_affect("urgente y molesto") == "urgent"


def test_style_hint_neutral_is_empty() -> None:
    assert af.style_hint("neutral") == ""


def test_style_hint_frustrated_is_spanish_and_calm() -> None:
    h = af.style_hint("frustrated").lower()
    assert h and ("calma" in h or "breve" in h)


def test_style_hint_unknown_is_empty() -> None:
    assert af.style_hint("zzz") == ""
