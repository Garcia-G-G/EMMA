"""The dual retriever: BM25 over tool descriptions + kNN over labelled utterances.

Two independent views of the same utterance. BM25 is the scorer and tokenizer
``core.tool_selection`` already ships (imported, not copied), ranked over EVERY
tool — the session retriever leaves the core out of its pool; the brain can't.
kNN is the local embedder against the example index.

Each ``Candidate`` carries both sources. A tool one source didn't surface gets
score 0 / rank 0 from it — never a missing field (BRAIN-1 Part 3). kNN may also
surface the pseudo-tool ``__none__`` (the nearest examples wanted no tool).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.brain import phrasebook
from core.brain.decision import Candidate
from core.brain.embed import Embedder
from core.brain.index import ExampleIndex, Neighbor
from core.brain.normalize import Normalized
from core.tool_selection import BM25, doc_tokens, query_tokens

NONE_TOOL = "__none__"
# A labelled example the brain is NOT meant to serve (brain_path LLM / READ / L3)
# votes for this instead of its tools: "people who say this need the LLM".
LLM_TOOL = "__llm__"
PSEUDO_TOOLS = frozenset({NONE_TOOL, LLM_TOOL})


def embed_text(norm: Normalized) -> str:
    """What the embedder sees: the words only. Punctuation and spacing (which
    normalization rules may change) must not move a neighbor."""
    return " ".join(norm.words)


@dataclass(frozen=True)
class ToolDoc:
    """What the brain knows about a tool — plain data, handed in by the caller."""

    name: str
    description: str
    required: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    destructive: bool = False
    reversible: bool = False  # VOICE-1: only these run without a spoken yes


@dataclass(frozen=True)
class Retrieval:
    candidates: tuple[Candidate, ...]
    neighbors: tuple[Neighbor, ...]
    bm25_ms: float
    knn_ms: float


class DualRetriever:
    def __init__(
        self,
        tools: Sequence[ToolDoc],
        index: ExampleIndex | None = None,
        embedder: Embedder | None = None,
        texts: Mapping[str, str] | None = None,
    ) -> None:
        """``texts``: the BM25 document per tool. Default: the phrasebook (+ the
        tool name split on underscores). Never the LLM descriptions (BRAIN-1.5):
        those are written for a model to read, and Realtime's own index of them
        (core.tool_selection) must stay untouched."""
        self.tools = {t.name: t for t in tools}
        self._names = list(self.tools)
        if texts is None:
            book = phrasebook.load()
            texts = {t.name: phrasebook.bm25_text(t.name, book) for t in tools}
        self._bm = BM25([doc_tokens(texts.get(t.name, t.name.replace("_", " "))) for t in tools])
        self.index = index
        self.embedder = embedder

    @property
    def knn_status(self) -> str:
        """``ok`` | ``embedder_missing`` | ``index_empty`` — why kNN can't vote, if it can't."""
        if self.embedder is None or not self.embedder.available():
            return "embedder_missing"
        if self.index is None or len(self.index) == 0:
            return "index_empty"
        return "ok"

    @property
    def has_knn(self) -> bool:
        return (
            self.index is not None
            and self.embedder is not None
            and self.embedder.available()
            and len(self.index) > 0
        )

    def rank(
        self,
        norm: Normalized,
        k: int = 10,
        exclude_id: str | None = None,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> Retrieval:
        t0 = time.perf_counter()
        bm = self._bm.scores(query_tokens(norm.text)) if norm.tokens else [0.0] * len(self._names)
        bm_order = sorted(range(len(self._names)), key=lambda i: -bm[i])
        bm_rank = {self._names[i]: r for r, i in enumerate(bm_order, start=1) if bm[i] > 0}
        bm_score = {self._names[i]: bm[i] for i in range(len(self._names)) if bm[i] > 0}
        t1 = time.perf_counter()

        neighbors: list[Neighbor] = []
        knn_score: dict[str, float] = {}
        knn_near: dict[str, str] = {}
        if self.has_knn and norm.tokens:
            assert self.index is not None and self.embedder is not None
            vec = self.embedder.embed_one(embed_text(norm))
            neighbors = self.index.search(vec, k=k, exclude_id=exclude_id, exclude_ids=exclude_ids)
            for nb in neighbors:  # nearest first, so the first sighting is the max
                for tool in nb.example.accept_tools:
                    # Only tools the brain was handed (the AVAILABLE ones in the
                    # daemon) may get a vote; the index holds rows for every tool.
                    if tool not in self.tools and tool not in PSEUDO_TOOLS:
                        continue
                    if tool not in knn_score:
                        knn_score[tool] = nb.cosine
                        knn_near[tool] = nb.example.id
        knn_order = sorted(knn_score, key=lambda n: -knn_score[n])
        knn_rank = {n: r for r, n in enumerate(knn_order, start=1)}
        t2 = time.perf_counter()

        top_bm = [self._names[i] for i in bm_order[:k] if bm[i] > 0]
        names = list(dict.fromkeys([*knn_order, *top_bm]))
        cands = [
            Candidate(
                tool=n,
                bm25_score=round(bm_score.get(n, 0.0), 4),
                bm25_rank=bm_rank.get(n, 0),
                knn_score=round(knn_score.get(n, 0.0), 4),
                knn_rank=knn_rank.get(n, 0),
                knn_nearest_id=knn_near.get(n, ""),
            )
            for n in names
        ]
        # Order: best by either source first (min of the two ranks, 0 = absent).
        cands.sort(key=lambda c: min(r for r in (c.bm25_rank, c.knn_rank) if r) or 10**6)
        return Retrieval(tuple(cands), tuple(neighbors), (t1 - t0) * 1000, (t2 - t1) * 1000)

    def bm25_top(self, r: Retrieval) -> str | None:
        best = [c for c in r.candidates if c.bm25_rank == 1]
        return best[0].tool if best else None

    def knn_top(self, r: Retrieval) -> str | None:
        best = [c for c in r.candidates if c.knn_rank == 1]
        return best[0].tool if best else None
