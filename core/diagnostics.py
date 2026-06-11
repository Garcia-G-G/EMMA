"""Self-diagnostics, live tool reload, and honest telemetry (Prompt 37 A/B/C).

No new dependencies: battery/thermal via ``pmset``, disk via ``shutil``, uptime
via ``ps``, metrics via direct sqlite + the existing capability_gaps ledger and
episodic action log. Every gatherer is defensive — a probe that can't read its
subsystem returns None and the spoken summary just omits it.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings
from core.crash_handler import CRASH_DIR
from core.redaction import redact

log = structlog.get_logger("emma.diagnostics")

_MODULE_LOAD_TS = time.time()  # fallback uptime anchor (≈ daemon start)
_PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}


@dataclass(frozen=True)
class HealthReport:
    uptime_s: float | None
    disk_free_gb: float | None
    disk_total_gb: float | None
    battery_pct: int | None
    charging: bool | None
    thermal: str | None
    facts_count: int | None
    last_reflection_ago_s: float | None
    last_error: str | None
    mic_rms: float | None
    openai_rtt_ms: int | None = None  # filled by the async probe


def _uptime_s() -> float | None:
    try:
        import os

        out = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(os.getpid())],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except Exception:
        pass
    return time.time() - _MODULE_LOAD_TS


def _disk_gb() -> tuple[float | None, float | None]:
    try:
        usage = shutil.disk_usage(Path(settings.EMMA_HOME).expanduser())
        return (round(usage.free / 1e9, 1), round(usage.total / 1e9, 1))
    except Exception:
        return (None, None)


def _battery() -> tuple[int | None, bool | None]:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3)
        text = out.stdout
        m = re.search(r"(\d+)%", text)
        pct = int(m.group(1)) if m else None
        charging = ("AC Power" in text) or ("charging" in text.lower())
        return (pct, charging if pct is not None else None)
    except Exception:
        return (None, None)


def _thermal() -> str | None:
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3)
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out.stdout)
        if m:
            limit = int(m.group(1))
            return "normal" if limit >= 100 else f"throttled ({limit}%)"
    except Exception:
        pass
    return None


def _db_path() -> str:
    return str(Path(settings.MEMORY_DB_PATH).expanduser())


def _facts_count() -> int | None:
    try:
        conn = sqlite3.connect(_db_path())
        try:
            return int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None


def _last_reflection_ago_s() -> float | None:
    try:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(
                "SELECT MAX(last_seen_at) FROM facts WHERE source='reflection'"
            ).fetchone()
        finally:
            conn.close()
        return (time.time() - row[0]) if row and row[0] else None
    except Exception:
        return None


def _last_error(within_s: float = 24 * 3600) -> str | None:
    """The newest crash report's first line (sanitized), if recent. None otherwise."""
    try:
        crashes = sorted(CRASH_DIR.glob("crash_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not crashes:
            return None
        newest = crashes[0]
        if time.time() - newest.stat().st_mtime > within_s:
            return None
        for line in newest.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                return redact(line.strip())[:120]
    except Exception:
        pass
    return None


def _mic_rms() -> float | None:
    """Best-effort RMS from a tiny standalone capture. None if the mic is busy
    (the live session owns it) or sounddevice is unavailable."""
    try:
        import numpy as np
        import sounddevice as sd

        rec = sd.rec(int(0.3 * 16000), samplerate=16000, channels=1, dtype="int16")
        sd.wait()
        samples = rec.astype(np.float64)
        rms = float(np.sqrt(np.mean(samples * samples)))
        return round(rms / 32768.0, 3)  # normalized 0-1, like the spec's "0.12"
    except Exception:
        return None


def gather_health_sync() -> HealthReport:
    free, total = _disk_gb()
    pct, charging = _battery()
    return HealthReport(
        uptime_s=_uptime_s(),
        disk_free_gb=free,
        disk_total_gb=total,
        battery_pct=pct,
        charging=charging,
        thermal=_thermal(),
        facts_count=_facts_count(),
        last_reflection_ago_s=_last_reflection_ago_s(),
        last_error=_last_error(),
        mic_rms=_mic_rms(),
    )


async def openai_rtt_ms() -> int | None:
    """Round-trip latency to the OpenAI API as a proxy for Realtime WS health."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=4.0) as client:
            t0 = time.perf_counter()
            await client.get("https://api.openai.com/v1/models")  # 401 is fine; we want RTT
            return int((time.perf_counter() - t0) * 1000)
    except Exception:
        return None


# ---- live tool reload (B) ---------------------------------------------------


def reload_all_tools() -> dict[str, Any]:
    """Re-import every ``tools/*.py`` so @tool decorators re-register (a changed
    tool takes effect without a restart). Overwrites in place — never clears the
    registry, so a module that fails to import can't strip working tools; it's
    reported instead. ``base``/``registry`` are skipped (reloading them would
    wipe the live registry)."""
    import importlib
    import pkgutil

    import tools as tools_pkg
    from tools.base import get_registry

    reloaded: list[str] = []
    errors: list[dict[str, str]] = []
    for mod_info in pkgutil.iter_modules(tools_pkg.__path__):
        name = mod_info.name
        if name.startswith("_") or name in ("base", "registry"):
            continue
        try:
            module = importlib.import_module(f"tools.{name}")
            importlib.reload(module)
            reloaded.append(name)
        except Exception as exc:  # a broken edit must NOT crash the daemon
            errors.append({"module": name, "error": str(exc)})
            log.warning("tool_reload_failed", module=name, error=str(exc))
    return {"reloaded": reloaded, "errors": errors, "tool_count": len(get_registry())}


# ---- telemetry rollup (C) ---------------------------------------------------


def _read_gaps(since: float) -> list[dict[str, Any]]:
    ledger = Path(settings.EMMA_HOME).expanduser() / "capability_gaps.jsonl"
    if not ledger.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
            if datetime.fromisoformat(obj["ts"]).timestamp() >= since:
                out.append(obj)
        except Exception:
            continue
    return out


def telemetry_rollup(period: str = "week") -> dict[str, Any]:
    """Aggregate the capability_gaps ledger (failures) + episodic actions over a
    period. There is no universal 'total tool calls' counter, so we report what
    IS logged: failures by category/tool + their latency, and mutations performed."""
    window = _PERIODS.get(period, _PERIODS["week"])
    since = time.time() - window
    gaps = _read_gaps(since)
    cats = Counter(g.get("category", "?") for g in gaps)
    tools_c = Counter(g.get("tool", "?") for g in gaps)
    lat = sorted(g["elapsed_ms"] for g in gaps if isinstance(g.get("elapsed_ms"), int))

    def pct(p: float) -> int | None:
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else None

    actions = 0
    try:
        from memory import episodic

        actions = len(episodic._recent_sync(window, 100000))
    except Exception:
        pass

    return {
        "period": period,
        "failures": len(gaps),
        "top_categories": cats.most_common(3),
        "top_tools": tools_c.most_common(3),
        "lat_p50_ms": pct(0.50),
        "lat_p95_ms": pct(0.95),
        "actions_recorded": actions,
    }
