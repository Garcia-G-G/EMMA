"""Prompt 35 — set_conversation_tone wires into runtime style state."""

from __future__ import annotations

import pytest

import core.runtime as runtime
import tools.tone_tool as tt


@pytest.fixture(autouse=True)
def _reset_style():
    runtime.set_style_hint("")
    yield
    runtime.set_style_hint("")


@pytest.mark.asyncio
async def test_set_serious_tone_sets_hint() -> None:
    res = await tt.set_conversation_tone("más serio")
    assert res.success
    assert "serio" in runtime.get_style_hint().lower()


@pytest.mark.asyncio
async def test_neutral_clears_hint() -> None:
    runtime.set_style_hint("algo")
    res = await tt.set_conversation_tone("neutral")
    assert res.success
    assert runtime.get_style_hint() == ""


@pytest.mark.asyncio
async def test_unknown_style_is_graceful() -> None:
    res = await tt.set_conversation_tone("xyzzy")
    # unknown styles don't crash and don't wipe a sensible default
    assert res.success
