"""Always-on local wake detection via sherpa-onnx KeywordSpotter (offline KWS).

This replaces the Vosk grammar-mode path with the same *idea* — a fixed keyword
list that the decoder may only ever emit — but on an engine that actually ships
a macOS arm64 wheel (Vosk ships none, so it can't be installed on Apple Silicon).

Design (why it can answer to a bare one-word "Emma"):
sherpa-onnx's ``KeywordSpotter`` is a streaming transducer that runs a beam
search *constrained to a keyword list*. It is not a transcriber — it can only
decode one of the given keywords or stay silent, exactly like Vosk's
``[unk]``-plus-grammar trick. Each keyword carries its own trigger threshold and
boosting score, so the bare "emma" (the primary target) and the longer
"oye/hey emma" variants can be tuned independently. Non-wake speech — including
Spanish words that share the ``-ema-`` sound (tema, sistema, problema) — never
survives the beam search, so it is rejected without a loose substring match.

The wake phrases live in ``WAKE_PHRASES`` (code is the single source of truth).
At load we BPE-tokenize them against the model's ``bpe.model`` and write a
keywords file — no committed, drift-prone pre-tokenized asset.

Audio path mirrors ``core/speech_wake.py``: a 16 kHz mono int16
``RawInputStream``, the same warmup/echo suppression so Emma's own voice can't
self-trigger, and an ``asyncio.Event`` signalled from a worker thread. The
callback only enqueues raw bytes (realtime-safe); a worker thread that solely
owns the recognizer does the decode. Audio never leaves the Mac.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
import structlog

from config.settings import settings
from core import audio_devices, runtime_state
from core.audio import play_wake_chime

log = structlog.get_logger("emma.wake_sherpa")

_SAMPLE_RATE = 16000
_BLOCK = 1600  # 100 ms chunks — frequent enough for snappy keyword decoding

# Wake phrases the KWS decoder is restricted to. Mexican-Spanish + English
# phrasing; the bare "emma" is the primary target. Each entry is
# (phrase, threshold, boost): a lower threshold triggers more easily (the bare
# one-word "emma" gets the most sensitive threshold since it is the hardest and
# the priority); boosting helps a keyword survive the beam search. Tune on-device
# via SHERPA_KWS_* — accent detection is empirical, same as it was for Vosk.
WAKE_PHRASES: tuple[tuple[str, float, float], ...] = (
    ("emma", 0.15, 1.0),
    ("oye emma", 0.20, 1.0),
    ("hey emma", 0.20, 1.0),
    ("hola emma", 0.20, 1.0),
    ("ey emma", 0.20, 1.0),
)

_spotter: Any = None
_spotter_lock = asyncio.Lock()


def _normalize(text: str) -> str:
    """Lowercase + strip accents so 'Emma'/'éma' compare cleanly."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip()


def matches_wake(text: str) -> bool:
    """True if a KeywordSpotter result names the wake word.

    Pure + testable. The spotter is keyword-constrained, so any non-empty
    result is already a wake; the ``emma`` substring check is a defensive parity
    with the old Vosk matcher — a stray, non-wake decode ('tema', 'ema') can
    never carry the doubled-m.
    """
    norm = _normalize(text)
    if not norm:
        return False
    return "emma" in norm


def _model_dir() -> Path:
    return Path(settings.SHERPA_KWS_MODEL_PATH).expanduser()


def _find_one(model_dir: Path, *stems: str) -> str:
    """Return the first .onnx in ``model_dir`` whose name starts with a stem.

    The KWS tarballs name files with an epoch/chunk suffix
    (``encoder-epoch-12-avg-2-chunk-16-left-64.onnx``), so we match on prefix and
    prefer the full-precision (non-int8) build for detection quality.
    """
    for stem in stems:
        cands = sorted(p for p in model_dir.glob(f"{stem}*.onnx") if ".int8." not in p.name)
        if cands:
            return str(cands[0])
    raise SystemExit(
        f"sherpa KWS model at {model_dir} is missing a '{stems[0]}-*.onnx'. "
        "Re-run the installer (step 5) to re-download the wake model."
    )


def _write_keywords_file(model_dir: Path) -> Path:
    """BPE-tokenize WAKE_PHRASES against the model and write a keywords file.

    Regenerated on every load (cheap) so it always reflects WAKE_PHRASES. Written
    beside the model so it travels with it.
    """
    import sentencepiece as spm

    bpe = model_dir / "bpe.model"
    if not bpe.exists():
        raise SystemExit(
            f"sherpa KWS model at {model_dir} has no bpe.model — cannot tokenize "
            "the wake phrases. Re-run the installer (step 5)."
        )
    sp = spm.SentencePieceProcessor()
    sp.load(str(bpe))
    lines: list[str] = []
    for phrase, threshold, boost in WAKE_PHRASES:
        # Tokenize ONLY the phrase text; the :boost #threshold @label annotations
        # are appended after (they must not go through the tokenizer). The @label
        # is the phrase with spaces underscored, so a hit is self-identifying.
        pieces = sp.encode(phrase.upper(), out_type=str)
        label = phrase.replace(" ", "_")
        lines.append(f"{' '.join(pieces)} :{boost} #{threshold} @{label}")
    out = model_dir / "emma_keywords.generated.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


async def _get_spotter() -> Any:
    """Lazy-build and cache the KeywordSpotter (build is ~1s; decodes are cheap)."""
    global _spotter
    async with _spotter_lock:
        if _spotter is not None:
            return _spotter
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SystemExit(
                f"WAKE_WORD_ENGINE=sherpa but sherpa-onnx failed to import ({exc}). "
                "Run `uv sync` and retry."
            ) from exc
        model_dir = _model_dir()
        if not model_dir.exists():
            raise SystemExit(
                f"sherpa KWS model not found at {model_dir}. Re-run the installer "
                "(step 5) or set SHERPA_KWS_MODEL_PATH."
            )
        keywords_file = _write_keywords_file(model_dir)

        def _build() -> Any:
            return sherpa_onnx.KeywordSpotter(
                tokens=str(model_dir / "tokens.txt"),
                encoder=_find_one(model_dir, "encoder"),
                decoder=_find_one(model_dir, "decoder"),
                joiner=_find_one(model_dir, "joiner"),
                keywords_file=str(keywords_file),
                num_threads=1,
                provider="cpu",
            )

        _spotter = await asyncio.to_thread(_build)
        log.info(
            "sherpa_kws_loaded",
            model=str(model_dir),
            phrases=[p[0] for p in WAKE_PHRASES],
        )
        return _spotter


def _close_stream_background(stream: Any) -> None:
    """Abort+close a PortAudio stream off the loop (close can block on the HAL)."""

    def _close() -> None:
        for op in ("abort", "close"):
            with contextlib.suppress(Exception):
                getattr(stream, op)()

    threading.Thread(target=_close, name="sherpa-stream-close", daemon=True).start()


async def listen() -> None:
    """Block until a wake phrase is spotted in the live audio, then return.

    Plays the ack chime on a hit, mirroring the openWakeWord/Vosk branches.
    """
    import queue

    spotter = await _get_spotter()
    detected = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Layer B: clear any stuck bot-speaking flag, then suppress detection during
    # the stream-open warmup so Emma's residual audio can't self-trigger.
    runtime_state.force_clear()
    warmup_s = 0.0 if settings.EMMA_TEST_MODE else settings.WAKE_WARMUP_MS / 1000.0
    open_mono = time.monotonic()

    # The PortAudio callback must return within the buffer deadline, but the KWS
    # decode is a forward pass that can overrun it → dropped input + missed wakes.
    # So the callback ONLY enqueues raw bytes (O(1), realtime-safe); a worker
    # thread (which solely owns the sherpa stream) does the decode. This also
    # removes the cancel-time callback↔teardown race.
    audio_q: queue.Queue[bytes] = queue.Queue(maxsize=64)
    stop = threading.Event()

    def _cb(indata: Any, frames: int, _t: object, status: object) -> None:
        if status:
            log.warning("input_status", status=str(status))
        if runtime_state.suppress_wake(open_mono, warmup_s):
            return  # warmup / bot speaking (Layer B)
        with contextlib.suppress(queue.Full):
            audio_q.put_nowait(bytes(indata))

    def _decode_worker() -> None:
        s = spotter.create_stream()  # worker-owned, never the callback's
        while not stop.is_set():
            try:
                data = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                s.accept_waveform(_SAMPLE_RATE, samples)
                while spotter.is_ready(s):
                    spotter.decode_stream(s)
                    result = spotter.get_result(s)
                    if result and matches_wake(result):
                        log.info("sherpa_wake_heard", keyword=result)
                        spotter.reset_stream(s)
                        loop.call_soon_threadsafe(detected.set)
                        return
                    if result:
                        spotter.reset_stream(s)
            except Exception as exc:
                log.warning("sherpa_decode_failed", error=str(exc))

    worker = threading.Thread(target=_decode_worker, name="sherpa-decode", daemon=True)
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
        log.info("sherpa_listen_cancelled")
        stop.set()  # let the decode worker exit; it never touches a torn-down stream
        _close_stream_background(stream)
        raise
    finally:
        stop.set()

    stream.stop()
    stream.close()
    play_wake_chime()
