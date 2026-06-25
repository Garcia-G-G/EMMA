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
"""


# LANDING-27: columns added to the existing users table. ALTER is idempotent via
# the try/except (SQLite raises if the column already exists) — safe on every connect.
_MIGRATIONS = (
    "ALTER TABLE users ADD COLUMN password_hash TEXT",
    "ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN reset_token TEXT",
    "ALTER TABLE users ADD COLUMN reset_expires REAL",
    "ALTER TABLE users ADD COLUMN deleted_at REAL",
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DATABASE_URL, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        with contextlib.suppress(sqlite3.OperationalError):  # column already exists
            conn.execute(stmt)
    conn.commit()
    return conn


def init_db() -> None:
    connect().close()


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
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
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
        conn.execute(
            "UPDATE sessions SET ended_at=?, seconds_used=?, tokens_in=?, tokens_out=?, cost_usd=? WHERE id=?",
            (time.time(), seconds, tokens_in, tokens_out, round(cost_usd, 4), sid),
        )
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
