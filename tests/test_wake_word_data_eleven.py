"""Tests for scripts/wake_word_data_eleven.py (Prompt 16.2.1).

Every test mocks the network — no ElevenLabs call is ever made. We verify the
on-disk layout matches what train_wake_word.py reads (positive/ + negative/,
16 kHz mono 16-bit WAV), the cost estimate, and honest error handling.
"""

from __future__ import annotations

import importlib.util
import wave
from pathlib import Path

import httpx
import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "wake_word_data_eleven.py"


def _load():
    spec = importlib.util.spec_from_file_location("wake_word_data_eleven", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


mod = _load()

# 0.25 s of silence at 16 kHz, 16-bit mono — a stand-in for ElevenLabs pcm_16000.
_FAKE_PCM = b"\x00\x00" * 4000


def _fake_fetch(text, voice_id, voice_settings, on_retry=None):
    return _FAKE_PCM


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = _FAKE_PCM


def test_pcm_to_wav_is_16k_mono_16bit():
    raw = mod._pcm_to_wav(_FAKE_PCM)
    import io

    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getframerate() == 16_000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2


def test_generate_writes_expected_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_fetch", _fake_fetch)
    n_pos, n_neg = mod.generate(
        tmp_path,
        phrases=["hey emma", "oye emma"],
        neg_phrases=["hola", "adios"],
        voices=["voiceA", "voiceB"],
        n_pos=3,
        n_neg=2,
    )
    # 2 voices * 2 phrases * 3 = 12 positives; 2 voices * 2 neg-phrases * 2 = 8 negatives
    assert n_pos == 12
    assert n_neg == 8
    pos = list((tmp_path / "positive").glob("*.wav"))
    neg = list((tmp_path / "negative").glob("*.wav"))
    assert len(pos) == 12
    assert len(neg) == 8


def test_generated_wavs_are_readable_at_16k(tmp_path, monkeypatch):
    """train_wake_word._embed_dir requires sr==16000 — assert that holds."""
    monkeypatch.setattr(mod, "_fetch", _fake_fetch)
    mod.generate(tmp_path, ["hey emma"], ["hola"], ["voiceA"], n_pos=1, n_neg=1)
    for sub in ("positive", "negative"):
        wav = next((tmp_path / sub).glob("*.wav"))
        with wave.open(str(wav), "rb") as w:
            assert w.getframerate() == 16_000
            assert w.getnchannels() == 1


def test_estimate_counts_clips_and_chars():
    clips, chars = mod.estimate(
        phrases=["hey emma"],          # 8 chars
        neg_phrases=["hola"],          # 4 chars
        voices=["v1", "v2"],
        n_pos=2,
        n_neg=3,
    )
    # positives: 2 voices * 1 phrase * 2 = 4 clips, 4*8 chars
    # negatives: 2 voices * 1 phrase * 3 = 6 clips, 6*4 chars
    assert clips == 10
    assert chars == 4 * 8 + 6 * 4


def test_default_voices_dedupes(monkeypatch):
    monkeypatch.setattr(mod.settings, "ELEVENLABS_VOICE_ID_ES", "same")
    monkeypatch.setattr(mod.settings, "ELEVENLABS_VOICE_ID_EN", "same")
    assert mod.default_voices() == ["same"]


def test_fetch_raises_friendly_error_on_401(monkeypatch):
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp(401, "unauthorized"))
    with pytest.raises(mod.VoiceGenError, match="401"):
        mod._fetch("hey emma", "voiceA", {})


def test_fetch_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp(500, "server error"))
    with pytest.raises(mod.VoiceGenError, match="500"):
        mod._fetch("hey emma", "voiceA", {})


def test_fetch_402_raises_out_of_credits(monkeypatch):
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp(402, "payment required"))
    with pytest.raises(mod.OutOfCreditsError, match="créditos"):
        mod._fetch("hey emma", "voiceA", {})


def test_fetch_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)  # no real backoff in tests
    calls = {"n": 0}
    waits: list[float] = []

    def _post(*a, **k):
        calls["n"] += 1
        return _Resp(200) if calls["n"] >= 3 else _Resp(429, "slow down")

    monkeypatch.setattr(mod.httpx, "post", _post)
    out = mod._fetch("hey emma", "voiceA", {}, on_retry=waits.append)
    assert out == _FAKE_PCM
    assert calls["n"] == 3          # two 429s, then success
    assert waits == [1.0, 2.0]      # exponential backoff surfaced to caller


def test_fetch_429_exhausts_then_raises(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp(429, "slow down"))
    with pytest.raises(mod.VoiceGenError, match="limitando"):
        mod._fetch("hey emma", "voiceA", {})


def test_generate_progress_callback_reports_counts_and_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_fetch", _fake_fetch)
    events: list[tuple] = []
    mod.generate(
        tmp_path, ["hey emma"], ["hola"], ["voiceA"], n_pos=2, n_neg=1,
        progress_cb=lambda kind, done, total, chars: events.append((kind, done, total, chars)),
    )
    # 2 positives then 1 negative; chars accumulate across the whole run.
    assert [e[0] for e in events] == ["positive", "positive", "negative"]
    assert events[0][1:3] == (1, 2)        # done=1, total=2 positives
    assert events[-1][3] == 8 + 8 + 4      # "hey emma"*2 + "hola"


def test_fetch_wraps_network_error(monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(mod.httpx, "post", _boom)
    with pytest.raises(mod.VoiceGenError, match="conectar"):
        mod._fetch("hey emma", "voiceA", {})


def test_fetch_returns_pcm_on_200(monkeypatch):
    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp(200))
    assert mod._fetch("hey emma", "voiceA", {}) == _FAKE_PCM
