"""Voice control over Emma's proactive engine: enable/disable/snooze/list."""

from __future__ import annotations

import datetime as dt

from tools.base import ToolResult, tool


def _registered_names() -> dict[str, object]:
    # Importing registers the @scheduled/@polled jobs (idempotent).
    import core.proactive.proactivities  # noqa: F401
    from core.proactive import triggers

    jobs: dict[str, object] = {}
    for sj in triggers.scheduled_jobs():
        jobs[sj.name] = sj
    for pj in triggers.polled_jobs():
        jobs[pj.name] = pj
    return jobs


def _setting_for(name: str) -> str:
    return "PROACTIVE_" + name.strip().lower().replace(" ", "_").upper()


def _set_enabled(name: str, on: bool) -> ToolResult:
    jobs = _registered_names()
    key = name.strip().lower().replace(" ", "_")
    if key not in jobs:
        opts = ", ".join(sorted(jobs))
        return ToolResult(False, None, f"No conozco '{name}'. Opciones: {opts}.", False)
    from core.proactive import settings_writer

    settings_writer.persist(_setting_for(key), "True" if on else "False")
    verb = "Activé" if on else "Desactivé"
    return ToolResult(True, {"name": key, "enabled": on}, f"{verb} '{key}'.", False)


@tool()
async def enable_proactivity(name: str) -> ToolResult:
    """Activa una proactividad por nombre (p. ej. 'morning_briefing').

    Úsalo cuando Garcia diga "Emma, activa el briefing matinal" o
    "enciende los avisos de reuniones".
    """
    return _set_enabled(name, True)


@tool()
async def disable_proactivity(name: str) -> ToolResult:
    """Desactiva una proactividad por nombre.

    Úsalo cuando Garcia diga "Emma, apaga el recap del viernes".
    """
    return _set_enabled(name, False)


@tool(destructive=True)
async def snooze_proactivities(minutes: int = 60, confirmed: bool = False) -> ToolResult:
    """Pausa TODA la salida proactiva por N minutos. Pide confirmación.

    Úsalo cuando Garcia diga "Emma, no me molestes por 30 minutos" o
    "snooze 60".
    """
    minutes = max(1, int(minutes))
    if not confirmed:
        return ToolResult(
            True, {"minutes": minutes}, f"¿Pauso todo lo proactivo {minutes} minutos?", True
        )
    from core.proactive import engine

    until = engine.snooze(minutes)
    return ToolResult(
        True,
        {"until": until.isoformat()},
        f"Listo, en silencio hasta las {until.strftime('%H:%M')}.",
        False,
    )


@tool()
async def list_proactivities() -> ToolResult:
    """Lista las proactividades, si cada una está encendida, y su próximo disparo."""
    from croniter import croniter

    from core.proactive import triggers

    _registered_names()
    now = dt.datetime.now()
    lines: list[str] = []
    rows: list[dict[str, object]] = []
    for sj in triggers.scheduled_jobs():
        state = "ON" if sj.is_enabled() else "off"
        try:
            nxt = croniter(sj.cron, now).get_next(dt.datetime).strftime("%a %H:%M")
        except (ValueError, KeyError):
            nxt = "?"
        lines.append(f"{sj.name} [{state}] próx {nxt}")
        rows.append({"name": sj.name, "enabled": sj.is_enabled(), "next": nxt})
    for pj in triggers.polled_jobs():
        state = "ON" if pj.is_enabled() else "off"
        lines.append(f"{pj.name} [{state}] cada {pj.interval_s}s")
        rows.append({"name": pj.name, "enabled": pj.is_enabled(), "interval_s": pj.interval_s})
    spoken = "; ".join(lines) or "No hay proactividades registradas."
    return ToolResult(True, {"proactivities": rows}, spoken, False)


@tool()
async def set_quiet_hours(window: str) -> ToolResult:
    """Define las horas de silencio con un rango 'HH:MM-HH:MM' (coma para varios).

    Úsalo cuando Garcia diga "Emma, silencio de 11 de la noche a 7 de la mañana".
    """
    from core.proactive.quiet import _parse_windows

    parsed = _parse_windows(window)
    if not parsed:
        return ToolResult(False, None, "Formato inválido. Usa algo como '22:30-07:30'.", False)
    from core.proactive import settings_writer

    settings_writer.persist("PROACTIVE_QUIET_HOURS", window.strip())
    return ToolResult(
        True, {"windows": window.strip()}, f"Horas de silencio: {window.strip()}.", False
    )
