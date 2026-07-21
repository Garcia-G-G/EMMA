"""PAID-ONBOARDING Part 2 — the daemon parks alive when unpaired (managed mode).

No terminal pairing, no exit: a fresh managed install boots, publishes
``needs_onboarding``, and waits until the app writes a device token. A dev/BYOK
daemon (EMMA_REQUIRE_PAIRING unset) and an already-paired daemon are unaffected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import structlog

import emma.__main__ as emma_main
from core import orchestrator


@pytest.fixture(autouse=True)
def _clear_shutdown():
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False
    yield
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False


@pytest.mark.asyncio
async def test_ensure_paired_noop_when_not_managed(monkeypatch):
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    # pairing must never be consulted in dev/BYOK mode.
    monkeypatch.setattr("core.pairing.is_paired", AsyncMock(side_effect=AssertionError))
    await orchestrator._ensure_paired()
    assert orchestrator.onboarding_needed() is False


@pytest.mark.asyncio
async def test_ensure_paired_already_paired_loads_cache(monkeypatch):
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    monkeypatch.setattr("core.pairing.is_paired", AsyncMock(return_value=True))
    load = AsyncMock()
    monkeypatch.setattr("core.pairing.load_token_cache", load)
    await orchestrator._ensure_paired()
    load.assert_awaited_once()
    assert orchestrator.onboarding_needed() is False


@pytest.mark.asyncio
async def test_ensure_paired_parks_then_proceeds_when_app_pairs(monkeypatch):
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    # Unpaired for the first two polls, then the app writes the token.
    is_paired = AsyncMock(side_effect=[False, False, True])
    monkeypatch.setattr("core.pairing.is_paired", is_paired)
    load = AsyncMock()
    monkeypatch.setattr("core.pairing.load_token_cache", load)
    # Don't actually sleep between polls.
    monkeypatch.setattr(orchestrator.asyncio, "sleep", AsyncMock())
    events: list[dict] = []
    monkeypatch.setattr(
        orchestrator.events_bus, "publish",
        lambda etype, **f: events.append({"type": etype, **f}),
    )

    await orchestrator._ensure_paired()

    states = [e["state"] for e in events if e["type"] == "state"]
    assert "needs_onboarding" in states          # showed the app the onboarding cue
    assert states[-1] == "waiting_for_wake"       # …then handed off to the wake loop
    load.assert_awaited_once()                    # token cache loaded for managed calls
    assert orchestrator.onboarding_needed() is False  # flag cleared on the way out


@pytest.mark.asyncio
async def test_ensure_paired_exits_park_on_shutdown(monkeypatch):
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    monkeypatch.setattr("core.pairing.is_paired", AsyncMock(return_value=False))
    monkeypatch.setattr("core.pairing.load_token_cache", AsyncMock())

    async def _sleep(_s):  # a shutdown arriving mid-park must unwind the wait
        orchestrator._shutdown.set()

    monkeypatch.setattr(orchestrator.asyncio, "sleep", _sleep)
    monkeypatch.setattr(orchestrator.events_bus, "publish", lambda *a, **k: None)

    await orchestrator._ensure_paired()
    assert orchestrator.onboarding_needed() is False


def test_credential_preflight_skipped_in_managed_mode(monkeypatch):
    # Managed daemon has NO local sk- key — the preflight must not exit 2, or the
    # daemon could never boot to show onboarding.
    monkeypatch.setenv("EMMA_REQUIRE_PAIRING", "1")
    monkeypatch.setattr(emma_main.settings, "OPENAI_API_KEY", "")
    assert emma_main._credential_preflight(structlog.get_logger("test")) is None


def test_credential_preflight_still_guards_byok(monkeypatch):
    # Dev/BYOK daemon (flag unset) with a bad key still fails fast (exit 2).
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    monkeypatch.setattr(emma_main.settings, "OPENAI_API_KEY", "")
    assert emma_main._credential_preflight(structlog.get_logger("test")) == 2
