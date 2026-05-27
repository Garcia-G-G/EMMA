"""Tests for tools.base: registry, decorator, and schema generation."""

from __future__ import annotations

from typing import Literal

from tools.base import (
    RegisteredTool,
    ToolResult,
    _function_to_parameters,
    _py_type_to_schema,
    get_registry,
    tool,
)


def test_py_type_to_schema_primitives() -> None:
    assert _py_type_to_schema(str) == {"type": "string"}
    assert _py_type_to_schema(int) == {"type": "integer"}
    assert _py_type_to_schema(float) == {"type": "number"}
    assert _py_type_to_schema(bool) == {"type": "boolean"}


def test_py_type_to_schema_literal() -> None:
    schema = _py_type_to_schema(Literal["a", "b"])
    assert schema == {"type": "string", "enum": ["a", "b"]}


def test_py_type_to_schema_optional() -> None:
    schema = _py_type_to_schema(str | None)
    assert schema == {"type": "string"}


def test_py_type_to_schema_list() -> None:
    schema = _py_type_to_schema(list[int])
    assert schema == {"type": "array", "items": {"type": "integer"}}


def test_function_to_parameters() -> None:
    def example(name: str, count: int = 5) -> ToolResult:
        return ToolResult(True, None, "", False)

    params = _function_to_parameters(example)
    assert params["type"] == "object"
    assert "name" in params["properties"]
    assert "count" in params["properties"]
    assert "name" in params["required"]
    assert "count" not in params["required"]


def test_function_to_parameters_hides_internal_args() -> None:
    def example(query: str, confirmed: bool = False, cancelled: bool = False) -> ToolResult:
        return ToolResult(True, None, "", False)

    params = _function_to_parameters(example)
    assert "confirmed" not in params["properties"]
    assert "cancelled" not in params["properties"]
    assert "query" in params["properties"]


def test_tool_decorator_registers() -> None:
    @tool("_test_tool_xyz")
    def _test_tool_xyz(x: str) -> ToolResult:
        """A test tool."""
        return ToolResult(True, None, x, False)

    registry = get_registry()
    assert "_test_tool_xyz" in registry
    entry = registry["_test_tool_xyz"]
    assert isinstance(entry, RegisteredTool)
    assert entry.description == "A test tool."


def test_tool_decorator_with_aliases() -> None:
    @tool("_test_alias_main", aliases=("_test_alias_a",))
    def _test_alias_main(x: str) -> ToolResult:
        """Aliased."""
        return ToolResult(True, None, x, False)

    registry = get_registry()
    assert "_test_alias_main" in registry
    assert "_test_alias_a" in registry
    assert registry["_test_alias_a"].name == "_test_alias_main"


def test_tool_result_defaults() -> None:
    r = ToolResult(True, {"key": "val"}, "ok")
    assert r.success is True
    assert r.requires_confirmation is False
