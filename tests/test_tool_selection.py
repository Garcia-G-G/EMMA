"""LAUNCH-12 Part 2: per-request tool selection, the loader, and the 128 ceiling."""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import settings
from core import tool_selection as ts
from tools import registry


@pytest.fixture(autouse=True)
def _reset() -> Any:
    ts.reset()
    yield
    ts.reset()


def _spec(name: str, desc: str = "") -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {}}}


# ---- the hard ceiling --------------------------------------------------------


def test_ceiling_is_128_everywhere() -> None:
    assert ts.MAX_TOOLS_PER_REQUEST == 128
    assert registry.MAX_TOOLS_PER_REQUEST == ts.MAX_TOOLS_PER_REQUEST


def test_full_set_fallback_never_exceeds_128(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even a budget above the ceiling (the default 175) or "disabled" (0).
    for budget in (175, 0, 10_000):
        monkeypatch.setattr(settings, "REALTIME_TOOL_BUDGET", budget)
        assert len(registry.openai_tool_specs()) <= 128


def test_cap_truncates_loudly() -> None:
    specs = [_spec(f"t{i}") for i in range(200)]
    assert len(ts.cap(specs)) == 128


def test_session_open_set_is_small_and_under_128() -> None:
    ts.init(registry.available_specs())
    names = [s["function"]["name"] for s in ts.current_specs()]
    assert len(names) <= 128
    assert len(names) <= len(ts.CORE_TOOLS) + ts.STICKY_MAX
    assert ts.LOADER_TOOL in names


def test_worst_case_session_stays_under_128() -> None:
    ts.init(registry.available_specs())
    for text in ("notas whatsapp", "calendario correo", "pestañas terminal", "github repo clonar"):
        ts._add(ts._selector.retrieve(text) if ts._selector else [])
    assert len(ts.current_specs()) <= min(128, len(ts.CORE_TOOLS) + ts.STICKY_MAX)


# ---- the core ----------------------------------------------------------------


def test_every_core_tool_is_registered() -> None:
    registry.available_tools()
    names = set(registry.get_registry())
    missing = [n for n in ts.CORE_TOOLS if n not in names]
    assert not missing, missing


def test_screen_vision_stays_in_core() -> None:
    for n in ("describe_screen", "look_at_screen"):
        assert n in ts.CORE_TOOLS


def test_tools_named_by_the_always_on_prompt_are_core() -> None:
    for n in (
        "remember_stt_correction",
        "recall_last_action",
        "recall_facts",
        "set_conversation_tone",
    ):
        assert n in ts.CORE_TOOLS


def test_core_is_a_stable_prefix() -> None:
    ts.init(registry.available_specs())
    before = [s["function"]["name"] for s in ts.current_specs()]
    ts._add(["append_to_note", "send_whatsapp"])
    after = [s["function"]["name"] for s in ts.current_specs()]
    assert after[: len(before)] == before  # retrieved tools only ever append
    # best-ranked is appended last (most recent → survives eviction longest)
    assert after[len(before) :] == ["send_whatsapp", "append_to_note"]


# ---- retrieval ---------------------------------------------------------------


def test_retrieval_uses_the_moved_trigger_phrases() -> None:
    ts.init(registry.available_specs())
    assert ts._selector is not None
    assert "close_duplicate_tabs" in ts._selector.retrieve("cierra las pestañas duplicadas")
    assert "append_to_note" in ts._selector.retrieve("agrega leche a mi nota Compras")


def test_retrieval_never_returns_core() -> None:
    ts.init(registry.available_specs())
    assert ts._selector is not None
    assert not set(ts._selector.retrieve("describe la pantalla abre spotify")) & set(ts.CORE_TOOLS)


def test_select_for_text_is_core_plus_k_without_loader() -> None:
    sel = ts.ToolSelector(registry.available_specs())
    names = [s["function"]["name"] for s in sel.select_for_text("mándale un whatsapp a Juan")]
    assert ts.LOADER_TOOL not in names
    assert len(names) <= len(sel.core) - 1 + ts.RETRIEVE_K
    assert "send_whatsapp" in names


def test_sticky_is_lru_bounded() -> None:
    ts.init([_spec(n) for n in ts.CORE_TOOLS] + [_spec(f"x{i}") for i in range(30)])
    ts._add([f"x{i}" for i in range(20)])
    # the 12 best-ranked (earliest in the retrieval list) survive
    assert set(ts._sticky) == {f"x{i}" for i in range(ts.STICKY_MAX)}


# ---- the loader + live session update ---------------------------------------


@pytest.mark.asyncio
async def test_load_pushes_the_enlarged_set_once() -> None:
    ts.init(registry.available_specs())
    pushed: list[list[str]] = []

    async def applier(specs: list[dict[str, Any]]) -> None:
        pushed.append([s["function"]["name"] for s in specs])

    ts.set_applier(applier)
    got = await ts.load("agrega leche a mi nota Compras")
    assert "append_to_note" in got
    assert len(pushed) == 1 and "append_to_note" in pushed[0]
    await ts.load("agrega leche a mi nota Compras")  # nothing new → no update
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_failed_update_does_not_raise() -> None:
    ts.init(registry.available_specs())

    async def boom(specs: list[dict[str, Any]]) -> None:
        raise RuntimeError("socket closed")

    ts.set_applier(boom)
    assert await ts.load("agrega leche a mi nota Compras") == []


@pytest.mark.asyncio
async def test_find_tools_tool_reports_what_it_loaded() -> None:
    from tools.tool_loader_tool import find_tools

    ts.init(registry.available_specs())
    r = await find_tools("cierra las pestañas duplicadas")
    assert r.success and "close_duplicate_tabs" in r.data["loaded"]
    r = await find_tools("zzzz qqqq")
    assert not r.success and r.data["loaded"] == []


@pytest.mark.asyncio
async def test_session_properties_open_with_core_not_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import conversation

    monkeypatch.setattr(settings, "TOOL_RETRIEVAL", True)
    props = await conversation._build_session_properties()
    names = [t["name"] for t in props.tools or []]
    assert ts.LOADER_TOOL in names
    assert len(names) <= len(ts.CORE_TOOLS) + ts.STICKY_MAX
    assert "describe_screen" in names


@pytest.mark.asyncio
async def test_retrieval_off_is_the_capped_full_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import conversation

    monkeypatch.setattr(settings, "TOOL_RETRIEVAL", False)
    props = await conversation._build_session_properties()
    assert 40 < len(props.tools or []) <= 128


def test_every_available_tool_is_reachable() -> None:
    """Nothing dropped silently: every available tool is core or retrievable by name."""
    specs = registry.available_specs()
    sel = ts.ToolSelector(specs)
    unreachable = [
        s["function"]["name"]
        for s in specs
        if s["function"]["name"] not in sel.core
        and s["function"]["name"] not in sel.retrieve(s["function"]["name"].replace("_", " "), 20)
    ]
    assert not unreachable, unreachable


def test_sticky_set_is_size_bounded_even_for_the_largest_tools() -> None:
    import json

    ts.init(registry.available_specs())
    assert ts._selector is not None
    biggest = sorted(ts._selector.pool, key=ts._spec_chars, reverse=True)[:12]
    ts._add(biggest)
    total = sum(len(json.dumps(ts._selector.by_name[n], ensure_ascii=False)) for n in ts._sticky)
    assert total <= ts.STICKY_CHAR_BUDGET
    assert ts._sticky  # the newest survive


@pytest.mark.asyncio
async def test_a_complete_turn_stays_under_8k_tokens_worst_case() -> None:
    """Prompt + core + the worst sticky set must stay under 8k tokens.

    A tokenizer is not a project dependency, so this converts characters with
    ratios measured by tiktoken o200k (LAUNCH-12-VERIFY.md): prompt 4.17
    chars/token, the fixed core 3.85, and the variable sticky part at 3.11 — the
    DENSEST ratio of any tool spec, so the retrieved block is never undercounted.
    """
    import json

    from core import conversation

    prompt = await conversation._build_instructions()
    ts.init(registry.available_specs())
    assert ts._selector is not None
    core_chars = len(
        json.dumps(
            conversation._adapt_tool_specs_for_realtime(ts.current_specs()), ensure_ascii=False
        )
    )
    ts._add(sorted(ts._selector.pool, key=ts._spec_chars, reverse=True)[:12])
    sticky = [ts._selector.by_name[n] for n in ts._sticky]
    sticky_chars = len(
        json.dumps(conversation._adapt_tool_specs_for_realtime(sticky), ensure_ascii=False)
    )
    total = len(prompt) / 4.17 + core_chars / 3.85 + sticky_chars / 3.11
    assert total < 8_000, total


@pytest.mark.asyncio
async def test_best_retrieved_tool_survives_the_size_cap() -> None:
    ts.init(registry.available_specs())
    assert ts._selector is not None
    top = ts._selector.retrieve("agrega leche a mi nota Compras")[0]
    got = await ts.load("agrega leche a mi nota Compras")
    assert top in got


def test_wake_phrase_is_not_a_query() -> None:
    sel = ts.ToolSelector(registry.available_specs())
    assert sel.retrieve("Hey Emma") == []
    assert sel.retrieve("Oye Emma, abre la conexión staging") == sel.retrieve("abre la conexión staging")
