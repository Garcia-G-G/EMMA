"""Tool contract, registration decorator, and signature → JSON schema.

A tool is any callable (sync or async) that takes typed arguments and
returns a :class:`ToolResult`. Exceptions must be caught inside the tool
and surfaced via ``success=False``.

Tools that take a ``confirmed: bool = False`` argument participate in the
two-phase confirmation flow: the first call (without confirmation) sets
up the action and returns ``requires_confirmation=True`` with a question
in ``user_message``; the orchestrator replays with ``confirmed=True``
after the user assents.
"""
from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, Union, get_args, get_origin

ToolFunc = Callable[..., Any]
F = TypeVar("F", bound=ToolFunc)


@dataclass(frozen=True)
class ToolResult:
    success: bool
    data: Any
    user_message: str
    requires_confirmation: bool = False


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    fn: ToolFunc
    description: str
    parameters: dict[str, Any]
    destructive: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


_REGISTRY: dict[str, RegisteredTool] = {}


def _py_type_to_schema(tp: Any) -> dict[str, Any]:
    origin = get_origin(tp)
    args = get_args(tp)

    if tp is str or tp is type(None):
        return {"type": "string"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is bool:
        return {"type": "boolean"}
    if origin is Literal:
        return {"type": "string", "enum": [str(a) for a in args]}
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _py_type_to_schema(non_none[0])
        return {}  # fall back to "anything"
    if origin in (list, tuple):
        item = args[0] if args else str
        return {"type": "array", "items": _py_type_to_schema(item)}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}


def _docstring_summary(fn: ToolFunc) -> str:
    doc = (fn.__doc__ or "").strip()
    if not doc:
        return ""
    # OpenAI is happy with multi-paragraph descriptions; keep first two paragraphs.
    paragraphs = [p.strip() for p in doc.split("\n\n") if p.strip()]
    return "\n\n".join(paragraphs[:2])


def _function_to_parameters(fn: ToolFunc) -> dict[str, Any]:
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "confirmed":
            continue  # internal flag, not exposed to the LLM
        anno = hints.get(name, str)
        properties[name] = _py_type_to_schema(anno)
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool(
    name: str | None = None,
    *,
    destructive: bool = False,
    aliases: tuple[str, ...] = (),
) -> Callable[[F], F]:
    """Register ``fn`` in the tool registry."""

    def decorator(fn: F) -> F:
        key = name or fn.__name__
        entry = RegisteredTool(
            name=key,
            fn=fn,
            description=_docstring_summary(fn),
            parameters=_function_to_parameters(fn),
            destructive=destructive,
            aliases=aliases,
        )
        _REGISTRY[key] = entry
        for alias in aliases:
            _REGISTRY[alias] = entry
        return fn

    return decorator


def get_registry() -> dict[str, RegisteredTool]:
    return _REGISTRY
