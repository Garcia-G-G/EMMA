"""Per-request tool selection: a small always-on core + retrieval (LAUNCH-12).

Sending all ~172 available tools cost ~18-22k input tokens per turn, which no
free text tier accepts and every architecture pays for. Now a turn carries:

* ``CORE_TOOLS`` — ~24 tools any turn may need regardless of topic, in a fixed
  order so the prefix stays byte-stable (cacheable);
* ``find_tools`` — the loader: when the core lacks a capability the model calls
  it with its own phrasing of the need, the retriever adds the matches, and the
  model calls the real tool in its next step;
* a *sticky* set — tools retrieved from each finished user transcript, kept for
  the following turns (LRU, ``STICKY_MAX``), appended AFTER the core.

Why a loader instead of retrieving from the current utterance: Emma's Realtime
session runs ``server_vad`` with automatic responses, so the server starts
answering before the transcript of that utterance exists. The cascade path has
the text first and calls :func:`select_for_text` directly.

Retriever: BM25 over name + description (+ ``tools/guidance.py`` trigger
phrases). Measured on the frozen 140-item set (LAUNCH-12-VERIFY.md §Part 2):
core + BM25 top-10 = 90.4 % recall; a local qwen3-embedding hybrid reached
95.7 % but needs a 639 MB model the installer does not ship, and OpenAI
embeddings cost money. Dependency-free BM25 ships; ``find_tools`` covers misses.

Hard limit: never more than ``MAX_TOOLS_PER_REQUEST`` (128) tools in any request
— Groq enforces it, and other providers may.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

log = structlog.get_logger("emma.tool_selection")

MAX_TOOLS_PER_REQUEST = 128
RETRIEVE_K = 10  # tools added per find_tools call / per transcript
STICKY_MAX = 12  # retrieved tools kept across turns (LRU)
# Size bound on the sticky set, so a turn stays under 8k tokens whatever gets
# retrieved: the session opens at ~6.1k (prompt 2.7k + core 3.4k, o200k), and
# 5,000 chars of tool JSON is <= ~1.6k tokens even at the densest measured
# ratio (3.11 chars/token). A count alone can't promise that — the 12 largest
# tools are 3.5k tokens. LAUNCH-12-VERIFY.md §Part 2.
STICKY_CHAR_BUDGET = 5_000

LOADER_TOOL = "find_tools"

# Justified one by one in LAUNCH-12-VERIFY.md §Part 2. Order is part of the
# cache key — append, don't reorder.
CORE_TOOLS: tuple[str, ...] = (
    # Named by the always-on prompt: the rule breaks if its tool is absent.
    "remember_stt_correction",  # Learn from corrections
    "recall_last_action",  # Anaphora resolution
    "recall_facts",  # Long-term memory
    "set_conversation_tone",  # Emotional attunement
    # Session lifecycle — must work whatever the conversation is about.
    "shutdown_emma",
    "snooze_listening",
    # Memory.
    "remember_fact",
    # Screen vision — non-negotiable (audit 2026-08-03 §2): "¿qué ves?" can
    # follow any topic, and the AX→screenshot fallback needs both layers.
    "describe_screen",
    "look_at_screen",
    "summarize_screen",
    "summarize_pane",
    "read_pane_text",
    # Background-task status (CLAUDE.md background-tasks convention).
    "task_status",
    "list_my_tasks",
    # Everyday actions: most-called in production logs.
    "open_url",
    "open_application",
    "current_datetime_speak",
    "play_track",
    "pause",
    "set_volume",
    "search_web",
    "describe_capabilities",
    "run_command",
    # The loader itself.
    LOADER_TOOL,
)

_TOK = re.compile(r"[a-záéíóúñü0-9]+")


def _toks(s: str) -> list[str]:
    s = s.lower().replace("_", " ")
    return [t for t in _TOK.findall(s) if len(t) > 2]


# The wake phrase ("Hey Emma", "Oye Emma") opens most utterances but is never
# the request — matching it pulled restart_emma / shutdown-style tools into
# nearly every turn. Dropped from QUERIES only; descriptions are untouched.
_QUERY_STOP = frozenset({"emma", "hey", "oye", "hola"})


def _query_toks(s: str) -> list[str]:
    return [t for t in _toks(s) if t not in _QUERY_STOP]


class _BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 0.0
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, q: list[str]) -> list[float]:
        out = []
        for dl, tf in zip(self.len, self.tf, strict=True):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += (
                        self.idf[t]
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
                    )
            out.append(s)
        return out


class ToolSelector:
    """Core + BM25 retrieval over a fixed list of Chat-Completions tool specs."""

    def __init__(self, specs: list[dict[str, Any]]) -> None:
        self.by_name = {s["function"]["name"]: s for s in specs}
        self.core = [n for n in CORE_TOOLS if n in self.by_name]
        core = set(self.core)
        self.pool = [n for n in self.by_name if n not in core]
        self._bm = _BM25(
            [_toks(n + " " + self.by_name[n]["function"].get("description", "")) for n in self.pool]
        )

    def retrieve(self, text: str, k: int = RETRIEVE_K) -> list[str]:
        """Top-``k`` non-core tool names for ``text`` (only positive scores)."""
        q = _query_toks(text)
        if not q or not self.pool:
            return []
        sc = self._bm.scores(q)
        order = sorted(range(len(self.pool)), key=lambda i: -sc[i])
        return [self.pool[i] for i in order[:k] if sc[i] > 0]

    def select_for_text(self, text: str, k: int = RETRIEVE_K) -> list[dict[str, Any]]:
        """Cascade path: the utterance is known up front → core + top-k, no loader."""
        names = [n for n in self.core if n != LOADER_TOOL] + self.retrieve(text, k)
        return cap([self.by_name[n] for n in names])


def cap(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce the hard per-request ceiling. Loud, never silent."""
    if len(specs) > MAX_TOOLS_PER_REQUEST:
        log.error(
            "tool_request_cap",
            requested=len(specs),
            cap=MAX_TOOLS_PER_REQUEST,
            dropped=[s["function"]["name"] for s in specs[MAX_TOOLS_PER_REQUEST:]],
        )
        return specs[:MAX_TOOLS_PER_REQUEST]
    return specs


# ---- Realtime session state -------------------------------------------------
# Module-level on purpose: Pipecat micro-sessions restart on every wake, and the
# sticky set should survive them the way session_memory does.

Applier = Callable[[list[dict[str, Any]]], Awaitable[None]]

_selector: ToolSelector | None = None
_sticky: OrderedDict[str, None] = OrderedDict()
_applier: Applier | None = None


def init(specs: list[dict[str, Any]]) -> None:
    """(Re)build the selector from the available specs (on every session build)."""
    global _selector
    _selector = ToolSelector(specs)
    for n in list(_sticky):
        if n not in _selector.by_name:
            del _sticky[n]


def set_applier(fn: Applier | None) -> None:
    """Install the callback that pushes a new tool list to the live session."""
    global _applier
    _applier = fn


def current_specs() -> list[dict[str, Any]]:
    """Core (fixed order) then sticky (insertion order) — stable prefix first."""
    if _selector is None:
        return []
    names = _selector.core + [n for n in _sticky if n not in _selector.core]
    return cap([_selector.by_name[n] for n in names])


def _spec_chars(name: str) -> int:
    assert _selector is not None
    return len(json.dumps(_selector.by_name[name], ensure_ascii=False))


def _add(names: list[str]) -> list[str]:
    assert _selector is not None
    added = []
    core = set(_selector.core)
    # Best match LAST so it is the most recent entry: the LRU/size eviction
    # below drops the oldest, which must be the weakest retrieved, not the best.
    for n in reversed(names):
        if n in core:
            continue
        if n in _sticky:
            _sticky.move_to_end(n)
            continue
        _sticky[n] = None
        added.append(n)
    while len(_sticky) > STICKY_MAX:
        _sticky.popitem(last=False)
    while _sticky and sum(_spec_chars(n) for n in _sticky) > STICKY_CHAR_BUDGET:
        _sticky.popitem(last=False)
    return [n for n in added if n in _sticky]


async def load(text: str, k: int = RETRIEVE_K) -> list[str]:
    """Retrieve for ``text`` and push the enlarged tool list if it changed.

    Returns every retrieved name now available to the model (new or already
    loaded), so the loader can tell the model what it may call.
    """
    if _selector is None:
        return []
    found = _selector.retrieve(text, k)
    added = _add(found)
    if added and _applier is not None:
        try:
            await _applier(current_specs())
        except Exception as exc:  # a failed update must not break the turn
            log.warning("tool_set_update_failed", error=str(exc))
            return []
    if added:
        log.info("tools_loaded", added=added, total=len(current_specs()))
    return [n for n in found if n in _sticky]


def reset() -> None:
    """Test hook."""
    global _selector, _applier
    _selector = None
    _applier = None
    _sticky.clear()
