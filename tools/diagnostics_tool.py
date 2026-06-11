"""Self-diagnostics, live reload, and telemetry voice tools (Prompt 37 A/B/C)."""

from __future__ import annotations

import asyncio

import structlog

from core import diagnostics as diag
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.diagnostics")


def _fmt_uptime(s: float | None) -> str | None:
    if s is None:
        return None
    h, m = int(s // 3600), int((s % 3600) // 60)
    return f"{h}h {m}m" if h else f"{m}m"


@tool()
async def diagnose_self() -> ToolResult:
    """Diagnóstico honesto de Emma: mic, latencia, errores, uptime, memoria, disco, batería.

    Para "¿cómo estás?", "¿funcionas bien?", "diagnóstico", "¿todo en orden?".
    """
    h = await asyncio.to_thread(diag.gather_health_sync)
    rtt = await diag.openai_rtt_ms()

    bits: list[str] = []
    up = _fmt_uptime(h.uptime_s)
    if up:
        bits.append(f"llevo {up} despierta")
    if rtt is not None:
        bits.append(f"latencia a OpenAI {rtt} ms")
    if h.mic_rms is not None:
        bits.append(f"mic en {h.mic_rms:.2f}")
    if h.facts_count is not None:
        bits.append(f"{h.facts_count} hechos en memoria")
    if h.disk_free_gb is not None:
        bits.append(f"{h.disk_free_gb} GB libres en disco")
    if h.battery_pct is not None:
        bits.append(f"batería {h.battery_pct}%" + (" (cargando)" if h.charging else ""))
    if h.thermal and h.thermal != "normal":
        bits.append(f"térmico {h.thermal}")

    head = "Estoy bien" if not h.last_error else "Funciono, pero hubo un error reciente"
    tail = f" Último error: {h.last_error}." if h.last_error else " Sin errores recientes."
    spoken = f"{head}. " + (", ".join(bits) + "." if bits else "") + tail

    data = {
        "uptime_s": h.uptime_s, "openai_rtt_ms": rtt, "mic_rms": h.mic_rms,
        "facts_count": h.facts_count, "last_reflection_ago_s": h.last_reflection_ago_s,
        "disk_free_gb": h.disk_free_gb, "battery_pct": h.battery_pct,
        "charging": h.charging, "thermal": h.thermal, "last_error": h.last_error,
    }
    return ToolResult(True, data, spoken.strip(), False)


@tool()
async def reload_tools() -> ToolResult:
    """Recarga las herramientas desde disco sin reiniciar (para probar un cambio).

    Para "recarga las herramientas", "recárgate", "vuelve a cargar tus tools".
    """
    r = await asyncio.to_thread(diag.reload_all_tools)
    n = len(r["reloaded"])
    errs = r["errors"]
    if errs:
        names = ", ".join(e["module"] for e in errs)
        spoken = f"Recargué {n} módulos, pero {len(errs)} fallaron: {names}. Los demás siguen vivos."
    else:
        spoken = f"Recargué {n} módulos de herramientas, 0 errores. Tengo {r['tool_count']} herramientas activas."
    return ToolResult(True, r, spoken, False)


@tool()
async def telemetry_summary(period: str = "week") -> ToolResult:
    """Resumen honesto de uso: acciones, fallos, causas y latencia ("resumen de la semana").

    `period` es "day" / "week" / "month". Lee el registro de fallos y el log de
    acciones — no hay un contador total de llamadas, así que reporto lo que SÍ se
    registra: acciones hechas y fallos con su causa y latencia.
    """
    t = await asyncio.to_thread(diag.telemetry_rollup, period)
    label = {"day": "Hoy", "week": "Esta semana", "month": "Este mes"}.get(period, period)
    parts = [f"{label}: registré {t['actions_recorded']} acción(es) y {t['failures']} fallo(s)"]
    if t["failures"] and t["top_categories"]:
        cat, count = t["top_categories"][0]
        parts.append(f"el fallo más común fue «{cat}» ({count})")
    if t["lat_p95_ms"] is not None:
        parts.append(f"latencia de fallos p95 {t['lat_p95_ms']} ms")
    return ToolResult(True, t, ". ".join(parts) + ".", False)
