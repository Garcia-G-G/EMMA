"""Wake-word engine selector (Phase 16).

``core.wake_word.listen_for_wake_word`` dispatches on ``WAKE_WORD_ENGINE``:

- default / unknown  → the openWakeWord branch (``_listen_openwakeword``),
- ``"pvporcupine"``   → the Picovoice branch (``_listen_porcupine``).

These tests mock the engines / SDKs so they run with no microphone and no
``pvporcupine`` install. The Picovoice SDK is injected into ``sys.modules`` as
a fake so the lazy ``import pvporcupine`` inside the branch resolves.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import settings
from core import wake_word


@pytest.fixture
def quiet_chime(monkeypatch):
    """Never actually play the ack tone during tests."""
    monkeypatch.setattr(wake_word, "play_wake_chime", lambda: None)


class TestEngineSelector:
    @pytest.mark.asyncio
    async def test_default_engine_picks_openwakeword(self, monkeypatch):
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "openwakeword")
        oww = AsyncMock()
        pv = AsyncMock()
        monkeypatch.setattr(wake_word, "_listen_openwakeword", oww)
        monkeypatch.setattr(wake_word, "_listen_porcupine", pv)

        await wake_word.listen_for_wake_word()

        oww.assert_awaited_once()
        pv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_engine_falls_back_to_openwakeword(self, monkeypatch):
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "totally-bogus")
        oww = AsyncMock()
        pv = AsyncMock()
        monkeypatch.setattr(wake_word, "_listen_openwakeword", oww)
        monkeypatch.setattr(wake_word, "_listen_porcupine", pv)

        await wake_word.listen_for_wake_word()

        oww.assert_awaited_once()
        pv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pvporcupine_engine_picks_porcupine_branch(self, monkeypatch):
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "pvporcupine")
        oww = AsyncMock()
        pv = AsyncMock()
        monkeypatch.setattr(wake_word, "_listen_openwakeword", oww)
        monkeypatch.setattr(wake_word, "_listen_porcupine", pv)

        await wake_word.listen_for_wake_word()

        pv.assert_awaited_once()
        oww.assert_not_awaited()


def _install_fake_porcupine(monkeypatch, process_returns=0):
    """Inject a fake ``pvporcupine`` module; return (module, engine instance)."""
    engine = MagicMock()
    engine.frame_length = 512
    engine.sample_rate = 16000
    engine.process = MagicMock(return_value=process_returns)
    engine.delete = MagicMock()

    module = types.ModuleType("pvporcupine")
    module.create = MagicMock(return_value=engine)
    monkeypatch.setitem(sys.modules, "pvporcupine", module)
    return module, engine


class _FakeStream:
    """A sounddevice stream stub that delivers exactly one frame on start()."""

    def __init__(self, **kwargs):
        self._cb = kwargs["callback"]
        self._blocksize = kwargs["blocksize"]

    def start(self):
        # One frame of silence — enough to drive porcupine.process() once.
        self._cb(b"\x00\x00" * self._blocksize, self._blocksize, None, None)

    def stop(self):
        pass

    def close(self):
        pass


class TestPorcupineBranch:
    @pytest.mark.asyncio
    async def test_create_called_with_sensitivity_from_settings(
        self, monkeypatch, tmp_path, quiet_chime
    ):
        ppn = tmp_path / "emma.ppn"
        ppn.write_bytes(b"fake-ppn")
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "pvporcupine")
        monkeypatch.setattr(settings, "PICOVOICE_ACCESS_KEY", "test-access-key")
        monkeypatch.setattr(settings, "WAKE_WORD_PATH", str(ppn))
        monkeypatch.setattr(settings, "WAKE_WORD_THRESHOLD", 0.55)
        # The fake stream delivers its single frame at t=0; disable the Layer-B
        # wake warmup so that frame isn't suppressed as boundary echo.
        monkeypatch.setattr(settings, "WAKE_WARMUP_MS", 0)
        module, engine = _install_fake_porcupine(monkeypatch, process_returns=0)
        monkeypatch.setattr(wake_word.sd, "RawInputStream", _FakeStream)

        # process()>=0 on the single delivered frame → detection → returns.
        await wake_word._listen_porcupine()

        module.create.assert_called_once()
        _, kwargs = module.create.call_args
        assert kwargs["access_key"] == "test-access-key"
        assert kwargs["keyword_paths"] == [str(ppn)]
        assert kwargs["sensitivities"] == [0.55]
        engine.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_access_key_raises_systemexit(self, monkeypatch, tmp_path):
        ppn = tmp_path / "emma.ppn"
        ppn.write_bytes(b"fake-ppn")
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "pvporcupine")
        monkeypatch.setattr(settings, "PICOVOICE_ACCESS_KEY", "")
        monkeypatch.setattr(settings, "WAKE_WORD_PATH", str(ppn))
        _install_fake_porcupine(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            await wake_word._listen_porcupine()
        assert "PICOVOICE_ACCESS_KEY" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_ppn_file_raises_systemexit(self, monkeypatch, tmp_path):
        missing = tmp_path / "nope.ppn"  # never created
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "pvporcupine")
        monkeypatch.setattr(settings, "PICOVOICE_ACCESS_KEY", "test-access-key")
        monkeypatch.setattr(settings, "WAKE_WORD_PATH", str(missing))
        _install_fake_porcupine(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            await wake_word._listen_porcupine()
        assert str(missing) in str(exc.value) or "not found" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_package_raises_clear_systemexit(self, monkeypatch):
        # Ensure the import fails even if pvporcupine gets installed later.
        monkeypatch.setitem(sys.modules, "pvporcupine", None)
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "pvporcupine")

        with pytest.raises(SystemExit) as exc:
            await wake_word._listen_porcupine()
        assert "pvporcupine" in str(exc.value)


class TestNearMissLogger:
    """Sub-threshold wake scores must be visible in debug logs (accent tuning)."""

    def test_logs_near_miss_between_floor_and_threshold(self, monkeypatch):
        calls: list[float] = []
        monkeypatch.setattr(wake_word.log, "info", lambda event, **kw: calls.append(kw["score"]))
        note = wake_word._make_near_miss_logger(threshold=0.5, floor=0.1, interval_s=0.0)
        note(0.30)
        assert calls == [0.3]

    def test_ignores_floor_noise_and_hits(self, monkeypatch):
        calls: list[float] = []
        monkeypatch.setattr(wake_word.log, "info", lambda event, **kw: calls.append(kw["score"]))
        note = wake_word._make_near_miss_logger(threshold=0.5, floor=0.1, interval_s=0.0)
        note(0.05)  # below floor: ambient noise, stay quiet
        note(0.80)  # above threshold: that's a detection, not a near miss
        assert calls == []

    def test_rate_limited(self, monkeypatch):
        calls: list[float] = []
        monkeypatch.setattr(wake_word.log, "info", lambda event, **kw: calls.append(kw["score"]))
        note = wake_word._make_near_miss_logger(threshold=0.5, floor=0.1, interval_s=60.0)
        note(0.30)
        note(0.40)  # within the rate-limit window: dropped
        assert calls == [0.3]
