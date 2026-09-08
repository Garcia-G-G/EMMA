"""Tool discovery, lookup, OpenAI function specs, and dispatch."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path
from typing import Any

import structlog

import tools
from config.settings import settings
from tools.base import RegisteredTool, ToolNameCollisionError, ToolResult, get_registry

log = structlog.get_logger("emma.tools.registry")

# "base"/"registry" hold no tools. "availability" is the probe helpers.
_SKIP = {"base", "registry", "availability"}
_discovered = False


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    pkg_path = Path(tools.__file__).parent
    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        if module_info.name in _SKIP:
            continue
        try:
            importlib.import_module(f"tools.{module_info.name}")
        except ToolNameCollisionError:
            # Never swallow this one. Two tools claiming a name means one is
            # silently gone; degrading it to "that module failed to import"
            # would hide the very failure the check exists to surface.
            raise
        except Exception as exc:
            log.error("tool_module_import_failed", module=module_info.name, error=str(exc))
    _discovered = True
    # Refresh self/capabilities.md from the now-populated registry so
    # the `describe_capabilities` tool reads a current list rather than
    # the day-zero scaffold stub. Best-effort; never fatal.
    try:
        from tools.self_tool import regenerate_capabilities_md

        regenerate_capabilities_md()
    except Exception as exc:
        log.warning("self_capabilities_regen_failed", error=str(exc))


def list_tools() -> list[str]:
    _discover()
    # De-duplicate aliases so the LLM only sees canonical names.
    seen: set[str] = set()
    out: list[str] = []
    for _name, entry in get_registry().items():
        if entry.name in seen:
            continue
        seen.add(entry.name)
        out.append(entry.name)
    return sorted(out)


def get_tool(name: str) -> RegisteredTool | None:
    _discover()
    return get_registry().get(name)


# Modules whose tools are NEVER trimmed by the budget, in priority order.
# These are what Emma is: she sees the screen, remembers, controls the machine
# and its apps, handles files, and owns calendar / reminders / notes. Screen
# vision leads the list deliberately — it is the feature whose apparent
# disappearance triggered this audit.
_CORE_MODULES: tuple[str, ...] = (
    "screen_vision_tool",
    "visual_screen_tool",
    "memory_tool",
    "secrets_tool",
    "lifecycle_tool",
    "session_actions_tool",
    "system",
    "app_control",
    "app_url_tool",
    "preferences",
    "finder_tool",
    "file_ops_tool",
    "file_edit",
    "calendar_tool",
    "reminders_tool",
    "notes_tool",
)
_CORE_RANK = {m: i for i, m in enumerate(_CORE_MODULES)}

# Trimmable, but in a DELIBERATE order — the second tier, ranked by what Emma
# loses most by losing it. Everything below core used to sort by module *name*,
# which encodes no notion of value: with 172 of 175 budget used, the first
# casualties were whatever fell late in the alphabet (`web`, `workflow_tool`,
# `youtube`) while `birthday_tool` survived on the strength of its 'b'. That put
# `search_web` and `open_url` — the latter guarded by a test in this very suite —
# three tools from the cliff.
#
# Listing a module here is a claim that it should outlive the long tail, not that
# it is safe. Anything unlisted forms the trim zone and is dropped first, ordered
# by module for stability. Keep this ranked, not alphabetical.
_PRIORITY_MODULES: tuple[str, ...] = (
    # Answering a question at all. The single most-used non-core capability.
    "web",
    "url_summary_tool",
    # The user's real browser (open_url resolves here, not to safari_tool).
    "user_browser",
    # Reaching people.
    "messages_tool",
    "mail_tool",
    # The background-task convention's voice surface: a long-running task that
    # can be started but never queried or cancelled is worse than no task.
    "tasks_tool",
    # Everyday small talk with the machine.
    "datetime_tool",
    "timer_tool",
    "history_tool",
    "self_tool",
    # Named in the environment detection shortlist; a daily driver.
    "music",
    # Escape hatches and dev work.
    "shell",
    "shell_tool",
    "terminal_actions",
    "ide_actions",
    "git_tool",
    "dev",
    "codex_tool",
    "agents_tool",
    # Secondary browser paths — real, but user_browser is the primary.
    "safari_tool",
    "browser",
    # Behavioral surfaces: useful, and cheap to lose for a turn.
    "proactive_tool",
    "diagnostics",
    "diagnostics_tool",
)
_PRIORITY_RANK = {m: i for i, m in enumerate(_PRIORITY_MODULES)}


def _module_of(entry: RegisteredTool) -> str:
    return getattr(entry.fn, "__module__", "").rsplit(".", 1)[-1]


def _probe(fn: Any, what: str) -> bool:
    """Run an availability predicate. A broken probe never removes a tool."""
    try:
        return bool(fn())
    except Exception as exc:
        log.warning("tool_availability_probe_failed", probe=what, error=str(exc))
        return True


def _is_available(entry: RegisteredTool) -> bool:
    """Can this tool actually do anything on this machine right now?

    Two gates, both cheap and re-evaluated every session: the tool's own
    ``available=`` predicate, and its module's ``available()`` if it defines
    one. Unavailable tools stay registered and dispatchable — they are only
    dropped from the payload sent to the model.
    """
    if entry.available is not None and not _probe(entry.available, entry.name):
        return False
    module = sys.modules.get(getattr(entry.fn, "__module__", ""))
    probe = getattr(module, "available", None) if module is not None else None
    return not (callable(probe) and not _probe(probe, _module_of(entry)))


def available_tools() -> list[RegisteredTool]:
    """Registered tools that can run here, deduped, in budget-priority order.

    Three tiers, in order: ``_CORE_MODULES`` (never trimmed), then
    ``_PRIORITY_MODULES`` (trimmable, but ranked by value), then everything else
    grouped by module name. The order is what the budget trims from the tail of,
    so the first two tiers are declared deliberately; only the unlisted tail
    falls back to alphabetical, and it does so because it IS the tail.
    """
    _discover()
    seen: set[str] = set()
    entries: list[RegisteredTool] = []
    for entry in get_registry().values():
        if entry.name in seen:
            continue
        seen.add(entry.name)
        if _is_available(entry):
            entries.append(entry)

    def key(e: RegisteredTool) -> tuple[int, int, str, str]:
        mod = _module_of(e)
        rank = _CORE_RANK.get(mod)
        if rank is not None:
            return (0, rank, "", e.name)
        rank = _PRIORITY_RANK.get(mod)
        if rank is not None:
            return (1, rank, "", e.name)
        return (2, 0, mod, e.name)

    return sorted(entries, key=key)


def _spec(entry: RegisteredTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": entry.name,
            "description": entry.description,
            "parameters": entry.parameters,
        },
    }


def openai_tool_specs() -> list[dict[str, Any]]:
    """Advertisable tools formatted for OpenAI's ``tools`` parameter.

    Two reductions, in order:

    1. **Availability** — tools that cannot run here (no credential, no CLI, no
       browser binary) are dropped. Every advertised tool costs ~60 input
       tokens on *every* turn, so a tool that can only fail is pure cost.
    2. **Budget** — if more than ``REALTIME_TOOL_BUDGET`` survive, the tail is
       trimmed. Core modules are never trimmed, and the drop is logged by name
       at ERROR. It is never silent.
    """
    entries = available_tools()
    budget = int(settings.REALTIME_TOOL_BUDGET)
    if budget <= 0 or len(entries) <= budget:
        return [_spec(e) for e in entries]

    core = [e for e in entries if _module_of(e) in _CORE_RANK]
    rest = [e for e in entries if _module_of(e) not in _CORE_RANK]
    room = budget - len(core)
    if room < 0:
        # The guaranteed core alone exceeds the budget. Keep all of it anyway —
        # amputating core is worse than overshooting — and say so loudly.
        log.error(
            "tool_budget_below_core",
            budget=budget,
            core=len(core),
            hint="raise REALTIME_TOOL_BUDGET or shrink _CORE_MODULES",
        )
        kept, dropped = core, rest
    else:
        kept, dropped = core + rest[:room], rest[room:]
    log.error(
        "tool_budget_exceeded",
        budget=budget,
        available=len(entries),
        kept=len(kept),
        dropped=[e.name for e in dropped],
    )
    return [_spec(e) for e in kept]


def unavailable_tools() -> list[str]:
    """Registered tool names filtered out as unavailable on this machine."""
    _discover()
    seen: set[str] = set()
    out: list[str] = []
    for entry in get_registry().values():
        if entry.name in seen:
            continue
        seen.add(entry.name)
        if not _is_available(entry):
            out.append(entry.name)
    return sorted(out)


async def dispatch(name: str, args: dict[str, Any]) -> ToolResult:
    """Look up `name` and call it with `args`. Catches exceptions."""
    entry = get_tool(name)
    if entry is None:
        return ToolResult(False, None, f"Tool {name} no está registrada.", False)
    try:
        result = entry.fn(**args)
        if inspect.isawaitable(result):
            result = await result
    except TypeError as exc:
        # Never log `args` itself: for secret-tier tools (remember_secret,
        # recall_secret, post_to_x, …) it contains plaintext values. Log only
        # the key names so a malformed call can't leak a password to disk.
        log.error("tool_bad_args", tool=name, arg_keys=sorted(args), error=str(exc))
        return ToolResult(False, None, f"Argumentos inválidos para {name}.", False)
    except Exception as exc:
        log.exception("tool_runtime_error", tool=name)
        return ToolResult(False, None, f"Falló {name}: {exc}", False)
    if not isinstance(result, ToolResult):
        return ToolResult(False, None, f"{name} no retornó ToolResult.", False)
    return result


def result_to_tool_message(tc_id: str, result: ToolResult) -> dict[str, Any]:
    """Build the {role: tool, ...} message dict for the OpenAI history."""
    payload = {
        "success": result.success,
        "user_message": result.user_message,
        "requires_confirmation": result.requires_confirmation,
        "data": _json_safe(result.data),
    }
    return {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)
