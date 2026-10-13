"""The record of one brain decision. Every field is logged; nothing is a side effect.

A ``Decision`` carries everything the gate looked at — candidates with their
per-source scores, every slot with where it came from, every feature with the
threshold it was compared to — so thresholds get tuned later from logged data
instead of guesses (BRAIN-1, rule 2).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Verdict = Literal["local", "defer", "clarify", "refuse", "none"]
SlotScalar = str | int | float | bool


@dataclass(frozen=True)
class SlotValue:
    """One resolved argument. ``source`` says how: ``fuzzy:apps``, ``regex:number``, ``residue``."""

    value: SlotScalar
    source: str
    confidence: float
    raw_span: str


@dataclass(frozen=True)
class Feature:
    """One number (or flag) the gate looked at, with what it was compared to."""

    name: str
    value: float | str | bool
    threshold: float | None = None
    passed: bool | None = None


@dataclass(frozen=True)
class Candidate:
    """A tool from the dual retriever. Both sources always populated: absent = 0, rank 0."""

    tool: str
    bm25_score: float = 0.0
    bm25_rank: int = 0
    knn_score: float = 0.0
    knn_rank: int = 0
    knn_nearest_id: str = ""


@dataclass
class Decision:
    utterance: str
    normalized: str
    verdict: Verdict
    reason: str
    candidates: tuple[Candidate, ...] = ()
    chosen: str | None = None
    slots: dict[str, SlotValue] = field(default_factory=dict)
    features: tuple[Feature, ...] = ()
    needs_confirmation: bool = False
    timings: dict[str, float] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        """A JSON-ready dict (what the log writes)."""
        rec = asdict(self)
        rec["candidates"] = [asdict(c) for c in self.candidates]
        rec["features"] = [asdict(f) for f in self.features]
        rec["slots"] = {k: asdict(v) for k, v in self.slots.items()}
        return rec
