"""Proactive engine main loop.

Runs as a background asyncio task (spawned from ``emma/__main__`` when
``PROACTIVE_ENABLED``). Every minute boundary it:
  - runs each enabled Scheduled job whose cron fires this minute,
  - runs each enabled Polled job whose interval has elapsed,
  - pushes each returned ProactiveEvent through quiet/DND demotion → delivery.

A global snooze (set by the ``snooze_proactivities`` voice tool) suppresses all
dispatch until it expires. Jobs fire at most once per minute boundary.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Coroutine
from typing import Any

import structlog
from croniter import croniter

from config.settings import settings
from core.proactive import delivery, quiet, triggers
from core.proactive.types import ProactiveEvent

log = structlog.get_logger("emma.proactive.engine")

_last_fire: dict[str, dt.datetime] = {}
_snoozed_until: dt.datetime | None = None
# Hold strong refs to spawned tasks so they aren't GC'd mid-flight (RUF006).
_tasks: set[asyncio.Task[Any]] = set()


def _spawn(coro: Coroutine[Any, Any, Any], name: str | None = None) -> None:
    t = asyncio.create_task(coro, name=name)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def snooze(minutes: int) -> dt.datetime:
    """Suppress all proactive dispatch for ``minutes``. Returns the wake time."""
    global _snoozed_until
    _snoozed_until = dt.datetime.now() + dt.timedelta(minutes=max(1, int(minutes)))
    log.info("proactive_snoozed", until=_snoozed_until.isoformat())
    return _snoozed_until


def is_snoozed(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    return bool(_snoozed_until is not None and now < _snoozed_until)


async def _run_one(name: str, coro: Awaitable[ProactiveEvent | None]) -> None:
    try:
        event: ProactiveEvent | None = await coro
    except Exception as exc:
        log.error("proactive_job_failed", name=name, error=str(exc))
        return
    if event is None:
        return
    in_call = await quiet.in_meeting_now() if settings.PROACTIVE_RESPECT_MEETINGS else False
    effective = quiet.adjust_priority(event.priority, quiet.in_quiet_hours(), in_call)
    await delivery.deliver(event, effective)


def _cron_fires_now(cron: str, now: dt.datetime) -> bool:
    """True if ``cron`` fires exactly at minute ``now``."""
    try:
        it = croniter(cron, now - dt.timedelta(minutes=1))
        return bool(it.get_next(dt.datetime) == now)
    except (ValueError, KeyError):
        log.error("proactive_bad_cron", cron=cron)
        return False


async def _tick(now: dt.datetime) -> None:
    if is_snoozed(now):
        return
    for sjob in triggers.scheduled_jobs():
        if not sjob.is_enabled():
            continue
        if _cron_fires_now(sjob.cron, now) and _last_fire.get(sjob.name) != now:
            _last_fire[sjob.name] = now
            _spawn(_run_one(sjob.name, sjob.fn()), name=f"proactive:{sjob.name}")
    real_now = dt.datetime.now()
    for pjob in triggers.polled_jobs():
        if not pjob.is_enabled():
            continue
        key = f"poll:{pjob.name}"
        last = _last_fire.get(key)
        if last is None or (real_now - last).total_seconds() >= pjob.interval_s:
            _last_fire[key] = real_now
            _spawn(_run_one(pjob.name, pjob.fn()), name=f"proactive:{pjob.name}")


async def run() -> None:
    """Engine entry point: register proactivities, start the bg subscriber, tick."""
    log.info("proactive_engine_started")
    # Importing the module runs the @scheduled/@polled decorators.
    from core.proactive import proactivities

    _spawn(proactivities.background_task_subscriber(), name="emma-proactive-bg")

    while True:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        await _tick(now)
        # Sleep to the next minute boundary.
        await asyncio.sleep(max(1.0, 60 - dt.datetime.now().second))
