"""Phase 19.5-B — smart fuzzy append for notes.

The Notes enumeration is mocked: a fake osascript reads the AppleScript
``whose name <op> "<q>"`` clause and returns the fixture notes that match that
operator, so find_by_title's exact→starts-with→contains tiers behave realistically.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from tools.disambiguation import Match, suffix_prompt, word_common_prefix


def _fake_osascript(notes: list[tuple[str, str]]):
    """notes = [(id, title)]. Returns an async osascript honoring is/starts/contains."""
    captured: list[str] = []

    async def osa(script: str, timeout_s: float = 15.0) -> str:
        captured.append(script)
        m = re.search(r'whose name (is|starts with|contains) "([^"]*)"', script)
        if not m:
            return ""  # e.g. a create-note script — no matches to enumerate
        op, q = m.group(1), m.group(2).lower()
        hits = []
        for nid, title in notes:
            tl = title.lower()
            if (
                (op == "is" and tl == q)
                or (op == "starts with" and tl.startswith(q))
                or (op == "contains" and q in tl)
            ):
                hits.append(f"{nid}‖2026-06-04T10:00:00‖{title}‖")
        return "\n".join(hits)

    osa.captured = captured  # type: ignore[attr-defined]
    return osa


def _patch_notes(notes):
    return (
        patch("actions.macos.osascript", new=_fake_osascript(notes)),
        patch("actions.macos.osascript_or_friendly", new=AsyncMock(return_value=(True, ""))),
    )


# ---- B1/B2 pure helpers ----------------------------------------------------


class TestHelpers:
    def test_word_common_prefix_is_word_aware(self):
        assert (
            word_common_prefix(["Pendientes para hoy", "Pendientes para el miércoles"])
            == "Pendientes para"
        )
        assert word_common_prefix(["Compras", "Compromiso"]) == ""  # no shared whole word

    def test_suffix_prompt_temporal(self):
        ms = [Match("1", "Pendientes para hoy"), Match("2", "Pendientes para el miércoles")]
        msg = suffix_prompt(ms, "Pendientes para", lang="es")
        assert "¿para cuándo?" in msg
        assert "'hoy'" in msg and "'el miércoles'" in msg

    def test_suffix_prompt_falls_back_to_numeric_when_many(self):
        ms = [Match(str(i), f"Lista {i}") for i in range(5)]  # >4
        msg = suffix_prompt(ms, "Lista", lang="es")
        assert "Cuál número" in msg or "número" in msg


# ---- B3 append flows -------------------------------------------------------


class TestSmartAppend:
    @pytest.mark.asyncio
    async def test_single_starts_with_appends(self):
        from tools import notes_tool

        p1, p2 = _patch_notes([("id1", "Pendientes para mañana")])
        with p1, p2:
            r = await notes_tool.append_to_note("Pendientes", "leche")
        assert r.success and r.requires_confirmation is False
        assert "Agregado a 'Pendientes para mañana'" in r.user_message

    @pytest.mark.asyncio
    async def test_exact_match_still_works(self):
        from tools import notes_tool

        p1, p2 = _patch_notes([("id1", "Pendientes"), ("id2", "Pendientes para mañana")])
        with p1, p2:
            r = await notes_tool.append_to_note("Pendientes", "leche")
        # exact "Pendientes" short-circuits → single hit, appended (no prompt)
        assert r.success and r.requires_confirmation is False
        assert "Agregado a 'Pendientes'" in r.user_message

    @pytest.mark.asyncio
    async def test_two_matches_asks_by_suffix(self):
        from tools import notes_tool

        p1, p2 = _patch_notes(
            [("id1", "Pendientes para hoy"), ("id2", "Pendientes para el miércoles")]
        )
        with p1, p2:
            r = await notes_tool.append_to_note("Pendientes", "reunión")
        assert r.requires_confirmation is True
        assert "¿para cuándo?" in r.user_message
        assert "'hoy'" in r.user_message and "'el miércoles'" in r.user_message

    @pytest.mark.asyncio
    async def test_suffix_narrows_to_one(self):
        from tools import notes_tool

        p1, p2 = _patch_notes(
            [("id1", "Pendientes para hoy"), ("id2", "Pendientes para el miércoles")]
        )
        with p1, p2:
            r = await notes_tool.append_to_note("Pendientes", "reunión", suffix="miércoles")
        assert r.success and r.requires_confirmation is False
        assert "Agregado a 'Pendientes para el miércoles'" in r.user_message

    @pytest.mark.asyncio
    async def test_suffix_no_survivor_offers_create(self):
        from tools import notes_tool

        notes = [("id1", "Pendientes para hoy"), ("id2", "Pendientes para el miércoles")]
        p1, p2 = _patch_notes(notes)
        with p1, p2:
            r = await notes_tool.append_to_note("Pendientes", "regalo", suffix="jueves")
        assert r.requires_confirmation is True
        assert "Pendientes para jueves" in r.user_message

        # confirmed re-call creates it
        osa = _fake_osascript(notes)
        with (
            patch("actions.macos.osascript", new=osa),
            patch("actions.macos.osascript_or_friendly", new=AsyncMock(return_value=(True, ""))),
        ):
            r2 = await notes_tool.append_to_note(
                "Pendientes", "regalo", suffix="jueves", create_if_missing=True, confirmed=True
            )
        assert r2.success
        assert any(
            "Pendientes para jueves" in s and "<div>" in s for s in osa.captured
        )  # create ran

    @pytest.mark.asyncio
    async def test_zero_matches_offers_create_then_creates(self):
        from tools import notes_tool

        p1, p2 = _patch_notes([])  # nothing exists
        with p1, p2:
            r = await notes_tool.append_to_note("Notas de trabajo", "TODO")
        assert r.requires_confirmation is True
        assert "¿La creo nueva?" in r.user_message

        osa = _fake_osascript([])
        with (
            patch("actions.macos.osascript", new=osa),
            patch("actions.macos.osascript_or_friendly", new=AsyncMock(return_value=(True, ""))),
        ):
            r2 = await notes_tool.append_to_note(
                "Notas de trabajo", "TODO", create_if_missing=True, confirmed=True
            )
        assert r2.success
        assert any("Notas de trabajo" in s and "<div>TODO</div>" in s for s in osa.captured)
