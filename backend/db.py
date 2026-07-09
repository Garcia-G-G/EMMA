"""SQLite store for users + sessions + demo rate-limiting (Prompt 31, B3 / A3).

Single-file SQLite (DATABASE_URL); fine for the free tier, swap for Postgres at
scale. All money/usage accounting lives here so the cost guard + per-plan caps read
one source of truth.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
import uuid
from typing import Any

from backend.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  name TEXT,
  provider TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  plan TEXT DEFAULT 'free',
  stripe_customer_id TEXT,
  created_at REAL NOT NULL,
  monthly_session_count INTEGER DEFAULT 0,
  monthly_seconds_used REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  started_at REAL NOT NULL,
  ended_at REAL,
  seconds_used REAL DEFAULT 0,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS demo_hits (
  ip TEXT NOT NULL,
  ts REAL NOT NULL
);
-- PAIR-DEVICE-1: RFC 8628 device authorization grant. device_code + access_token
-- are stored ONLY as sha256 hashes; the real values live on the daemon (Keychain).
CREATE TABLE IF NOT EXISTS device_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_code TEXT UNIQUE NOT NULL,
  device_code_hash TEXT UNIQUE NOT NULL,
  user_id INTEGER REFERENCES users(id),
  authorized INTEGER NOT NULL DEFAULT 0,
  device_name TEXT,
  expires_at REAL NOT NULL,
  last_polled_at REAL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_device_codes_user_code ON device_codes(user_code);
CREATE INDEX IF NOT EXISTS ix_device_codes_hash ON device_codes(device_code_hash);
CREATE TABLE IF NOT EXISTS device_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT UNIQUE NOT NULL,
  device_name TEXT NOT NULL,
  last_seen_at REAL,
  last_ip TEXT,
  created_at REAL NOT NULL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS ix_device_tokens_user ON device_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_device_tokens_hash ON device_tokens(token_hash);

-- CLIENT-INSTALL-PIPELINE Phase 1: per-session metering for managed voice.
-- One row per proxied realtime session; `seconds` is server-measured wall time.
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  device_id INTEGER NOT NULL REFERENCES device_tokens(id),
  seconds INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_events_user ON usage_events(user_id);
CREATE INDEX IF NOT EXISTS ix_usage_events_created ON usage_events(created_at);

-- ABUSE-PROTECTION-2: per-user flags (kill switch + anomaly state).
CREATE TABLE IF NOT EXISTS user_flags (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  disabled INTEGER NOT NULL DEFAULT 0,
  disabled_reason TEXT,
  disabled_at REAL,
  anomaly_score REAL NOT NULL DEFAULT 0.0,
  last_anomaly_at REAL,
  throttle_until REAL
);

-- Append-only, hash-chained audit of every disable/enable/throttle. prev_hash +
-- row_hash form a tamper-evident ledger; a broken chain proves tampering.
-- NEVER DELETE or UPDATE rows here.
CREATE TABLE IF NOT EXISTS user_status_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  action TEXT NOT NULL,        -- disable | enable | throttle | unthrottle | anomaly_flag
  actor_id INTEGER,            -- NULL = system/anomaly
  reason TEXT,
  ip TEXT,
  ua TEXT,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_user_status_events_user ON user_status_events(user_id);
CREATE INDEX IF NOT EXISTS ix_user_status_events_created ON user_status_events(created_at);
"""


# LANDING-27: columns added to the existing users table. ALTER is idempotent via
# the try/except (SQLite raises if the column already exists) — safe on every connect.
_MIGRATIONS = (
    "ALTER TABLE users ADD COLUMN password_hash TEXT",
    "ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN reset_token TEXT",
    "ALTER TABLE users ADD COLUMN reset_expires REAL",
    "ALTER TABLE users ADD COLUMN deleted_at REAL",
    "ALTER TABLE users ADD COLUMN stripe_subscription_item_id TEXT",  # Phase 5 metered billing
    # CLIENT-INSTALL Phase 2A: token-level metering on usage_events (HTTP + realtime).
    "ALTER TABLE usage_events ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN audio_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_events ADD COLUMN model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE usage_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'realtime'",
)


# Paths whose schema + migrations have already run this process. Re-running the
# full `executescript` + 5 ALTERs on EVERY query was a real lock-contention + perf
# cost on the hot demo path; do it once per DB file instead. (Tests use a fresh
# tmp path each, so each still gets initialized exactly once.)
_INITIALIZED: set[str] = set()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        with contextlib.suppress(sqlite3.OperationalError):  # column already exists
            conn.execute(stmt)
    conn.commit()
    _INITIALIZED.add(settings.DATABASE_URL)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DATABASE_URL, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if settings.DATABASE_URL not in _INITIALIZED:
        _ensure_schema(conn)
    return conn


def init_db() -> None:
    # Force (re)creation for this path — also covers a test that points at a fresh DB.
    conn = sqlite3.connect(settings.DATABASE_URL, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        _ensure_schema(conn)
    finally:
        conn.close()


# ---- users ------------------------------------------------------------------


def upsert_user(email: str, name: str, provider: str, provider_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(email,name,provider,provider_id,created_at) VALUES(?,?,?,?,?)",
                (email, name, provider, provider_id, time.time()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_user(user_id: int) -> dict[str, Any] | None:
    # `AND deleted_at IS NULL` is load-bearing: the cookie session resolves the user
    # BY ID here, so without this filter a soft-deleted account keeps authenticating
    # (downloads, demo minutes, billing) until its 30-day cookie expires.
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_plan(user_id: int, plan: str, stripe_customer_id: str | None = None) -> None:
    conn = connect()
    try:
        if stripe_customer_id:
            conn.execute("UPDATE users SET plan=?, stripe_customer_id=? WHERE id=?",
                         (plan, stripe_customer_id, user_id))
        else:
            conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        conn.commit()
    finally:
        conn.close()


def set_plan_by_customer(stripe_customer_id: str, plan: str) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE users SET plan=? WHERE stripe_customer_id=?", (plan, stripe_customer_id))
        conn.commit()
    finally:
        conn.close()


# ---- email/password accounts (LANDING-27) -----------------------------------


def get_user_by_email(email: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email)=lower(?) AND deleted_at IS NULL", (email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_local_user(email: str, password_hash: str, name: str = "") -> dict[str, Any] | None:
    """Create an email/password user. Returns the row, or None if the email exists."""
    conn = connect()
    try:
        try:
            conn.execute(
                "INSERT INTO users(email,name,provider,provider_id,password_hash,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (email, name, "local", "", password_hash, time.time()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return None  # UNIQUE(email) — already registered
        row = conn.execute("SELECT * FROM users WHERE lower(email)=lower(?)", (email,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_password(user_id: int, password_hash: str) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE users SET password_hash=?, reset_token=NULL, reset_expires=NULL "
                     "WHERE id=?", (password_hash, user_id))
        conn.commit()
    finally:
        conn.close()


def set_reset_token(email: str, token: str, expires: float) -> bool:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE users SET reset_token=?, reset_expires=? WHERE lower(email)=lower(?) "
            "AND deleted_at IS NULL", (token, expires, email))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def user_by_reset_token(token: str, now: float | None = None) -> dict[str, Any] | None:
    now = now or time.time()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE reset_token=? AND reset_expires > ? AND deleted_at IS NULL",
            (token, now),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_email(user_id: int, email: str) -> bool:
    conn = connect()
    try:
        try:
            conn.execute("UPDATE users SET email=?, email_verified=0 WHERE id=?", (email, user_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # taken
    finally:
        conn.close()


def soft_delete_user(user_id: int) -> None:
    """GDPR soft delete — anonymize email + stamp deleted_at (hard purge is a later job)."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE users SET deleted_at=?, password_hash=NULL, reset_token=NULL, "
            "email=? WHERE id=?", (time.time(), f"deleted-{user_id}@removed.invalid", user_id))
        conn.commit()
    finally:
        conn.close()


def user_seconds_today(user_id: int, now: float | None = None) -> float:
    """Seconds of demo this user spent in the last 24h — for the per-plan daily cap."""
    now = now or time.time()
    conn = connect()
    try:
        v = conn.execute(
            "SELECT COALESCE(SUM(seconds_used),0) FROM sessions WHERE user_id=? AND started_at > ?",
            (user_id, now - 86400),
        ).fetchone()[0]
        return float(v or 0.0)
    finally:
        conn.close()


def user_seconds_month(user_id: int, now: float | None = None) -> float:
    """Seconds spent in the last 30 days — the per-plan MONTHLY cap, windowed.

    The `users.monthly_seconds_used` counter is lifetime-cumulative and never resets,
    so reading it as month-to-date turned the monthly cap into a permanent lockout.
    We compute the window from `sessions` instead (same approach as the daily cap)."""
    now = now or time.time()
    conn = connect()
    try:
        v = conn.execute(
            "SELECT COALESCE(SUM(seconds_used),0) FROM sessions WHERE user_id=? AND started_at > ?",
            (user_id, now - 2592000),
        ).fetchone()[0]
        return float(v or 0.0)
    finally:
        conn.close()


# ---- sessions ---------------------------------------------------------------


def create_session(user_id: int | None) -> str:
    sid = uuid.uuid4().hex
    conn = connect()
    try:
        conn.execute("INSERT INTO sessions(id,user_id,started_at) VALUES(?,?,?)",
                     (sid, user_id, time.time()))
        conn.commit()
        return sid
    finally:
        conn.close()


def end_session(sid: str, seconds: float, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    conn = connect()
    try:
        # Idempotent: both the demo WS and the realtime proxy call this in a finally,
        # and a reconnect can fire it twice for one sid. Only the FIRST close (ended_at
        # still NULL) bills the user's running counters — a re-entrant close no-ops.
        cur = conn.execute(
            "UPDATE sessions SET ended_at=?, seconds_used=?, tokens_in=?, tokens_out=?, cost_usd=? "
            "WHERE id=? AND ended_at IS NULL",
            (time.time(), seconds, tokens_in, tokens_out, round(cost_usd, 4), sid),
        )
        if cur.rowcount:
            row = conn.execute("SELECT user_id FROM sessions WHERE id=?", (sid,)).fetchone()
            if row and row["user_id"]:
                conn.execute(
                    "UPDATE users SET monthly_session_count=monthly_session_count+1, "
                    "monthly_seconds_used=monthly_seconds_used+? WHERE id=?",
                    (seconds, row["user_id"]),
                )
        conn.commit()
    finally:
        conn.close()


def recent_sessions(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- rate limit + budget ----------------------------------------------------


def demo_count_24h(ip: str, now: float | None = None) -> int:
    now = now or time.time()
    conn = connect()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM demo_hits WHERE ip=? AND ts > ?", (ip, now - 86400)
        ).fetchone()[0])
    finally:
        conn.close()


def record_demo_hit(ip: str) -> None:
    conn = connect()
    try:
        conn.execute("INSERT INTO demo_hits(ip,ts) VALUES(?,?)", (ip, time.time()))
        conn.commit()
    finally:
        conn.close()


def user_sessions_today(user_id: int, now: float | None = None) -> int:
    now = now or time.time()
    conn = connect()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=? AND started_at > ?", (user_id, now - 86400)
        ).fetchone()[0])
    finally:
        conn.close()


def month_cost_usd(now: float | None = None) -> float:
    now = now or time.time()
    conn = connect()
    try:
        v = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM sessions WHERE started_at > ?", (now - 2592000,)
        ).fetchone()[0]
        return float(v or 0.0)
    finally:
        conn.close()


def day_cost_usd(now: float | None = None) -> float:
    """Total session cost in the last 24h — the demo's daily-ceiling brake (24.7-B2).

    The per-IP/24h limit caps individual abusers; this caps the WALLET regardless
    of how many IPs (VPN rotation) attack the demo in one day."""
    now = now or time.time()
    conn = connect()
    try:
        v = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM sessions WHERE started_at > ?", (now - 86400,)
        ).fetchone()[0]
        return float(v or 0.0)
    finally:
        conn.close()


def day_session_stats(now: float | None = None) -> dict[str, float]:
    """(sessions, cost_usd) in the last 24h — for the daily ops report (24.7-E2)."""
    now = now or time.time()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM sessions WHERE started_at > ?",
            (now - 86400,),
        ).fetchone()
        return {"sessions": int(row[0] or 0), "cost_usd": float(row[1] or 0.0)}
    finally:
        conn.close()


# ---- ABUSE-PROTECTION-2: user flags (kill switch + anomaly state) -----------


def get_user_flags(user_id: int) -> dict[str, Any] | None:
    """Return the flags row for a user, or None if none exists yet."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM user_flags WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ensure_user_flags(user_id: int) -> None:
    """Create an empty flags row if absent. Idempotent."""
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO user_flags(user_id, disabled, anomaly_score) VALUES(?, 0, 0.0)",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_disabled(user_id: int, reason: str) -> None:
    """Mark a user disabled (admin kill switch). Idempotent + atomic — a single
    upsert, so there's no ensure-then-update window where the flag could be lost."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO user_flags(user_id, disabled, disabled_reason, disabled_at, anomaly_score) "
            "VALUES(?, 1, ?, ?, 0.0) "
            "ON CONFLICT(user_id) DO UPDATE SET disabled=1, "
            "disabled_reason=excluded.disabled_reason, disabled_at=excluded.disabled_at",
            (user_id, reason, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_user_disabled(user_id: int) -> None:
    """Re-enable a disabled user. Idempotent."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE user_flags SET disabled=0, disabled_reason=NULL, disabled_at=NULL WHERE user_id=?",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_throttle(user_id: int, until_ts: float) -> None:
    """Throttle a user until ``until_ts`` (anomaly response). Atomic upsert."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO user_flags(user_id, disabled, anomaly_score, throttle_until, last_anomaly_at) "
            "VALUES(?, 0, 0.0, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "throttle_until=excluded.throttle_until, last_anomaly_at=excluded.last_anomaly_at",
            (user_id, until_ts, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def update_anomaly_score(user_id: int, ema_new: float) -> None:
    """Persist an absolute anomaly score. Atomic upsert. (For the rolling fold used
    on the hot path, prefer :func:`bump_anomaly_score`, which is race-free.)"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO user_flags(user_id, disabled, anomaly_score) VALUES(?, 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET anomaly_score=excluded.anomaly_score",
            (user_id, ema_new),
        )
        conn.commit()
    finally:
        conn.close()


def bump_anomaly_score(user_id: int, z: float, alpha: float = 0.4) -> float:
    """Atomically fold ``z`` into the rolling EMA (``(1-alpha)*old + alpha*z``) and
    return the NEW score. ``BEGIN IMMEDIATE`` serializes concurrent session-closes for
    the same user, so no contribution is lost to a last-writer-wins race — which is
    exactly the many-sessions-at-once pattern the anomaly detector must catch."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO user_flags(user_id, disabled, anomaly_score) VALUES(?, 0, 0.0)",
            (user_id,),
        )
        conn.execute(
            "UPDATE user_flags SET anomaly_score = (1 - ?) * anomaly_score + ? * ? WHERE user_id=?",
            (alpha, alpha, z, user_id),
        )
        row = conn.execute(
            "SELECT anomaly_score FROM user_flags WHERE user_id=?", (user_id,)
        ).fetchone()
        conn.commit()
        return float(row["anomaly_score"])
    finally:
        conn.close()


# ---- ABUSE-PROTECTION-2: append-only hash-chained audit trail ---------------


def append_status_event(
    user_id: int,
    action: str,
    actor_id: int | None = None,
    reason: str = "",
    ip: str | None = None,
    ua: str | None = None,
) -> str:
    """Append an audit event with a SHA-256 hash chain. Returns the new row_hash.

    ``BEGIN IMMEDIATE`` takes a write lock so two concurrent appends can't read the
    same prev_hash and fork the chain.
    """
    import hashlib
    import json

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")  # write lock — serializes appends
        prev_row = conn.execute(
            "SELECT row_hash FROM user_status_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = prev_row["row_hash"] if prev_row else "0" * 64
        ts = time.time()
        payload_obj = {
            "user_id": user_id, "action": action, "actor_id": actor_id,
            "reason": reason, "ip": ip, "ua": ua, "ts": ts,
        }
        payload = json.dumps(payload_obj, sort_keys=True)
        row_hash = hashlib.sha256((prev + payload).encode()).hexdigest()
        conn.execute(
            "INSERT INTO user_status_events "
            "(user_id, action, actor_id, reason, ip, ua, prev_hash, row_hash, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, action, actor_id, reason, ip, ua, prev, row_hash, ts),
        )
        conn.commit()
        return row_hash
    finally:
        conn.close()


def status_events_for_user(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM user_status_events WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_audit_chain() -> tuple[bool, int | None]:
    """Walk the whole audit chain and verify every hash. Returns (True, None) if
    intact, else (False, first_broken_id). O(N) — run offline, not on a request."""
    import hashlib
    import json

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM user_status_events ORDER BY id ASC"
        ).fetchall()
        prev = "0" * 64
        for r in rows:
            payload_obj = {
                "user_id": r["user_id"], "action": r["action"],
                "actor_id": r["actor_id"], "reason": r["reason"],
                "ip": r["ip"], "ua": r["ua"], "ts": r["created_at"],
            }
            payload = json.dumps(payload_obj, sort_keys=True)
            expected = hashlib.sha256((prev + payload).encode()).hexdigest()
            if r["prev_hash"] != prev or r["row_hash"] != expected:
                return False, int(r["id"])
            prev = r["row_hash"]
        return True, None
    finally:
        conn.close()


# ---- ABUSE-PROTECTION-2: usage aggregates for daily cap + anomaly baseline ---


def seconds_used_today(user_id: int) -> int:
    """Sum of usage_events.seconds since midnight UTC (Capa 4 daily cap)."""
    import datetime

    start_of_day = (
        datetime.datetime.now(datetime.UTC)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) AS s FROM usage_events "
            "WHERE user_id=? AND created_at >= ?",
            (user_id, start_of_day),
        ).fetchone()
        return int(row["s"] or 0)
    finally:
        conn.close()


def recent_session_seconds(user_id: int, limit: int = 14) -> list[int]:
    """Last N non-zero session lengths — the anomaly z-score baseline."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT seconds FROM usage_events WHERE user_id=? AND seconds > 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [int(r["seconds"]) for r in rows]
    finally:
        conn.close()


def revoke_all_device_tokens_for_user(user_id: int) -> int:
    """Mark all of a user's active device tokens revoked. Returns the count.
    (device_tokens is otherwise owned by device_pairing.py; this bulk revoke lives
    here because the kill switch needs it alongside the other db.py helpers.)"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE device_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (time.time(), user_id),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()
