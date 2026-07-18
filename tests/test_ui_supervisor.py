"""EMMA-APP Part 6 — daemon supervises the menubar UI (DoD item 8).

Killing the UI never kills the daemon (it's respawned); killing the daemon
terminates the UI child. The .app bundle generation is on-device (install.sh).
"""

from __future__ import annotations

import asyncio

import pytest

import emma.__main__ as m

_REAL_SLEEP = asyncio.sleep  # captured before any monkeypatch of asyncio.sleep


@pytest.mark.asyncio
async def test_supervisor_spawns_then_respawns_on_death(monkeypatch) -> None:
    spawned = {"n": 0}
    gate = {"proc": None}

    class _FakeProc:
        pid = 999
        returncode = 0

        def __init__(self) -> None:
            self._done = asyncio.Event()

        async def wait(self) -> int:
            await self._done.wait()
            return 0

        def die(self) -> None:
            self._done.set()

        def terminate(self) -> None:
            self._done.set()

    async def fake_exec(*a, **k):
        spawned["n"] += 1
        gate["proc"] = _FakeProc()
        return gate["proc"]

    monkeypatch.setattr(m.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(m.asyncio, "sleep", _fast_sleep)  # skip the respawn backoff

    log = m.structlog.get_logger("test")
    task = asyncio.create_task(m._supervise_ui(log))
    await _REAL_SLEEP(0)
    for _ in range(50):  # let the first spawn happen
        if spawned["n"] >= 1:
            break
        await _REAL_SLEEP(0)
    assert spawned["n"] == 1

    gate["proc"].die()  # UI process dies -> supervisor must respawn it
    for _ in range(50):
        if spawned["n"] >= 2:
            break
        await _REAL_SLEEP(0)
    assert spawned["n"] >= 2  # respawned, daemon still alive

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_supervisor_terminates_child_on_daemon_stop(monkeypatch) -> None:
    ev = {"terminated": False}

    class _FakeProc:
        pid = 1
        returncode = None

        def __init__(self) -> None:
            self._done = asyncio.Event()

        async def wait(self) -> int:
            await self._done.wait()
            return 0

        def terminate(self) -> None:
            ev["terminated"] = True
            self._done.set()

    async def fake_exec(*a, **k):
        return _FakeProc()

    monkeypatch.setattr(m.asyncio, "create_subprocess_exec", fake_exec)
    log = m.structlog.get_logger("test")
    task = asyncio.create_task(m._supervise_ui(log))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ev["terminated"] is True  # daemon shutdown tears the UI down


async def _fast_sleep(_s: float) -> None:
    await _REAL_SLEEP(0)
