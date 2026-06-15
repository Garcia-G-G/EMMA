"""Echo / barge-in calibration CLI (Prompt 35): ``python -m emma.calibrate``.

The echo-gate thresholds in ``config/settings.py`` were hand-measured on one
MacBook ("4000-12600 RMS"). This tool measures the *current* machine + room and
writes per-machine thresholds to ``~/.emma/calibration.json``, which the pipeline
loads over the defaults when building the EchoGateFilter. Refinement = data-driven
thresholds instead of hardcoded constants.

Three short recordings:
  1. silence  -> noise floor
  2. Emma talking (``say``) -> speaker-echo band
  3. you talking normally  -> voice level

No new dependency: ``sounddevice`` + ``numpy`` (already used by the wake word).
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

_CALIBRATION_PATH = Path.home() / ".emma" / "calibration.json"
_SAMPLE_RATE = 24000
_TUNED_KEYS = ("BARGE_IN_RMS_SPIKE", "BARGE_IN_RMS_WINDOW")


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return math.sqrt(float(np.mean(samples.astype(np.float64) ** 2)))


def recommend_thresholds(noise_rms: float, echo_rms: float, voice_rms: float) -> dict[str, Any]:
    """Compute barge-in thresholds from measured RMS bands. Pure / testable.

    The instant SPIKE threshold must sit above the echo floor (so Emma's own
    speaker output never self-interrupts) yet at/below the user's measured voice
    (so a real barge-in still registers). The WINDOW threshold is a gentler,
    sustained-speech trigger and stays below the spike.
    """
    floor = max(float(noise_rms), float(echo_rms))
    ok = voice_rms >= floor * 1.4  # need clear separation to trust the numbers

    spike = floor * 1.6
    window = floor * 1.25
    if voice_rms > 0:
        spike = min(spike, voice_rms * 0.7)
        window = min(window, voice_rms * 0.55)
    spike = max(spike, floor + 2000)
    window = max(window, floor + 1200)
    window = min(window, spike - 500)  # window must stay under the spike

    note = (
        "" if ok else
        "La voz no quedó suficientemente por encima del eco. Repite en un lugar "
        "más silencioso y hablando un poco más fuerte para una mejor calibración."
    )
    return {
        "ok": ok,
        "note": note,
        "BARGE_IN_RMS_SPIKE": round(spike),
        "BARGE_IN_RMS_WINDOW": round(window),
        "noise_rms": round(float(noise_rms)),
        "echo_rms": round(float(echo_rms)),
        "voice_rms": round(float(voice_rms)),
    }


def load_calibration(path: Path | None = None) -> dict[str, Any]:
    """Read saved per-machine thresholds. Returns {} if absent or unreadable."""
    p = path or _CALIBRATION_PATH
    try:
        data = json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}
    return {k: data[k] for k in _TUNED_KEYS if isinstance(data.get(k), (int, float))}


def _record(seconds: float) -> np.ndarray:
    """Capture mono int16 mic audio (real I/O; mocked in tests)."""
    import sounddevice as sd

    frames = int(seconds * _SAMPLE_RATE)
    buf = sd.rec(frames, samplerate=_SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return np.asarray(buf, dtype=np.int16).reshape(-1)


async def _say(text: str) -> None:
    proc = await asyncio.create_subprocess_exec("say", text)
    await proc.wait()


async def run_calibration(write: bool = True) -> dict[str, Any]:
    """Drive the three recordings, compute, and (optionally) persist. Spanish UX."""
    print("Calibración de Emma — quédate quieto un momento.\n")

    print("1) Silencio: no hables ni hagas ruido (2 s)...")
    noise = _rms(_record(2.0))

    print("2) Voy a hablar yo; tú no digas nada...")
    say_task = asyncio.create_task(_say("Hola, soy Emma. Estoy midiendo el eco de tus bocinas."))
    echo = _rms(_record(2.5))
    await say_task

    print('3) Ahora habla normal: di "Emma, qué hora es" (2 s)...')
    voice = _rms(_record(2.0))

    rec = recommend_thresholds(noise, echo, voice)
    print(
        f"\nMedidas — ruido: {rec['noise_rms']}, eco: {rec['echo_rms']}, voz: {rec['voice_rms']}\n"
        f"Recomendado — spike: {rec['BARGE_IN_RMS_SPIKE']}, ventana: {rec['BARGE_IN_RMS_WINDOW']}"
    )
    if not rec["ok"]:
        print(f"\n⚠ {rec['note']}\n(No se guardó nada.)")
        return rec

    if write:
        _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CALIBRATION_PATH.write_text(json.dumps(
            {k: rec[k] for k in _TUNED_KEYS}, indent=2
        ))
        print(f"\n✓ Guardado en {_CALIBRATION_PATH}. Reinicia a Emma para aplicarlo.")
    return rec


def main() -> int:
    try:
        asyncio.run(run_calibration())
        return 0
    except KeyboardInterrupt:
        print("\nCalibración cancelada.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
