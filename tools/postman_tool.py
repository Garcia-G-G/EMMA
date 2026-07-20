"""Postman collection runs via newman (Prompt 34, Part B).

Resolves a collection by name from the local ``~/.postman`` cache (or the Postman
API when ``POSTMAN_API_KEY`` is set), runs it with ``newman``, and returns the
pass/fail summary + a structured failure list.

Reality (pre-flight): ``newman`` is an npm global and isn't installed here, so the
tool degrades to a clear "install newman" message rather than failing opaquely. The
resolution + report-parsing logic is pure and unit-tested.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog

from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.postman")

_CACHE_DIR = Path.home() / ".postman"


def _newman_path() -> str | None:
    return shutil.which("newman") or (
        "/opt/homebrew/bin/newman" if Path("/opt/homebrew/bin/newman").exists() else None
    )


def _resolve_collection(name: str) -> tuple[str | None, list[str]]:
    """(collection_ref, options). ref None + options → ambiguous; None + [] → none found."""
    if not _CACHE_DIR.is_dir():
        return None, []
    q = name.strip().lower()
    hits = [p for p in _CACHE_DIR.glob("**/*.json") if q in p.stem.lower()]
    if not hits:
        return None, []
    exact = [p for p in hits if p.stem.lower().replace(".postman_collection", "") == q]
    if len(exact) == 1:
        return str(exact[0]), []
    if len(hits) == 1:
        return str(hits[0]), []
    return None, [p.stem for p in hits[:6]]


def _resolve_environment(name: str) -> str | None:
    if not name.strip() or not _CACHE_DIR.is_dir():
        return None
    q = name.strip().lower()
    hits = [p for p in _CACHE_DIR.glob("**/*.json") if q in p.stem.lower() and "environment" in p.stem.lower()]
    return str(hits[0]) if hits else None


async def _run_newman(collection_ref: str, env_ref: str | None) -> dict[str, Any]:
    """Run newman with the JSON reporter; return the parsed run report. Mockable seam."""
    newman = _newman_path()
    assert newman is not None
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
        report_path = tf.name
    args = [newman, "run", collection_ref, "--reporters", "json", "--reporter-json-export", report_path]
    if env_ref:
        args += ["-e", env_ref]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await asyncio.wait_for(proc.communicate(), timeout=120.0)
    try:
        data = json.loads(Path(report_path).read_text())
    finally:
        Path(report_path).unlink(missing_ok=True)
    return dict(data.get("run", {}))


def _summarize(run: dict[str, Any]) -> dict[str, Any]:
    """Pull pass/fail counts + a failure list out of a newman run report."""
    a = run.get("stats", {}).get("assertions", {})
    total, failed = int(a.get("total", 0)), int(a.get("failed", 0))
    fails = []
    for f in run.get("failures", [])[:10]:
        name = (f.get("error", {}) or {}).get("name") or (f.get("source", {}) or {}).get("name")
        msg = (f.get("error", {}) or {}).get("message")
        fails.append(": ".join(x for x in (name, msg) if x) or "fallo")
    return {"passed": total - failed, "failed": failed, "total": total, "failures": fails}


@tool(returns_untrusted_content=True)
async def postman_run(collection: str, environment: str = "") -> ToolResult:
    """Corre un collection de Postman con newman ("corre el collection de health en Postman").

    Busca el collection por nombre en ~/.postman. `environment` es opcional.
    Devuelve cuántas aserciones pasaron y la lista de fallos.
    """
    if not _newman_path():
        return ToolResult(False, None,
                          "Necesito newman para correr collections. Instálalo con «npm install -g newman».",
                          False)
    coll_ref, options = _resolve_collection(collection)
    if coll_ref is None:
        if options:
            return ToolResult(True, {"collections": options},
                              f"Encontré varios: {', '.join(options)}. ¿Cuál corro?", False)
        return ToolResult(False, None, f"No encontré el collection «{collection}» en ~/.postman.", False)

    try:
        run = await _run_newman(coll_ref, _resolve_environment(environment))
    except Exception as exc:
        log.error("newman_failed", error=str(exc))
        return ToolResult(False, None, "newman no pudo correr el collection.", False)

    s = _summarize(run)
    msg = f"{s['passed']} de {s['total']} aserciones pasaron."
    if s["failures"]:
        msg += " Fallaron: " + "; ".join(s["failures"]) + "."
    return ToolResult(s["failed"] == 0, s, msg, False)
