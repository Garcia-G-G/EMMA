"""Daily cost guard for deep_research (Prompt 33, Part C).

Each deep_research call costs ~$0.01 (gpt-4o-mini + ~3 page fetches). We cap the
day at ``_CAP`` calls and log the running spend. State is a tiny JSON file at
``~/.emma/research_usage.json`` that resets when the calendar day rolls over.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings

log = structlog.get_logger("emma.research_budget")

_CAP = 50
_COST_PER_CALL = 0.01


def _path() -> Path:
    return Path(settings.EMMA_HOME).expanduser() / "research_usage.json"


def _load() -> dict[str, Any]:
    today = dt.date.today().isoformat()
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("date") != today:  # new day → reset
        return {"date": today, "count": 0, "cost_usd": 0.0}
    return {"date": today, "count": int(data.get("count", 0)), "cost_usd": float(data.get("cost_usd", 0.0))}


def usage_today() -> int:
    return int(_load().get("count", 0))


def remaining() -> int:
    return max(0, _CAP - usage_today())


def can_run() -> bool:
    return remaining() > 0


def cap() -> int:
    return _CAP


def record(cost: float = _COST_PER_CALL) -> int:
    """Charge one call; returns the new running count. Logs the incurred cost."""
    data = _load()
    count = int(data.get("count", 0)) + 1
    total = round(float(data.get("cost_usd", 0.0)) + cost, 4)
    out = {"date": data["date"], "count": count, "cost_usd": total}
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out))
    except OSError as exc:
        log.warning("research_usage_write_failed", error=str(exc))
    log.info("research_cost", call=count, incurred_usd=cost, day_total_usd=total)
    return count
