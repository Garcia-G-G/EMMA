"""BRAIN-1.5 Part 1 — the phrasebook: coverage, honesty, neutrality."""

from __future__ import annotations

from collections import defaultdict

from core.brain.normalize import normalize
from core.brain.phrasebook import expand, load, placeholders
from core.brain_host import labelled_examples
from tools import registry

BOOK = load()


def _key(text: str) -> tuple[str, ...]:
    return normalize(text).tokens


def test_every_tool_has_15_es_and_15_en() -> None:
    assert len(BOOK) >= 77
    for tool, rows in BOOK.items():
        es = [r for r in rows if r.lang == "es"]
        en = [r for r in rows if r.lang == "en"]
        assert len(es) >= 15 and len(en) >= 15, f"{tool}: es={len(es)} en={len(en)}"


def test_covers_every_tool_the_labelled_set_routes_to() -> None:
    needed = {t for e in labelled_examples() for t in e.accept_tools if t != "__none__"}
    assert needed <= set(BOOK), f"no phrasebook: {sorted(needed - set(BOOK))}"


def test_every_tool_is_registered_and_placeholders_match_its_schema() -> None:
    for tool, rows in BOOK.items():
        entry = registry.get_tool(tool)
        assert entry is not None, f"{tool} is not a registered tool"
        params = set((entry.parameters or {}).get("properties", {}))
        for r in rows:
            for p in placeholders(r.template):
                assert p in params, f"{tool}: {{{p}}} is not a parameter ({sorted(params)})"


def test_no_phrasing_under_two_tools() -> None:
    owners: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for tool, rows in BOOK.items():
        for r in rows:
            owners[_key(r.template)].add(tool)
    dupes = {" ".join(k): sorted(v) for k, v in owners.items() if len(v) > 1}
    assert not dupes, f"same phrasing under several tools: {dupes}"


def test_no_labelled_utterance_is_in_the_phrasebook() -> None:
    """The 140 are the exam: not as a template, not as any filled expansion."""
    exam = {_key(e.text): e.id for e in labelled_examples()}
    leaks = []
    for tool, rows in BOOK.items():
        for r in rows:
            for text in [r.template, *expand(r)]:
                if _key(text) in exam:
                    leaks.append((exam[_key(text)], tool, text))
    assert not leaks, f"labelled items copied into the phrasebook: {leaks}"


# Regional words the language convention keeps out (CLAUDE.md): a closed list of
# common ones. If unsure whether a word is regional, it doesn't go in at all.
_REGIONAL = {
    "ahorita", "orale", "órale", "guey", "güey", "wey", "chido", "padre", "chamba", "neta",
    "vale", "tio", "tío", "mola", "guay", "coger", "che", "boludo", "pibe", "laburo", "pana",
    "chevere", "chévere", "parce", "bacan", "bacán", "cachai", "fome", "vos", "ustedes",
    "compu", "celu", "carro", "computador", "computadora", "ordenador", "móvil", "celular",
    "súbele", "bájale", "pica", "picale", "brinca", "junta", "juntas", "regrésame", "regresa",
}


def test_neutral_language() -> None:
    hits = []
    for tool, rows in BOOK.items():
        for r in rows:
            words = set(normalize(r.template).words)
            bad = words & _REGIONAL
            if bad:
                hits.append((tool, r.template, sorted(bad)))
    assert not hits, f"regional vocabulary: {hits}"
