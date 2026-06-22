"""Emma self-control voice tools — shutdown / restart / sleep."""

from __future__ import annotations

import pytest

from core import dev_state, orchestrator
from tools import lifecycle_tool as lc


@pytest.fixture(autouse=True)
def _reset():
    dev_state.shutdown_requested.clear()
    orchestrator._snooze_until = 0.0
    yield
    dev_state.shutdown_requested.clear()
    orchestrator._snooze_until = 0.0


@pytest.mark.asyncio
async def test_shutdown_requires_confirmation_first() -> None:
    # 24.6 audit: lifecycle tools are destructive — must NOT fire on a cold call
    # (else injected content could DoS Emma). First call only asks.
    res = await lc.shutdown_emma()
    assert not res.success and res.requires_confirmation
    assert not dev_state.shutdown_requested.is_set()


@pytest.mark.asyncio
async def test_shutdown_sets_flag_when_confirmed() -> None:
    res = await lc.shutdown_emma(confirmed=True)
    assert res.success and res.ends_session
    assert dev_state.shutdown_requested.is_set()


@pytest.mark.asyncio
async def test_restart_requires_confirmation_first(monkeypatch) -> None:
    called = {"popen": False}
    monkeypatch.setattr(lc.subprocess, "Popen", lambda *a, **k: called.update(popen=True))
    res = await lc.restart_emma()
    assert not res.success and res.requires_confirmation
    assert called["popen"] is False  # nothing spawned on a cold call


@pytest.mark.asyncio
async def test_restart_spawns_detached_kickstart_when_confirmed(monkeypatch) -> None:
    calls = {}

    def fake_popen(args, **kw):
        calls["args"] = args
        calls["new_session"] = kw.get("start_new_session")
        return object()

    monkeypatch.setattr(lc.subprocess, "Popen", fake_popen)
    res = await lc.restart_emma(confirmed=True)
    assert res.success and res.ends_session
    assert "launchctl kickstart -k" in calls["args"][2]
    assert calls["new_session"] is True  # must outlive Emma's own restart


@pytest.mark.asyncio
async def test_restart_degrades_when_spawn_fails(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(lc.subprocess, "Popen", boom)
    res = await lc.restart_emma(confirmed=True)
    assert not res.success  # honest failure, no crash


@pytest.mark.asyncio
async def test_snooze_listening_requires_confirmation_first() -> None:
    res = await lc.snooze_listening(20)
    assert not res.success and res.requires_confirmation
    assert orchestrator.snooze_remaining_s() == 0  # not snoozed on a cold call


@pytest.mark.asyncio
async def test_snooze_listening_pauses_when_confirmed() -> None:
    res = await lc.snooze_listening(20, confirmed=True)
    assert res.success and res.ends_session
    assert res.data["minutes"] == 20
    assert "20 min" in res.user_message
    assert orchestrator.snooze_remaining_s() > 1100  # ~20 min still pending


@pytest.mark.asyncio
async def test_snooze_listening_clamps_floor() -> None:
    await lc.snooze_listening(0, confirmed=True)  # floored to 1 minute, never 0/negative
    assert orchestrator.snooze_remaining_s() > 0


def test_orchestrator_snooze_remaining_decays(monkeypatch) -> None:
    base = [1000.0]
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: base[0])
    orchestrator.snooze_listening(1)  # deadline = 1000 + 60
    assert orchestrator.snooze_remaining_s() == pytest.approx(60, abs=1)
    base[0] = 1070.0  # past the deadline
    assert orchestrator.snooze_remaining_s() == 0.0
