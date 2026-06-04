"""19.7: tests for the voice acceptance harness itself (no audio, no HTTP)."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.acceptance import audio_gen
from tests.acceptance.runner import _run_all_voice, load_scenarios


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"\x00\x00" * 2400):
        self.status_code = status_code
        self.content = content
        self.text = "boom" if status_code != 200 else ""


class TestAudioGenCache:
    def test_second_synthesize_skips_http(self, monkeypatch, tmp_path):
        monkeypatch.setattr(audio_gen, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(audio_gen, "MANIFEST_PATH", tmp_path / "manifest.json")
        post = MagicMock(return_value=_FakeResponse())
        monkeypatch.setattr(audio_gen.httpx, "post", post)

        p1 = audio_gen.synthesize("hola", scenario_id="T1")
        p2 = audio_gen.synthesize("hola", scenario_id="T1")

        assert p1 == p2 and p1.exists()
        assert post.call_count == 1  # cache hit on the second call

    def test_bad_api_key_friendly_spanish_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(audio_gen, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(audio_gen, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(audio_gen.httpx, "post", MagicMock(return_value=_FakeResponse(401)))

        with pytest.raises(audio_gen.VoiceGenError) as exc:
            audio_gen.synthesize("hola distinta")
        assert "API key" in str(exc.value)
        assert "Traceback" not in str(exc.value)

    def test_manifest_records_text_and_scenario(self, monkeypatch, tmp_path):
        monkeypatch.setattr(audio_gen, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(audio_gen, "MANIFEST_PATH", tmp_path / "manifest.json")
        monkeypatch.setattr(audio_gen.httpx, "post", MagicMock(return_value=_FakeResponse()))
        audio_gen.synthesize("qué onda", scenario_id="T9")
        manifest = audio_gen._manifest_load()
        entry = next(iter(manifest.values()))
        assert entry["text"] == "qué onda"
        assert entry["scenario_id"] == "T9"


class TestPlaybackDeviceResolver:
    def _fake_sd(self, devices: list[dict[str, Any]]):
        mod = types.SimpleNamespace(query_devices=lambda: devices)
        return mod

    def test_matches_by_name_substring(self, monkeypatch):
        from core import audio_devices

        devices = [
            {"name": "MacBook Air Microphone", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "MacBook Air Speakers", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
        ]
        monkeypatch.setitem(sys.modules, "sounddevice", self._fake_sd(devices))
        assert audio_devices.find_device_index("blackhole", kind="input") == 2
        assert audio_devices.find_device_index("BlackHole", kind="output") == 2
        assert audio_devices.find_device_index("Speakers", kind="output") == 1
        assert audio_devices.find_device_index("Speakers", kind="input") is None
        assert audio_devices.find_device_index("nope", kind="input") is None

    def test_production_returns_none(self, monkeypatch):
        from config.settings import settings
        from core import audio_devices

        monkeypatch.setattr(settings, "EMMA_TEST_MODE", False)
        monkeypatch.setattr(settings, "EMMA_TEST_INPUT_DEVICE", "BlackHole")
        assert audio_devices.test_input_device_index() is None


class TestRunnerVoiceMode:
    def test_voice_mode_routes_through_mocked_lifecycle(self, monkeypatch):
        from tests.acceptance import voice_driver

        scenario = {
            "id": "T01",
            "name": "fake",
            "language": "es",
            "utterance": "Hey Emma, prueba.",
            "expected_actions": [{"tool": "current_time"}],
            "expected_spoken_pattern": "listo",
        }
        fake_proc = MagicMock()
        fake_proc.stop.return_value = 0

        def fake_run(s, *, input_device, output_device, proc=None):
            extras = voice_driver.VoiceExtras(
                audio_path="/fake.wav", wake_detected=True, wake_latency_ms=900, transcript="prueba"
            )
            summary = {
                "tool_calls": [{"name": "current_time", "args": {}, "success": True}],
                "spoken_text": "Listo.",
                "turn_ms": 1234,
            }
            return summary, extras, fake_proc

        monkeypatch.setattr(voice_driver, "run_voice_scenario", fake_run)
        outcomes, extras_by_id, _chars = _run_all_voice([scenario], "", "")

        assert [o.status for o in outcomes] == ["PASS"]
        assert extras_by_id["T01"].wake_latency_ms == 900
        fake_proc.stop.assert_called_once()  # daemon torn down at the end

    def test_transcript_assertion_fails_on_mismatch(self):
        from tests.acceptance.voice_driver import VoiceExtras, check_voice_extras

        scenario = {"expected_transcript_pattern": "tokio"}
        extras = VoiceExtras(transcript="qué hora es en Londres")
        issues = check_voice_extras(scenario, extras)
        assert issues and "tokio" in issues[0]

    def test_capability_gap_assertion(self):
        from tests.acceptance.voice_driver import VoiceExtras, check_voice_extras

        scenario = {"expected_no_capability_gaps": True}
        extras = VoiceExtras(capability_gaps=[{"tool": "x", "success": False}])
        assert check_voice_extras(scenario, extras)
        extras_ok = VoiceExtras(capability_gaps=[])
        assert check_voice_extras(scenario, extras_ok) == []


class TestCorpusYAMLLoadable:
    def test_eighty_scenarios_loadable_with_mock_text(self):
        scenarios = load_scenarios()
        assert len(scenarios) >= 80
        ids = [s["id"] for s in scenarios]
        assert len(ids) == len(set(ids)), "duplicate scenario ids"
        for s in scenarios:
            assert s.get("mock_spoken_text"), f"{s['id']} lacks mock_spoken_text (CI mock mode)"
            assert s.get("utterance"), f"{s['id']} lacks an utterance"

    def test_follows_chains_reference_existing_ids(self):
        scenarios = load_scenarios()
        ids = {s["id"] for s in scenarios}
        for s in scenarios:
            if s.get("follows"):
                assert s["follows"] in ids, f"{s['id']} follows unknown id {s['follows']}"
