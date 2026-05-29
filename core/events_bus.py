"""In-process event bus for dashboard forwarding.

Pipecat handlers and the orchestrator publish typed events; the dashboard
WebSocket subscribers receive them. Lossy by design: if a subscriber's queue
is full, that event drops for that subscriber.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

log = structlog.get_logger("emma.events_bus")

_subs: set[asyncio.Queue[dict[str, Any]]] = set()


def subscribe() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
    _subs.add(q)
    return q


def unsubscribe(q: asyncio.Queue[dict[str, Any]]) -> None:
    _subs.discard(q)


def publish(event_type: str, **fields: Any) -> None:
    """Fire-and-forget publish. Drops events on slow subscribers (no backpressure)."""
    payload = {"type": event_type, **fields}
    for q in _subs:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("events_bus_drop", subscriber_full=True)
