"""Tests for the JARVIS visualizer: event bus, dashboard route, and WS /events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request

import pytest
import websockets

from core import events_bus


@pytest.fixture(autouse=True)
def _restore_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# --- event bus -------------------------------------------------------------


def test_bus_pubsub_order_preserved():
    q = events_bus.subscribe()
    try:
        for i in range(3):
            events_bus.publish("tick", n=i)
        got = [q.get_nowait() for _ in range(3)]
    finally:
        events_bus.unsubscribe(q)
    assert [g["n"] for g in got] == [0, 1, 2]
    assert all(g["type"] == "tick" for g in got)


def test_bus_drops_on_full_queue():
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    events_bus._subs.add(q)
    try:
        for i in range(5):
            events_bus.publish("flood", n=i)
        delivered = []
        while not q.empty():
            delivered.append(q.get_nowait())
    finally:
        events_bus._subs.discard(q)
    # maxsize=2 -> at most 2 delivered, the rest dropped (lossy by design)
    assert len(delivered) == 2


# --- dashboard HTTP + WS ---------------------------------------------------


def test_visualizer_route_and_events_ws():
    from dashboard import server

    async def run():
        server.PORT = 39210  # isolate from any running dashboard
        task = asyncio.create_task(server.start())
        await asyncio.sleep(1.2)  # let HTTP thread + WS server bind
        try:
            # HTTP 200 for /visualizer (blocking urllib -> executor)
            def _get():
                with urllib.request.urlopen("http://localhost:39210/visualizer", timeout=3) as r:
                    return r.status, r.read().decode("utf-8", "replace")

            status, body = await asyncio.get_event_loop().run_in_executor(None, _get)
            assert status == 200
            assert "jb-host" in body and "three.min.js" in body  # the HUD scaffold

            # WS /events sends the init payload within 1s
            async with websockets.connect("ws://localhost:39211/events") as ws:
                init = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            assert init["type"] == "init"
            assert "vad_threshold" in init
            assert "tools_count" in init
        finally:
            # The forever-running server task won't finish promptly on cancel
            # (serve() cleanup blocks), so bound the wait instead of awaiting it.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(task, timeout=3.0)

    asyncio.run(run())
