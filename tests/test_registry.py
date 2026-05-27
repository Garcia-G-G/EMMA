"""Tests for tools.registry: discovery, dispatch, and tool spec generation."""

from __future__ import annotations

import asyncio

from tools.registry import dispatch, get_tool, list_tools, openai_tool_specs


def test_discovery_finds_tools() -> None:
    names = list_tools()
    assert len(names) > 0
    assert "run_command" in names
    assert "current_time" in names
    assert "health_check" in names


def test_openai_tool_specs_format() -> None:
    specs = openai_tool_specs()
    assert len(specs) > 0
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"


def test_get_tool_found() -> None:
    entry = get_tool("current_time")
    assert entry is not None
    assert entry.name == "current_time"


def test_get_tool_not_found() -> None:
    assert get_tool("nonexistent_tool_xyz") is None


def test_dispatch_unknown_tool() -> None:
    result = asyncio.get_event_loop().run_until_complete(dispatch("nonexistent_tool_xyz", {}))
    assert not result.success
    assert "no est" in result.user_message.lower() or "not" in result.user_message.lower()


def test_dispatch_bad_args() -> None:
    result = asyncio.get_event_loop().run_until_complete(
        dispatch("current_time", {"nonexistent_param": 42})
    )
    assert not result.success


def test_dispatch_current_time() -> None:
    result = asyncio.get_event_loop().run_until_complete(dispatch("current_time", {}))
    assert result.success
    assert result.data is not None
    assert "iso" in result.data
