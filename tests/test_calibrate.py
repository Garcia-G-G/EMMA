"""Prompt 35 — echo calibration logic (audio capture mocked)."""

from __future__ import annotations

import json

import emma.calibrate as cal


def test_recommend_basic_orders_thresholds() -> None:
    r = cal.recommend_thresholds(noise_rms=500, echo_rms=6000, voice_rms=20000)
    assert r["ok"] is True
    # spike sits above the echo floor and at/below the measured voice
    assert 6000 < r["BARGE_IN_RMS_SPIKE"] <= 20000
    # window barge-in is gentler than the instant spike
    assert r["BARGE_IN_RMS_WINDOW"] < r["BARGE_IN_RMS_SPIKE"]


def test_recommend_spike_stays_under_voice() -> None:
    r = cal.recommend_thresholds(noise_rms=500, echo_rms=6000, voice_rms=9000)
    assert r["BARGE_IN_RMS_SPIKE"] <= 9000
    assert r["BARGE_IN_RMS_WINDOW"] < r["BARGE_IN_RMS_SPIKE"]


def test_recommend_warns_when_voice_not_above_echo() -> None:
    r = cal.recommend_thresholds(noise_rms=500, echo_rms=10000, voice_rms=11000)
    assert r["ok"] is False
    assert r["note"]


def test_load_calibration_missing_returns_empty(tmp_path) -> None:
    assert cal.load_calibration(tmp_path / "nope.json") == {}


def test_load_calibration_reads_file(tmp_path) -> None:
    p = tmp_path / "calibration.json"
    p.write_text(json.dumps({"BARGE_IN_RMS_SPIKE": 12345, "BARGE_IN_RMS_WINDOW": 6789}))
    got = cal.load_calibration(p)
    assert got["BARGE_IN_RMS_SPIKE"] == 12345
    assert got["BARGE_IN_RMS_WINDOW"] == 6789


def test_rms_of_silence_is_zero() -> None:
    import numpy as np

    assert cal._rms(np.zeros(2400, dtype=np.float64)) == 0.0
