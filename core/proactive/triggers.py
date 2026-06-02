"""Trigger registration. Two kinds: scheduled (cron) and event-driven (polled).

Each registration records the *name* of the ``settings`` flag that enables it
(not the value at import time) so that toggling a flag at runtime — e.g. via the
``enable_proactivity`` voice tool writing back to ``.env`` and reloading
settings — takes effect on the next tick without a daemon restart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.proactive.types import ProactiveEvent

TriggerFn = Callable[[], Awaitable[ProactiveEvent | None]]


@dataclass
class _Job:
    name: str
    fn: TriggerFn
    enabled_setting: str

    def is_enabled(self) -> bool:
        # Read live so .env-backed toggles apply without a restart.
        from config.settings import settings

        return bool(getattr(settings, self.enabled_setting, False))


@dataclass
class Scheduled(_Job):
    cron: str


@dataclass
class Polled(_Job):
    interval_s: int


_SCHEDULED: list[Scheduled] = []
_POLLED: list[Polled] = []


def scheduled(name: str, cron: str, enabled_setting: str) -> Callable[[TriggerFn], TriggerFn]:
    """Register a cron-scheduled proactivity. ``enabled_setting`` is the
    ``settings`` attribute that flips it on/off (read live each tick)."""

    def deco(fn: TriggerFn) -> TriggerFn:
        _SCHEDULED.append(Scheduled(name=name, fn=fn, enabled_setting=enabled_setting, cron=cron))
        return fn

    return deco


def polled(name: str, interval_s: int, enabled_setting: str) -> Callable[[TriggerFn], TriggerFn]:
    """Register a polled (event-driven) proactivity that runs every
    ``interval_s`` seconds while its ``enabled_setting`` is true."""

    def deco(fn: TriggerFn) -> TriggerFn:
        _POLLED.append(
            Polled(name=name, fn=fn, enabled_setting=enabled_setting, interval_s=interval_s)
        )
        return fn

    return deco


def scheduled_jobs() -> list[Scheduled]:
    return list(_SCHEDULED)


def polled_jobs() -> list[Polled]:
    return list(_POLLED)
