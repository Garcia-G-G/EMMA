"""Step 42 Part 1 — memory's own pinned embedder; the brain keeps e5."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from core.brain.embed import E5, Embedder, fetch
from memory.local_embed import MEMORY_MODEL, embedder
from tests.memory_recall_set import (
    COMPATIBLE,
    CONTRADICTIONS,
    DUPLICATES,
    FACTS,
    QUERIES,
    UNANSWERABLE,
)

INSTALLER = Path(__file__).resolve().parents[1] / "_landing" / "install.sh"


def test_the_brain_keeps_e5_and_memory_has_its_own() -> None:
    assert Embedder().spec is E5
    assert embedder().spec is MEMORY_MODEL and MEMORY_MODEL.name != E5.name
    assert MEMORY_MODEL.repo.startswith("sentence-transformers/")  # the Apache-2.0 original


@pytest.mark.skipif(not INSTALLER.exists(), reason="_landing/ not checked out")
def test_the_installer_pins_what_the_code_pins() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    for spec in (E5, MEMORY_MODEL):
        assert spec.revision in text, spec.name
        for digest in spec.sha256.values():
            assert digest in text, (spec.name, digest)


def test_a_file_with_the_wrong_hash_is_deleted_never_loaded(tmp_path: Path) -> None:
    for rel in (MEMORY_MODEL.onnx_file, MEMORY_MODEL.tokenizer_file):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"not the model")
    with pytest.raises(ValueError):
        fetch(tmp_path, spec=MEMORY_MODEL)
    assert not (tmp_path / MEMORY_MODEL.onnx_file).exists()


def test_the_recall_set_meets_the_adr_minimums() -> None:
    assert len(FACTS) >= 60 and len(QUERIES) >= 150 and len(UNANSWERABLE) >= 40
    assert len(CONTRADICTIONS) >= 30 and len(DUPLICATES) >= 20 and COMPATIBLE
    assert all(i in FACTS for _, gold in QUERIES for i in gold)


def _recall_at_1(emb: Embedder) -> int:
    ids = list(FACTS)
    facts = emb.embed([FACTS[i] for i in ids], prefix=emb.spec.doc_prefix)
    qs = emb.embed([q for q, _ in QUERIES])
    best = (qs @ facts.T).argmax(axis=1)
    return sum(ids[int(b)] in gold for b, (_, gold) in zip(best, QUERIES, strict=True))


@pytest.mark.skipif(not (Embedder().available() and embedder().available()),
                    reason="both local embedders needed")
def test_part1_evidence_memory_model_beats_e5_on_the_set() -> None:
    """The measurement behind the choice (Step 42 Part 1), pinned."""
    e5, mem = _recall_at_1(Embedder()), _recall_at_1(embedder())
    assert mem >= 0.9 * len(QUERIES) and mem > e5, (mem, e5)
    v = embedder().embed(["mi perro se llama Toby", "my dog is called Toby"])
    assert v.shape == (2, MEMORY_MODEL.dim) and float(np.dot(v[0], v[1])) > 0.8
