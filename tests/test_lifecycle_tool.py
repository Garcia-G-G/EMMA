"""Emma self-control voice tools — shutdown / restart / sleep."""

from __future__ import annotations

import pytest

from core import dev_state, orchestrator
from tools import lifecycle_tool as lc


@pytest.fixture(autouse=True)
def _reset():
    dev_state.shutdown_requested.clear()
    orchestrator._snooze_until = 0.0
    orchestrator._muted = False
    yield
    dev_state.shutdown_requested.clear()
    orchestrator._snooze_until = 0.0
    orchestrator._muted = False


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

    monkeypatch.setattr(lc, "_launchd_label", lambda: "com.emma.daemon")
    monkeypatch.setattr(lc.subprocess, "Popen", fake_popen)
    res = await lc.restart_emma(confirmed=True)
    assert res.success and res.ends_session
    assert "launchctl kickstart -k" in calls["args"][2]
    # kickstart must target the label launchd actually knows, not a hardcoded guess
    assert "com.emma.daemon" in calls["args"][2]
    assert calls["new_session"] is True  # must outlive Emma's own restart


@pytest.mark.asyncio
async def test_restart_targets_legacy_label_when_that_is_registered(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(lc, "_launchd_label", lambda: "com.garcia.emma")
    monkeypatch.setattr(lc.subprocess, "Popen", lambda args, **k: calls.update(args=args))
    res = await lc.restart_emma(confirmed=True)
    assert res.success
    assert "com.garcia.emma" in calls["args"][2]


@pytest.mark.asyncio
async def test_restart_fails_honestly_when_not_under_launchd(monkeypatch) -> None:
    # `python -m emma --debug` in a terminal is under no label: restart can't work,
    # so it must say so rather than claim "ahora vuelvo" and never come back.
    called = {"popen": False}
    monkeypatch.setattr(lc, "_launchd_label", lambda: None)
    monkeypatch.setattr(lc.subprocess, "Popen", lambda *a, **k: called.update(popen=True))
    res = await lc.restart_emma(confirmed=True)
    assert not res.success and not res.requires_confirmation
    assert called["popen"] is False  # nothing spawned when there's nothing to kickstart
    assert "servicio" in res.user_message.lower()


@pytest.mark.asyncio
async def test_restart_degrades_when_spawn_fails(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(lc, "_launchd_label", lambda: "com.emma.daemon")
    monkeypatch.setattr(lc.subprocess, "Popen", boom)
    res = await lc.restart_emma(confirmed=True)
    assert not res.success  # honest failure, no crash


def test_launchd_label_prefers_daemon_and_asks_launchctl(monkeypatch) -> None:
    seen = []

    class R:
        def __init__(self, rc): self.returncode = rc

    def fake_run(args, **kw):
        seen.append(args)
        # com.emma.daemon (checked first) is registered → rc 0
        return R(0 if args[-1].endswith("com.emma.daemon") else 1)

    monkeypatch.setattr(lc.subprocess, "run", fake_run)
    assert lc._launchd_label() == "com.emma.daemon"
    assert seen[0][:2] == ["launchctl", "print"]


def test_launchd_label_falls_back_to_legacy(monkeypatch) -> None:
    class R:
        def __init__(self, rc): self.returncode = rc

    def fake_run(args, **kw):
        return R(0 if args[-1].endswith("com.garcia.emma") else 1)

    monkeypatch.setattr(lc.subprocess, "run", fake_run)
    assert lc._launchd_label() == "com.garcia.emma"


def test_launchd_label_none_when_no_agent(monkeypatch) -> None:
    class R:
        def __init__(self, rc): self.returncode = rc

    monkeypatch.setattr(lc.subprocess, "run", lambda *a, **k: R(1))
    assert lc._launchd_label() is None


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


@pytest.mark.asyncio
async def test_mute_mic_stops_capture_indefinitely() -> None:
    # mute is instant (no confirmation) and indefinite — sets the mute flag so the
    # wake loop never opens the stream (mic released), and ends the current session.
    assert orchestrator.is_muted() is False
    res = await lc.mute_mic()
    assert res.success and res.ends_session and not res.requires_confirmation
    assert orchestrator.is_muted() is True
    assert orchestrator.snooze_remaining_s() == 0  # mute is not a timed snooze


@pytest.mark.asyncio
async def test_unmute_mic_resumes_listening() -> None:
    orchestrator.mute_mic()
    res = await lc.unmute_mic()
    assert res.success
    assert orchestrator.is_muted() is False


def test_orchestrator_snooze_remaining_decays(monkeypatch) -> None:
    base = [1000.0]
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: base[0])
    orchestrator.snooze_listening(1)  # deadline = 1000 + 60
    assert orchestrator.snooze_remaining_s() == pytest.approx(60, abs=1)
    base[0] = 1070.0  # past the deadline
    assert orchestrator.snooze_remaining_s() == 0.0
