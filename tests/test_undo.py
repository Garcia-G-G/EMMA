"""Part D — voice-driven undo across the reverse-blueprint patterns."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

import tools.history_tool as ht
from memory.episodic import ActionRecord
from tools.base import ToolResult


def _rec(**kw) -> ActionRecord:
    base = dict(id=5, ts=time.time(), tool_name="create_note", args={"title": "X"}, result=None,
                user_speech="crea X", reverse_kind="inverse_call",
                reverse={"kind": "inverse_call", "tool": "delete_note", "args": {"title": "X"}},
                reversed_at=None)
    base.update(kw)
    return ActionRecord(**base)


@pytest.mark.asyncio
async def test_undo_requires_confirmation_first(monkeypatch) -> None:
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=_rec()))
    res = await ht.undo_last_action()
    assert res.requires_confirmation and not res.data.get("reversed")


@pytest.mark.asyncio
async def test_undo_inverse_call_dispatches_inverse_and_bypasses_gate(monkeypatch) -> None:
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=_rec()))
    seen = {}

    async def fake_dispatch(tool, args):
        seen.update(tool=tool, args=args)
        return ToolResult(True, None, "ok", False)

    monkeypatch.setattr(ht, "dispatch", fake_dispatch)
    marked = {}
    monkeypatch.setattr(ht.episodic, "mark_reversed", AsyncMock(side_effect=lambda i: marked.update(id=i)))
    res = await ht.undo_last_action(confirmed=True)
    assert res.success
    assert seen["tool"] == "delete_note" and seen["args"]["confirmed"] is True  # gate bypassed
    assert marked["id"] == 5


@pytest.mark.asyncio
async def test_undo_restore_text_writes_blob_back(monkeypatch, tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("NUEVO CONTENIDO")
    rec = _rec(reverse_kind="restore_text",
               reverse={"kind": "restore_text", "path": str(f), "before": "ORIGINAL"})
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=rec))
    monkeypatch.setattr(ht.episodic, "mark_reversed", AsyncMock())
    res = await ht.undo_last_action(confirmed=True)
    assert res.success
    assert f.read_text() == "ORIGINAL"


@pytest.mark.asyncio
async def test_undo_noop_explains_honestly(monkeypatch) -> None:
    rec = _rec(tool_name="post_to_x", reverse_kind="noop", reverse={"kind": "noop"})
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=rec))
    res = await ht.undo_last_action(confirmed=True)
    assert not res.success and "No puedo deshacer" in res.user_message


@pytest.mark.asyncio
async def test_undo_manual_gives_hint(monkeypatch) -> None:
    rec = _rec(reverse_kind="manual", reverse={"kind": "manual", "hint": "usa Time Machine"})
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=rec))
    res = await ht.undo_last_action(confirmed=True)
    assert not res.success and "Time Machine" in res.user_message


@pytest.mark.asyncio
async def test_undo_nothing_to_undo(monkeypatch) -> None:
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=None))
    res = await ht.undo_last_action(confirmed=True)
    assert not res.success and "nada reciente" in res.user_message


@pytest.mark.asyncio
async def test_undo_by_id_already_reversed_is_clean(monkeypatch) -> None:
    monkeypatch.setattr(ht.episodic, "get", AsyncMock(return_value=_rec(reversed_at=time.time())))
    res = await ht.undo_action_by_id(5, confirmed=True)
    assert not res.success and "ya la deshice" in res.user_message


@pytest.mark.asyncio
async def test_undo_inverse_failure_does_not_mark_reversed(monkeypatch) -> None:
    monkeypatch.setattr(ht.episodic, "last_undoable", AsyncMock(return_value=_rec()))

    async def failing_dispatch(tool, args):
        return ToolResult(False, None, "no se pudo", False)

    monkeypatch.setattr(ht, "dispatch", failing_dispatch)
    marked = AsyncMock()
    monkeypatch.setattr(ht.episodic, "mark_reversed", marked)
    res = await ht.undo_last_action(confirmed=True)
    assert not res.success
    marked.assert_not_called()  # don't mark reversed if the reverse failed
