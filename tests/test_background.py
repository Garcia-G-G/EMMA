"""Tests for the background-task registry (Prompt 15.12).

Each test uses its own temp tasks.jsonl via _Registry(db_path=...), and
_notify_macos is mocked so no real Notification Center banners fire.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from core import background


@pytest.fixture(autouse=True)
def _no_notify():
    with patch("core.background._notify_macos", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _reg(tmp_path):
    return background._Registry(db_path=tmp_path / "tasks.jsonl")


def test_roundtrip_completed_and_persisted(tmp_path):
    reg = _reg(tmp_path)

    async def noop(ctrl):
        ctrl.append_output("done\n")
        return 0

    async def run():
        rec = await reg.start("rt", "shell", noop)
        return await reg.wait(rec.id, timeout_s=3)

    done = asyncio.run(run())
    assert done.status == "completed"
    assert done.exit_code == 0
    assert "done" in done.last_output

    lines = (tmp_path / "tasks.jsonl").read_text().splitlines()
    assert len(lines) >= 2  # start row + complete row
    assert json.loads(lines[-1])["status"] == "completed"


def test_cancel_marks_cancelled(tmp_path):
    reg = _reg(tmp_path)

    async def sleeper(ctrl):
        await asyncio.sleep(30)
        return 0

    async def run():
        rec = await reg.start("sleep30", "shell", sleeper)
        await asyncio.sleep(0.1)
        ok = await reg.cancel(rec.id)
        await reg.wait(rec.id, timeout_s=3)
        return ok, reg.get(rec.id)

    ok, rec = asyncio.run(run())
    assert ok is True
    assert rec.status == "cancelled"


def test_output_buffer_truncates_at_8kb(tmp_path):
    reg = _reg(tmp_path)
    big = "x" * 200

    async def spammer(ctrl):
        for _ in range(200):  # 40KB raw appended
            ctrl.append_output(big)
        return 0

    async def run():
        rec = await reg.start("spam", "shell", spammer)
        return await reg.wait(rec.id, timeout_s=3)

    done = asyncio.run(run())
    assert len(done.last_output) <= 8192


def test_list_filter_by_status(tmp_path):
    reg = _reg(tmp_path)

    async def ok(ctrl):
        return 0

    async def fail(ctrl):
        return 1

    async def cancelme(ctrl):
        await asyncio.sleep(30)
        return 0

    async def run():
        r1 = await reg.start("ok", "shell", ok)
        r2 = await reg.start("fail", "shell", fail)
        r3 = await reg.start("cancelme", "shell", cancelme)
        await asyncio.sleep(0.1)
        await reg.cancel(r3.id)
        for r in (r1, r2, r3):
            await reg.wait(r.id, timeout_s=3)
        return reg

    reg2 = asyncio.run(run())
    assert len(reg2.list(status="completed")) == 1
    assert len(reg2.list(status="failed")) == 1
    assert len(reg2.list(status="cancelled")) == 1


def test_persistence_marks_inflight_aborted_after_restart(tmp_path):
    db = tmp_path / "tasks.jsonl"
    # Simulate a daemon that crashed mid-task: a "running" row on disk.
    db.write_text(
        json.dumps(
            {
                "id": "ghost1",
                "name": "ghost",
                "kind": "shell",
                "started_at": time.time(),
                "status": "running",
            }
        )
        + "\n"
    )
    # A fresh registry (= daemon restart) must reclassify it.
    reg = background._Registry(db_path=db)
    rec = reg.get("ghost")
    assert rec is not None
    assert rec.status == "aborted"
    assert "restart" in rec.error


def test_completed_task_frees_handle_and_buffer(tmp_path):
    reg = _reg(tmp_path)

    async def noop(ctrl):
        ctrl.append_output("hi\n")
        return 0

    async def run():
        rec = await reg.start("rt", "shell", noop)
        await reg.wait(rec.id, timeout_s=3)
        return rec.id

    task_id = asyncio.run(run())
    # the live handle + output buffer must not pin memory after completion
    assert task_id not in reg._handles
    assert task_id not in reg._output_bufs
    # but the record (with its final output) is retained
    assert reg.get(task_id).last_output.strip() == "hi"


def test_load_compacts_duplicate_rows(tmp_path):
    db = tmp_path / "tasks.jsonl"
    # Append-only history: the same task id written 3 times (start + 2 updates).
    rows = [
        {"id": "t1", "name": "x", "kind": "shell", "started_at": 100.0, "status": "running"},
        {"id": "t1", "name": "x", "kind": "shell", "started_at": 100.0, "status": "running"},
        {"id": "t1", "name": "x", "kind": "shell", "started_at": 100.0, "status": "completed"},
    ]
    db.write_text("".join(json.dumps(r) + "\n" for r in rows))
    reg = background._Registry(db_path=db)
    assert len(reg._tasks) == 1
    # load rewrites the file compacted → one row per id
    assert len(db.read_text().splitlines()) == 1
