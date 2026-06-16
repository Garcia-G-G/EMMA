"""TablePlus query execution (Prompt 34, Part A).

19.6-B17 added URL-scheme *opening* of TablePlus connections; this runs a query
inside one and reads back rows.

Reality (pre-flight): TablePlus ships NO query-executing CLI — only the GUI binary.
So the documented ``TablePlus -cli`` path is a no-op today; the tool detects a real
``tableplus-cli`` if one ever lands, and otherwise falls back to the AX/keystroke
path the spec calls for (activate TablePlus, type the SQL into the focused editor,
run it with Cmd-R). Structured result-reading needs the CLI; the keystroke fallback
runs the query but can only return a best-effort note.

Safety: any write (INSERT/UPDATE/DELETE/DDL) is gated behind confirmation; SELECTs run
straight through.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

from core import dictionary
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.tableplus")

_WRITE_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "replace", "merge", "grant", "revoke",
)
_MAX_ROWS = 50


def _is_write(sql: str) -> bool:
    """True if `sql` looks like a write/DDL (its first keyword)."""
    s = sql.strip().lstrip("(").lower()
    first = s.split(None, 1)[0] if s.split() else ""
    return first in _WRITE_KEYWORDS


def _cli_path() -> str | None:
    """Path to a real query-executing TablePlus CLI, if one exists. Today: None."""
    found = shutil.which("tableplus-cli") or shutil.which("tableplus")
    if found:
        return found
    cand = Path("/Applications/TablePlus.app/Contents/MacOS/tableplus-cli")
    return str(cand) if cand.exists() else None


async def _run_cli(cli: str, connection: str, sql: str, timeout: float = 30.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        cli, "-c", connection, "-q", sql,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (proc.returncode or 0, out.decode("utf-8", "replace"))


def _parse_rows(raw: str) -> list[dict[str, str]]:
    """Parse the CLI's tabular/TSV output into up to _MAX_ROWS dict rows."""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split("\t")]
    rows: list[dict[str, str]] = []
    for ln in lines[1:_MAX_ROWS + 1]:
        cells = ln.split("\t")
        rows.append({header[i] if i < len(header) else f"col{i}": c.strip() for i, c in enumerate(cells)})
    return rows


async def _run_ax(sql: str) -> ToolResult:
    """Fallback: type the SQL into TablePlus's focused editor and run it (Cmd-R).

    Needs TablePlus already open on the target connection. Result-reading is
    best-effort (no CLI), so we report that the query was sent."""
    script = (
        'tell application "TablePlus" to activate\n'
        'delay 0.4\n'
        'tell application "System Events"\n'
        '  keystroke "a" using command down\n'   # select existing editor contents
        f'  keystroke {_as_applescript(sql)}\n'
        '  key code 15 using command down\n'      # Cmd-R → run
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.wait_for(proc.communicate(), timeout=20.0)
        ok = proc.returncode == 0
    except Exception as exc:
        log.warning("tableplus_ax_failed", error=str(exc))
        return ToolResult(False, None, "No pude controlar TablePlus. Ábrelo con la conexión y reintenta.", False)
    if not ok:
        return ToolResult(False, None,
                          "No pude ejecutar en TablePlus (¿está abierto con esa conexión?).", False)
    return ToolResult(
        True, {"via": "ax", "rows": None},
        "Ejecuté la consulta en TablePlus. Sin el CLI no puedo leer las filas de vuelta; "
        "míralas en la ventana.", False,
    )


def _as_applescript(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@tool()
async def tableplus_query(connection: str, sql: str, confirmed: bool = False) -> ToolResult:
    """Ejecuta SQL dentro de una conexión de TablePlus y lee los resultados.

    "Ejecuta select count(*) from users en mi base learning-rots". Resuelve la
    conexión por nombre. Las consultas de escritura (INSERT/UPDATE/DELETE/DDL) piden
    confirmación; los SELECT corren directo. Devuelve hasta 50 filas.
    """
    if not sql.strip():
        return ToolResult(False, None, "¿Qué consulta ejecuto?", False)
    conn = dictionary.find_connection(connection)
    conn_name = (conn or {}).get("name", connection) if conn else connection

    if _is_write(sql) and not confirmed:
        return ToolResult(
            True, {"connection": conn_name, "sql": sql},
            f"Esa consulta modifica datos en «{conn_name}»: «{sql.strip()[:80]}». ¿La ejecuto?",
            requires_confirmation=True,
        )

    cli = _cli_path()
    if cli:
        try:
            rc, out = await _run_cli(cli, conn_name, sql)
        except Exception as exc:
            log.error("tableplus_cli_failed", error=str(exc))
            return ToolResult(False, None, "La consulta falló o se pasó del tiempo.", False)
        if rc != 0:
            return ToolResult(False, {"output": out[:500]}, f"TablePlus devolvió un error: {out[:160]}", False)
        rows = _parse_rows(out)
        n = len(rows)
        msg = f"{n} fila(s)." if n else "Listo, sin filas de resultado."
        return ToolResult(True, {"connection": conn_name, "rows": rows, "via": "cli"}, msg, False)

    # No CLI on this TablePlus → AX/keystroke fallback.
    return await _run_ax(sql)
