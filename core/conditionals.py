"""Conditional triggers (Prompt 32): "si X pasa, haz Y".

A conditional pairs a whitelisted *trigger* with an *action* (a tool call) and an
optional expiry. The background watcher (``watch``) polls each active trigger's
source — mail, calendar, or the clock — and on the first match dispatches the
action exactly once. Persisted to ``~/.emma/conditionals.db``.

Trigger DSL — three whitelisted forms only (no arbitrary code, by design):
  - ``email_from("ana@x.com", contains="confirmo")``  — a mail from that sender
    whose body/subject contains the text (``contains`` optional).
  - ``calendar_event("Café con Ana") created``          — an event with that title exists.
  - ``time_at("2026-06-17T09:00:00")``                  — the clock reaches that time
    (ISO, or a light natural form like "mañana 9am").

The watcher reuses the proactive-engine precedent (a polled background loop). The
tick logic (:func:`check_once`) is pure and dispatcher-injected, so it is fully
unit-testable without mail/calendar/clock side effects.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

from config.settings import settings

log = structlog.get_logger("emma.conditionals")

TriggerKind = Literal["email_from", "calendar_event", "time_at"]
Dispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS conditionals("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "trigger_dsl TEXT NOT NULL,"
    "action_tool TEXT NOT NULL,"
    "action_args TEXT NOT NULL,"
    "expires_at TEXT,"
    "created_at TEXT NOT NULL,"
    "fired_at TEXT,"
    "status TEXT NOT NULL DEFAULT 'active')"
)


def _db_path() -> Path:
    return Path(settings.EMMA_HOME).expanduser() / "conditionals.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    return conn


def _reset_for_test() -> None:
    """Drop the on-disk store (tests point EMMA_HOME at a fresh tmp dir)."""
    p = _db_path()
    if p.exists():
        p.unlink()


# ---- DSL --------------------------------------------------------------------


@dataclass
class Trigger:
    kind: TriggerKind
    params: dict[str, Any]


_RE_EMAIL = re.compile(
    r'^\s*email_from\(\s*["\']([^"\']+)["\']\s*'
    r'(?:,\s*contains\s*=\s*["\']([^"\']*)["\']\s*)?\)\s*$'
)
_RE_CAL = re.compile(r'^\s*calendar_event\(\s*["\']([^"\']+)["\']\s*\)\s*created\s*$')
_RE_TIME = re.compile(r'^\s*time_at\(\s*["\']([^"\']+)["\']\s*\)\s*$')


def _parse_when(s: str) -> dt.datetime:
    s = s.strip()
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        pass
    low = s.lower()
    explicit_day = any(w in low for w in ("mañana", "manana", "tomorrow"))
    day = dt.date.today() + (dt.timedelta(days=1) if explicit_day else dt.timedelta())
    # Require a real time-of-day (":MM" or am/pm) so "junio 17" doesn't parse 17 as
    # an hour. A bare clock time with no day → roll to tomorrow if it's already past
    # (else a "9am" set at 2pm would fire instantly on the next watcher tick).
    m = re.search(r"\b(\d{1,2})(?::(\d{2})|\s*(am|pm))", low)
    if m:
        h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        when = dt.datetime(day.year, day.month, day.day, h, mn)
        if when < dt.datetime.now() and not explicit_day:
            when += dt.timedelta(days=1)
        return when
    raise ValueError(f"no entiendo la hora: {s!r}")


def parse_trigger(dsl: str) -> Trigger:
    """Parse a whitelisted trigger DSL string. Raises ValueError on anything else."""
    m = _RE_EMAIL.match(dsl)
    if m:
        return Trigger("email_from", {"addr": m.group(1), "contains": m.group(2) or ""})
    m = _RE_CAL.match(dsl)
    if m:
        return Trigger("calendar_event", {"name": m.group(1)})
    m = _RE_TIME.match(dsl)
    if m:
        return Trigger("time_at", {"when": _parse_when(m.group(1))})
    raise ValueError(f"trigger no reconocido (solo email_from / calendar_event / time_at): {dsl!r}")


def describe_trigger(dsl: str) -> str:
    """A Spanish phrase for a trigger, for confirmation + the pending list."""
    try:
        t = parse_trigger(dsl)
    except ValueError:
        return dsl
    if t.kind == "email_from":
        base = f"cuando te llegue un correo de {t.params['addr']}"
        c = t.params.get("contains")
        return base + (f" que diga «{c}»" if c else "")
    if t.kind == "calendar_event":
        return f"cuando exista el evento «{t.params['name']}» en tu calendario"
    if t.kind == "time_at":
        return f"el {t.params['when']:%d/%m a las %H:%M}"
    return dsl


async def trigger_matches(trigger: Trigger, dispatcher: Dispatcher, now: dt.datetime) -> bool:
    """True if the trigger's source currently satisfies it."""
    if trigger.kind == "time_at":
        return bool(now >= trigger.params["when"])

    if trigger.kind == "email_from":
        addr = trigger.params["addr"].lower()
        contains = (trigger.params.get("contains") or "").lower()
        # Filter by SENDER (not subject) and read the body preview, so `contains`
        # can match the email body — the documented use ("contains=confirmo").
        res = await dispatcher("recent_from", {"sender": addr, "limit": 15})
        blob = _blob(res)
        return addr in blob and (not contains or contains in blob)

    if trigger.kind == "calendar_event":
        name = trigger.params["name"].lower()
        start = now.replace(microsecond=0)
        end = start + dt.timedelta(days=90)
        res = await dispatcher(
            "events_in_range", {"start_iso": start.isoformat(), "end_iso": end.isoformat()}
        )
        return name in _blob(res)

    return False


def _blob(res: Any) -> str:
    msg = getattr(res, "user_message", "") or ""
    data = getattr(res, "data", None)
    try:
        data_s = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        data_s = str(data)
    return (msg + " " + data_s).lower()


# ---- store ------------------------------------------------------------------


def add(trigger_dsl: str, action_tool: str, action_args: dict[str, Any],
        expires_at: str | None) -> int:
    """Persist a conditional. Validates the trigger DSL up front (raises ValueError)."""
    parse_trigger(trigger_dsl)  # validate or raise before we store anything
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO conditionals(trigger_dsl,action_tool,action_args,expires_at,created_at) "
            "VALUES(?,?,?,?,?)",
            (trigger_dsl, action_tool, json.dumps(action_args, ensure_ascii=False),
             expires_at or None, dt.datetime.now().isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def active() -> list[sqlite3.Row]:
    conn = _connect()
    try:
        return list(conn.execute(
            "SELECT * FROM conditionals WHERE status='active' ORDER BY id"
        ))
    finally:
        conn.close()


def _set_status(cid: int, status: str, fired: bool = False) -> None:
    conn = _connect()
    try:
        if fired:
            conn.execute(
                "UPDATE conditionals SET status=?, fired_at=? WHERE id=?",
                (status, dt.datetime.now().isoformat(), cid),
            )
        else:
            conn.execute("UPDATE conditionals SET status=? WHERE id=?", (status, cid))
        conn.commit()
    finally:
        conn.close()


def mark_fired(cid: int) -> None:
    _set_status(cid, "fired", fired=True)


def mark_expired(cid: int) -> None:
    _set_status(cid, "expired")


# ---- watcher ----------------------------------------------------------------


async def check_once(dispatcher: Dispatcher, now: dt.datetime | None = None) -> list[int]:
    """One watcher tick: expire stale rows, fire matched ones exactly once.

    Returns the ids fired this tick. Fired/expired rows leave the active set, so a
    second call with the same state fires nothing (no double-fire).
    """
    now = now or dt.datetime.now()
    fired: list[int] = []
    for row in active():
        cid = int(row["id"])
        exp = row["expires_at"]
        if exp:
            try:
                if dt.datetime.fromisoformat(exp) <= now:
                    mark_expired(cid)
                    continue
            except ValueError:
                pass
        try:
            trig = parse_trigger(row["trigger_dsl"])
        except ValueError:
            log.warning("conditional_bad_trigger", id=cid, dsl=row["trigger_dsl"])
            mark_expired(cid)
            continue
        try:
            matched = await trigger_matches(trig, dispatcher, now)
        except Exception as exc:
            log.warning("conditional_match_failed", id=cid, error=str(exc))
            continue
        if not matched:
            continue
        # The action's confirmed flag (for destructive tools) was baked in at
        # schedule time — the user already confirmed the whole conditional. Only
        # mark fired on a REAL success: a failure or a requires_confirmation
        # bounce must NOT burn the one-shot, or the action silently never happens.
        try:
            res = await dispatcher(row["action_tool"], json.loads(row["action_args"]))
        except Exception as exc:
            log.error("conditional_action_failed", id=cid, tool=row["action_tool"], error=str(exc))
            continue
        if not getattr(res, "success", False) or getattr(res, "requires_confirmation", False):
            log.warning("conditional_action_not_executed", id=cid, tool=row["action_tool"])
            continue  # retry next tick (bounded by expiry)
        mark_fired(cid)
        fired.append(cid)
        log.info("conditional_fired", id=cid, tool=row["action_tool"])
    return fired


async def watch(interval_s: float = 30.0) -> None:
    """Background loop: poll the triggers every ``interval_s``. Spawned from __main__."""
    import asyncio

    from tools.registry import dispatch

    log.info("conditionals_watcher_started", interval_s=interval_s)
    while True:
        try:
            await check_once(dispatch)
        except Exception as exc:
            log.error("conditionals_tick_failed", error=str(exc))
        await asyncio.sleep(interval_s)
