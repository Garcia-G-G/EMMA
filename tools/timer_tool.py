"""Voice timers (Prompt 38-B). On expiry: macOS notification + spoken alert."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog

from actions import macos
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.timer")

_timers: dict[str, dict[str, Any]] = {}  # id -> {label, duration_min, ends_at, task}


def _mins(label: str, duration_min: int) -> str:
    lab = f" «{label}»" if label else ""
    return f"el timer{lab} de {duration_min} minuto(s)"


async def _run_timer(tid: str, duration_min: int, label: str) -> None:
    try:
        await asyncio.sleep(duration_min * 60)
    except asyncio.CancelledError:
        return
    _timers.pop(tid, None)
    phrase = f"{_mins(label, duration_min).capitalize()} terminó."
    macos.notify("Emma — Timer", phrase)
    # Spoken alert that works even with no live session (macOS TTS).
    try:
        proc = await asyncio.create_subprocess_exec(
            "say", phrase, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
    except Exception as exc:
        log.warning("timer_say_failed", error=str(exc))


@tool()
async def start_timer(duration_min: int, label: str = "") -> ToolResult:
    """Inicia un temporizador en segundo plano ("timer de 25 minutos", "ponme 10 minutos").

    Al terminar, Emma avisa con una notificación y en voz alta. `label` es opcional.
    """
    try:
        duration_min = int(duration_min)
    except (TypeError, ValueError):
        return ToolResult(False, None, "¿De cuántos minutos lo pongo?", False)
    if duration_min <= 0:
        return ToolResult(False, None, "El timer tiene que ser de al menos un minuto.", False)
    tid = uuid.uuid4().hex[:8]
    task = asyncio.create_task(_run_timer(tid, duration_min, label.strip()))
    _timers[tid] = {
        "label": label.strip(), "duration_min": duration_min,
        "ends_at": time.time() + duration_min * 60, "task": task,
    }
    lab = f" para «{label.strip()}»" if label.strip() else ""
    return ToolResult(
        True, {"id": tid, "duration_min": duration_min}, f"Listo, timer de {duration_min} minutos{lab}.", False
    )


@tool()
async def list_timers() -> ToolResult:
    """Dice qué temporizadores siguen activos ("¿qué timers tengo?")."""
    now = time.time()
    active = []
    for tid, t in list(_timers.items()):
        left = t["ends_at"] - now
        if left <= 0:
            continue
        mins = max(1, round(left / 60))
        active.append({"id": tid, "label": t["label"], "minutes_left": mins})
    if not active:
        return ToolResult(True, {"timers": []}, "No tienes timers activos.", False)
    parts = [f"{a['minutes_left']} min" + (f" ({a['label']})" if a["label"] else "") for a in active]
    return ToolResult(True, {"timers": active}, "Timers activos: " + ", ".join(parts) + ".", False)
