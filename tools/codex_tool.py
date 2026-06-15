"""Voice-callable delegation to Emma's native coding sub-agent (Prompt 23).

Mirrors ``tools/agents_tool.py:delegate_to_claude_code`` (confirmation gate,
optional git worktree, background dispatch, completion notification) — but
the work runs through ``core.coding_agent.run_agent`` (Responses API, in
process) instead of an external CLI. No `codex` binary, no Gatekeeper.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings
from core import events_bus
from core.background import MAX_PARALLEL_TASKS, registry
from core.coding_agent import estimate_cost_usd, run_agent
from memory import episodic
from tools.agents_tool import setup_worktree
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.codex")

# Garcia may name a model by voice; only these are honored (anything else
# falls back to the configured default — never an arbitrary string).
_ALLOWED_MODELS = frozenset({"gpt-5.3-codex", "gpt-5-codex", "gpt-5.2-codex", "gpt-5.5"})


@tool(destructive=True)
async def delegate_to_codex(
    task: str,
    cwd: str = "~/Documents/EMMA",
    branch: str = "",
    model: str = "",
    confirmed: bool = False,
) -> ToolResult:
    """Encarga una tarea de programación a un agente de codificación interno.

    Úsalo cuando Garcia pida trabajo NO trivial sobre un repo: "agrega login
    con Google", "refactoriza este módulo", "arregla el bug de X", "escribe
    tests". Para cambios chiquitos de una o dos líneas usa edit_file_* en su
    lugar.

    El agente corre en segundo plano (lee, escribe y prueba archivos dentro
    de `cwd`) y Emma avisa por notificación cuando termina. Si pasas `branch`,
    trabaja en un worktree de git nuevo para que tu checkout quede limpio.
    """
    repo = Path(os.path.expanduser(cwd)).resolve()
    home = Path.home().resolve()
    if repo != home and not repo.is_relative_to(home):
        return ToolResult(
            False, None, "Solo puedo trabajar dentro de tu carpeta de usuario.", False
        )
    if not repo.is_dir():
        return ToolResult(False, None, f"No encontré la carpeta {repo}.", False)
    if not settings.OPENAI_API_KEY:
        return ToolResult(False, None, "No tengo la API key de OpenAI configurada.", False)
    if branch and not (repo / ".git").exists():
        return ToolResult(False, None, f"{repo} no es un repo git, no puedo crear una rama.", False)

    chosen_model = model if model in _ALLOWED_MODELS else settings.CODING_AGENT_MODEL
    est = estimate_cost_usd(task, chosen_model)

    if not confirmed:
        target = f"una rama nueva '{branch}'" if branch else "el directorio actual"
        warn = f" Estimo ~${est:.2f}." if est > settings.CODING_AGENT_MAX_COST_USD else ""
        return ToolResult(
            True,
            {"task": task, "cwd": str(repo), "branch": branch, "estimate_usd": round(est, 2)},
            f"¿Le encargo al agente «{task[:90]}» en {target}?{warn}",
            requires_confirmation=True,
        )

    reg = registry()
    if reg.at_capacity():
        return ToolResult(
            False,
            None,
            f"Tengo {MAX_PARALLEL_TASKS} tareas corriendo ya; espera a que termine alguna.",
            False,
        )

    work_dir = await setup_worktree(repo, branch)

    async def runner(ctrl: Any) -> int:
        result = await run_agent(task, str(work_dir), model=chosen_model, task_id=ctrl.task_id)
        ctrl.append_output(
            f"[{result.status}] {result.summary}\n"
            f"iters={result.iters_used} tools={result.tool_calls} "
            f"cost=${result.cost_usd:.3f}\ntranscript={result.transcript_path}"
        )
        events_bus.publish(
            "task_completed",
            kind="coding_agent",
            status=result.status,
            cost_usd=round(result.cost_usd, 4),
            summary=result.summary[:200],
        )
        # exit 0 only when the agent reports a clean finish.
        return 0 if result.status == "ok" else 1

    rec = await reg.start(
        name=f"codex:{task[:24]}",
        kind="coding_agent",
        coro_factory=runner,
        meta={"task": task, "cwd": str(work_dir), "branch": branch, "model": chosen_model},
    )
    where = f"la rama '{branch}'" if branch else work_dir.name
    data: dict[str, Any] = {"id": rec.id}
    # 28.1-C1: reversing Codex is operational, not programmatic — Garcia may want
    # to keep the work. Hand him the exact discard command (a manual blueprint).
    if branch:
        wt = f"emma-wt-{branch.replace('/', '-')}"
        data["_reverse_blueprint"] = episodic.blueprint_manual(
            f"Para descartar ese trabajo: git -C {repo} worktree remove ../{wt} --force "
            f"&& git -C {repo} branch -D {branch}"
        )
    return ToolResult(
        True, data, f"Le pedí al agente en {where}. Te aviso cuando termine.", False
    )


@tool()
async def codex_status(task_id: str = "") -> ToolResult:
    """Dice cómo va la última tarea del agente de codificación (o una por id).

    Úsalo cuando Garcia pregunte "¿cómo va el agente?" / "¿ya terminó Codex?".
    """
    reg = registry()
    if task_id:
        rec = reg.get(task_id)
    else:
        agent_tasks = [r for r in reg.list(limit=50) if r.kind == "coding_agent"]
        rec = agent_tasks[0] if agent_tasks else None
    if rec is None:
        return ToolResult(True, {"tasks": []}, "No tengo tareas del agente todavía.", False)
    spoken = {
        "running": f"Sigue trabajando en «{rec.meta.get('task', '')[:60]}».",
        "completed": f"Ya terminó: {rec.last_output[-160:] or 'listo'}",
        "failed": f"Falló: {rec.error or rec.last_output[-160:]}",
        "cancelled": "Esa tarea se canceló.",
        "aborted": "Esa tarea se interrumpió (reinicié antes de que acabara).",
    }.get(rec.status, f"Estado: {rec.status}.")
    return ToolResult(True, {"id": rec.id, "status": rec.status}, spoken, False)
