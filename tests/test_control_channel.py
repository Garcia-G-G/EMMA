"""EMMA-APP Part 3 — UI->daemon control channel (server side + Parar).

The WS transport + menu clicks need on-device verification, but the dispatch
logic (which closes the mute->unmute hole) and the stop-speech helper are pure
and tested here.
"""

from __future__ import annotations

import pytest

from core import conversation, orchestrator
from dashboard import server


@pytest.fixture(autouse=True)
def _reset():
    orchestrator._muted = False
    orchestrator._snooze_until = 0.0
    conversation._active_task = None
    yield
    orchestrator._muted = False
    orchestrator._snooze_until = 0.0
    conversation._active_task = None


class _FakeReq:
    def __init__(self, origin: str | None) -> None:
        self.headers = {"Origin": origin} if origin else {}


class _FakeWS:
    def __init__(self, origin: str | None) -> None:
        self.request = _FakeReq(origin)


def test_origin_check_blocks_cross_site_hijack() -> None:
    # A malicious page opening ws://127.0.0.1/control must NOT be able to unmute
    # or shut down Emma. WS ignores same-origin policy, so we check Origin ourselves.
    assert server._origin_ok(_FakeWS(None)) is True  # native app (no Origin)
    assert server._origin_ok(_FakeWS("http://127.0.0.1:3200")) is True  # own dashboard
    assert server._origin_ok(_FakeWS("http://localhost:3200")) is True
    assert server._origin_ok(_FakeWS("https://evil.example")) is False  # foreign page
    assert server._origin_ok(_FakeWS("http://127.0.0.1:9999")) is False  # wrong port


@pytest.mark.asyncio
async def test_unmute_reaches_orchestrator() -> None:
    orchestrator.mute_mic()
    assert orchestrator.is_muted() is True
    res = await server.dispatch_control({"cmd": "unmute"})
    # the click flipped the real daemon flag — the way back voice can't give
    assert res["ok"] is True and res["muted"] is False
    assert orchestrator.is_muted() is False


@pytest.mark.asyncio
async def test_mute_reaches_orchestrator() -> None:
    res = await server.dispatch_control({"cmd": "mute"})
    assert res["ok"] is True and res["muted"] is True
    assert orchestrator.is_muted() is True


@pytest.mark.asyncio
async def test_request_accessibility_runs_in_the_daemon() -> None:
    # LAUNCH-2.1: declining Accessibility must be recoverable FROM THE APP. The
    # daemon (as EmmaDaemon.app) makes the request, so the alert/row belong to Emma.
    from unittest.mock import patch

    from core import permissions

    with (
        patch.object(permissions, "request_accessibility_trust", return_value=False) as req,
        patch.object(permissions, "_open_settings") as opened,
    ):
        res = await server.dispatch_control({"cmd": "request_accessibility"})
    assert res["ok"] is True and res["granted"] is False
    req.assert_called_once()
    opened.assert_called_once_with("Accessibility")


@pytest.mark.asyncio
async def test_snooze_uses_given_minutes() -> None:
    res = await server.dispatch_control({"cmd": "snooze", "minutes": 15})
    assert res["ok"] is True
    assert 800 < res["snooze_remaining_s"] <= 900  # ~15 min pending


@pytest.mark.asyncio
async def test_status_reports_state_without_side_effects() -> None:
    res = await server.dispatch_control({"cmd": "status"})
    assert res["ok"] is True and res["cmd"] == "status"
    assert res["muted"] is False and res["snooze_remaining_s"] == 0


@pytest.mark.asyncio
async def test_unknown_command_is_rejected() -> None:
    res = await server.dispatch_control({"cmd": "rm -rf"})
    assert res["ok"] is False and "unknown" in res["error"]


@pytest.mark.asyncio
async def test_stop_dispatches_to_stop_active_speech(monkeypatch) -> None:
    called = {}

    async def fake_stop() -> bool:
        called["stop"] = True
        return True

    monkeypatch.setattr(conversation, "stop_active_speech", fake_stop)
    res = await server.dispatch_control({"cmd": "stop"})
    assert res["ok"] is True and called.get("stop") is True


@pytest.mark.asyncio
async def test_stop_active_speech_noop_without_session() -> None:
    conversation._active_task = None
    assert await conversation.stop_active_speech() is False


@pytest.mark.asyncio
async def test_stop_active_speech_queues_interruption() -> None:
    class _FakeTask:
        def __init__(self) -> None:
            self.frames: list = []

        async def queue_frame(self, frame) -> None:
            self.frames.append(frame)

    task = _FakeTask()
    conversation._active_task = task
    assert await conversation.stop_active_speech() is True
    assert len(task.frames) == 1  # one interruption frame queued onto the live task


# --- LAUNCH-11 Part 2: the channel is authenticated, not just origin-checked --
#
# _origin_ok deliberately returns True for a client that sends NO Origin header,
# because the native UI is such a client. That is every non-browser process on
# the machine (audit P1-13): it could read 200 rows of personal memory, unmute a
# deliberately muted mic, unpair the Mac, and shut Emma down. An Origin allowlist
# stops foreign WEB PAGES; it was never authentication.


class _TokenWS:
    """A fake handshake carrying a path (where the token rides) and an Origin."""

    def __init__(self, path: str, origin: str | None = None) -> None:
        self.request = _FakeReq(origin)
        self.request.path = path


def _tok() -> str:
    from core import control_auth

    return control_auth.token()


def test_a_client_with_no_token_is_rejected() -> None:
    assert server._token_ok(_TokenWS("/control")) is False


def test_a_client_with_the_wrong_token_is_rejected() -> None:
    assert server._token_ok(_TokenWS("/control?token=not-the-token")) is False


def test_a_client_with_the_right_token_is_accepted() -> None:
    assert server._token_ok(_TokenWS(f"/control?token={_tok()}")) is True


def test_origin_and_token_are_both_required() -> None:
    """Defence in depth: the token stops local processes, the Origin check still
    stops a foreign page that somehow learned the token."""
    good = f"/control?token={_tok()}"
    assert server._socket_ok(_TokenWS(good, None)) is True
    assert server._socket_ok(_TokenWS(good, "http://127.0.0.1:3200")) is True
    assert server._socket_ok(_TokenWS(good, "https://evil.example")) is False
    assert server._socket_ok(_TokenWS("/control", None)) is False


def test_the_token_file_is_not_world_readable(tmp_path, monkeypatch) -> None:
    """It is a shared secret on a multi-process machine; 0600 is the floor."""
    import importlib
    import stat

    monkeypatch.setenv("EMMA_HOME", str(tmp_path))
    monkeypatch.delenv("EMMA_CONTROL_TOKEN", raising=False)
    from core import control_auth

    importlib.reload(control_auth)
    control_auth.token()

    path = tmp_path / "control_token"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_token_is_stable_within_a_process(tmp_path, monkeypatch) -> None:
    import importlib

    monkeypatch.setenv("EMMA_HOME", str(tmp_path))
    monkeypatch.delenv("EMMA_CONTROL_TOKEN", raising=False)
    from core import control_auth

    importlib.reload(control_auth)
    assert control_auth.token() == control_auth.token()


def test_an_injected_token_wins(tmp_path, monkeypatch) -> None:
    """The daemon hands its UI child the token through the environment, so the
    child must use that rather than minting one of its own."""
    import importlib

    monkeypatch.setenv("EMMA_HOME", str(tmp_path))
    monkeypatch.setenv("EMMA_CONTROL_TOKEN", "handed-down-token")
    from core import control_auth

    importlib.reload(control_auth)
    assert control_auth.token() == "handed-down-token"
