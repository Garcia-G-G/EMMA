"""Audio device resolution for the voice acceptance harness (19.7-VAH2).

Production Emma always uses the system default mic/speakers — these helpers
return ``None`` unless ``EMMA_TEST_MODE`` is on AND a test device is named,
so the production audio path is byte-for-byte unchanged.

Devices are matched by NAME SUBSTRING ("BlackHole"), never by index — PortAudio
indexes shift as devices come and go. sounddevice and Pipecat's PyAudio share
PortAudio's device numbering, so an index resolved here is valid for both.

Source: python-sounddevice.readthedocs.io/en/latest/api/checking-hardware.html
"""

from __future__ import annotations

from typing import Any

import structlog

from config.settings import settings

log = structlog.get_logger("emma.audio_devices")


def find_device_index(name_substr: str, *, kind: str) -> int | None:
    """PortAudio index of the first ``kind`` ("input"|"output") device whose
    name contains ``name_substr`` (case-insensitive). None if no match."""
    q = (name_substr or "").strip().lower()
    if not q:
        return None
    import sounddevice as sd  # lazy: importing PortAudio touches CoreAudio

    chan_key = "max_input_channels" if kind == "input" else "max_output_channels"
    devices: Any = sd.query_devices()
    for idx, dev in enumerate(devices):
        if dev.get(chan_key, 0) > 0 and q in str(dev.get("name", "")).lower():
            return idx
    return None


def test_input_device_index() -> int | None:
    """The harness-configured input device, or None (= system default).

    Only active when EMMA_TEST_MODE is set — production always gets None.
    """
    if not settings.EMMA_TEST_MODE or not settings.EMMA_TEST_INPUT_DEVICE:
        return None
    idx = find_device_index(settings.EMMA_TEST_INPUT_DEVICE, kind="input")
    if idx is None:
        log.warning(
            "test_input_device_not_found",
            wanted=settings.EMMA_TEST_INPUT_DEVICE,
            falling_back="system default",
        )
    else:
        log.info("test_input_device_active", device=settings.EMMA_TEST_INPUT_DEVICE, index=idx)
    return idx
