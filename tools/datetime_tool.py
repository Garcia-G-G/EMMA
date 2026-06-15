"""Spoken date/time in natural Spanish (Prompt 38-A)."""

from __future__ import annotations

import datetime as dt

from tools.base import ToolResult, tool

_DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _part_of_day(hour: int) -> str:
    if hour < 6:
        return "de la madrugada"
    if hour < 12:
        return "de la mañana"
    if hour < 20:
        return "de la tarde"
    return "de la noche"


@tool()
async def current_datetime_speak() -> ToolResult:
    """Dice la fecha y la hora en español natural.

    Para "¿qué hora es?", "¿qué día es hoy?", "¿qué fecha es?".
    """
    now = dt.datetime.now()
    day, month = _DAYS[now.weekday()], _MONTHS[now.month - 1]
    h12 = now.hour % 12 or 12
    es_la = "es la" if h12 == 1 else "son las"
    spoken = (
        f"{day} {now.day} de {month}, {es_la} {h12}:{now.minute:02d} {_part_of_day(now.hour)}"
    )
    return ToolResult(True, {"iso": now.isoformat(), "weekday": day}, spoken, False)
