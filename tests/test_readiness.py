"""VOICE-1 Part 0 — readiness is mic + wake + ElevenLabs + embedder; OpenAI is gone from it."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
import structlog

import emma.__main__ as emma_main
from config import settings as settings_mod
from config.settings import settings
from core import orchestrator, readiness, secrets


@pytest.fixture(autouse=True)
def _cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VOICE_BACKEND", "cascade")
    monkeypatch.setattr(orchestrator, "_PAIR_POLL_MIN_S", 0.01)
    monkeypatch.setattr(orchestrator, "_PAIR_POLL_MAX_S", 0.01)
    orchestrator._shutdown.clear()
    orchestrator._onboarding_needed = False


def _probes(monkeypatch: pytest.MonkeyPatch, **ok: bool) -> None:
    for name in readiness.REQUIREMENTS:
        value = ok.get(name, True)
        monkeypatch.setitem(readiness._PROBES, name, lambda v=value: v)


def test_new_keys_are_secret_tier_and_migrate_to_keychain() -> None:
    for name in ("ELEVENLABS_API_KEY", "OPENROUTER_API_KEY"):
        assert name in settings_mod._CREDENTIAL_FIELDS
        assert name.endswith(secrets._CRED_SUFFIXES)
        assert name not in settings_mod._BYOK_ONLY_CREDENTIALS  # read in every tier


def test_readiness_is_four_local_checks_and_never_openai() -> None:
    assert readiness.REQUIREMENTS == ("mic", "wake_model", "elevenlabs_key", "embedder")
    src = inspect.getsource(readiness)
    assert "OPENAI_API_KEY" not in src and "openai_api_key" not in src and "pairing" not in src
    assert "openrouter" not in readiness.REQUIREMENTS  # optional until BRAIN-2


def test_check_names_exactly_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _probes(monkeypatch)
    assert readiness.check().ready
    _probes(monkeypatch, elevenlabs_key=False, embedder=False)
    state = readiness.check()
    assert not state.ready and state.missing == ("elevenlabs_key", "embedder")
    assert all(readiness.FIXES[m] for m in state.missing)


def test_key_probe_reads_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "")
    assert not readiness.has_elevenlabs_key()
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "sk_x")
    assert readiness.has_elevenlabs_key()


def test_wake_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "sherpa")
    monkeypatch.setattr(settings, "SHERPA_KWS_MODEL_PATH", str(tmp_path))
    assert not readiness.has_wake_model()
    (tmp_path / "tokens.txt").write_text("x")
    assert readiness.has_wake_model()


def test_cascade_boot_needs_no_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(emma_main, "_looks_managed", lambda: False)  # would demand sk-
    assert emma_main._credential_preflight(structlog.get_logger("t")) is None


@pytest.mark.asyncio
async def test_ready_daemon_does_not_park(monkeypatch: pytest.MonkeyPatch) -> None:
    _probes(monkeypatch)
    await asyncio.wait_for(orchestrator._ensure_ready(), timeout=1)
    assert not orchestrator.onboarding_needed()


@pytest.mark.asyncio
async def test_parks_until_onboarding_writes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OpenAI key, no pairing: the key landing in Keychain is what un-parks it."""
    _probes(monkeypatch)
    monkeypatch.setitem(readiness._PROBES, "elevenlabs_key", readiness.has_elevenlabs_key)
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    reads = {"n": 0}

    async def fake_retrieve(label: str) -> str | None:
        assert label == "ELEVENLABS_API_KEY"
        reads["n"] += 1
        return "sk_from_onboarding" if reads["n"] >= 3 else None

    monkeypatch.setattr(secrets, "retrieve", fake_retrieve)
    seen: list[bool] = []

    async def watch() -> None:
        while not reads["n"]:
            await asyncio.sleep(0.005)
        seen.append(orchestrator.onboarding_needed())

    await asyncio.wait_for(asyncio.gather(orchestrator._ensure_ready(), watch()), timeout=2)
    assert seen == [True]
    assert settings.ELEVENLABS_API_KEY == "sk_from_onboarding"
    assert not orchestrator.onboarding_needed()


@pytest.mark.asyncio
async def test_park_exits_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    _probes(monkeypatch, mic=False)

    async def stop() -> None:
        await asyncio.sleep(0.05)
        orchestrator._shutdown.set()

    await asyncio.wait_for(asyncio.gather(orchestrator._ensure_ready(), stop()), timeout=2)
    orchestrator._shutdown.clear()


@pytest.mark.asyncio
async def test_cascade_main_loop_parks_on_readiness_not_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def ready() -> None:
        calls.append("ready")
        orchestrator._shutdown.set()

    async def paired() -> None:
        calls.append("paired")

    monkeypatch.setattr(orchestrator, "_ensure_ready", ready)
    monkeypatch.setattr(orchestrator, "_ensure_paired", paired)
    await orchestrator.main_loop()
    orchestrator._shutdown.clear()
    assert calls == ["ready"]


@pytest.mark.asyncio
async def test_cascade_never_asks_the_minutes_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_mod.Settings, "_is_managed", lambda self: True)
    assert await orchestrator._check_out_of_minutes() is False
