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


# ---- 28.1: calendar / reminders / codex blueprint coverage ------------------

from pathlib import Path  # noqa: E402

from tools.disambiguation import FIELD_SEP  # noqa: E402


@pytest.mark.asyncio
async def test_calendar_create_event_inverse_blueprint_shape(monkeypatch) -> None:
    import tools.calendar_tool as ct

    monkeypatch.setattr(ct.macos, "osascript", AsyncMock(return_value=""))
    res = await ct.create_event("TEST 28.1", "2026-06-16T15:00:00", confirmed=True)
    bp = res.data["_reverse_blueprint"]
    assert bp["kind"] == "inverse_call" and bp["tool"] == "delete_event"
    assert bp["args"]["title"] == "TEST 28.1"


@pytest.mark.asyncio
async def test_calendar_delete_event_snapshot_blueprint_shape(monkeypatch) -> None:
    import tools.calendar_tool as ct

    enum = f"uid1{FIELD_SEP}2026-06-16T15:00:00{FIELD_SEP}TEST{FIELD_SEP}\n"
    monkeypatch.setattr(ct.macos, "osascript", AsyncMock(return_value=enum))
    monkeypatch.setattr(ct, "_read_event_snapshot",
                        AsyncMock(return_value={"start_iso": "2026-06-16T15:00:00", "duration_min": 60, "location": "Office"}))
    monkeypatch.setattr(ct.macos, "osascript_or_friendly", AsyncMock(return_value=(True, "")))
    res = await ct.delete_event("TEST", confirmed=True)
    bp = res.data["_reverse_blueprint"]
    assert bp["tool"] == "create_event" and bp["args"]["location"] == "Office" and bp["args"]["duration_min"] == 60


@pytest.mark.asyncio
async def test_reminders_complete_blueprint_shape(monkeypatch) -> None:
    import tools.reminders_tool as rt

    enum = f"rid1{FIELD_SEP}2026-06-16{FIELD_SEP}TEST{FIELD_SEP}\n"
    monkeypatch.setattr(rt.macos, "osascript", AsyncMock(return_value=enum))
    monkeypatch.setattr(rt.macos, "osascript_or_friendly", AsyncMock(return_value=(True, "")))
    res = await rt.complete_reminder("TEST", confirmed=True)
    bp = res.data["_reverse_blueprint"]
    assert bp["tool"] == "uncomplete_reminder" and bp["args"]["title"] == "TEST"


@pytest.mark.asyncio
async def test_reminders_create_delete_blueprint_shapes(monkeypatch) -> None:
    import tools.reminders_tool as rt

    monkeypatch.setattr(rt.macos, "osascript", AsyncMock(return_value=""))
    res_c = await rt.add_reminder("TEST", confirmed=True)
    assert res_c.data["_reverse_blueprint"]["tool"] == "delete_reminder"

    enum = f"rid1{FIELD_SEP}2026-06-16{FIELD_SEP}TEST{FIELD_SEP}\n"
    monkeypatch.setattr(rt.macos, "osascript", AsyncMock(return_value=enum))
    monkeypatch.setattr(rt.macos, "osascript_or_friendly", AsyncMock(return_value=(True, "")))
    res_d = await rt.delete_reminder("TEST", confirmed=True)
    bp = res_d.data["_reverse_blueprint"]
    assert bp["tool"] == "add_reminder" and bp["args"]["due_iso"] == "2026-06-16"


@pytest.mark.asyncio
async def test_codex_delegate_manual_blueprint_shape(monkeypatch) -> None:
    import tools.codex_tool as cx

    monkeypatch.setattr(cx.settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(cx, "setup_worktree", AsyncMock(return_value=Path("/tmp/emma-wt-feat")))

    class _Reg:
        def at_capacity(self):
            return False
        start = AsyncMock(return_value=type("Rec", (), {"id": "t1"})())

    monkeypatch.setattr(cx, "registry", lambda: _Reg())
    res = await cx.delegate_to_codex("añade un docstring", branch="feat", confirmed=True)
    bp = res.data["_reverse_blueprint"]
    assert bp["kind"] == "manual"
    assert "branch -D feat" in bp["hint"] and "worktree remove" in bp["hint"]
