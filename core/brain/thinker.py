"""The Thinker seam: who answers a turn the brain defers (VOICE-1 Part 2).

VOICE-1 ships exactly one implementation, ``NoThinker``: it says, in a varied
template, that Emma can't do that one on her own yet — and the turn is logged
with its full decision record, which is the dataset BRAIN-2's ``HaikuThinker``
inherits. That thinker reads exactly the ``Turn`` defined here, so plugging it
in touches nothing in the pipeline.

``Turn`` carries what a model would need, already pruned (LLM-DIET):

* the transcript and the brain's whole ``Decision`` (candidates, slots, the
  defer reason) — the model starts from the brain's best guess, not from zero;
* A5, session context as ``(tool, one-line result)`` for the last few turns,
  never raw transcripts or tool payloads;
* A4, memory facts ranked by relevance to THIS utterance (BM25 over the facts,
  no embedding call, no LLM), not the top-15 by confidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from core.brain.decision import Decision, SlotScalar
from core.brain.normalize import fold, normalize
from core.brain.respond import Responder, language
from core.tool_selection import BM25, doc_tokens, query_tokens

CONTEXT_TURNS = 4  # A5: how many past turns a thinker sees
# Set True by whoever installs a real Thinker (Steps 41/42). With none, Emma does
# only what the brain decides locally, and "¿qué puedes hacer?" says exactly that.
WIRED = False
FACTS_K = 5  # A4: how many facts, by relevance


@dataclass(frozen=True)
class ContextTurn:
    """One past turn, pruned: what ran (or None) and a one-line result."""

    tool: str | None
    result: str


@dataclass(frozen=True)
class Turn:
    transcript: str
    decision: Decision
    context: tuple[ContextTurn, ...] = ()
    facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThinkerResult:
    """What to say, and optionally a tool to run (it still goes through dispatch's guard)."""

    text: str
    tool: str | None = None
    args: dict[str, SlotScalar] = field(default_factory=dict)


class Thinker(Protocol):
    async def think(self, turn: Turn) -> ThinkerResult: ...


# Requests to keep, recall or forget a memory. While memory can't store on this
# machine, NoThinker says so instead of "lo apunté" (nothing would be kept).
MEMORY_TOOLS = frozenset({"remember_fact", "recall_facts", "forget_fact", "forget_last_turn"})


# How a memory request opens, both languages, neutral. With memory_tool unavailable
# retrieval never proposes it, so the words themselves say what was asked.
_MEMORY_OPENERS = tuple(" ".join(fold(p).split()) for p in (
    "recuerda que", "recuerdame que", "recuerdeme que", "acuerdate de que", "acuerdate que",
    "no olvides que", "guarda que", "apunta que", "anota que", "ten en cuenta que",
    "que sabes de mi", "que recuerdas de mi", "olvida que", "olvidate de que",
    "remember that", "don't forget that", "dont forget that", "keep in mind that", "note that",
    "what do you know about me", "what do you remember about me", "forget that",
))


def asks_memory(d: Decision) -> bool:
    """The brain chose a memory tool, or the sentence opens like a memory request."""
    if d.chosen in MEMORY_TOOLS:
        return True
    said = " ".join(normalize(d.utterance).tokens)
    return said.startswith(_MEMORY_OPENERS)


class NoThinker:
    """The only thinker in VOICE-1: no model, a template that doesn't sound like an error."""

    def __init__(self, responder: Responder | None = None) -> None:
        self.responder = responder or Responder()

    async def think(self, turn: Turn) -> ThinkerResult:
        lang = language(turn.transcript)
        if asks_memory(turn.decision):
            return ThinkerResult(text=self.responder.no_memory(lang))
        return ThinkerResult(text=self.responder.not_yet(lang))


def rank_facts(query: str, facts: Sequence[str], k: int = FACTS_K) -> tuple[str, ...]:
    """The ``k`` facts most relevant to ``query`` by BM25; none that share no term.

    Deterministic, local, free: the A4 replacement for "top-15 by confidence".
    Ties keep the caller's order (which is confidence-first from the store).
    """
    if not facts or k <= 0:
        return ()
    bm = BM25([doc_tokens(f) for f in facts])
    scores = bm.scores(query_tokens(query))
    ranked = sorted(range(len(facts)), key=lambda i: (-scores[i], i))
    return tuple(facts[i] for i in ranked[:k] if scores[i] > 0)


def one_line(text: str, limit: int = 80) -> str:
    """A result squeezed to one short line for the A5 context."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
