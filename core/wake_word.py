"""openWakeWord-driven wake-word listener.

The custom 'Hey Emma' ONNX model is trained per-user via the Colab
linked in the README (see "Wake word"). The model file is a
user-provided asset; this module loads it lazily on the first call to
``listen_for_wake_word()`` and keeps the constructed Model warm across
turns - rebuilding it on every wake is far more expensive than
predicting against it.

openWakeWord's ``Model.predict`` expects 80 ms chunks of int16 mono
audio at 16 kHz (= 1280 samples). The sounddevice ``RawInputStream``
callback delivers exactly that, runs ``predict``, and signals the
awaiting coroutine via ``loop.call_soon_threadsafe`` when the
configured wake-word's score crosses the threshold.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
import structlog

from config.settings import settings
from core.audio import play_wake_chime

log = structlog.get_logger("emma.wake_word")

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz - openWakeWord's expected input size

_model: Any = None
_model_lock = asyncio.Lock()


async def _get_model() -> Any:
    """Lazy-construct (and keep warm) the openWakeWord Model singleton."""
    global _model
    async with _model_lock:
        if _model is not None:
            return _model

        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise SystemExit(
                f"openwakeword failed to import ({exc}). Run `uv sync` and retry."
            ) from exc

        BUILTIN_NAMES = {
            "alexa", "hey_mycroft", "hey_jarvis",
            "hey_rhasspy", "weather", "timer",
        }
        raw = settings.WAKE_WORD_PATH

        if raw in BUILTIN_NAMES:
            wakeword_arg = raw
            framework = "onnx"
            source_label = f"<built-in: {raw}>"
        else:
            path = Path(raw).expanduser()
            if not path.exists():
                builtins = ", ".join(sorted(BUILTIN_NAMES))
                raise SystemExit(
                    f"Wake word '{raw}' is neither a file nor a built-in. "
                    f"Either set WAKE_WORD_PATH to one of: {builtins}, "
                    "or train a custom .onnx via the Colab linked in README.md."
                )
            wakeword_arg = str(path)
            framework = "onnx"
            source_label = str(path)

        try:
            _model = await asyncio.to_thread(
                Model,
                wakeword_models=[wakeword_arg],
                inference_framework=framework,
            )
        except Exception as exc:
            raise SystemExit(
                f"Failed to load wake-word model at {source_label}: {exc}. "
                "See the 'Wake word' section in README.md."
            ) from exc

        log.info(
            "wake_model_loaded",
            source=source_label,
            framework=framework,
            name=settings.WAKE_WORD_NAME,
        )
        return _model

def _reset_model(model: Any) -> None:
    """Clear openWakeWord's retained activation state.

    Without this, the model still produces a near-1.0 score for several
    seconds after a real detection - which fires `wake` again the
    instant the orchestrator loops back to listen, producing a tone
    loop. ``Model.reset`` exists in openWakeWord 0.6+; we guard against
    its absence in case the SDK shifts.
    """
    try:
        model.reset()
    except AttributeError:
        pass
    except Exception as exc:
        log.warning("wake_model_reset_failed", error=str(exc))


async def listen_for_wake_word() -> None:
    """Block until 'Hey Emma' is detected, then return. Plays an ack tone."""
    model = await _get_model()

    # Clear retained state from any prior detection before listening
    # again - otherwise the model fires "wake" on its own afterglow.
    _reset_model(model)

    detected = asyncio.Event()
    loop = asyncio.get_running_loop()
    threshold = settings.WAKE_WORD_THRESHOLD
    name = settings.WAKE_WORD_NAME

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        try:
            samples = np.frombuffer(bytes(indata), dtype=np.int16)  # type: ignore[arg-type]
            predictions = model.predict(samples)
            score = float(predictions.get(name, 0.0))
            if score >= threshold:
                loop.call_soon_threadsafe(detected.set)
        except Exception as exc:
            log.warning("wake_predict_failed", error=str(exc))

    stream = sd.RawInputStream(
        samplerate=_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=_CHUNK_SAMPLES,
        callback=_cb,
    )
    stream.start()
    try:
        await detected.wait()
    finally:
        stream.stop()
        stream.close()
        # Belt-and-suspenders: also reset after detection so the next
        # listen starts clean even if the next-turn entry reset is
        # somehow skipped. Model stays warm; only its state is cleared.
        _reset_model(model)
        # Note: we intentionally do NOT free `_model` - keep it warm.

    play_wake_chime()
