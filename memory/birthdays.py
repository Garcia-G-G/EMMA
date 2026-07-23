"""Local birthday store (Prompt 38-D) — ``~/.emma/birthdays.db``.

A simple month/day table the user populates by voice ("guarda que el cumpleaños
de X es Y"). Read by the birthday voice tools and the proactive morning alert.
Year is intentionally not stored — a birthday recurs every year.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from config.settings import settings

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS birthdays "
    "(name TEXT PRIMARY KEY, month INTEGER NOT NULL, day INTEGER NOT NULL)"
)


def _connect() -> sqlite3.Connection:
    path = Path(settings.EMMA_HOME).expanduser() / "birthdays.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def remember(name: str, month: int, day: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO birthdays (name, month, day) VALUES (?, ?, ?)",
            (name.strip(), int(month), int(day)),
        )
        conn.commit()
    finally:
        conn.close()


def today() -> list[str]:
    t = dt.date.today()
    conn = _connect()
    try:
        return [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM birthdays WHERE month=? AND day=?", (t.month, t.day)
            )
        ]
    finally:
        conn.close()


def this_week() -> list[tuple[str, int, int]]:
    """(name, month, day) for everyone whose birthday falls in the next 7 days."""
    today_d = dt.date.today()
    upcoming = {((today_d + dt.timedelta(days=i)).month, (today_d + dt.timedelta(days=i)).day)
                for i in range(7)}
    conn = _connect()
    try:
        rows = conn.execute("SELECT name, month, day FROM birthdays").fetchall()
    finally:
        conn.close()
    return [(r["name"], r["month"], r["day"]) for r in rows if (r["month"], r["day"]) in upcoming]
