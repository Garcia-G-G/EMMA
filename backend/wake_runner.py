"""Wake Word Studio — background runner (Prompt 16.3, Part B).

Bridges the 16.2 / 16.2.1 CLI scripts to the background task registry. No new
training logic lives here: it imports the scripts' `generate()` (ElevenLabs
synthesis) and `train()` (openWakeWord) and drives them with progress callbacks,
writing a structured snapshot into the TaskRecord.meta that the SSE endpoint
reads (Part A3).

Design choice — Option 1 from the spec (import the scripts and call their
functions with callbacks) over subprocess+stdout parsing: it keeps types intact,
surfaces real per-clip / per-epoch events, and shares one cost formula. The
heavy deps (torch / openwakeword / httpx-against-ElevenLabs) are imported lazily
INSIDE the worker via `_load_*` so importing this module (for the routes/tests)
costs nothing.

Progress is honest: every snapshot corresponds to a real pipeline event
(clip N/M generated, epoch K/E trained, validated). No synthetic pulsing.
"""

from __future__ import annotations

import asyncio
import errno
import importlib.util
import re
import time
from pathlib import Path
from typing import Any

from backend.config import settings
from core.background import registry

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"

# Real phases the pipeline goes through (spec's `augmenting` is folded into
# training — the 16.2 trainer augments by window-sliding inside the loop, there
# is no separate augmentation pass, and we don't show phases that don't happen).
PHASES = (
    "queued", "generating_voices", "training", "validating",
    "done", "failed", "cancelled",
)

# Fraction of the overall progress bar each phase owns (honest weighting:
# synthesis is the slow ElevenLabs part, training the slow CPU part).
_GEN_SPAN = (0.0, 0.45)
_TRAIN_SPAN = (0.45, 0.90)
_VAL_SPAN = (0.90, 1.0)


def slugify(phrase: str) -> str:
    """`"Hey Emma"` → `"hey_emma"` — the model filename + WAKE_WORD_NAME."""
    s = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")
    return s or "wake"


def _csv(raw: str) -> list[str]:
    return [v.strip() for v in raw.split(",") if v.strip()]


def _daemon_voice_ids() -> tuple[str, str]:
    """The daemon's configured ES/EN ElevenLabs voices, as the default pool."""
    try:
        from config.settings import settings as dsettings

        return dsettings.ELEVENLABS_VOICE_ID_ES, dsettings.ELEVENLABS_VOICE_ID_EN
    except Exception:
        return "", ""


def resolve_voices(voices_es: int, voices_en: int) -> list[str]:
    """Resolve requested voice counts → concrete ElevenLabs voice IDs.

    Pools come from WAKE_VOICE_POOL_ES/_EN (comma-sep), falling back to the
    daemon's two configured voices so this works out of the box. If a caller
    asks for more voices than the pool holds, they get the whole pool — the
    runner surfaces the real count it used, never a fake one.
    """
    fb_es, fb_en = _daemon_voice_ids()
    pool_es = _csv(settings.WAKE_VOICE_POOL_ES) or ([fb_es] if fb_es else [])
    pool_en = _csv(settings.WAKE_VOICE_POOL_EN) or ([fb_en] if fb_en else [])
    return pool_es[: max(0, voices_es)] + pool_en[: max(0, voices_en)]


def estimate_chars(phrases: list[str], neg_phrases: list[str], voices: list[str],
                   n_pos: int, n_neg: int) -> int:
    """Character spend the run would incur — the live cost estimate's basis."""
    nv = len(voices)
    pos = nv * n_pos * sum(len(p) for p in phrases)
    neg = nv * n_neg * sum(len(p) for p in neg_phrases)
    return pos + neg


def estimate_cost_usd(chars: int) -> float:
    return round(chars * settings.WAKE_COST_PER_1K_CHARS / 1000.0, 4)


def eta_seconds(started_at: float, progress_pct: float, now: float | None = None) -> int | None:
    """Linear ETA from elapsed time and fraction done (None until it's meaningful)."""
    if progress_pct <= 0.02:
        return None
    elapsed = (now or time.time()) - started_at
    return max(0, int(elapsed / progress_pct * (1.0 - progress_pct)))


def initial_meta(params: dict[str, Any], voices: list[str]) -> dict[str, Any]:
    """The TaskRecord.meta seeded at job creation (phase = queued)."""
    return {
        "phase": "queued",
        "progress_pct": 0.0,
        "message": "En cola…",
        "cost_so_far_usd": 0.0,
        "samples_generated": {"positive": 0, "negative": 0},
        "voices_used": len(voices),
        "slug": slugify(params["phrases"][0]),
        "recommended_threshold": None,
        "validation": None,
        "model_path": None,
        "error": None,
    }


def _load_module(filename: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_data_module():  # seam for tests to monkeypatch
    return _load_module("wake_word_data_eleven.py", "wake_word_data_eleven")


def _load_train_module():  # seam for tests to monkeypatch
    return _load_module("train_wake_word.py", "train_wake_word")


def _lerp(span: tuple[float, float], frac: float) -> float:
    lo, hi = span
    return round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 4)


async def run_job(controller: Any, params: dict[str, Any]) -> int:
    """Registry coro_factory entrypoint. Mutates this job's TaskRecord.meta."""
    rec = registry().get(controller.task_id)
    meta = rec.meta if rec is not None else {}
    return await _run(params, meta, controller)


async def _run(params: dict[str, Any], meta: dict[str, Any], controller: Any) -> int:
    """Core pipeline driver — pure w.r.t. the meta dict so tests can inspect it."""

    def emit(**kw: Any) -> None:
        meta.update(kw)
        meta["updated_at"] = time.time()

    voices = resolve_voices(params["voices_es"], params["voices_en"])
    if not voices:
        emit(phase="failed", error="No hay voces de ElevenLabs configuradas.",
             message="No hay voces configuradas.")
        return 1

    phrases = params["phrases"]
    neg_phrases = params["negative_phrases"]
    n_pos, n_neg = params["n_per_voice"], params["n_neg_per_voice"]
    total_pos = len(voices) * len(phrases) * n_pos
    total_neg = len(voices) * len(neg_phrases) * n_neg
    job_dir = Path(settings.WAKE_DATA_DIR) / controller.task_id
    model_path = job_dir / f"{slugify(phrases[0])}.onnx"

    try:
        wd = _load_data_module()
        tw = _load_train_module()

        # ---- Phase 1: generate voices (ElevenLabs) --------------------------
        emit(phase="generating_voices", progress_pct=_lerp(_GEN_SPAN, 0.0),
             message="Generando voces sintéticas…")

        def on_clip(kind: str, done: int, total: int, chars: int) -> None:
            absolute = done if kind == "positive" else total_pos + done
            frac = absolute / max(1, total_pos + total_neg)
            sg = meta.get("samples_generated", {"positive": 0, "negative": 0})
            sg[kind] = done
            emit(phase="generating_voices", progress_pct=_lerp(_GEN_SPAN, frac),
                 message=f"Generando {kind} {done}/{total}",
                 cost_so_far_usd=estimate_cost_usd(chars), samples_generated=sg)

        def on_retry(wait: float) -> None:
            emit(message=f"Límite de tasa de ElevenLabs; esperando {int(wait)}s…")

        await asyncio.to_thread(
            wd.generate, job_dir, phrases, neg_phrases, voices, n_pos, n_neg,
            on_clip, on_retry,
        )

        # ---- Phase 2 + 3: train + validate (openWakeWord) ------------------
        def on_train(phase: str, done: int, total: int, msg: str) -> None:
            frac = done / max(1, total)
            if phase == "validating":
                emit(phase="validating", progress_pct=_lerp(_VAL_SPAN, frac), message=msg)
            else:  # embedding / training
                emit(phase="training", progress_pct=_lerp(_TRAIN_SPAN, frac), message=msg)

        stats = await asyncio.to_thread(
            tw.train, job_dir, model_path, params["epochs"], True, on_train,
        )

        emit(phase="done", progress_pct=1.0, message="Listo.",
             model_path=str(model_path),
             recommended_threshold=stats.get("recommended_threshold"),
             validation=stats.get("validation"))
        return 0

    except asyncio.CancelledError:
        emit(phase="cancelled", message="Cancelado.")
        raise
    except Exception as exc:  # map to honest Spanish failures
        emit(phase="failed", error=_friendly_error(wd_err=exc), message=_friendly_error(wd_err=exc))
        return 1


def _friendly_error(wd_err: Exception) -> str:
    """Translate a pipeline exception into a recoverable Spanish message (B3)."""
    name = type(wd_err).__name__
    if name == "OutOfCreditsError":
        return "Sin créditos en ElevenLabs. Recarga o usa menos muestras."
    if name == "TrainingError":
        return str(wd_err)
    if isinstance(wd_err, OSError) and wd_err.errno == errno.ENOSPC:
        return "Sin espacio en disco."
    if name == "VoiceGenError":
        return str(wd_err)
    return f"Falló el trabajo: {wd_err}"
