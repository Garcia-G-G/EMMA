"""Always-on local wake detection via Vosk (offline streaming STT).

Unlike a single-phrase wake model (openWakeWord) or a keyword engine
(Porcupine), this keeps a continuous offline transcription running and fires
when it hears the wake phrase — Emma is "always listening" and answers to
"hey emma" / "oye emma". Audio never leaves the Mac (Vosk is fully local).

Robustness trick: the recognizer runs in GRAMMAR-constrained mode — it may only
emit one of the wake phrases or ``[unk]``, so it matches the user's accent
against just the phrases instead of the whole dictionary. On the user's real
recordings this lifted detection from 0/15 (free dictation) to 11/15.

Audio path mirrors core/wake_word.py: a 16 kHz mono int16 ``RawInputStream``,
the same warmup/echo suppression so Emma's own voice can't self-trigger, and an
``asyncio.Event`` signalled from the callback thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import sounddevice as sd
import structlog

from config.settings import settings
from core import audio_devices, runtime_state
from core.audio import play_wake_chime

log = structlog.get_logger("emma.speech_wake")

_SAMPLE_RATE = 16000
_BLOCK = 4000  # 250 ms chunks — frequent enough for snappy partial-result checks

# Wake phrases the grammar is restricted to. Mexican-Spanish + English phrasing;
# the bare "emma" makes it forgiving. "[unk]" lets the recognizer reject anything
# that isn't a wake phrase instead of forcing a (wrong) match.
WAKE_PHRASES = ("hey emma", "oye emma", "hola emma", "ey emma", "emma")
_GRAMMAR = json.dumps([*WAKE_PHRASES, "[unk]"])

_model: Any = None
_model_lock = asyncio.Lock()


def _normalize(text: str) -> str:
    """Lowercase + strip accents so 'Emma'/'éma' compare cleanly."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip()


def matches_wake(text: str) -> bool:
    """True if a Vosk (grammar-constrained) transcript names the wake word.

    Pure + testable: grammar mode only emits wake phrases or '[unk]', so any
    transcript carrying 'emma' (accent-stripped) is a hit.
    """
    norm = _normalize(text)
    if not norm or "[unk]" in norm:
        return False
    # Grammar mode only ever emits the wake phrases (all spelled "emma") or [unk],
    # so the doubled-m is a clean signal — "tema"/"ema" never reach here.
    return "emma" in norm


def _model_path() -> Path:
    return Path(settings.VOSK_MODEL_PATH).expanduser()


async def _get_model() -> Any:
    """Lazy-load and cache the Vosk model (load is ~1-2s; predicts are cheap)."""
    global _model
    async with _model_lock:
        if _model is not None:
            return _model
        try:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)  # silence Kaldi's stderr spew
        except ImportError as exc:
            raise SystemExit(
                f"WAKE_WORD_ENGINE=vosk but the 'vosk' package isn't installed ({exc}). "
                "Run `uv pip install vosk`."
            ) from exc
        path = _model_path()
        if not path.exists():
            raise SystemExit(
                f"Vosk model not found at {path}. Download one from "
                "https://alphacephei.com/vosk/models and set VOSK_MODEL_PATH."
            )
        _model = await asyncio.to_thread(Model, str(path))
        log.info("vosk_model_loaded", path=str(path), phrases=list(WAKE_PHRASES))
        return _model


def _close_stream_background(stream: Any) -> None:
    """Abort+close a PortAudio stream off the loop (close can block on the HAL)."""

    def _close() -> None:
        for op in ("abort", "close"):
            with contextlib.suppress(Exception):
                getattr(stream, op)()

    threading.Thread(target=_close, name="vosk-stream-close", daemon=True).start()


async def listen() -> None:
    """Block until the wake phrase is heard in the live transcription, then return.

    Plays the ack chime on a hit, mirroring the openWakeWord branch's lifecycle.
    """
    import queue

    from vosk import KaldiRecognizer

    model = await _get_model()
    detected = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Layer B: clear any stuck bot-speaking flag, then suppress detection during
    # the stream-open warmup so Emma's residual audio can't self-trigger.
    runtime_state.force_clear()
    warmup_s = 0.0 if settings.EMMA_TEST_MODE else settings.WAKE_WARMUP_MS / 1000.0
    open_mono = time.monotonic()

    # The PortAudio callback must return within the buffer deadline (~250 ms here),
    # but Kaldi's AcceptWaveform is a full forward pass that can overrun it →
    # dropped input + missed wakes (audit fix). So the callback ONLY enqueues raw
    # bytes (O(1), realtime-safe); a worker thread (which solely owns `rec`) does
    # the decode. This also removes the cancel-time callback↔teardown race.
    audio_q: queue.Queue[bytes] = queue.Queue(maxsize=64)
    stop = threading.Event()

    def _cb(indata: Any, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        if runtime_state.suppress_wake(open_mono, warmup_s):
            return  # warmup / bot speaking (Layer B)
        # drop a frame rather than block the realtime audio thread if the worker lags
        with contextlib.suppress(queue.Full):
            audio_q.put_nowait(bytes(indata))

    def _decode_worker() -> None:
        rec = KaldiRecognizer(model, _SAMPLE_RATE, _GRAMMAR)  # worker-owned, never the callback's
        while not stop.is_set():
            try:
                data = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "")
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "")
                if matches_wake(text):
                    log.info("vosk_wake_heard", text=text)
                    loop.call_soon_threadsafe(detected.set)
                    return
            except Exception as exc:
                log.warning("vosk_predict_failed", error=str(exc))

    worker = threading.Thread(target=_decode_worker, name="vosk-decode", daemon=True)
    worker.start()

    stream = sd.RawInputStream(
        samplerate=_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=_BLOCK,
        callback=_cb,
        device=audio_devices.test_input_device_index(),  # None in production
    )
    stream.start()
    try:
        await detected.wait()
    except asyncio.CancelledError:
        log.info("vosk_listen_cancelled")
        stop.set()  # let the decode worker exit; it never touches a torn-down stream
        _close_stream_background(stream)
        raise
    finally:
        stop.set()

    stream.stop()
    stream.close()
    play_wake_chime()
