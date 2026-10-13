"""BRAIN-1 Part 3 — the dual retriever, measured on the 140 labelled items.

Pinned numbers are floors: a change that lowers one fails; raising one means
re-pinning in the same commit that earns it (BRAIN-1 rule 3). kNN is scored
leave-one-out — an item never retrieves itself.

Needs the local embedder (``core.brain.embed.fetch()`` puts it in
~/.emma/models). Without it the kNN-dependent tests SKIP, loudly, rather than
pass on a BM25-only retriever.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.brain.embed import Embedder
from core.brain.index import Example
from core.brain.normalize import normalize
from core.brain.retrieve import DualRetriever, Retrieval
from core.brain_host import build_index, holdouts, labelled_examples, tool_docs

EMBEDDER = Embedder()
needs_model = pytest.mark.skipif(
    not EMBEDDER.available(),
    reason="local embedder missing: python -c 'from core.brain.embed import fetch; fetch()'",
)

# Pinned on the 115 items whose label includes a real tool. kNN leave-one-out,
# and any phrasebook row within cosine 0.97 of the item held out (BRAIN-1.5).
# BRAIN-1 values (description BM25, labelled-only kNN) in comments.
PIN = {
    "items": 115,
    "bm25_r1": 80,  # 69.6 %  (was 60, 52.2 %)
    "bm25_r5": 102,  # 88.7 %  (was 78)
    "knn_r1": 82,  # 71.3 %  (was 46; 89 before LLM-labelled rows voted "__llm__")
    "knn_r5": 107,  # 93.0 %  (was 66)
    "agree": 65,  # 56.5 % — BM25 top-1 == kNN top-1  (was 20, 17.4 %; 70 pre-review)
    "agree_correct": 61,  # 94 % of the agreements are the right tool  (was 18)
}


@dataclass
class Scored:
    example: Example
    retrieval: Retrieval
    bm25: list[str]
    knn: list[str]


def _ok(tool: str, ex: Example) -> bool:
    return tool != "__none__" and tool in ex.accept_tools


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[DualRetriever, dict[str, frozenset[str]]]]:
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("brain") / "brain.db")
    yield DualRetriever(tool_docs(), index, EMBEDDER), holdouts(index, EMBEDDER, labelled_examples())
    index.close()


@pytest.fixture(scope="module")
def retriever(built: tuple[DualRetriever, dict[str, frozenset[str]]]) -> DualRetriever:
    return built[0]


@pytest.fixture(scope="module")
def scored(built: tuple[DualRetriever, dict[str, frozenset[str]]]) -> list[Scored]:
    retriever, held = built
    out: list[Scored] = []
    for ex in labelled_examples():
        if not any(a != "__none__" for a in ex.accept_tools):
            continue
        r = retriever.rank(normalize(ex.text), k=10, exclude_id=ex.id, exclude_ids=held[ex.id])
        bm = [c.tool for c in sorted((c for c in r.candidates if c.bm25_rank), key=lambda c: c.bm25_rank)]
        kn = [c.tool for c in sorted((c for c in r.candidates if c.knn_rank), key=lambda c: c.knn_rank)]
        out.append(Scored(ex, r, bm, kn))
    return out


@needs_model
def test_recall_and_agreement_are_pinned(scored: list[Scored]) -> None:
    got = {
        "items": len(scored),
        "bm25_r1": sum(bool(s.bm25) and _ok(s.bm25[0], s.example) for s in scored),
        "bm25_r5": sum(any(_ok(t, s.example) for t in s.bm25[:5]) for s in scored),
        "knn_r1": sum(bool(s.knn) and _ok(s.knn[0], s.example) for s in scored),
        "knn_r5": sum(any(_ok(t, s.example) for t in s.knn[:5]) for s in scored),
        "agree": sum(bool(s.bm25 and s.knn) and s.bm25[0] == s.knn[0] for s in scored),
        "agree_correct": sum(
            bool(s.bm25 and s.knn) and s.bm25[0] == s.knn[0] and _ok(s.bm25[0], s.example)
            for s in scored
        ),
    }
    assert got["items"] == PIN["items"]
    for key in ("bm25_r1", "bm25_r5", "knn_r1", "knn_r5", "agree_correct"):
        assert got[key] >= PIN[key], f"{key} fell: {got[key]} < pinned {PIN[key]} ({got})"
    # Agreement is the gate's first test: more is only good if it stays right.
    wrong_agree = got["agree"] - got["agree_correct"]
    assert wrong_agree <= PIN["agree"] - PIN["agree_correct"], got


@needs_model
def test_leave_one_out_never_returns_the_item_itself(scored: list[Scored]) -> None:
    for s in scored:
        assert all(nb.example.id != s.example.id for nb in s.retrieval.neighbors)


@needs_model
def test_both_sources_always_populated(scored: list[Scored]) -> None:
    for s in scored:
        for c in s.retrieval.candidates:
            assert c.bm25_rank >= 0 and c.knn_rank >= 0  # 0 = absent, never missing
            assert (c.knn_rank == 0) == (c.knn_score == 0.0)
            assert (c.knn_rank == 0) == (c.knn_nearest_id == "")
            assert c.bm25_rank or c.knn_rank, "a candidate neither source surfaced"


def best_p95(round_ms: Callable[[], list[float]], rounds: int = 3) -> float:
    """p95 of the best of up to ``rounds`` rounds.

    A latency test must measure the code, not whatever else the Mac is doing
    (a browser, an indexer, another test run). The best round is the capability;
    a genuinely slow path is slow in every round and still fails.
    """
    best = float("inf")
    for _ in range(rounds):
        ms = sorted(round_ms())
        best = min(best, ms[int(len(ms) * 0.95)])
        if best < 100:
            break
    return best


@needs_model
def test_rank_latency(scored: list[Scored], retriever: DualRetriever) -> None:
    ms = sorted(s.retrieval.bm25_ms + s.retrieval.knn_ms for s in scored)
    p95 = ms[int(len(ms) * 0.95)]
    if p95 >= 100:  # contention during the fixture's pass: re-measure, same index
        assert retriever.has_knn
        sample = [s.example.text for s in scored[:40]]

        def round_ms() -> list[float]:
            out = []
            for text in sample:
                r = retriever.rank(normalize(text))
                out.append(r.bm25_ms + r.knn_ms)
            return out

        p95 = best_p95(round_ms)
    assert p95 < 100, f"retrieval p95 {p95:.1f} ms"


def test_without_the_model_knn_is_absent_not_broken(tmp_path: Path) -> None:
    retriever = DualRetriever(tool_docs(), None, Embedder(tmp_path / "no-model"))
    r = retriever.rank(normalize("Hey Emma, sube el volumen a 70."))
    assert not retriever.has_knn
    assert r.candidates and all(c.knn_rank == 0 and c.knn_score == 0.0 for c in r.candidates)
    assert retriever.bm25_top(r) is not None  # BM25 alone still ranks


@needs_model
def test_embedder_is_fast_warm() -> None:
    EMBEDDER.embed_one("warm up")

    def round_ms() -> list[float]:
        ms = []
        for text in ("sube el volumen a 70", "abre espotify", "qué hora es") * 10:
            t0 = time.perf_counter()
            EMBEDDER.embed_one(text)
            ms.append((time.perf_counter() - t0) * 1000)
        return ms

    assert best_p95(round_ms) < 100


def test_index_rows_carry_their_source(tmp_path: Path) -> None:
    from core.brain.index import Example, ExampleIndex

    idx = ExampleIndex(tmp_path / "b.db")
    vec = EMBEDDER.embed(["abre safari", "abre finder"]) if EMBEDDER.available() else None
    if vec is None:
        pytest.skip("local embedder missing")
    idx.replace_all(
        [Example("a", "abre safari", "abre safari", "PHRASEBOOK", ("open_application",), "es", "phrasebook"),
         Example("b", "abre finder", "abre finder", "L2", ("open_application",), "es", "labelled")],
        vec,
    )
    assert (idx.count("phrasebook"), idx.count("labelled"), idx.count("learned")) == (1, 1, 0)
    with pytest.raises(ValueError):
        idx.replace_all([Example("c", "x", "x", "", (), "es", "bogus")], vec[:1])


def test_a_brain1_index_is_rebuilt_not_crashed_on(tmp_path: Path) -> None:
    """BRAIN-1's brain.db had no `source` column; it is a cache, so it starts over."""
    import sqlite3

    from core.brain.index import ExampleIndex

    p = tmp_path / "brain.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE examples (rowid INTEGER PRIMARY KEY, id TEXT, text TEXT, normalized TEXT,"
                " brain_path TEXT, accept_tools TEXT, lang_hint TEXT)")
    con.execute("INSERT INTO examples VALUES (1,'x','x','x','L1','[]','es')")
    con.commit()
    con.close()
    idx = ExampleIndex(p)
    assert len(idx) == 0 and idx.count("labelled") == 0


def test_the_index_is_rebuilt_only_when_its_inputs_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon boot must not re-embed 4,000+ rows (20-150 s) when nothing changed."""
    import numpy as np

    import core.brain_host as host
    from core.brain.embed import DIM
    from core.brain.index import Example, ExampleIndex

    builds: list[str] = []

    def fake_build(embedder: object, path: Path) -> ExampleIndex:
        builds.append(str(path))
        idx = ExampleIndex(path)
        idx.replace_all([Example("x", "x", "x", "L1", ("mute",), "es", "labelled")],
                        np.ones((1, DIM), dtype=np.float32))
        return idx

    monkeypatch.setattr(host, "build_index", fake_build)
    db = tmp_path / "brain.db"
    host.open_index(None, db).close()  # type: ignore[arg-type]
    host.open_index(None, db).close()  # type: ignore[arg-type]
    assert len(builds) == 1, "rebuilt with unchanged inputs"
    monkeypatch.setattr(host, "index_fingerprint", lambda: "phrasebook edited")
    host.open_index(None, db).close()  # type: ignore[arg-type]
    assert len(builds) == 2, "kept a stale index after its inputs changed"
