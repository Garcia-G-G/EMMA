"""Tests for core.conversation helpers (no real Pipecat/OpenAI needed)."""

from __future__ import annotations

from core.conversation import _adapt_tool_specs_for_realtime, _build_instructions


def test_adapt_tool_specs_flattens() -> None:
    chat_specs = [
        {
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "A test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    realtime = _adapt_tool_specs_for_realtime(chat_specs)
    assert len(realtime) == 1
    spec = realtime[0]
    assert spec["type"] == "function"
    assert spec["name"] == "test_tool"
    assert spec["description"] == "A test"
    assert "function" not in spec


def test_adapt_tool_specs_passthrough() -> None:
    flat_spec = {"type": "function", "name": "already_flat", "description": "ok", "parameters": {}}
    result = _adapt_tool_specs_for_realtime([flat_spec])
    assert result == [flat_spec]


def test_build_instructions_has_sections() -> None:
    instructions = _build_instructions()
    assert "# Role" in instructions
    assert "# Personality" in instructions
    assert "# Language" in instructions
    assert "# Response Length" in instructions
    assert "# Forbidden" in instructions
    assert "Garcia" in instructions
    assert "Emma" in instructions
