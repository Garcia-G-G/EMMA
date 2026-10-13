"""BRAIN-1.5 Part 3 — reproduce the gate study table on the 140.

    .venv/bin/python scripts/brain/gate_study.py

Variants A-D, each with the argument-completeness check on and off. Evaluation
is honest the same way as tests/test_brain_eval.py: each item is excluded from
its own neighbors, and any phrasebook row within cosine 0.97 of it is held out.
"""

from __future__ import annotations

import dataclasses
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.brain.brain import Brain
from core.brain.embed import Embedder
from core.brain.slots import Vocab
from core.brain_host import (
    build_index,
    evaluate,
    holdouts,
    is_wrong,
    labelled_examples,
    thresholds_from_settings,
    tool_docs,
    vocab,
)
from tests.brain_slot_fixtures import (
    APPS,
    CONNECTIONS,
    EXPECTED,
    NOTES,
    PAGES,
    PLAYLISTS,
)

THETA2 = 0.94
VARIANTS = {
    "A. agreement required (BRAIN-1)": dict(relaxed_agreement=False, knn_single_min=None),
    "B. relaxed agreement (top-3)": dict(relaxed_agreement=True, knn_single_min=None),
    f"C. single source (θ1 off, θ2={THETA2})": dict(relaxed_agreement=False, knn_single_min=THETA2),
    "D. B + C (shipped)": dict(relaxed_agreement=True, knn_single_min=THETA2),
}


def main() -> None:
    emb = Embedder()
    ex = labelled_examples()
    idx = build_index(emb, ":memory:")
    held = holdouts(idx, emb, ex)
    base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
    v = Vocab(apps=base.apps, aliases=base.aliases, pages=PAGES, notes=base.notes,
              playlists=base.playlists, connections=base.connections, contacts=base.contacts)
    docs = tool_docs()
    print("| Variant | Args check | Served | Local | Wrong | Defer reasons |")
    print("|---|---|---:|---:|---:|---|")
    for name, kw in VARIANTS.items():
        for on in (True, False):
            th = dataclasses.replace(thresholds_from_settings(), require_slots=on, bm25_single_min=None, **kw)
            res = evaluate(Brain(docs, v, th, idx, emb), ex, held)
            served = sum(d.verdict != "defer" for _, d in res)
            local = sum(d.verdict == "local" for _, d in res)
            wrong = [e.id for e, d in res if is_wrong(e, d, EXPECTED.get(e.id))]
            reasons = Counter(d.reason for _, d in res if d.verdict == "defer")
            print(f"| {name} | {'on' if on else 'off'} | {served} ({100 * served / 140:.1f} %) | "
                  f"{local} ({100 * local / 140:.1f} %) | {len(wrong)}{' ' + ', '.join(wrong) if wrong else ''} | "
                  f"{', '.join(f'{k} {c}' for k, c in reasons.most_common())} |")


if __name__ == "__main__":
    main()
