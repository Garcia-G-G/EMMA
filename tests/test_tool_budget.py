"""The tool budget: availability filtering, priority order, and the hard cap.

Closes the bug class from LAUNCH-1: the registry grew from 116 to 185 tools
with nothing watching the number, nothing checking what the server accepted,
and nothing stopping two tools from claiming one name.

Note on the number: the pre-launch audit assumed a 128-tool ceiling in the
OpenAI Realtime API. Step 0 disproved it — the server echoed all 183 tools and
the model still selected the one at index 127 (see
``_planning/notes/LAUNCH-1-VERIFY.md``). So these tests assert against the
configured budget rather than a magic 128 that does not exist; the budget is a
cost guard (~60 input tokens per tool per turn), and asserting on it is what
actually keeps the count from drifting.
"""

from __future__ import annotations

import pytest

from config.settings import settings
from tools import availability, registry
from tools.base import (
    RegisteredTool,
    ToolNameCollisionError,
    get_registry,
    tool,
)
from tools.registry import (
    _CORE_MODULES,
    available_tools,
    list_tools,
    openai_tool_specs,
    unavailable_tools,
)

# --- the cap ---------------------------------------------------------------


def test_advertised_tool_count_within_budget() -> None:
    """The one-line guard. If this fails, someone added tools past the budget."""
    specs = openai_tool_specs()
    assert len(specs) <= settings.REALTIME_TOOL_BUDGET, (
        f"{len(specs)} tools advertised, budget is {settings.REALTIME_TOOL_BUDGET}. "
        f"Give the new tools an available() predicate or raise REALTIME_TOOL_BUDGET "
        f"deliberately — every tool costs ~60 input tokens on EVERY turn."
    )


def test_budget_trims_the_tail_and_never_the_core(monkeypatch: pytest.MonkeyPatch) -> None:
    core_modules = set(_CORE_MODULES)
    core_names = {e.name for e in available_tools() if registry._module_of(e) in core_modules}
    assert core_names, "no core tools found — _CORE_MODULES drifted from the tool modules"

    monkeypatch.setattr(settings, "REALTIME_TOOL_BUDGET", len(core_names) + 3)
    kept = {s["function"]["name"] for s in openai_tool_specs()}
    assert len(kept) == len(core_names) + 3
    assert core_names <= kept, "the budget trimmed a guaranteed-core tool"


def test_budget_keeps_whole_core_even_when_core_alone_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_modules = set(_CORE_MODULES)
    core_names = {e.name for e in available_tools() if registry._module_of(e) in core_modules}
    monkeypatch.setattr(settings, "REALTIME_TOOL_BUDGET", 3)
    kept = {s["function"]["name"] for s in openai_tool_specs()}
    assert kept == core_names  # overshoots the budget on purpose, never amputates core


def test_budget_zero_disables_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "REALTIME_TOOL_BUDGET", 0)
    assert len(openai_tool_specs()) == len(available_tools())


def test_screen_vision_is_in_the_guaranteed_core() -> None:
    """The feature the audit thought this bug had broken. It leads the core."""
    assert _CORE_MODULES[0] == "screen_vision_tool"
    monkey_free_names = [s["function"]["name"] for s in openai_tool_specs()]
    for name in ("describe_screen", "read_window_text", "find_button", "summarize_screen"):
        assert name in monkey_free_names


# --- availability ----------------------------------------------------------


def test_unavailable_tools_stay_dispatchable() -> None:
    """Filtering decides what the model is TOLD about, not what Emma can run."""
    registered = set(list_tools())
    for name in unavailable_tools():
        assert name in registered
        assert registry.get_tool(name) is not None


def test_module_available_predicate_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.github_tool as gh

    monkeypatch.setattr(settings, "GITHUB_TOKEN", "ghp_realtoken")
    availability.reset_cache()
    assert gh.available() is True
    assert "my_repos" in {s["function"]["name"] for s in openai_tool_specs()}

    monkeypatch.setattr(settings, "GITHUB_TOKEN", "")
    assert gh.available() is False
    assert "my_repos" not in {s["function"]["name"] for s in openai_tool_specs()}


def test_per_tool_available_predicate_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_web is gated on a search key; its module-mates are not."""
    monkeypatch.setattr(settings, "BRAVE_API_KEY", None)
    monkeypatch.setattr(settings, "TAVILY_API_KEY", None)
    names = {s["function"]["name"] for s in openai_tool_specs()}
    assert "search_web" not in names
    assert "deep_research" not in names
    assert "summarize_page" in names  # same module, needs no key
    assert "summarize_url" in names  # fetches directly, never searched

    monkeypatch.setattr(settings, "BRAVE_API_KEY", "brave-key")
    assert "search_web" in {s["function"]["name"] for s in openai_tool_specs()}


def test_availability_is_reevaluated_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a static list captured at import — flipping a setting flips the payload."""
    monkeypatch.setattr(settings, "NOTION_API_KEY", "")
    assert "notion_append" not in {s["function"]["name"] for s in openai_tool_specs()}
    monkeypatch.setattr(settings, "NOTION_API_KEY", "secret_abc")
    assert "notion_append" in {s["function"]["name"] for s in openai_tool_specs()}


def test_broken_probe_keeps_the_tool() -> None:
    """Under-filtering costs tokens; over-filtering silently removes a capability."""

    def boom() -> bool:
        raise RuntimeError("probe exploded")

    entry = RegisteredTool(
        name="_probe_test", fn=lambda: None, description="", parameters={}, available=boom
    )
    assert registry._is_available(entry) is True


def test_dev_tools_hidden_without_a_git_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.dev as dev

    monkeypatch.setattr(dev.availability, "has_path", lambda _p: False)
    assert dev.available() is False
    names = {s["function"]["name"] for s in openai_tool_specs()}
    assert "open_workspace_for_debugging" not in names
    assert "describe_my_health" not in names


# --- name collisions -------------------------------------------------------


def test_duplicate_tool_name_raises() -> None:
    """The guard that would have caught three open_url tools on day one."""
    with pytest.raises(ToolNameCollisionError, match="current_time"):

        @tool(name="current_time")
        def _impostor() -> None: ...


def test_hot_reload_reregisters_without_raising() -> None:
    """``reload_all_tools`` re-runs every decorator with NEW function objects.

    The collision guard compares modules, not function identity, precisely so
    this keeps working — an identity check would make every reload raise.
    """
    import importlib

    import tools.system

    before = registry.get_tool("current_time")
    assert before is not None
    importlib.reload(tools.system)
    after = registry.get_tool("current_time")
    assert after is not None
    assert after.fn is not before.fn  # genuinely a new object
    assert after.fn.__module__ == "tools.system"


def test_open_url_resolved_in_favor_of_user_browser() -> None:
    entry = registry.get_tool("open_url")
    assert entry is not None
    assert entry.fn.__module__ == "tools.user_browser"
    safari = registry.get_tool("open_url_safari")
    assert safari is not None
    assert safari.fn.__module__ == "tools.safari_tool"
    assert registry.get_tool("open_url_web") is None


def test_every_registered_name_is_unique_per_function() -> None:
    """Two entries sharing a name would mean one implementation is unreachable."""
    by_name: dict[str, object] = {}
    for entry in get_registry().values():
        prev = by_name.setdefault(entry.name, entry.fn)
        assert prev is entry.fn, f"{entry.name} maps to two different functions"
