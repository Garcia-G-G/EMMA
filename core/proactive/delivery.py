"""Take a ProactiveEvent + its effective priority and actually deliver it.

Routing by effective priority:
- SILENT  → log only (already published to the bus).
- AMBIENT → visualizer ticker only (the bus event).
- NOTIFY  → macOS notification.
- SPEAK   → notification + Emma speaks it in her real voice.
- URGENT  → same as SPEAK (the demotion logic upstream never lowers URGENT).
"""

from __future__ import annotations

import asyncio

import structlog

from actions import macos
from core import events_bus
from core.proactive import voice as proactive_voice
from core.proactive.types import Priority, ProactiveEvent

log = structlog.get_logger("emma.proactive.delivery")


def _one_line(text: str, limit: int = 200) -> str:
    """Collapse to a single line and cap length (notification-safe)."""
    return " ".join((text or "").split())[:limit]


async def deliver(event: ProactiveEvent, effective: Priority) -> None:
    log.info("proactive_deliver", source=event.source, prio=int(effective))
    events_bus.publish(
        "proactive",
        source=event.source,
        priority=int(effective),
        summary=event.summary_es[:160],
    )

    if effective <= Priority.SILENT or effective == Priority.AMBIENT:
        # SILENT: nothing further. AMBIENT: the bus event already drives the ticker.
        return

    if effective >= Priority.NOTIFY:
        body = _one_line(event.summary_es)
        # macos.notify escapes quotes/backslashes; _one_line removes newlines.
        await asyncio.to_thread(macos.notify, "Emma", body)

    if effective >= Priority.SPEAK:
        # Garcia's explicit preference: real voice, not the macOS `say` fallback.
        await proactive_voice.speak_unprompted(event.detail or event.summary_es)
