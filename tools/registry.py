"""Tool discovery, lookup, OpenAI function specs, and dispatch."""
from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from typing import Any

import structlog

import tools
from tools.base import RegisteredTool, ToolResult, get_registry

log = structlog.get_logger("emma.tools.registry")

_SKIP = {"base", "registry"}
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
    for name, entry in get_registry().items():
        if entry.name in seen:
            continue
        seen.add(entry.name)
        out.append(entry.name)
    return sorted(out)


def get_tool(name: str) -> RegisteredTool | None:
    _discover()
    return get_registry().get(name)


def openai_tool_specs() -> list[dict[str, Any]]:
    """All registered tools formatted for OpenAI's `tools` parameter."""
    _discover()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for entry in get_registry().values():
        if entry.name in seen:
            continue
        seen.add(entry.name)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": entry.name,
                    "description": entry.description,
                    "parameters": entry.parameters,
                },
            }
        )
    return out


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
        log.error("tool_bad_args", tool=name, args=args, error=str(exc))
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
