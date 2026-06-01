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
    async def test_missing_package_raises_clear_systemexit(self, monkeypatch):
        # Ensure the import fails even if pvporcupine gets installed later.
        monkeypatch.setitem(sys.modules, "pvporcupine", None)
        monkeypatch.setattr(settings, "WAKE_WORD_ENGINE", "pvporcupine")

        with pytest.raises(SystemExit) as exc:
            await wake_word._listen_porcupine()
        assert "pvporcupine" in str(exc.value)
