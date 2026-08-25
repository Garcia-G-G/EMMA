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


def test_daemon_entry_hard_exits_to_dodge_dlclose_deadlock(monkeypatch):
    # The daemon must os._exit past Py_FinalizeEx: native extension teardown
    # (portaudio/CoreAudio HAL, sherpa_onnx, sqlite) can deadlock in dlclose, so the
    # process "shuts down" cleanly yet never dies (voice "apágate"/SIGTERM hangs
    # forever). Lock the contract: the exit helper calls os._exit with the code.
    codes: list = []
    monkeypatch.setattr("os._exit", lambda c: codes.append(c))
    emma_main._flush_and_hard_exit(0)
    emma_main._flush_and_hard_exit(2)
    emma_main._flush_and_hard_exit("boom")  # non-int (e.g. SystemExit(str)) → 0
    assert codes == [0, 2, 0]


def test_wake_preflight_passes_when_model_present(monkeypatch, tmp_path):
    (tmp_path / "tokens.txt").write_text("x")
    monkeypatch.setattr(emma_main.settings, "WAKE_WORD_ENGINE", "sherpa")
    monkeypatch.setattr(emma_main.settings, "SHERPA_KWS_MODEL_PATH", str(tmp_path))
    assert emma_main._wake_preflight(structlog.get_logger("t")) is None


def test_wake_preflight_fails_terminally_when_model_missing(monkeypatch, tmp_path):
    # Missing sherpa model → a terminal exit (int), NOT the lazy SystemExit that
    # would escape main_loop and 30s-loop under launchd.
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))  # isolate boot_guard state
    monkeypatch.setattr(emma_main.settings, "WAKE_WORD_ENGINE", "sherpa")
    monkeypatch.setattr(emma_main.settings, "SHERPA_KWS_MODEL_PATH", str(tmp_path / "nope"))
    rc = emma_main._wake_preflight(structlog.get_logger("t"))
    assert rc in (0, 1)  # retryable (1) at first, 0 once the streak says stay-down


def test_wake_preflight_skips_non_sherpa_engines(monkeypatch):
    monkeypatch.setattr(emma_main.settings, "WAKE_WORD_ENGINE", "openwakeword")
    assert emma_main._wake_preflight(structlog.get_logger("t")) is None


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


def test_credential_preflight_still_guards_byok(monkeypatch, tmp_path):
    """A BYOK daemon with a MALFORMED key still fails fast.

    The key must be present-but-wrong. An EMPTY key no longer reaches this
    branch: settings._is_managed() now treats "no local key at all" as managed,
    because a BYOK daemon by definition has one (LAUNCH-4 Part 1 — managed mode
    must not hinge on a single environment variable). See the test below.
    """
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))  # keep boot_guard out of ~/.emma
    monkeypatch.setattr(emma_main.settings, "OPENAI_API_KEY", "not-a-real-key")
    # Non-zero = "retry me" (launchd restarts on EVERY non-zero code); the exit
    # only becomes 0 once the streak proves a restart cannot fix it.
    assert emma_main._credential_preflight(structlog.get_logger("test")) == 1


def test_no_local_key_parks_instead_of_exiting(monkeypatch, tmp_path):
    """The fragility fix: one missing env var must not kill the daemon.

    A managed daemon whose EMMA_REQUIRE_PAIRING went missing used to die on a
    credential it is never supposed to have. Now "no OPENAI_API_KEY" is itself
    enough to resolve managed mode, so it parks and shows onboarding.
    """
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))
    monkeypatch.setattr(emma_main.settings, "OPENAI_API_KEY", "")
    assert emma_main._credential_preflight(structlog.get_logger("test")) is None


def test_repeated_failures_eventually_stay_down(monkeypatch, tmp_path):
    """Non-zero forever is the respawn loop. Exit 0 is how it stops."""
    monkeypatch.delenv("EMMA_REQUIRE_PAIRING", raising=False)
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))
    monkeypatch.setattr(emma_main.settings, "OPENAI_API_KEY", "not-a-real-key")
    log = structlog.get_logger("test")
    codes = [emma_main._credential_preflight(log) for _ in range(6)]
    assert codes[0] == 1, "the first failure must be retryable (it may be transient)"
    assert codes[-1] == 0, "a persistent failure must stop launchd retrying"
