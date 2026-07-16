"""Regression guards for the additive usage_events token-column migration.

Context: db._MIGRATIONS already ALTERs the 6 token columns onto usage_events
(idempotent via suppress(OperationalError)), applied by _ensure_schema on every
fresh connect() / init_db(). These tests lock that behavior in for both a fresh DB
and a legacy DB that predates the columns, so a future refactor can't silently break
metering. (PHASE-2-3-GAPS confirmed the columns are present — this is the guard.)
"""

from __future__ import annotations

import sqlite3

import pytest

from backend import db, metering

_TOKEN_COLS = ("input_tokens", "output_tokens", "cached_tokens", "audio_tokens", "model", "kind")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "DATABASE_URL", str(tmp_path / "mig.db"))
    db._INITIALIZED.discard(str(tmp_path / "mig.db"))
    db.init_db()
    yield


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_token_columns(tmp_db):
    conn = db.connect()
    try:
        cols = _cols(conn, "usage_events")
        for c in _TOKEN_COLS:
            assert c in cols, f"fresh DB missing {c}"
    finally:
        conn.close()


def test_migration_adds_columns_to_legacy_db(tmp_path, monkeypatch):
    """Build the OLD usage_events shape by hand (seconds-only), then run init_db and
    assert the token columns appear and the legacy row survives with defaults."""
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db.settings, "DATABASE_URL", str(path))
    db._INITIALIZED.discard(str(path))

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
        -- token_hash present so _SCHEMA's ix_device_tokens_hash index resolves; a real
        -- legacy DB had the full device_tokens shape — only usage_events was legacy.
        CREATE TABLE device_tokens (id INTEGER PRIMARY KEY, user_id INTEGER, token_hash TEXT);
        CREATE TABLE usage_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          device_id INTEGER NOT NULL,
          seconds INTEGER NOT NULL,
          created_at REAL NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users(id,email) VALUES(1,'a@b.c')")
    conn.execute("INSERT INTO device_tokens(id,user_id) VALUES(1,1)")
    conn.execute(
        "INSERT INTO usage_events(user_id,device_id,seconds,created_at) VALUES(1,1,300,1000.0)"
    )
    conn.commit()
    conn.close()

    db._INITIALIZED.discard(str(path))
    db.init_db()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        cols = _cols(conn, "usage_events")
        for c in _TOKEN_COLS:
            assert c in cols, f"migration missed {c}"
        row = conn.execute("SELECT * FROM usage_events WHERE id=1").fetchone()
        assert row["seconds"] == 300  # legacy data preserved
        assert row["input_tokens"] == 0  # NOT NULL DEFAULT 0 backfill
        assert row["kind"] == "realtime"  # DEFAULT 'realtime'
    finally:
        conn.close()


def test_record_usage_writes_tokens(tmp_db):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO users(id,email,provider,provider_id,created_at) "
            "VALUES(1,'a@b.c','local','x',1000.0)"
        )
        conn.execute(
            "INSERT INTO device_tokens(id,user_id,token_hash,device_name,created_at) "
            "VALUES(1,1,'h','Mac',1000.0)"
        )
        conn.commit()
    finally:
        conn.close()

    metering.record_usage(
        1, 1, seconds=42, input_tokens=100, output_tokens=50,
        cached_tokens=10, audio_tokens=90, model="gpt-realtime", kind="realtime",
    )

    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM usage_events ORDER BY id DESC LIMIT 1").fetchone()
        assert row["seconds"] == 42
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["cached_tokens"] == 10
        assert row["audio_tokens"] == 90
        assert row["model"] == "gpt-realtime"
        assert row["kind"] == "realtime"
        u = conn.execute(
            "SELECT monthly_seconds_used, monthly_session_count FROM users WHERE id=1"
        ).fetchone()
        assert u["monthly_seconds_used"] == 42
        assert u["monthly_session_count"] == 1
    finally:
        conn.close()
