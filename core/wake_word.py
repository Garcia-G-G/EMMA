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
import contextlib
import threading
import time
from collections.abc import Callable
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

# Project root, so a relative WAKE_WORD_PATH (e.g. "wake_words/emma.ppn") resolves
# the same whether the daemon launches from the repo or from launchd's cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve(raw: str) -> Path:
    """Expand ~ and anchor a relative wake-word path to the project root."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (_PROJECT_ROOT / p)


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

        builtin_names = {
            "alexa",
            "hey_mycroft",
            "hey_jarvis",
            "hey_rhasspy",
            "weather",
            "timer",
        }
        raw = settings.WAKE_WORD_PATH

        if raw in builtin_names:
            wakeword_arg = raw
            framework = "onnx"
            source_label = f"<built-in: {raw}>"
        else:
            path = Path(raw).expanduser()
            if not path.exists():
                builtins = ", ".join(sorted(builtin_names))
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


def _make_near_miss_logger(
    threshold: float, floor: float = 0.10, interval_s: float = 1.0
) -> Callable[[float], None]:
    """Rate-limited logger for sub-threshold wake scores.

    Garcia's Spanish-accented "hey jarvis" often scores well below the
    English-TTS ~0.99 — without seeing those scores we can't tune
    WAKE_WORD_THRESHOLD with data. Scores in [floor, threshold) are logged at
    most once per ``interval_s``; ambient noise (< floor) stays silent.
    """
    last = 0.0

    def note(score: float) -> None:
        nonlocal last
        if score < floor or score >= threshold:
            return
        now = time.monotonic()
        if now - last >= interval_s:
            last = now
            log.info("wake_score_near_miss", score=round(score, 3), threshold=threshold)

    return note


def _close_stream_background(stream: Any) -> None:
    """Abort + close a PortAudio stream off the event loop.

    sounddevice ``stop``/``close`` can block indefinitely on the CoreAudio HAL
    mutex (same hazard as ``check_microphone``). During shutdown we must not let
    that hang the cancellation, so we abort (drop buffers immediately) and close
    in a daemon thread we never join — if it wedges, it dies with the process.
    """

    def _close() -> None:
        for op in ("abort", "close"):
            with contextlib.suppress(Exception):
                getattr(stream, op)()

    threading.Thread(target=_close, name="wake-stream-close", daemon=True).start()


async def _listen_openwakeword() -> None:
    """Block until the openWakeWord model fires, then return. Plays an ack tone."""
    model = await _get_model()

    # Clear retained state from any prior detection before listening
    # again - otherwise the model fires "wake" on its own afterglow.
    _reset_model(model)

    detected = asyncio.Event()
    loop = asyncio.get_running_loop()
    threshold = settings.WAKE_WORD_THRESHOLD
    name = settings.WAKE_WORD_NAME
    note_near_miss = _make_near_miss_logger(threshold)

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        try:
            samples = np.frombuffer(bytes(indata), dtype=np.int16)  # type: ignore[call-overload]
            predictions = model.predict(samples)
            score = float(predictions.get(name, 0.0))
            if score >= threshold:
                loop.call_soon_threadsafe(detected.set)
            else:
                note_near_miss(score)
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
    except asyncio.CancelledError:
        # Shutdown (Ctrl+C / SIGTERM): close in the background so a blocking
        # PortAudio close can't hang the exit, then propagate the cancel.
        log.info("wake_listen_cancelled")
        _close_stream_background(stream)
        _reset_model(model)
        raise

    # Normal detection path: close synchronously so the next listen reopens
    # a fresh stream. Reset clears openWakeWord's afterglow (otherwise it
    # re-fires "wake" on the next loop). Model stays warm; only state clears.
    stream.stop()
    stream.close()
    _reset_model(model)
    play_wake_chime()


async def _listen_porcupine() -> None:
    """Block until Picovoice Porcupine detects the wake word, then return.

    Porcupine ships a self-contained ``.ppn`` keyword model (trained in the
    Picovoice Console) and a tiny C engine: ``process()`` takes one frame of
    ``frame_length`` int16 samples at ``sample_rate`` (16 kHz) and returns the
    matched keyword index (``>= 0``) or ``-1``. We feed it straight from the
    sounddevice callback, mirroring the openWakeWord branch's lifecycle
    (background close on cancel, ack chime on hit).
    """
    try:
        import pvporcupine
    except ImportError as exc:
        raise SystemExit(
            "WAKE_WORD_ENGINE=pvporcupine but the 'pvporcupine' package is not "
            "installed. Run `uv add pvporcupine` (path A), or set "
            "WAKE_WORD_ENGINE=openwakeword in .env to use the open-source engine."
        ) from exc

    key = settings.PICOVOICE_ACCESS_KEY
    if not key:
        raise SystemExit(
            "WAKE_WORD_ENGINE=pvporcupine but PICOVOICE_ACCESS_KEY is missing. "
            "Add it to .env (from https://console.picovoice.ai/) — it migrates "
            "to Keychain on the next install."
        )

    model_path = _resolve(settings.WAKE_WORD_PATH)
    if not model_path.exists():
        raise SystemExit(
            f"Porcupine keyword file not found at {model_path}. Train 'Emma' in "
            "the Picovoice Console, download the macOS .ppn, and drop it at "
            "wake_words/emma.ppn (matching WAKE_WORD_PATH)."
        )
    sensitivity = float(settings.WAKE_WORD_THRESHOLD)

    try:
        porcupine = await asyncio.to_thread(
            pvporcupine.create,
            access_key=key,
            keyword_paths=[str(model_path)],
            sensitivities=[sensitivity],
        )
    except Exception as exc:  # pvporcupine.PorcupineError + friends
        raise SystemExit(
            f"Failed to initialise Porcupine with {model_path}: {exc}. Check the "
            "AccessKey and that the .ppn was built for macOS (Apple Silicon)."
        ) from exc

    log.info(
        "wake_model_loaded",
        engine="pvporcupine",
        source=str(model_path),
        sensitivity=sensitivity,
        name=settings.WAKE_WORD_NAME,
    )

    detected = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _cb(indata: object, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        try:
            samples = np.frombuffer(bytes(indata), dtype=np.int16)  # type: ignore[call-overload]
            if porcupine.process(samples) >= 0:
                loop.call_soon_threadsafe(detected.set)
        except Exception as exc:
            log.warning("wake_predict_failed", error=str(exc))

    stream = sd.RawInputStream(
        samplerate=porcupine.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=porcupine.frame_length,
        callback=_cb,
    )
    stream.start()
    try:
        await detected.wait()
    except asyncio.CancelledError:
        log.info("wake_listen_cancelled")
        _close_stream_background(stream)
        with contextlib.suppress(Exception):
            porcupine.delete()
        raise

    log.info("wake_detected", engine="pvporcupine")
    stream.stop()
    stream.close()
    with contextlib.suppress(Exception):
        porcupine.delete()
    play_wake_chime()


async def listen_for_wake_word() -> None:
    """Block until the configured wake word fires, then return (plays a chime).

    Engine is selected by ``WAKE_WORD_ENGINE`` (``pvporcupine`` or, by default,
    ``openwakeword``). An unset/unknown value falls back to openWakeWord so a
    broken ``.env`` never bricks wake detection — the openWakeWord branch itself
    falls back to the built-in ``hey_jarvis`` model when WAKE_WORD_PATH is unset.
    """
    engine = (settings.WAKE_WORD_ENGINE or "openwakeword").strip().lower()
    if engine == "pvporcupine":
        await _listen_porcupine()
    else:
        if engine != "openwakeword":
            log.warning("wake_engine_unknown", engine=engine, falling_back="openwakeword")
        await _listen_openwakeword()


# Alias for standalone scripts / docs that call wait_for_wake().
wait_for_wake = listen_for_wake_word
