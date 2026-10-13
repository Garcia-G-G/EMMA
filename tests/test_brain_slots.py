"""BRAIN-1 Part 4 — closed-vocabulary slots against the labelled set.

Every L2 item must resolve exactly the values in ``brain_slot_fixtures``
(written before the resolver), for each tool it lists. Every L1 item must leave
no required parameter missing. A wrong value fails.
"""

from __future__ import annotations

import pytest

from core.brain.normalize import normalize
from core.brain.slots import TOOLS, Vocab, resolve
from core.brain_host import labelled_examples, tool_docs, vocab
from tests.brain_slot_fixtures import APPS, CONNECTIONS, EXPECTED, NOTES, PAGES, PLAYLISTS

DOCS = {d.name: d for d in tool_docs()}
ITEMS = {e.id: e for e in labelled_examples()}


@pytest.fixture(scope="module")
def v() -> Vocab:
    base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
    return Vocab(
        apps=base.apps, aliases=base.aliases, pages=PAGES, notes=base.notes,
        playlists=base.playlists, connections=base.connections, contacts=base.contacts,
    )


def test_every_l2_item_has_a_fixture() -> None:
    l2 = {i for i, e in ITEMS.items() if e.brain_path == "L2"}
    assert l2 == set(EXPECTED), f"missing {sorted(l2 - set(EXPECTED))} extra {sorted(set(EXPECTED) - l2)}"


def _cases() -> list[object]:
    out: list[object] = []
    for item_id, by_tool in sorted(EXPECTED.items()):
        for tool, slots in by_tool.items():
            out.append(pytest.param(item_id, tool, slots, id=f"{item_id}-{tool}"))
    return out


@pytest.mark.parametrize(("item_id", "tool", "expected"), _cases())
def test_l2_item_resolves_its_values(v: Vocab, item_id: str, tool: str, expected: dict[str, object]) -> None:
    ex = ITEMS[item_id]
    assert tool in ex.accept_tools, f"fixture tool {tool} not accepted for {item_id}"
    r = resolve(tool, normalize(ex.text), v, required=DOCS[tool].required)
    got = {p: r.slots[p].value for p in expected if p in r.slots}
    assert got == expected, f"{item_id} {ex.text!r}: got {got}, slots={r.slots}"
    assert not r.missing, f"{item_id}: required {r.missing} unresolved"


@pytest.mark.parametrize(
    "item_id", sorted(i for i, e in ITEMS.items() if e.brain_path == "L1")
)
def test_l1_item_needs_nothing_it_lacks(v: Vocab, item_id: str) -> None:
    ex = ITEMS[item_id]
    tools = [t for t in ex.accept_tools if t != "__none__" and t in DOCS]
    assert tools
    assert any(
        not resolve(t, normalize(ex.text), v, required=DOCS[t].required).missing for t in tools
    )


def test_every_l1_l2_item_has_a_route() -> None:
    """At least one accepted tool per L1/L2 item has a slot/frame spec.

    Alternatives in an accept set ("open_in_ide" as the third option for
    "abre cursor") don't each need one; the brain needs one way through.
    """
    orphans = [
        i for i, e in ITEMS.items()
        if e.brain_path in ("L1", "L2") and not any(t in TOOLS for t in e.accept_tools)
    ]
    assert not orphans, f"no spec for any accepted tool: {orphans}"


# ---- matcher behaviour the fixtures don't isolate ---------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("sube el volumen a 70", 70),
        ("pon el brillo al ochenta por ciento", 80),
        ("volumen a setenta y cinco", 75),
        ("set the volume to seventy five percent", 75),
        ("pon el volumen a la mitad", 50),
    ],
)
def test_numbers(v: Vocab, text: str, value: int) -> None:
    assert resolve("set_volume", normalize(text), v).slots["percent"].value == value


def test_below_the_floor_is_unresolved_not_guessed(v: Vocab) -> None:
    r = resolve("open_application", normalize("abre photoshock"), v, required=("name",))
    assert "name" not in r.slots and r.missing == ("name",)


def test_alias_beats_fuzzy_and_is_recorded(v: Vocab) -> None:
    s = resolve("open_application", normalize("abre guasap"), v).slots["name"]
    assert (s.value, s.source, s.confidence, s.raw_span) == ("WhatsApp", "alias:apps", 1.0, "guasap")


def test_fuzzy_records_ratio_and_span(v: Vocab) -> None:
    s = resolve("open_my_page", normalize("AYEMA ABRE MI GITCHUB"), v).slots["name"]
    assert s.value == "github" and s.source == "fuzzy:pages" and 0.85 <= s.confidence < 1.0
    assert "gitchub" in s.raw_span  # the key ("gitchub") or the title ("mi gitchub")


def test_leftover_reports_what_nothing_explained(v: Vocab) -> None:
    # sc_A05: current_time has no tz slot here (that's L3), so "tokio" is unexplained.
    r = resolve("current_time", normalize("Hey Emma, ¿qué hora es en Tokio?"), v)
    assert r.leftover == ("tokio",)


def test_residue_only_with_a_free_text_field(v: Vocab) -> None:
    r = resolve("mute", normalize("silencia el audio de la sala"), v)
    assert r.residue_param is None and "sala" in r.leftover
