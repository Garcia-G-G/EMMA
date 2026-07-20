"""File operations (Prompt 36): find, analyze disk, free space, batch rename.

Search wraps Spotlight (``mdfind``); disk analysis wraps ``du``. The two
destructive tools (free_space_assist, rename_batch) are gated by confirmation AND
the $HOME path guard from ``file_edit``; deletions go to the Trash (reversible),
never an unguarded ``rm``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import shutil
from pathlib import Path
from typing import Any

import structlog

from tools.base import ToolResult, tool
from tools.file_edit import _resolve_in_home

log = structlog.get_logger("emma.tools.file_ops")

_KIND_UTI = {
    "pdf": 'kMDItemContentType == "com.adobe.pdf"',
    "image": 'kMDItemContentTypeTree == "public.image"',
    "video": 'kMDItemContentTypeTree == "public.movie"',
    "audio": 'kMDItemContentTypeTree == "public.audio"',
    "doc": 'kMDItemContentTypeTree == "public.composite-content"',
    "code": 'kMDItemContentTypeTree == "public.source-code"',
}
_KIND_ES = {"pdf": "PDFs", "image": "imágenes", "video": "videos", "audio": "audios",
            "doc": "documentos", "code": "archivos de código"}
_MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
           "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}


def _human(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.0f} {unit}" if unit in ("B", "KB") else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _since_start(since: str) -> dt.datetime | None:
    """Parse a Spanish time phrase to a start datetime (for the date filter)."""
    s = (since or "").strip().lower()
    if not s:
        return None
    today = dt.date.today()
    if s in ("ayer", "yesterday"):
        d = today - dt.timedelta(days=1)
        return dt.datetime(d.year, d.month, d.day)
    if "semana" in s:
        d = today - dt.timedelta(days=7)
        return dt.datetime(d.year, d.month, d.day)
    if "mes" in s:
        return dt.datetime(today.year, today.month, 1)
    if "hoy" in s:
        return dt.datetime(today.year, today.month, today.day)
    for name, mo in _MONTHS.items():
        if name in s:
            year = today.year if mo <= today.month else today.year - 1
            return dt.datetime(year, mo, 1)
    return None


def _build_mdfind_expr(query: str, kind: str, since: str) -> str:
    parts: list[str] = []
    q = (query or "").replace('"', "").strip()
    if q:
        parts.append(f'(kMDItemDisplayName == "*{q}*"cd || kMDItemTextContent == "*{q}*"cd)')
    if kind in _KIND_UTI:
        parts.append(_KIND_UTI[kind])
    start = _since_start(since)
    if start is not None:
        parts.append(f'kMDItemFSContentChangeDate >= $time.iso({start.strftime("%Y-%m-%dT00:00:00Z")})')
    return " && ".join(parts) or "kMDItemFSName == '*'"


async def _run(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (proc.returncode or 0, out.decode("utf-8", "replace"))
    except Exception as exc:  # timeout, OSError, decode — never break the tool
        log.warning("subprocess_failed", args=args[:2], error=str(exc))
        return (1, "")


# ---- A: find_file -----------------------------------------------------------


@tool(returns_untrusted_content=True)
async def find_file(query: str, kind: str = "", since: str = "", under_dir: str = "") -> ToolResult:
    """Busca archivos con Spotlight ("¿dónde está el PDF del contrato de diciembre?").

    `kind`: pdf/image/video/audio/doc/code. `since`: "diciembre"/"ayer"/"esta
    semana"/"este mes". `under_dir`: carpeta donde buscar (~/Downloads, etc).
    """
    expr = _build_mdfind_expr(query, kind.strip().lower(), since)
    args = ["mdfind"]
    base = _resolve_in_home(under_dir) if under_dir.strip() else None
    if base is not None:
        args += ["-onlyin", str(base)]
    args.append(expr)
    _rc, out = await _run(args, timeout=15.0)
    paths = [p for p in out.splitlines() if p][:50]
    rows: list[dict[str, Any]] = []
    for p in paths:
        try:
            st = os.stat(p)
            rows.append({"path": p, "name": os.path.basename(p), "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    rows = rows[:10]
    if not rows:
        return ToolResult(True, {"results": []}, "No encontré nada con esa descripción.", False)
    kind_word = _KIND_ES.get(kind.strip().lower(), "archivos")
    where = f" en {os.path.basename(str(base))}" if base is not None else ""
    top = ", ".join(f"«{r['name']}»" for r in rows[:3])
    msg = f"Encontré {len(rows)} {kind_word}{where}: {top}." + (" ¿Cuál abro?" if len(rows) > 1 else "")
    for r in rows:
        r["size_human"] = _human(r["size"])
    return ToolResult(True, {"results": rows, "count": len(rows)}, msg, False)


# ---- B: analyze_disk_usage --------------------------------------------------


@tool()
async def analyze_disk_usage(dir: str = "~", top: int = 10) -> ToolResult:
    """Dice qué carpetas ocupan más espacio ("¿qué está ocupando lugar?").

    Escanea las carpetas de primer nivel de `dir` y sugiere candidatos a limpiar.
    """
    base = _resolve_in_home(dir)
    if base is None or not base.is_dir():
        return ToolResult(False, None, "Solo puedo analizar carpetas dentro de tu usuario.", False)
    _rc, out = await _run(["du", "-d", "1", str(base)], timeout=45.0)
    entries: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        kb, path = parts
        if path.rstrip("/") == str(base).rstrip("/"):
            continue
        try:
            entries.append((int(kb) * 1024, path))
        except ValueError:
            continue
    entries.sort(reverse=True)
    entries = entries[: max(1, min(int(top), 25))]
    if not entries:
        return ToolResult(True, {"entries": []}, "No pude medir el uso de disco ahí.", False)
    listing = [{"path": p, "size": b, "size_human": _human(b), "name": os.path.basename(p)} for b, p in entries]
    free = shutil.disk_usage(str(base)).free
    top3 = ", ".join(f"{e['name']} ({e['size_human']})" for e in listing[:3])
    msg = f"Lo que más ocupa: {top3}. Tienes {_human(free)} libres. Di «libera espacio» si quieres limpiar."
    return ToolResult(True, {"entries": listing, "free": free}, msg, False)


# ---- C: free_space_assist ---------------------------------------------------


def _candidates() -> dict[str, list[dict[str, Any]]]:
    """Safe-to-remove candidates by category. Read-only — never deletes."""
    home = Path.home()
    out: dict[str, list[dict[str, Any]]] = {"dmgs": [], "caches": [], "node_modules": []}
    downloads = home / "Downloads"
    if downloads.is_dir():
        for f in downloads.glob("*.dmg"):
            try:
                out["dmgs"].append({"path": str(f), "size": f.stat().st_size})
            except OSError:
                continue
    caches = home / "Library" / "Caches"
    if caches.is_dir():
        try:
            size = sum(p.stat().st_size for p in caches.rglob("*") if p.is_file())
            if size:
                out["caches"].append({"path": str(caches), "size": size})
        except OSError:
            pass
    return out


@tool(destructive=True)
async def free_space_assist(categories: str = "dmgs", confirmed: bool = False) -> ToolResult:
    """Sugiere y libera espacio de forma segura. SIEMPRE confirma antes de borrar.

    `categories` (coma-separadas): dmgs (instaladores viejos en Descargas) /
    caches (cachés de Library). Sin confirmar solo muestra; al confirmar mueve a
    la Papelera (reversible) la categoría elegida. Por defecto sólo 'dmgs'.
    """
    cands = await asyncio.to_thread(_candidates)
    wanted = {c.strip() for c in categories.split(",") if c.strip()} or {"dmgs"}
    selected = {k: v for k, v in cands.items() if k in wanted and v}
    total = sum(item["size"] for items in selected.values() for item in items)

    if not any(cands.values()):
        return ToolResult(True, {"candidates": {}}, "No encontré nada obvio que liberar.", False)
    if not confirmed:
        summary = "; ".join(
            f"{k}: {len(v)} ({_human(sum(i['size'] for i in v))})" for k, v in cands.items() if v
        )
        return ToolResult(
            True, {"candidates": cands, "selected": list(wanted)},
            f"Puedo liberar — {summary}. Voy a mover a la Papelera: {', '.join(wanted)} "
            f"({_human(total)}). ¿Confirmo?", requires_confirmation=True,
        )

    moved, freed = 0, 0
    for items in selected.values():
        for item in items:
            p = _resolve_in_home(item["path"])
            if p is None or not p.exists():
                continue
            try:
                freed += item["size"]
                await _run(["osascript", "-e",
                            f'tell application "Finder" to delete POSIX file "{p}"'], timeout=20.0)
                moved += 1
            except Exception as exc:
                log.warning("free_space_delete_failed", path=str(p), error=str(exc))
    return ToolResult(True, {"moved": moved, "freed": freed},
                      f"Listo, moví {moved} elementos a la Papelera (~{_human(freed)}).", False)


# ---- D: rename_batch --------------------------------------------------------


@tool(destructive=True)
async def rename_batch(
    pattern: str, replace: str, under_dir: str, kind: str = "", confirmed: bool = False
) -> ToolResult:
    """Renombra en lote con vista previa. SIEMPRE confirma antes de aplicar.

    Reemplaza `pattern` por `replace` en los nombres de archivo bajo `under_dir`
    (ej. ".heic" → ".jpg"). `kind` filtra por extensión opcional. No sobrescribe
    archivos existentes ni cruza carpetas.
    """
    base = _resolve_in_home(under_dir)
    if base is None or not base.is_dir():
        return ToolResult(False, None, "Solo puedo renombrar dentro de tu usuario.", False)
    if not pattern:
        return ToolResult(False, None, "¿Qué parte del nombre reemplazo?", False)

    ext = ("." + kind.lower().lstrip(".")) if kind.strip() else ""
    plan: list[tuple[Path, Path]] = []
    for f in sorted(base.rglob("*")):
        if not f.is_file() or pattern not in f.name:
            continue
        if ext and not f.name.lower().endswith(ext):
            continue
        new = f.with_name(f.name.replace(pattern, replace))
        if new == f or new.exists():
            continue
        plan.append((f, new))

    if not plan:
        return ToolResult(True, {"count": 0}, f"No encontré archivos con «{pattern}» para renombrar.", False)
    preview = [{"from": a.name, "to": b.name} for a, b in plan[:5]]
    if not confirmed:
        sample = "; ".join(f"{p['from']} → {p['to']}" for p in preview)
        return ToolResult(
            True, {"count": len(plan), "preview": preview},
            f"Voy a renombrar {len(plan)} archivo(s): {sample}"
            + (f" … y {len(plan) - 5} más" if len(plan) > 5 else "") + ". ¿Confirmo?",
            requires_confirmation=True,
        )
    done, errors = 0, 0
    for src, dst in plan:
        try:
            if not dst.exists():
                src.rename(dst)
                done += 1
        except OSError as exc:
            errors += 1
            log.warning("rename_failed", src=str(src), error=str(exc))
    tail = f" ({errors} con error)" if errors else ""
    return ToolResult(True, {"renamed": done, "errors": errors}, f"Renombré {done} archivo(s){tail}.", False)
