"""Wake Word Studio — HTTP/SSE routes (Prompt 16.3, Part A).

Wraps the scripted training pipeline (16.2 / 16.2.1) via `backend.wake_runner`
and the background task registry. Six endpoints under /wake plus the page itself
and a small /wake/config the frontend reads for auth state + the live cost rate.

Auth: every job endpoint is gated by `require_access`, toggled by
WAKE_STUDIO_REQUIRE_AUTH (default True). The page + config stay open so an
unauthenticated visitor sees a calm login prompt instead of a 401 wall.

The ElevenLabs key is NEVER exposed here — the runner reads it from the daemon's
Keychain-backed settings inside the worker. The browser only ever sees costs.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from backend import wake_runner
from backend.auth import current_user, require_user
from backend.config import settings
from core.background import registry

router = APIRouter()

_STATIC = Path(__file__).parent / "static"
_BANNED_FILE = Path(__file__).parent / "wake_banned.txt"
_MAX_PHRASE_LEN = 40
_KIND = "wake_train"

DEFAULT_NEGATIVES = [
    "Emma", "Anna", "hey there", "hola", "ya terminé", "abre el navegador",
]

# 24.6 audit: clamp to sane ceilings. epochs is NOT in the cost formula, so without
# a cap a single job (epochs=100_000_000, passing the $ guard) pins a CPU core
# indefinitely. The others bound synthesis volume + voice fan-out.
_FIELD_CEILINGS = {"voices_es": 20, "voices_en": 20, "n_per_voice": 500,
                   "n_neg_per_voice": 100, "epochs": 200}


# ---- request model + validation ---------------------------------------------


class WakeJobRequest(BaseModel):
    phrases: list[str]
    negative_phrases: list[str] = DEFAULT_NEGATIVES
    voices_es: int = 1
    voices_en: int = 1
    n_per_voice: int = 50
    n_neg_per_voice: int = 8
    epochs: int = 40
    max_cost_usd: float = 5.0

    @field_validator("phrases")
    @classmethod
    def _check_phrases(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v if p.strip()]
        if not 1 <= len(cleaned) <= 3:
            raise ValueError("Escribe entre 1 y 3 frases.")
        for p in cleaned:
            if len(p) > _MAX_PHRASE_LEN:
                raise ValueError(f"«{p[:20]}…» es muy larga (máx {_MAX_PHRASE_LEN} caracteres).")
        banned = _load_banned()
        for p in cleaned:
            low = p.lower()
            if any(b in low for b in banned):
                raise ValueError("Esa frase contiene una palabra no permitida.")
        return cleaned

    @field_validator("voices_es", "voices_en", "n_per_voice", "n_neg_per_voice", "epochs")
    @classmethod
    def _bounded(cls, v: int, info: Any) -> int:
        return max(0, min(int(v), _FIELD_CEILINGS.get(info.field_name, v)))


def _load_banned() -> list[str]:
    try:
        lines = _BANNED_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")]


# ---- auth gate --------------------------------------------------------------


async def require_access(request: Request) -> dict[str, Any] | None:
    """Gate job endpoints. Open when the toggle is off; 401 otherwise."""
    if not settings.WAKE_STUDIO_REQUIRE_AUTH:
        return await current_user(request)
    return await require_user(request)


# Module-level dependency singleton (keeps Depends() out of arg defaults — B008).
_ACCESS = Depends(require_access)


# ---- helpers ----------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _get_job(job_id: str):
    rec = registry().get(job_id)
    if rec is None or rec.kind != _KIND:
        raise HTTPException(404, "Trabajo no encontrado.")
    return rec


def _job_view(rec: Any) -> dict[str, Any]:
    m = rec.meta or {}
    phase = m.get("phase", "queued")
    # reconcile: a registry-level cancel/fail that the worker didn't stamp.
    if rec.status == "cancelled" and phase not in ("cancelled", "done", "failed"):
        phase = "cancelled"
    active = phase in ("queued", "generating_voices", "training", "validating")
    return {
        "id": rec.id,
        "phase": phase,
        "progress_pct": m.get("progress_pct", 0.0),
        "message": m.get("message", ""),
        "cost_so_far_usd": m.get("cost_so_far_usd", 0.0),
        "samples_generated": m.get("samples_generated", {"positive": 0, "negative": 0}),
        "voices_used": m.get("voices_used"),
        "started_at": _iso(rec.started_at),
        "eta_seconds": wake_runner.eta_seconds(rec.started_at, m.get("progress_pct", 0.0)) if active else None,
        "recommended_threshold": m.get("recommended_threshold"),
        "validation": m.get("validation"),
        "error": m.get("error"),
    }


# ---- A1: create job ---------------------------------------------------------


_MAX_CONCURRENT_WAKE_JOBS = 2  # 24.6 audit: cap concurrent training (CPU/disk/$ DoS)


@router.post("/wake/jobs")
async def create_job(req: WakeJobRequest, _user: Any = _ACCESS) -> Any:
    # 24.6 audit (HIGH): bound concurrent training jobs. Each spawns torch + paid
    # ElevenLabs synthesis; unbounded POSTs saturate the Mac and run up spend.
    active = sum(1 for r in registry().list(status="running")
                 if r.kind == _KIND and (r.meta or {}).get("phase") not in
                 ("done", "failed", "cancelled"))
    if active >= _MAX_CONCURRENT_WAKE_JOBS:
        raise HTTPException(429, "Hay demasiados entrenamientos en curso. Espera a que terminen.")
    if req.max_cost_usd > settings.WAKE_MAX_COST_USD:
        raise HTTPException(400, f"El límite de costo no puede pasar de ${settings.WAKE_MAX_COST_USD:.0f}.")
    voices = wake_runner.resolve_voices(req.voices_es, req.voices_en)
    if not voices:
        raise HTTPException(400, "No hay voces de ElevenLabs configuradas en el servidor.")
    chars = wake_runner.estimate_chars(
        req.phrases, req.negative_phrases, voices, req.n_per_voice, req.n_neg_per_voice)
    cost = wake_runner.estimate_cost_usd(chars)
    if cost > req.max_cost_usd:
        raise HTTPException(
            400, f"El costo estimado (${cost:.2f}) supera tu límite (${req.max_cost_usd:.2f}). "
            "Baja las muestras o sube el límite.")

    params = req.model_dump()
    slug = wake_runner.slugify(req.phrases[0])
    rec = await registry().start(
        name=f"wake:{slug}",
        kind=_KIND,
        coro_factory=lambda c: wake_runner.run_job(c, params),
        meta=wake_runner.initial_meta(params, voices),
    )
    return {
        "job_id": rec.id,
        "stream_url": f"/wake/jobs/{rec.id}/stream",
        "estimated_cost_usd": cost,
        "voices_used": len(voices),
    }


# ---- A2: job state ----------------------------------------------------------


@router.get("/wake/jobs/{job_id}")
async def get_job(job_id: str, _user: Any = _ACCESS) -> Any:
    return _job_view(_get_job(job_id))


# ---- A3: SSE stream ---------------------------------------------------------


@router.get("/wake/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request, _user: Any = _ACCESS) -> Any:
    _get_job(job_id)  # 404 early if missing

    async def gen():
        last = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            view = _job_view(_get_job(job_id))
            payload = json.dumps(view, ensure_ascii=False)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
                idle = 0
            else:
                idle += 1
                if idle >= 2:  # heartbeat for long, quiet phases (~2s)
                    yield ": keepalive\n\n"
                    idle = 0
            if view["phase"] in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ---- A4: download model -----------------------------------------------------


@router.get("/wake/jobs/{job_id}/model")
async def download_model(job_id: str, _user: Any = _ACCESS) -> Any:
    rec = _get_job(job_id)
    m = rec.meta or {}
    if m.get("phase") != "done" or not m.get("model_path"):
        raise HTTPException(409, "El modelo todavía no está listo.")
    path = Path(m["model_path"])
    if not path.exists():
        raise HTTPException(404, "El archivo del modelo no existe.")
    slug = m.get("slug", "wake")
    return FileResponse(str(path), media_type="application/octet-stream", filename=f"{slug}.onnx")


# ---- A5: install into the daemon --------------------------------------------


@router.post("/wake/jobs/{job_id}/install")
async def install_model(job_id: str, _user: Any = _ACCESS) -> Any:
    rec = _get_job(job_id)
    m = rec.meta or {}
    if m.get("phase") != "done" or not m.get("model_path"):
        raise HTTPException(409, "El modelo todavía no está listo.")
    src = Path(m["model_path"])
    if not src.exists():
        raise HTTPException(404, "El archivo del modelo no existe.")
    slug = m.get("slug", "wake")
    models_dir = Path(settings.WAKE_MODELS_DIR)
    models_dir.mkdir(parents=True, exist_ok=True)
    dst = models_dir / f"{slug}.onnx"
    shutil.copy(src, dst)

    threshold = m.get("recommended_threshold") or 0.5
    _update_daemon_env(str(dst), slug, threshold)
    # active-model sentinel — a Prompt-37 hot-reload watcher (or the next start)
    # picks this up without a manual launchctl dance.
    (models_dir / "active.json").write_text(
        json.dumps({"model": dst.name, "path": str(dst), "name": slug, "threshold": threshold},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return {"installed": True, "active_model": dst.name, "threshold": threshold}


def _update_daemon_env(model_path: str, name: str, threshold: float) -> None:
    """Set WAKE_WORD_* keys in the daemon's .env, preserving everything else.

    Only ever writes these three non-secret keys — never touches credentials.
    """
    env_path = Path(settings.WAKE_DAEMON_ENV_FILE)
    updates = {
        "WAKE_WORD_PATH": model_path,
        "WAKE_WORD_NAME": name,
        "WAKE_WORD_THRESHOLD": str(threshold),
    }
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln else ""
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(ln)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---- A6: cancel + cleanup ---------------------------------------------------


@router.delete("/wake/jobs/{job_id}")
async def cancel_job(job_id: str, _user: Any = _ACCESS) -> Any:
    rec = _get_job(job_id)
    await registry().cancel(job_id)
    if rec.meta is not None:
        rec.meta["phase"] = "cancelled"
        rec.meta["message"] = "Cancelado."
    job_dir = Path(settings.WAKE_DATA_DIR) / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
    return {"cancelled": True}


# ---- page + config ----------------------------------------------------------


@router.get("/wake", response_class=HTMLResponse)
async def wake_page() -> Any:
    return HTMLResponse((_STATIC / "wake" / "index.html").read_text(encoding="utf-8"))


@router.get("/wake/config")
async def wake_config(request: Request) -> Any:
    user = await current_user(request)
    return JSONResponse({
        "require_auth": settings.WAKE_STUDIO_REQUIRE_AUTH,
        "authenticated": user is not None,
        "cost_per_1k_chars": settings.WAKE_COST_PER_1K_CHARS,
        "max_cost_usd": settings.WAKE_MAX_COST_USD,
        "voices_available": len(wake_runner.resolve_voices(999, 999)),
        "defaults": {
            "negative_phrases": DEFAULT_NEGATIVES,
            "voices_es": 1, "voices_en": 1,
            "n_per_voice": 50, "n_neg_per_voice": 8, "epochs": 40, "max_cost_usd": 5.0,
        },
    })
