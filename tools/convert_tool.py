"""Unit + currency conversion (Prompt 38-G). Math-only; currency via CoinGecko."""

from __future__ import annotations

import time

import httpx
import structlog

from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.convert")

# Factors to a base unit (metres / kilograms).
_LENGTH = {"m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001, "mi": 1609.34, "mile": 1609.34,
           "milla": 1609.34, "ft": 0.3048, "pie": 0.3048, "in": 0.0254, "pulgada": 0.0254,
           "yd": 0.9144, "yarda": 0.9144}
_MASS = {"kg": 1.0, "g": 0.001, "mg": 1e-6, "lb": 0.453592, "libra": 0.453592,
         "oz": 0.0283495, "onza": 0.0283495, "ton": 1000.0, "tonelada": 1000.0}
_TEMP = {"c", "f", "k", "celsius", "fahrenheit", "kelvin"}

_rates_cache: tuple[float, dict[str, float]] = (0.0, {})  # (ts, {currency: value-per-BTC})
_RATES_TTL = 3600.0


def _norm(u: str) -> str:
    return (u or "").strip().lower().rstrip("s") if u else ""


def _to_celsius(v: float, unit: str) -> float:
    if unit in ("c", "celsius"):
        return v
    if unit in ("f", "fahrenheit"):
        return (v - 32) * 5 / 9
    return v - 273.15  # kelvin


def _from_celsius(c: float, unit: str) -> float:
    if unit in ("c", "celsius"):
        return c
    if unit in ("f", "fahrenheit"):
        return c * 9 / 5 + 32
    return c + 273.15


async def _fiat_rates() -> dict[str, float]:
    global _rates_cache
    ts, rates = _rates_cache
    if rates and time.time() - ts < _RATES_TTL:
        return rates
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get("https://api.coingecko.com/api/v3/exchange_rates")
        data = resp.json().get("rates", {})
    rates = {k: float(v["value"]) for k, v in data.items() if "value" in v}
    _rates_cache = (time.time(), rates)
    return rates


async def _convert_currency(value: float, frm: str, to: str) -> tuple[float | None, str]:
    try:
        rates = await _fiat_rates()
    except Exception as exc:
        log.warning("coingecko_failed", error=str(exc))
        return None, "No pude obtener los tipos de cambio ahora mismo."
    f, t = frm.lower(), to.lower()
    if f not in rates or t not in rates:
        return None, f"No conozco la moneda «{frm if f not in rates else to}»."
    return value * (rates[t] / rates[f]), ""


@tool()
async def convert(value: float, from_unit: str, to_unit: str) -> ToolResult:
    """Convierte un valor entre unidades o monedas.

    Longitud (m/km/mi/ft…), masa (kg/lb/oz…), temperatura (c/f/k) y moneda
    (USD/MXN/EUR…). Para "cuánto es 100 USD en MXN", "5 km en millas", "20°C en F".
    """
    f, t = _norm(from_unit), _norm(to_unit)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ToolResult(False, None, "El valor debe ser un número.", False)

    if f in _LENGTH and t in _LENGTH:
        out = v * _LENGTH[f] / _LENGTH[t]
    elif f in _MASS and t in _MASS:
        out = v * _MASS[f] / _MASS[t]
    elif f in _TEMP and t in _TEMP:
        out = _from_celsius(_to_celsius(v, f), t)
    else:
        # currency uses the ORIGINAL (uppercased) codes, not the singularized form
        cur, err = await _convert_currency(v, from_unit.strip(), to_unit.strip())
        if cur is None:
            return ToolResult(False, None, err, False)
        out = cur
    rounded = round(out, 2)
    return ToolResult(
        True, {"value": rounded, "from": from_unit, "to": to_unit},
        f"{value} {from_unit} son {rounded} {to_unit}.", False,
    )
