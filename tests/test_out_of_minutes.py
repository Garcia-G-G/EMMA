"""LAUNCH-7 Part 4 — the out-of-minutes moment.

Deliberately NOT triggered by the WebSocket close code. backend/realtime_proxy.py
calls ws.close(code=4402, reason="balance_zero") at :109 and :119, both BEFORE
ws.accept() at :144 — so the handshake is rejected and the code never reaches the
client — and a mid-session cut closes at :231 with no code at all, which
_ZOMBIE_MARKERS reads as an ordinary zombie. Until LAUNCH-6 fixes that ordering a
close code cannot distinguish "out of minutes" from "bad token", so the daemon
asks the balance route, which is authoritative either way.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from core import orchestrator


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._p, self.status_code = payload, status

    def json(self) -> dict[str, Any]:
        return self._p


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._r = resp

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def get(self, _path: str) -> _Resp:
        return self._r


def _balance(left: float, status: int = 200):
    async def _authed() -> _Client:
        return _Client(_Resp({"total_left_min": left}, status))
    return _authed


@pytest.fixture(autouse=True)
def _reset():
    orchestrator._out_of_minutes = False
    yield
    orchestrator._out_of_minutes = False


@pytest.mark.asyncio
async def test_no_check_in_dev_mode() -> None:
    """A BYOK/dev daemon has no managed balance; never speak at it."""
    with patch.object(orchestrator.settings, "_is_managed", return_value=False):
        assert await orchestrator._check_out_of_minutes() is False
    assert orchestrator.out_of_minutes() is False


@pytest.mark.asyncio
async def test_minutes_left_is_silent() -> None:
    with (
        patch.object(orchestrator.settings, "_is_managed", return_value=True),
        patch("core.pairing.authed_client", _balance(12.5)),
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        assert await orchestrator._check_out_of_minutes() is False
    spawn.assert_not_called()
    assert orchestrator.out_of_minutes() is False


@pytest.mark.asyncio
async def test_zero_minutes_speaks_and_publishes_once() -> None:
    published: list[tuple[str, dict]] = []
    with (
        patch.object(orchestrator.settings, "_is_managed", return_value=True),
        patch("core.pairing.authed_client", _balance(0.0)),
        patch.object(orchestrator.events_bus, "publish",
                     side_effect=lambda ev, **kw: published.append((ev, kw))),
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        spawn.return_value.wait = _noop
        assert await orchestrator._check_out_of_minutes() is True
        # Second call: state persists, but she must NOT nag.
        assert await orchestrator._check_out_of_minutes() is True

    assert orchestrator.out_of_minutes() is True
    states = [kw.get("state") for ev, kw in published if ev == "state"]
    assert states == ["out_of_minutes"], f"spoke/published more than once: {states}"
    assert spawn.call_count == 1
    said = " ".join(str(a) for a in spawn.call_args[0])
    assert "minutos" in said and "app" in said
    # `say`, not the Realtime API — the session that would have spoken it is gone.
    assert spawn.call_args[0][0] == "say"


async def _noop() -> int:
    return 0


@pytest.mark.asyncio
async def test_topping_up_clears_the_state() -> None:
    with (
        patch.object(orchestrator.settings, "_is_managed", return_value=True),
        patch("core.pairing.authed_client", _balance(0.0)),
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        spawn.return_value.wait = _noop
        await orchestrator._check_out_of_minutes()
    assert orchestrator.out_of_minutes() is True

    with (
        patch.object(orchestrator.settings, "_is_managed", return_value=True),
        patch("core.pairing.authed_client", _balance(30.0)),
    ):
        assert await orchestrator._check_out_of_minutes() is False
    assert orchestrator.out_of_minutes() is False


@pytest.mark.asyncio
async def test_offline_never_claims_out_of_minutes() -> None:
    """A network failure must not silently tell the user they ran out."""
    async def _boom() -> Any:
        raise RuntimeError("offline")

    with (
        patch.object(orchestrator.settings, "_is_managed", return_value=True),
        patch("core.pairing.authed_client", _boom),
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        assert await orchestrator._check_out_of_minutes() is False
    spawn.assert_not_called()
    assert orchestrator.out_of_minutes() is False


@pytest.mark.asyncio
async def test_non_200_is_not_treated_as_empty() -> None:
    with (
        patch.object(orchestrator.settings, "_is_managed", return_value=True),
        patch("core.pairing.authed_client", _balance(0.0, status=503)),
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        assert await orchestrator._check_out_of_minutes() is False
    spawn.assert_not_called()


def test_menubar_has_a_distinct_state() -> None:
    """"En espera" would be a lie while she cannot answer."""
    from emma.ui.__main__ import _ESTADO_LABEL, _ICON_FOR_STATE, _STATE_BUCKET

    assert _STATE_BUCKET["out_of_minutes"] == "out_of_minutes"
    assert _ICON_FOR_STATE["out_of_minutes"] != _ICON_FOR_STATE["idle"]
    assert _ESTADO_LABEL["out_of_minutes"] == "Sin minutos"


def test_never_auto_charges() -> None:
    """No purchase path exists in the daemon at all — buying is browser-only."""
    import inspect

    src = inspect.getsource(orchestrator._check_out_of_minutes)
    for forbidden in ("stripe", "charge", "buy", "payment", "refill"):
        assert forbidden not in src.lower(), forbidden
