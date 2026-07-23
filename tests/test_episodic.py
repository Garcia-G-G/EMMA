"""Part D — memory.episodic durable action log (sync cores, tmp DB)."""

from __future__ import annotations

import time
from datetime import date, datetime

import pytest

from memory import episodic as ep


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"

    class _S:
        MEMORY_DB_PATH = db

    monkeypatch.setattr(ep, "settings", _S)
    yield


def test_schema_migration_is_idempotent() -> None:
    ep._connect().close()
    ep._connect().close()  # second time must not raise
    rid = ep._record_sync("create_note", {"title": "X"}, {"ok": True}, "crea nota X", None)
    assert rid > 0


def test_record_and_recent_roundtrip() -> None:
    ep._record_sync("create_note", {"title": "Hola"}, {"id": "1"}, "crea nota Hola",
                    ep.blueprint_inverse("delete_note", {"title": "Hola"}))
    rows = ep._recent_sync(within_s=3600, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r.tool_name == "create_note"
    assert r.args["title"] == "Hola"
    assert r.reverse_kind == "inverse_call"
    assert r.reverse["tool"] == "delete_note"
    assert r.user_speech == "crea nota Hola"
    assert r.reversed_at is None


def test_secret_args_are_sanitized() -> None:
    ep._record_sync("login", {"user": "alex", "password": "hunter2", "token": "re_abc"}, None, "", None)
    r = ep._recent_sync(3600, 10)[0]
    assert "alex" in str(r.args)
    assert "hunter2" not in str(r.args)
    assert "password" not in r.args and "token" not in r.args


def test_query_by_date_filters_to_that_day() -> None:
    # Insert rows stamped on two different days by writing then back-dating.
    rid_old = ep._record_sync("delete_note", {"title": "viejo"}, None, "", None)
    rid_new = ep._record_sync("create_note", {"title": "nuevo"}, None, "", None)
    day_old = datetime(2026, 6, 8, 10, 0).timestamp()
    conn = ep._connect()
    conn.execute("UPDATE actions SET ts=? WHERE id=?", (day_old, rid_old))
    conn.commit()
    conn.close()
    rows = ep._query_by_date_sync(date(2026, 6, 8), limit=20)
    assert [r.id for r in rows] == [rid_old]
    today_rows = ep._query_by_date_sync(date.fromtimestamp(time.time()), 20)
    assert rid_new in [r.id for r in today_rows]


def test_last_undoable_skips_noop_and_reversed() -> None:
    ep._record_sync("post_to_x", {"text": "hola"}, None, "", ep.blueprint_noop())  # noop
    rid_done = ep._record_sync("create_event", {"title": "cita"}, None, "",
                               ep.blueprint_inverse("delete_event", {"id": "9"}))
    ep._mark_reversed_sync(rid_done)  # already reversed
    rid_live = ep._record_sync("create_note", {"title": "Z"}, None, "",
                               ep.blueprint_inverse("delete_note", {"title": "Z"}))
    r = ep._last_undoable_sync()
    assert r is not None and r.id == rid_live  # newest non-noop, not-yet-reversed


def test_mark_reversed_sets_timestamp() -> None:
    rid = ep._record_sync("create_note", {"title": "Q"}, None, "",
                          ep.blueprint_inverse("delete_note", {"title": "Q"}))
    ep._mark_reversed_sync(rid)
    assert ep._get_sync(rid).reversed_at is not None
    assert ep._last_undoable_sync() is None  # nothing left to undo


def test_oversized_blueprint_downgrades_to_manual() -> None:
    huge = "x" * (20 * 1024)  # 20 KB > 16 KB row cap
    rid = ep._record_sync("edit_file_replace", {"path": "/big.txt"}, None, "",
                          ep.blueprint_restore_text("/big.txt", huge))
    r = ep._get_sync(rid)
    assert r.reverse_kind == "manual"
    assert "Time Machine" in r.reverse["hint"]
    assert huge not in str(r.reverse)  # the blob was dropped


def test_result_json_capped() -> None:
    big_result = {"output": "y" * (8 * 1024)}  # > 4 KB result cap
    rid = ep._record_sync("search_github", {"q": "x"}, big_result, "", None)
    r = ep._get_sync(rid)
    assert r.result == {"_truncated": True}
