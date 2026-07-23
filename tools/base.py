"""Tool contract, registration decorator, and signature → JSON schema.

A tool is any callable (sync or async) that takes typed arguments and
returns a :class:`ToolResult`. Exceptions must be caught inside the tool
and surfaced via ``success=False``.

Tools that take a ``confirmed: bool = False`` argument participate in the
two-phase confirmation flow: the first call (without confirmation) sets
up the action and returns ``requires_confirmation=True`` with a
**binary yes/no question** in ``user_message``; the orchestrator replays
with ``confirmed=True`` after the user assents.

``requires_confirmation`` is for destructive / irreversible actions only
(add-to-cart, send-message, delete, post-publicly, install-and-set-default).
**Do not** use it for disambiguation, multi-choice, or any non-yes/no
follow-up - those return ``success=True`` with the candidates in
``user_message`` and let the LLM ask the next question naturally on the
next user turn.

Cancellation is opt-in. Tools that want a cleanup / fallback branch when
the user declines should declare ``cancelled: bool = False`` in their
signature. The orchestrator inspects the signature and only re-dispatches
with ``cancelled=True`` to tools that opt in - other tools are simply
not called on cancel.
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
    # When True, the conversation session closes after Emma speaks this result's
    # message (returning to wake-word listening). Used by playback tools so the
    # open mic stops fighting the music it just started (Emma has no acoustic
    # echo cancellation for third-party audio like Spotify).
    ends_session: bool = False


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    fn: ToolFunc
    description: str
    parameters: dict[str, Any]
    destructive: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # True for tools whose result carries text Emma did NOT hear from the user's own
    # microphone — email, web pages, on-screen text, notes, filenames, browser
    # tabs. That text is attacker-reachable, so the function handler fences it in
    # <untrusted_content> before it reaches the model (see core/conversation.py).
    returns_untrusted_content: bool = False


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


_INTERNAL_ARGS: frozenset[str] = frozenset({"confirmed", "cancelled"})


def _function_to_parameters(fn: ToolFunc) -> dict[str, Any]:
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in _INTERNAL_ARGS:
            continue  # internal flags, not exposed to the LLM
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
    returns_untrusted_content: bool = False,
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
            returns_untrusted_content=returns_untrusted_content,
        )
        _REGISTRY[key] = entry
        for alias in aliases:
            _REGISTRY[alias] = entry
        return fn

    return decorator


def get_registry() -> dict[str, RegisteredTool]:
    return _REGISTRY
