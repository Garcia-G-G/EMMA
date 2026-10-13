"""VOICE-1 Part 6 — the latency log and the report that reads it."""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.cascade import Latency
from emma import voice_report


def test_latency_records_turns_and_the_session(tmp_path: Path) -> None:
    log = tmp_path / "lat.jsonl"
    lat = Latency(wake_at=time.monotonic() - 1.0, path=log)
    lat.user_stopped()
    lat.audio_out()  # first audio: both measures
    lat.user_stopped()
    lat.audio_out()
    lat.audio_out()  # audio with no new turn: nothing
    lat.close()
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["turn", "turn", "session"]
    assert rows[0]["wake_to_audio_ms"] >= 1000 and "stop_to_audio_ms" in rows[0]
    assert "wake_to_audio_ms" not in rows[1]
    assert all("text" not in r and "utterance" not in r for r in rows)  # timings only


def test_report_percentiles_minutes_and_spend() -> None:
    rows = [{"kind": "turn", "stop_to_audio_ms": float(ms)} for ms in range(100, 1100, 100)]
    rows += [{"kind": "turn", "wake_to_audio_ms": 1500.0}, {"kind": "session", "seconds": 120}]
    lines, minutes = voice_report.latency_section(rows)
    assert minutes == 2.0 and "p50 600 ms" in lines[0] and "p95 1000 ms" in lines[0]
    spend = voice_report.spend_section({"STT": 100.0, "TTS": 900.0}, minutes, 0.22)
    assert "total 1000" in spend[0] and "$0.220 over 2.0 min = $0.1100/min" in spend[1]
    assert "unavailable" in voice_report.spend_section({}, 1.0, 0.22)[0]
