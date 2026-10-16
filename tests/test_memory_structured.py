"""Step 42 Part 3 — structured facts: contradictions by structure, 0 wrong on the set."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from memory import structured as st
from tests.memory_recall_set import (
    COMPATIBLE,
    CONTRADICTIONS,
    DUPLICATES,
    FACTS,
    QUERIES,
    UNANSWERABLE,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(st.SCHEMA)
    return c


def _second(a: str, b: str) -> str | None:
    c = sqlite3.connect(":memory:")
    sa, sb = st.parse_statement(a), st.parse_statement(b)
    if sa is None or sb is None:
        return None  # a free-text note: the vector path's job
    st.commit(c, st.propose(c, sa))
    return st.propose(c, sb).change


def test_zero_wrong_on_contradictions_duplicates_and_compatible_pairs() -> None:
    counts = {"contra": 0, "dup": 0}
    for a, b in CONTRADICTIONS:
        ch = _second(a, b)
        assert ch != "same", (a, b)  # a changed value is never taken as a restatement
        counts["contra"] += ch in ("change", "retract")
    for a, b in DUPLICATES:
        ch = _second(a, b)
        assert ch not in ("change", "retract"), (a, b)  # a restatement never overwrites
        counts["dup"] += ch == "same"
    for a, b in COMPATIBLE:
        assert _second(a, b) in (None, "new"), (a, b)  # both true: nothing replaced
    # Coverage floors (measured 2026-10-16): raise in the commit that earns more.
    assert counts == {"contra": 17, "dup": 10} or (counts["contra"] >= 17 and counts["dup"] >= 10)


def test_zero_wrong_on_slot_questions(conn: sqlite3.Connection) -> None:
    slot_of = {}
    for fid, text in FACTS.items():
        s = st.parse_statement(text)
        if s:
            st.commit(conn, st.propose(conn, s))
            slot_of[fid] = s
    right = 0
    for q, gold in QUERIES:
        k = st.parse_question(q)
        if not k:
            continue
        values = st.lookup(conn, *k)
        allowed = {slot_of[g].value for g in gold if g in slot_of}
        assert set(values) <= allowed, (q, k, values)  # never a value that doesn't answer
        right += bool(values)
    for q in UNANSWERABLE:
        k = st.parse_question(q)
        assert not (k and st.lookup(conn, *k)), q
    assert len(slot_of) >= 25 and right >= 47


def test_a_change_names_the_old_value_and_keeps_history(conn: sqlite3.Connection) -> None:
    st.commit(conn, st.propose(conn, st.parse_statement("mi perro se llama Toby")))  # type: ignore[arg-type]
    p = st.propose(conn, st.parse_statement("mi perro se llama Max"))  # type: ignore[arg-type]
    assert p.change == "change" and p.old is not None and p.old[1] == "Toby"
    st.commit(conn, p)
    assert st.lookup(conn, "dog", "name") == ["Max"]
    rows = conn.execute("SELECT value, valid_to IS NOT NULL, supersedes FROM user_facts ORDER BY id").fetchall()
    assert rows[0][:2] == ("Toby", 1) and rows[1][0] == "Max" and rows[1][2] is not None


def test_a_retraction_closes_the_slot(conn: sqlite3.Connection) -> None:
    st.commit(conn, st.propose(conn, st.parse_statement("soy vegetariano")))  # type: ignore[arg-type]
    p = st.propose(conn, st.parse_statement("ya no soy vegetariano"))  # type: ignore[arg-type]
    assert p.change == "retract"
    st.commit(conn, p)
    assert st.lookup(conn, "user", "diet") == []
    assert st.propose(conn, st.parse_statement("I'm not vegetarian anymore")).change == "nothing_to_retract"  # type: ignore[arg-type]


def test_allergies_accumulate(conn: sqlite3.Connection) -> None:
    for t in ("soy alérgico a los mariscos", "I'm allergic to penicillin"):
        p = st.propose(conn, st.parse_statement(t))  # type: ignore[arg-type]
        assert p.change == "new"
        st.commit(conn, p)
    assert sorted(st.lookup(conn, "user", "allergy")) == ["mariscos", "penicillin"]


@pytest.mark.parametrize(("es", "en"), [("soy vegetariano", "I'm vegetarian"),
                                        ("mi cumpleaños es el 12 de octubre", "my birthday is on October 12")])
def test_the_same_fact_in_two_languages_is_one_fact(es: str, en: str) -> None:
    assert _second(es, en) == "same"


@pytest.mark.parametrize("text", ["¿Qué hora es?", "Pon algo de música", "what's the weather like",
                                  "mi coche es rojo", "the meeting moved to Friday"])
def test_what_the_grammar_doesnt_read_is_not_a_slot(text: str) -> None:
    assert st.parse_statement(text) is None and st.parse_question(text) is None


def test_the_grammar_is_neutral() -> None:
    """AGENTS.md rule 2: no regional expressions, places or nationalities in the grammar."""
    from core.brain.when import REGIONAL_DENYLIST

    src = Path(st.__file__).read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    for word in REGIONAL_DENYLIST:
        assert not re.search(rf"\b{re.escape(word)}\b", code), word
