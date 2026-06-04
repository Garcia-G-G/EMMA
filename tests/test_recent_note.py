"""B21 (19.6): "la última nota" resolves to the most recently MODIFIED note,
never to a literal title."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.notes_tool as nt

# id‖iso-modification-date‖title — the light enumeration (no previews).
LIGHT = (
    "id-old‖2026-06-01T10:00:00‖Vieja\n"
    "id-new‖2026-06-04T18:30:00‖Pendientes para hoy\n"
    "id-mid‖2026-06-03T09:00:00‖Media\n"
)


@pytest.fixture()
def _wired(monkeypatch):
    monkeypatch.setattr(nt.macos, "osascript", AsyncMock(return_value=LIGHT))
    friendly = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(nt.macos, "osascript_or_friendly", friendly)
    # find_by_title must NOT be consulted on the recent path
    monkeypatch.setattr(
        nt, "find_by_title", AsyncMock(side_effect=AssertionError("title search used"))
    )
    return friendly


class TestMostRecentNote:
    @pytest.mark.asyncio
    async def test_returns_newest_by_modification_date(self, _wired):
        m = await nt._most_recent_note()
        assert m is not None
        assert m.id == "id-new"
        assert m.title == "Pendientes para hoy"

    @pytest.mark.asyncio
    async def test_empty_library_returns_none(self, monkeypatch):
        monkeypatch.setattr(nt.macos, "osascript", AsyncMock(return_value=""))
        assert await nt._most_recent_note() is None


class TestRecentRouting:
    @pytest.mark.asyncio
    async def test_append_recent_ignores_title(self, _wired):
        friendly = _wired
        res = await nt.append_to_note(title="ignored", text="recordar pan", recent=True)
        assert res.success
        script = friendly.await_args.args[0]
        assert 'note id "id-new"' in script  # appended to the newest, not "ignored"

    @pytest.mark.asyncio
    async def test_read_recent(self, _wired, monkeypatch):
        monkeypatch.setattr(nt, "_read_body", AsyncMock(return_value="cuerpo"))
        res = await nt.read_note(title="whatever", recent=True)
        assert res.success
        assert res.data["title"] == "Pendientes para hoy"

    @pytest.mark.asyncio
    async def test_delete_recent_asks_with_newest_title(self, _wired):
        res = await nt.delete_note(title="x", recent=True)
        assert res.requires_confirmation is True
        assert "Pendientes para hoy" in res.user_message

    @pytest.mark.asyncio
    async def test_resolve_recent_note_tool(self, _wired, monkeypatch):
        monkeypatch.setattr(nt, "_read_body", AsyncMock(return_value="línea1\nlínea2"))
        res = await nt.resolve_recent_note()
        assert res.success
        assert res.data["title"] == "Pendientes para hoy"
        assert res.data["preview"]


class TestExplicitTitleStillWorks:
    @pytest.mark.asyncio
    async def test_recent_false_uses_title_search(self, monkeypatch):
        """Default path unchanged: find_by_title drives the lookup."""
        called = AsyncMock(return_value=([], "none"))
        monkeypatch.setattr(nt, "find_by_title", called)
        monkeypatch.setattr(nt.macos, "osascript", AsyncMock(return_value=""))
        res = await nt.append_to_note(title="Pendientes", text="x")
        called.assert_awaited()
        assert res.requires_confirmation is True  # offers to create
