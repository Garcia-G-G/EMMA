"""Step 43 Part 3 — the time grammar: neutral, bilingual, deterministic."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from core.brain import when
from core.brain.normalize import normalize
from core.brain.slots import Vocab, resolve

# A Thursday, 9:00.
NOW = datetime(2026, 10, 15, 9, 0)


def _moment(text: str, overlay: dict[str, int] | None = None) -> datetime | None:
    n = normalize(text)
    hit = when.moment(n.tokens, [False] * len(n.tokens), n.folded, NOW, overlay)
    return hit[0] if hit else None


@pytest.mark.parametrize(("text", "want"), [
    ("a las nueve de la noche", datetime(2026, 10, 15, 21, 0)),
    ("a las 9:30", datetime(2026, 10, 15, 9, 30)),  # not passed yet: this morning
    ("a las 8:45", datetime(2026, 10, 15, 20, 45)),  # passed: this evening
    ("at 10", datetime(2026, 10, 15, 10, 0)),
    ("at nine thirty pm", datetime(2026, 10, 15, 21, 30)),
    ("a las ocho", datetime(2026, 10, 15, 20, 0)),  # "a las 8" at 9:00 -> 20:00, said back
    ("a las 15", datetime(2026, 10, 15, 15, 0)),
    ("a las nueve y media", datetime(2026, 10, 15, 9, 30)),
    ("a las diez y cuarto", datetime(2026, 10, 15, 10, 15)),
    ("a la una menos cuarto", datetime(2026, 10, 15, 12, 45)),
    ("half past eight", datetime(2026, 10, 15, 20, 30)),
    ("quarter to ten", datetime(2026, 10, 15, 9, 45)),
    ("al mediodía", datetime(2026, 10, 15, 12, 0)),
    ("mañana a las nueve", datetime(2026, 10, 16, 9, 0)),
    ("mañana at nine", datetime(2026, 10, 16, 9, 0)),
    ("tomorrow a las nueve", datetime(2026, 10, 16, 9, 0)),
    ("tomorrow at 9 pm", datetime(2026, 10, 16, 21, 0)),
    ("mañana por la mañana a las 7", datetime(2026, 10, 16, 7, 0)),
    ("pasado mañana", datetime(2026, 10, 17, 9, 0)),
    ("el viernes", datetime(2026, 10, 16, 9, 0)),
    ("on monday at 8", datetime(2026, 10, 19, 8, 0)),
    ("el jueves", datetime(2026, 10, 22, 9, 0)),  # today is Thursday: the next one
    ("esta noche a las 10", datetime(2026, 10, 15, 22, 0)),
    ("en veinte minutos", datetime(2026, 10, 15, 9, 20)),
    ("in 2 hours", datetime(2026, 10, 15, 11, 0)),
    ("en media hora", datetime(2026, 10, 15, 9, 30)),
    ("dentro de una hora", datetime(2026, 10, 15, 10, 0)),
    ("in an hour and a half", datetime(2026, 10, 15, 10, 30)),
])
def test_moments(text: str, want: datetime) -> None:
    assert _moment(text) == want


@pytest.mark.parametrize("text", ["llamar al dentista", "pon nueve canciones", "esta noche",
                                  "the nine o'clock news"])
def test_no_moment_is_invented(text: str) -> None:
    assert _moment(text) is None


@pytest.mark.parametrize(("text", "minutes"), [
    ("de veinte minutos", 20), ("for 25 minutes", 25), ("media hora", 30), ("una hora", 60),
    ("an hour and a half", 90), ("dos horas y diez minutos", 130), ("veinticinco minutos", 25),
    ("un minuto", 1), ("5 min", 5),
])
def test_durations(text: str, minutes: int) -> None:
    n = normalize(text)
    hit = when.duration(n.tokens, [False] * len(n.tokens))
    assert hit is not None and hit[0] == minutes


def test_the_default_grammar_has_no_regional_words() -> None:
    src = Path(when.__file__).read_text(encoding="utf-8")
    body = src.split("REGIONAL_DENYLIST = frozenset(")[1].split(")", 1)[1]  # all but the list
    for word in when.REGIONAL_DENYLIST:
        toks = word.split()
        pat = r'"\s*,\s*"'.join(re.escape(t) for t in toks)
        assert not re.search(rf'"{pat}"', body), word
        assert not re.search(rf"\b{re.escape(word)}\b", body), word


def test_a_profile_overlay_adds_phrases_absent_by_default() -> None:
    assert Vocab().time_phrases == {}
    assert _moment("en el siguiente bloque") is None
    assert _moment("en el siguiente bloque", overlay={"el siguiente bloque": 15}) == datetime(
        2026, 10, 15, 9, 15)


def test_a_reminder_resolves_title_and_time_in_two_languages() -> None:
    r = resolve("add_reminder", normalize("Recuérdame llamar al dentista tomorrow at nine."), Vocab(),
                required=("title",), now=NOW)
    assert r.slots["title"].value == "llamar al dentista"
    assert r.slots["due_iso"].value == "2026-10-16T09:00"
    assert not r.leftover and not r.missing


def test_a_timer_resolves_its_duration() -> None:
    r = resolve("start_timer", normalize("Pon un temporizador de veinte minutos."), Vocab(),
                required=("duration_min",), now=NOW)
    assert r.slots["duration_min"].value == 20 and not r.leftover


@pytest.mark.parametrize(("lang", "want"), [("es", "mañana a las 21:00"), ("en", "tomorrow at 21:00")])
def test_the_resolved_time_is_said_back(lang: str, want: str) -> None:
    assert when.spoken("2026-10-16T21:00", lang, NOW) == want


def test_the_confirmation_names_the_resolved_time() -> None:
    from core.brain.confirm import question

    # "a las 8" said at 9:00 resolves to 20:00 today; said back on the same clock.
    due = _moment("a las 8")
    assert due is not None
    q = question("add_reminder", {"title": "sacar la basura", "due_iso": due.isoformat()}, "es", NOW)
    assert q == "¿Recordarte «sacar la basura» hoy a las 20:00?"
    tomorrow = _moment("mañana a las nueve")
    assert tomorrow is not None
    q_en = question("add_reminder", {"title": "x", "due_iso": tomorrow.isoformat()}, "en", NOW)
    assert q_en == "Remind you to «x» tomorrow at 9:00?"
    assert question("add_reminder", {"title": "x"}, "en") == "Remind you to «x»?"
