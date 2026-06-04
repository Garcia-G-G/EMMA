"""B16 (19.6): voice-driven file editing at the filesystem layer.

Covers the four edit tools' mutations, newline normalization, count-limited
search&replace, the HOME-only path guard, the two-phase confirmation flow,
the IDE refresh hook, and a registry-dispatch smoke.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.file_edit as fe
from tools.base import ToolResult


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    """Pretend $HOME is tmp_path and stub the IDE refresh."""
    monkeypatch.setattr(fe, "_home", lambda: tmp_path)
    monkeypatch.setattr(
        fe, "open_in_ide", AsyncMock(return_value=ToolResult(True, {}, "ok", False))
    )
    return tmp_path


def _mk(tmp_path, name: str, content: str):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestAppend:
    @pytest.mark.asyncio
    async def test_appends_with_newline_normalization(self, tmp_path):
        p = _mk(tmp_path, "a.txt", "uno")  # no trailing newline
        res = await fe.edit_file_append(str(p), "dos", confirmed=True)
        assert res.success
        assert p.read_text() == "uno\ndos\n"

    @pytest.mark.asyncio
    async def test_newline_handling_is_idempotent(self, tmp_path):
        """Appending to an already-\\n-terminated file must not double blank lines."""
        p = _mk(tmp_path, "a.txt", "uno\n")
        await fe.edit_file_append(str(p), "dos", confirmed=True)
        assert p.read_text() == "uno\ndos\n"

    @pytest.mark.asyncio
    async def test_unconfirmed_asks_and_does_not_touch_file(self, tmp_path):
        p = _mk(tmp_path, "a.txt", "uno\n")
        res = await fe.edit_file_append(str(p), "dos")
        assert res.requires_confirmation is True
        assert p.read_text() == "uno\n"  # untouched

    @pytest.mark.asyncio
    async def test_missing_file_friendly_error(self, tmp_path):
        res = await fe.edit_file_append(str(tmp_path / "nope.txt"), "x", confirmed=True)
        assert res.success is False
        assert "No encontré" in res.user_message


class TestPrepend:
    @pytest.mark.asyncio
    async def test_prepends(self, tmp_path):
        p = _mk(tmp_path, "b.txt", "cuerpo\n")
        res = await fe.edit_file_prepend(str(p), "encabezado", confirmed=True)
        assert res.success
        assert p.read_text() == "encabezado\ncuerpo\n"


class TestReplace:
    @pytest.mark.asyncio
    async def test_overwrites_entire_file_only_when_confirmed(self, tmp_path):
        p = _mk(tmp_path, "c.txt", "viejo\n")
        res = await fe.edit_file_replace(str(p), "nuevo\n")
        assert res.requires_confirmation is True
        assert p.read_text() == "viejo\n"
        res = await fe.edit_file_replace(str(p), "nuevo\n", confirmed=True)
        assert res.success
        assert p.read_text() == "nuevo\n"


class TestSearchReplace:
    @pytest.mark.asyncio
    async def test_count_limits_replacements(self, tmp_path):
        p = _mk(tmp_path, "d.txt", "foo foo foo foo foo\n")
        res = await fe.edit_file_search_replace(str(p), "foo", "bar", count=2, confirmed=True)
        assert res.success
        assert p.read_text() == "bar bar foo foo foo\n"

    @pytest.mark.asyncio
    async def test_count_minus_one_replaces_all(self, tmp_path):
        p = _mk(tmp_path, "d.txt", "foo foo foo\n")
        await fe.edit_file_search_replace(str(p), "foo", "bar", count=-1, confirmed=True)
        assert p.read_text() == "bar bar bar\n"

    @pytest.mark.asyncio
    async def test_literal_not_regex(self, tmp_path):
        p = _mk(tmp_path, "e.txt", "a.c abc\n")
        await fe.edit_file_search_replace(str(p), "a.c", "X", count=-1, confirmed=True)
        assert p.read_text() == "X abc\n"  # '.' did not match 'b'

    @pytest.mark.asyncio
    async def test_search_not_found(self, tmp_path):
        p = _mk(tmp_path, "f.txt", "hola\n")
        res = await fe.edit_file_search_replace(str(p), "xyz", "abc", confirmed=True)
        assert res.success is False
        assert "xyz" in res.user_message
        assert p.read_text() == "hola\n"


class TestPathGuard:
    @pytest.mark.asyncio
    async def test_rejects_traversal_outside_home(self, tmp_path):
        res = await fe.edit_file_append(
            str(tmp_path / ".." / "etc" / "passwd"), "x", confirmed=True
        )
        assert res.success is False

    @pytest.mark.asyncio
    async def test_rejects_absolute_outside_home(self):
        res = await fe.edit_file_append("/etc/hosts", "x", confirmed=True)
        assert res.success is False


class TestIdeRefresh:
    @pytest.mark.asyncio
    async def test_successful_edit_opens_file_in_ide_once(self, tmp_path):
        p = _mk(tmp_path, "g.py", "x = 1\n")
        await fe.edit_file_append(str(p), "pass", confirmed=True)
        fe.open_in_ide.assert_awaited_once_with(str(p.resolve()))

    @pytest.mark.asyncio
    async def test_unconfirmed_does_not_open_ide(self, tmp_path):
        p = _mk(tmp_path, "g.py", "x = 1\n")
        await fe.edit_file_append(str(p), "pass")
        fe.open_in_ide.assert_not_awaited()


class TestDiffData:
    @pytest.mark.asyncio
    async def test_result_carries_unified_snippet_and_delta(self, tmp_path):
        p = _mk(tmp_path, "h.txt", "uno\n")
        res = await fe.edit_file_append(str(p), "dos\ntres", confirmed=True)
        assert "unified" in res.data
        assert "+dos" in res.data["unified"]
        assert res.data["lines_added"] == 2


class TestDispatch:
    @pytest.mark.asyncio
    async def test_smoke_through_registry(self, tmp_path):
        from tools import registry

        p = _mk(tmp_path, "i.txt", "uno\n")
        res = await registry.dispatch(
            "edit_file_append", {"path": str(p), "text": "dos", "confirmed": True}
        )
        assert res.success
        assert p.read_text() == "uno\ndos\n"
