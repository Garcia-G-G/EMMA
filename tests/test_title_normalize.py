"""B23 (19.6): 'Limitación: terminal Cursor' must not duplicate
'Limitación terminal Cursor' — titles compare punctuation/diacritics-tolerant."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.notes_tool as nt
from tools.disambiguation import Match, find_by_title, normalize_title


class TestNormalizeTitle:
    def test_colon_variant_equals_plain(self):
        assert normalize_title("Limitación: terminal Cursor") == normalize_title(
            "limitación terminal Cursor"
        )

    def test_accents_stripped_but_enye_stays(self):
        # ñ is a distinct letter in Spanish — never translate to n.
        assert normalize_title("Año Nuevo") == "año nuevo"

    def test_punctuation_and_whitespace_collapse(self):
        assert normalize_title('  "Pendientes,  hoy!"  ') == "pendientes hoy"

    def test_distinct_titles_stay_distinct(self):
        assert normalize_title("Compras") != normalize_title("Compras 2026")


class TestFindByTitleNormalizedTier:
    @pytest.mark.asyncio
    async def test_falls_back_to_normalized_equality(self):
        existing = Match(id="n1", title="Limitación terminal Cursor", when="2026-06-04T10:00:00")

        async def fake_enumerate(clause: str, limit: int):
            return [existing] if clause == "" else []  # only the catch-all sees it

        matches, strategy = await find_by_title(fake_enumerate, "Limitación: terminal Cursor")
        assert strategy == "normalized"
        assert matches == [existing]

    @pytest.mark.asyncio
    async def test_no_match_still_returns_none_strategy(self):
        async def fake_enumerate(clause: str, limit: int):
            return []

        matches, strategy = await find_by_title(fake_enumerate, "Nada")
        assert (matches, strategy) == ([], "none")


# Light enumeration output for create_note's near-duplicate guard.
LIGHT = "id-1‖2026-06-04T10:00:00‖limitación terminal Cursor\n"


class TestCreateNoteNearMatchGuard:
    @pytest.mark.asyncio
    async def test_near_match_asks_before_creating(self, monkeypatch):
        osa = AsyncMock(return_value=LIGHT)
        monkeypatch.setattr(nt.macos, "osascript", osa)
        res = await nt.create_note("Limitación: terminal Cursor", "body")
        assert res.requires_confirmation is True
        assert "limitación terminal Cursor" in res.user_message
        assert osa.await_count == 1  # only the enumeration ran — nothing created

    @pytest.mark.asyncio
    async def test_confirmed_creates_anyway(self, monkeypatch):
        osa = AsyncMock(return_value=LIGHT)
        monkeypatch.setattr(nt.macos, "osascript", osa)
        res = await nt.create_note("Limitación: terminal Cursor", "body", confirmed=True)
        assert res.success
        make_script = osa.await_args.args[0]
        assert "make new note" in make_script

    @pytest.mark.asyncio
    async def test_unique_title_creates_directly(self, monkeypatch):
        osa = AsyncMock(side_effect=["", ""])  # no near-match, then create
        monkeypatch.setattr(nt.macos, "osascript", osa)
        res = await nt.create_note("Receta de pozole", "maíz")
        assert res.success
        assert "make new note" in osa.await_args.args[0]

    @pytest.mark.asyncio
    async def test_exact_same_title_keeps_old_behavior(self, monkeypatch):
        """Same exact title → create (append_to_note disambiguation handles dups)."""
        exact = "id-1‖2026-06-04T10:00:00‖Limitación: terminal Cursor\n"
        osa = AsyncMock(side_effect=[exact, ""])
        monkeypatch.setattr(nt.macos, "osascript", osa)
        res = await nt.create_note("Limitación: terminal Cursor", "body")
        assert res.success
        assert res.requires_confirmation is False
