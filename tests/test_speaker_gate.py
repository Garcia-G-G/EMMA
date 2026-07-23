"""Prompt 35.1 — destructive-tool speaker gate + resemblyzer graceful degrade."""

from __future__ import annotations

import numpy as np
import pytest

from config.settings import settings
from core import speaker
from memory import voice_profiles as vp


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    speaker.reset()
    monkeypatch.setattr(settings, "SPEAKER_GATE_DESTRUCTIVE", True)
    yield
    speaker.reset()


def _present(monkeypatch, profiles: int = 1) -> None:
    monkeypatch.setattr(speaker, "_available", lambda: True)
    monkeypatch.setattr(vp, "count_sync", lambda: profiles)


# ---- gate logic -------------------------------------------------------------


def test_gate_off_without_resemblyzer(monkeypatch) -> None:
    monkeypatch.setattr(speaker, "_available", lambda: False)
    assert speaker.should_gate() is False  # degraded → never gate (treat as the user)


def test_gate_off_with_no_profiles(monkeypatch) -> None:
    _present(monkeypatch, profiles=0)
    assert speaker.should_gate() is False  # fresh install can't lock itself out


def test_gate_off_when_setting_disabled(monkeypatch) -> None:
    _present(monkeypatch, profiles=1)
    monkeypatch.setattr(settings, "SPEAKER_GATE_DESTRUCTIVE", False)
    assert speaker.should_gate() is False


def test_gate_on_when_enabled_and_enrolled(monkeypatch) -> None:
    _present(monkeypatch, profiles=1)
    assert speaker.should_gate() is True


@pytest.mark.asyncio
async def test_identify_now_returns_active_for_alex(monkeypatch) -> None:
    _present(monkeypatch, profiles=1)
    speaker.set_active("alex")
    # buffer empty → identify_now keeps the current active speaker (the user) → proceeds
    assert await speaker.identify_now() == "alex"


@pytest.mark.asyncio
async def test_identify_now_none_for_guest(monkeypatch) -> None:
    _present(monkeypatch, profiles=1)
    speaker.set_active(None)
    assert await speaker.identify_now() is None  # → destructive gate would refuse


# ---- resemblyzer degrade ----------------------------------------------------


def test_feed_audio_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(speaker, "_available", lambda: False)
    speaker.feed_audio(np.zeros(2400, dtype=np.int16).tobytes(), 24000)  # no error, no buffering


@pytest.mark.asyncio
async def test_embed_audio_raises_when_resemblyzer_missing() -> None:
    # resemblyzer is not installed in this env → embed_audio raises the typed error
    import importlib.util

    if importlib.util.find_spec("resemblyzer") is not None:
        pytest.skip("resemblyzer installed; degrade path not exercised")
    with pytest.raises(vp.SpeakerIDUnavailable):
        vp.embed_audio(np.zeros(16000, dtype=np.float32), 16000)


@pytest.mark.asyncio
async def test_identify_now_degrades_silently_without_resemblyzer(monkeypatch) -> None:
    # buffer has audio but resemblyzer missing → no exception escapes, keeps active
    monkeypatch.setattr(speaker, "_available", lambda: True)  # pass the tap guard
    speaker.feed_audio(np.ones(24000, dtype=np.int16).tobytes(), 24000)
    speaker.set_active("alex")
    import importlib.util
    if importlib.util.find_spec("resemblyzer") is not None:
        pytest.skip("resemblyzer installed")
    assert await speaker.identify_now() == "alex"  # SpeakerIDUnavailable → kept the user
