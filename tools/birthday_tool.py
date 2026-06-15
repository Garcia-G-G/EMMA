"""Birthday voice tools (Prompt 38-D)."""

from __future__ import annotations

import datetime as dt
import re

from memory import birthdays
from tools.base import ToolResult, tool

_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_MONTH_NAMES = {v: k for k, v in _MONTHS.items()}


def _parse_date(s: str) -> tuple[int, int] | None:
    """(month, day) from ISO (1990-06-15 / 06-15), '15 de junio', or '15/06'."""
    s = (s or "").strip().lower()
    iso = re.search(r"(?:\d{4}-)?(\d{1,2})-(\d{1,2})", s)
    if iso:
        mo, d = int(iso.group(1)), int(iso.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (mo, d)
    de = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)", s)
    if de and de.group(2) in _MONTHS:
        return (_MONTHS[de.group(2)], int(de.group(1)))
    slash = re.search(r"(\d{1,2})/(\d{1,2})", s)  # dd/mm
    if slash:
        d, mo = int(slash.group(1)), int(slash.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (mo, d)
    return None


@tool()
async def birthday_remember(name: str, date: str) -> ToolResult:
    """Guarda el cumpleaños de alguien ("guarda que el cumpleaños de Ana es el 8 de junio").

    `date` puede ser ISO (1990-06-15), '8 de junio' o '08/06' (día/mes).
    """
    name = (name or "").strip()
    if not name:
        return ToolResult(False, None, "¿De quién es el cumpleaños?", False)
    md = _parse_date(date)
    if md is None:
        return ToolResult(False, None, f"No entendí la fecha «{date}». Dime algo como '8 de junio'.", False)
    month, day = md
    birthdays.remember(name, month, day)
    return ToolResult(
        True, {"name": name, "month": month, "day": day},
        f"Anotado: {name} cumple el {day} de {_MONTH_NAMES[month]}.", False,
    )


@tool()
async def birthdays_today() -> ToolResult:
    """Dice quién cumple años hoy ("¿quién cumple hoy?")."""
    names = birthdays.today()
    if not names:
        return ToolResult(True, {"names": []}, "Hoy no cumple nadie de tu lista.", False)
    return ToolResult(True, {"names": names}, f"Hoy cumple {', '.join(names)}. 🎂", False)


@tool()
async def birthdays_this_week() -> ToolResult:
    """Dice quién cumple esta semana ("¿quién cumple esta semana?")."""
    rows = birthdays.this_week()
    if not rows:
        return ToolResult(True, {"birthdays": []}, "Nadie de tu lista cumple esta semana.", False)
    today_d = dt.date.today()
    parts = []
    for name, mo, day in rows:
        when = "hoy" if (mo, day) == (today_d.month, today_d.day) else f"el {day} de {_MONTH_NAMES[mo]}"
        parts.append(f"{name} ({when})")
    return ToolResult(
        True, {"birthdays": [{"name": n, "month": m, "day": d} for n, m, d in rows]},
        "Esta semana cumplen: " + ", ".join(parts) + ".", False,
    )
