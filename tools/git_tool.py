"""Clone a GitHub repo and open it in Garcia's preferred IDE — one voice command.

The single flow Phase 18 targets: resolve a repo (URL, owner/name, or a search
query), clone it shallowly as a background task (so Emma answers immediately),
and open the cloned folder in the detected IDE when the clone finishes. The
macOS notification from ``core.background`` (15.12) closes the loop.

This is deliberately NOT a general git tool — just this flow.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import structlog

from actions.environment import detect_preferred
from config.settings import settings
from core.background import registry
from tools.base import ToolResult, tool
from tools.dev import _APP_NAME_BY_KEY  # reuse the IDE key→display-name map
from tools.github_tool import get_repo_url

log = structlog.get_logger("emma.tools.git")

# Characters that would break out of a double-quoted shell value. Repo URLs and
# names from GitHub are safe; a crafted dest_subdir / ide override is the risk.
_SHELL_UNSAFE = re.compile(r'["`$\\\n\r]')


def _safe(s: str) -> bool:
    return not _SHELL_UNSAFE.search(s)


async def _resolve_repo_url(repo_url: str) -> tuple[str | None, ToolResult | None]:
    """Resolve a URL / owner-name / search query to a clone URL.

    Returns ``(clone_url, None)`` on success or ``(None, error_result)``.
    """
    url = repo_url.strip()
    if not url:
        return None, ToolResult(False, None, "Dime qué repo.", False)
    if url.startswith(("http://", "https://", "git@")):
        return url, None
    if "/" in url and " " not in url:
        return f"https://github.com/{url.strip('/')}.git", None
    res = await get_repo_url(url)  # free-form query → top match
    if not res.success or not res.data:
        return None, res
    return res.data["clone_url"], None


def _resolve_ide(ide_override: str) -> str | None:
    """Display name for ``open -a`` (e.g. 'Cursor'), or None if no IDE."""
    if ide_override.strip():
        key = ide_override.strip().lower()
        return _APP_NAME_BY_KEY.get(key, ide_override.strip())
    res = detect_preferred("ide")
    if res.app_name:
        return _APP_NAME_BY_KEY.get(res.app_name, res.app_name)
    return None


def _build_clone_cmd(url: str, dest: str, ide: str) -> str:
    """The shell command the background task runs (shallow clone + open IDE)."""
    return f'rm -rf "{dest}" && git clone --depth 1 "{url}" "{dest}" && open -a "{ide}" "{dest}"'


@tool(destructive=True)
async def clone_and_open(
    repo_url: str,
    dest_subdir: str = "",
    ide: str = "",
    confirmed: bool = False,
) -> ToolResult:
    """Clone a GitHub repo and open it in Garcia's preferred IDE.

    Use when Garcia says any of:
    - "Emma, clona <repo> en mi IDE"
    - "Emma, baja ese repo y ábrelo"
    - "Emma, clona el primero de los que encontraste"
    """
    url, err = await _resolve_repo_url(repo_url)
    if err is not None or url is None:
        return err or ToolResult(False, None, "No pude resolver el repo.", False)

    name = (dest_subdir or url.rstrip("/").split("/")[-1]).removesuffix(".git").strip("/")
    # Guard against a degenerate name (empty, ".", "..", or one with a path
    # separator): dest would collapse to CLONE_DIR itself and the clone
    # command's `rm -rf "{dest}"` would wipe the entire clone directory.
    if not name or name in (".", "..") or "/" in name:
        return ToolResult(False, None, "No pude derivar un nombre de carpeta válido para ese repo.", False)
    dest = Path(settings.CLONE_DIR) / name

    chosen = _resolve_ide(ide)
    if not chosen:
        return ToolResult(
            False,
            None,
            "No tengo un IDE configurado. Instala VS Code, Cursor o Zed primero.",
            False,
        )

    # Injection guard: refuse values that would break the double-quoted command.
    if not all(_safe(v) for v in (url, str(dest), chosen)):
        return ToolResult(
            False, None, "Ese nombre tiene caracteres que no puedo usar con seguridad.", False
        )

    if not confirmed:
        pre = f" (la carpeta {dest} ya existe; la sobrescribo)" if dest.exists() else ""
        return ToolResult(
            True,
            {"url": url, "dest": str(dest), "ide": chosen},
            f"¿Clono {name} en {dest}{pre} y lo abro en {chosen}?",
            requires_confirmation=True,
        )

    cmd = _build_clone_cmd(url, str(dest), chosen)

    async def runner(ctrl):  # type: ignore[no-untyped-def]
        proc = await asyncio.create_subprocess_exec(
            "/bin/zsh",
            "-lc",
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            ctrl.append_output(line.decode("utf-8", errors="replace"))
        return await proc.wait()

    rec = await registry().start(
        name=f"clone:{name}",
        kind="shell",
        coro_factory=runner,
        meta={"url": url, "dest": str(dest), "ide": chosen, "cmd": cmd},
    )
    return ToolResult(
        True,
        {"id": rec.id, "name": name, "dest": str(dest), "cmd": cmd},
        f"Listo, clonando {name}. Te aviso cuando esté listo en {chosen}.",
        False,
    )
