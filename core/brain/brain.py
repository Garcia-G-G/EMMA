"""``Brain.decide(transcript) -> Decision``: normalize, retrieve, gate. No LLM.

Holds the plain-data world the gate needs (tool metadata, the closed
vocabularies, thresholds) plus the retriever. Built by the host
(``core.brain_host``); executes nothing itself: ``processor.py`` acts on the decision.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from core.brain.decision import Decision
from core.brain.embed import Embedder
from core.brain.gate import Thresholds, decide
from core.brain.index import ExampleIndex
from core.brain.normalize import normalize
from core.brain.retrieve import DualRetriever, ToolDoc
from core.brain.slots import Vocab, split_clitics


class Brain:
    def __init__(
        self,
        tools: Sequence[ToolDoc],
        vocab: Vocab,
        thresholds: Thresholds,
        index: ExampleIndex | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.vocab = vocab
        self.thresholds = thresholds
        self.retriever = DualRetriever(tools, index, embedder)

    def decide(
        self,
        utterance: str,
        exclude_id: str | None = None,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> Decision:
        """``exclude_id(s)``: evaluation only — the item itself and any phrasing
        that near-copies it may not answer (leave-one-out)."""
        t0 = time.perf_counter()
        split, anaphor = split_clitics(utterance)
        norm = normalize(split)
        t1 = time.perf_counter()
        r = self.retriever.rank(norm, exclude_id=exclude_id, exclude_ids=exclude_ids)
        t2 = time.perf_counter()
        d = decide(norm, r, self.tools, self.vocab, self.thresholds, self.retriever.knn_status)
        d.utterance, d.anaphor = utterance, anaphor
        t3 = time.perf_counter()
        d.timings = {
            "normalize_ms": round((t1 - t0) * 1000, 3),
            "bm25_ms": round(r.bm25_ms, 3),
            "knn_ms": round(r.knn_ms, 3),
            "gate_ms": round((t3 - t2) * 1000, 3),
            "total_ms": round((t3 - t0) * 1000, 3),
        }
        return d
