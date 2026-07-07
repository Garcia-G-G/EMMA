"""CLIENT-INSTALL-PIPELINE Phase 1 — usage metering (raw sqlite).

One ``usage_events`` row per proxied realtime session; ``seconds`` is the
server-measured wall time. Ported from the prompt's SQLAlchemy sketch to
``backend.db`` (raw sqlite, epoch-float timestamps).
"""
from __future__ import annotations

import datetime
import time

from backend import db


def _month_start_epoch() -> float:
    now = datetime.datetime.now(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def record_usage(
    user_id: int,
    device_id: int,
    seconds: int = 0,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    audio_tokens: int = 0,
    model: str = "",
    kind: str = "realtime",
) -> None:
    """Persist a metered event (one row) and bump the user's monthly counters.

    Two callers: the realtime WS (seconds + token usage from response.done, kind
    'realtime') and the HTTP proxy (tokens only, kind 'http'/'http-stream'). The
    dashboard reads ``users.monthly_seconds_used`` / ``monthly_session_count``, so we
    keep those in sync in the same transaction (only when a session had seconds).
    """
    if seconds <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO usage_events(user_id,device_id,seconds,input_tokens,output_tokens,"
            "cached_tokens,audio_tokens,model,kind,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, device_id, int(seconds), int(input_tokens), int(output_tokens),
             int(cached_tokens), int(audio_tokens), model, kind, time.time()),
        )
        if seconds > 0:
            conn.execute(
                "UPDATE users SET monthly_seconds_used=COALESCE(monthly_seconds_used,0)+?, "
                "monthly_session_count=COALESCE(monthly_session_count,0)+1 WHERE id=?",
                (int(seconds), user_id),
            )
        conn.commit()
    finally:
        conn.close()


def record_usage_tokens(
    user_id: int,
    device_id: int,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    audio_tokens: int = 0,
    model: str = "",
    kind: str = "http",
) -> None:
    """Token-only metering for the HTTP proxy (no session seconds)."""
    record_usage(user_id, device_id, 0, input_tokens=input_tokens, output_tokens=output_tokens,
                 cached_tokens=cached_tokens, audio_tokens=audio_tokens, model=model, kind=kind)


def seconds_used_this_month(user_id: int) -> int:
    """Calendar-month seconds for cap enforcement (from usage_events, UTC)."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(seconds),0) FROM usage_events WHERE user_id=? AND created_at>=?",
            (user_id, _month_start_epoch()),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def minutes_used_this_month(user_id: int) -> float:
    """Calendar-month minutes (convenience wrapper over seconds)."""
    return seconds_used_this_month(user_id) / 60.0


def minutes_used_this_month_all_users() -> list[tuple[int, float]]:
    """Cron helper for the Stripe usage push (Phase 5): [(user_id, minutes)]."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT user_id, COALESCE(SUM(seconds),0) FROM usage_events "
            "WHERE created_at>=? GROUP BY user_id",
            (_month_start_epoch(),),
        ).fetchall()
        return [(r[0], (r[1] or 0) / 60.0) for r in rows]
    finally:
        conn.close()
