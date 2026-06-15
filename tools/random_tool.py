"""Coin/dice/random/uuid/password utilities (Prompt 38-C)."""

from __future__ import annotations

import asyncio
import random
import secrets
import string
import uuid

import structlog

from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.random")


@tool()
async def coin_flip() -> ToolResult:
    """Lanza una moneda: cara o cruz ("tira una moneda", "cara o cruz")."""
    r = random.choice(["cara", "cruz"])
    return ToolResult(True, {"result": r}, f"Salió {r}.", False)


@tool()
async def roll_dice(n: int = 1, sides: int = 6) -> ToolResult:
    """Tira `n` dados de `sides` caras ("saca un dado", "tira dos dados")."""
    n = max(1, min(int(n), 20))
    sides = max(2, min(int(sides), 1000))
    rolls = [random.randint(1, sides) for _ in range(n)]
    msg = f"Salió {rolls[0]}." if n == 1 else f"Saqué {rolls}, suman {sum(rolls)}."
    return ToolResult(True, {"rolls": rolls, "total": sum(rolls)}, msg, False)


@tool()
async def pick_random(items: list[str]) -> ToolResult:
    """Elige uno al azar de una lista ("elige por mí entre…", "¿cuál escojo?")."""
    if not items:
        return ToolResult(False, None, "Dame una lista para elegir.", False)
    pick = random.choice(items)
    return ToolResult(True, {"pick": pick}, f"Elijo: {pick}.", False)


@tool()
async def generate_uuid() -> ToolResult:
    """Genera un identificador único (UUID v4)."""
    u = str(uuid.uuid4())
    return ToolResult(True, {"uuid": u}, f"Tu UUID es {u}.", False)


@tool()
async def generate_password(length: int = 20, kind: str = "strong") -> ToolResult:
    """Genera una contraseña segura y la copia al portapapeles.

    `kind`: "strong" (con símbolos) o "simple" (solo letras y números). NUNCA
    se dice en voz alta — va directo al portapapeles.
    """
    length = max(8, min(int(length), 128))
    alphabet = string.ascii_letters + string.digits + ("!@#$%^&*-_=+?" if kind == "strong" else "")
    pw = "".join(secrets.choice(alphabet) for _ in range(length))
    copied = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy", stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(pw.encode()), timeout=5.0)
        copied = proc.returncode == 0
    except Exception as exc:
        log.warning("password_clipboard_failed", error=str(exc))
    # The raw password is never spoken or logged; data carries only metadata.
    msg = (
        f"Te copié una contraseña {'fuerte' if kind == 'strong' else 'simple'} de "
        f"{length} caracteres al portapapeles."
        if copied
        else "Generé la contraseña pero no pude copiarla al portapapeles."
    )
    return ToolResult(True, {"length": length, "kind": kind, "copied": copied}, msg, False)
