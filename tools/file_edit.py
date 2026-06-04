"""Voice-driven file editing at the FILESYSTEM layer (Bug 19.6-B16).

Architectural decision: edit the file on disk, never the IDE buffer.
Cursor/VS Code's automation surface is too shallow to mutate buffers
reliably, their file watchers pick up disk changes live, and the same tool
works whether the IDE is open or not. After a successful edit the file is
revealed in Garcia's IDE via ``open_in_ide`` so he SEES the change.

Safety rails:
- Paths resolve under ``$HOME`` only — anything else is rejected.
- Writes are atomic (`tempfile` + ``os.replace``) so a crash mid-write can
  never leave a half-written file. (CPython docs guarantee replace is an
  atomic rename on POSIX: docs.python.org/3/library/os.html#os.replace)
- All four tools are ``destructive=True`` two-phase: the first call answers
  with the diff summary + ``requires_confirmation``; the orchestrator
  re-calls with ``confirmed=True`` after Garcia's "sí".
- ``edit_file_search_replace`` is LITERAL, never regex (attack surface).
"""

from __future__ import annotations

import contextlib
import difflib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import structlog

from tools.base import ToolResult, tool
from tools.ide_actions import open_in_ide

log = structlog.get_logger("emma.tools.file_edit")

_SNIPPET_LINES = 12


def _home() -> Path:
    """Seam for tests; runtime is always Garcia's real home."""
    return Path.home()


def _resolve_in_home(raw: str) -> Path | None:
    """Expand ``~`` and resolve; None unless the result lives under $HOME."""
    try:
        p = Path(raw).expanduser().resolve()
    except OSError:
        return None
    home = _home().resolve()
    return p if (p == home or p.is_relative_to(home)) else None


def _atomic_write(path: Path, content: str) -> None:
    """Write via temp file + ``os.replace`` — a SIGINT mid-write never
    corrupts the original (atomic rename on the same filesystem)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _unified_snippet(old: str, new: str, name: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=name, tofile=name, lineterm="", n=1
    )
    return "\n".join(list(diff)[:_SNIPPET_LINES])


def _delta(old: str, new: str) -> tuple[int, int, int]:
    """(lines_added, lines_removed, byte_delta)."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    added = max(0, len(new_lines) - len(old_lines))
    removed = max(0, len(old_lines) - len(new_lines))
    return added, removed, len(new.encode()) - len(old.encode())


async def _apply(
    raw_path: str,
    new_content_fn: Callable[[str], str | ToolResult],
    *,
    confirmed: bool,
    confirm_question: str,
    done_message: str,
) -> ToolResult:
    """Shared engine: path guard → read → compute → confirm → atomic write → IDE."""
    p = _resolve_in_home(raw_path)
    if p is None:
        return ToolResult(
            False, None, "Solo puedo editar archivos dentro de tu carpeta de usuario.", False
        )
    if not p.is_file():
        return ToolResult(False, None, f"No encontré {p}.", False)
    try:
        old = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(
            False, None, f"No puedo editar {p.name}: no es un archivo de texto.", False
        )
    except OSError as exc:
        return ToolResult(False, None, f"No pude leer {p.name}: {exc}", False)

    result = new_content_fn(old)
    if isinstance(result, ToolResult):  # the mutation itself failed (e.g. search miss)
        return result
    new: str = result

    added, removed, byte_delta = _delta(old, new)
    snippet = _unified_snippet(old, new, p.name)
    data = {
        "path": str(p),
        "unified": snippet,
        "lines_added": added,
        "lines_removed": removed,
        "byte_delta": byte_delta,
    }

    if not confirmed:
        q = confirm_question.format(name=p.name, added=added, removed=removed)
        return ToolResult(True, data, q, requires_confirmation=True)

    try:
        _atomic_write(p, new)
    except OSError as exc:
        return ToolResult(False, None, f"No pude escribir {p.name}: {exc}", False)

    ide_res = await open_in_ide(str(p))
    data["ide_opened"] = bool(ide_res.success)
    log.info("file_edited", path=str(p), added=added, removed=removed, bytes=byte_delta)
    return ToolResult(
        True, data, done_message.format(name=p.name, added=added, removed=removed), False
    )


def _ensure_trailing_newline(s: str) -> str:
    return s if (not s or s.endswith("\n")) else s + "\n"


@tool(destructive=True)
async def edit_file_append(path: str, text: str, confirmed: bool = False) -> ToolResult:
    """Agrega `text` al FINAL de un archivo (con confirmación y diff hablado).

    Úsalo cuando Garcia diga "Emma, en mi archivo X agrega Y al final".
    Edita el disco directamente y luego abre/refresca el archivo en su IDE.
    """

    def mutate(old: str) -> str:
        return _ensure_trailing_newline(old) + _ensure_trailing_newline(text)

    return await _apply(
        path,
        mutate,
        confirmed=confirmed,
        confirm_question="Voy a agregar {added} línea(s) al final de {name} — ¿confirmas?",
        done_message="Edité {name} — agregué {added} línea(s) al final y lo abrí en tu IDE.",
    )


@tool(destructive=True)
async def edit_file_prepend(path: str, text: str, confirmed: bool = False) -> ToolResult:
    """Agrega `text` al INICIO de un archivo (con confirmación y diff hablado).

    Úsalo cuando Garcia diga "Emma, en mi archivo X agrega Y al principio".
    """

    def mutate(old: str) -> str:
        return _ensure_trailing_newline(text) + old

    return await _apply(
        path,
        mutate,
        confirmed=confirmed,
        confirm_question="Voy a agregar {added} línea(s) al inicio de {name} — ¿confirmas?",
        done_message="Edité {name} — agregué {added} línea(s) al inicio y lo abrí en tu IDE.",
    )


@tool(destructive=True)
async def edit_file_replace(path: str, content: str, confirmed: bool = False) -> ToolResult:
    """SOBRESCRIBE un archivo completo con `content`. Siempre pide confirmación.

    Úsalo solo cuando Garcia diga explícitamente "sobrescribe X con esto".
    Es la edición más riesgosa: el contenido anterior se pierde.
    """

    def mutate(_old: str) -> str:
        return content

    return await _apply(
        path,
        mutate,
        confirmed=confirmed,
        confirm_question=(
            "Voy a SOBRESCRIBIR {name} completo (+{added}/-{removed} líneas) — ¿confirmas?"
        ),
        done_message="Listo, sobrescribí {name} y lo abrí en tu IDE.",
    )


@tool(destructive=True)
async def edit_file_search_replace(
    path: str, search: str, replace: str, count: int = 1, confirmed: bool = False
) -> ToolResult:
    """Reemplaza texto LITERAL en un archivo: `search` → `replace`.

    `count=1` reemplaza solo la primera aparición (seguro por defecto);
    si Garcia pide "todas las ocurrencias", pasa `count=-1`. Nunca es regex.
    """
    if not search:
        return ToolResult(False, None, "¿Qué texto busco para reemplazar?", False)

    def mutate(old: str) -> str | ToolResult:
        if search not in old:
            return ToolResult(False, None, f"No encontré '{search}' en {Path(path).name}.", False)
        return old.replace(search, replace, count if count > 0 else -1)

    n_label = "todas las apariciones" if count < 0 else f"{count} aparición(es)"
    return await _apply(
        path,
        mutate,
        confirmed=confirmed,
        confirm_question=(
            f"Voy a reemplazar {n_label} de '{search}' por '{replace}' en {{name}} — ¿confirmas?"
        ),
        done_message="Listo, reemplacé el texto en {name} y lo abrí en tu IDE.",
    )
