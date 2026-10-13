"""The brain's host side: everything that turns Emma's live state into plain data.

``core/brain`` may not import ``tools`` (the registry pulls in every tool and,
with them, the platform). This module may — it is the adapter the import rule
forces. It builds ``ToolDoc``s from the registry and the example index from
the labelled set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from core.brain.brain import Brain
from core.brain.decision import Decision
from core.brain.embed import Embedder
from core.brain.gate import Thresholds
from core.brain.index import Example, ExampleIndex
from core.brain.normalize import fold, lang_hint, normalize
from core.brain.retrieve import ToolDoc, embed_text
from core.brain.slots import Vocab

LABELLED = Path(__file__).resolve().parent.parent / "scripts" / "spike_cascade" / "data" / "labelled.json"


def tool_docs(available_only: bool = False) -> list[ToolDoc]:
    """Every registered tool (or only those that can run here) as plain data.

    Descriptions are the LLM-facing ones, routing guidance included
    (``registry._spec``) — the text BM25 was measured on in LAUNCH-12.
    """
    from tools import registry

    names = (
        [t.name for t in registry.available_tools()] if available_only else registry.list_tools()
    )
    docs: list[ToolDoc] = []
    for name in names:
        entry = registry.get_tool(name)
        if entry is None:
            continue
        fn = registry._spec(entry)["function"]
        params = fn.get("parameters", {}) or {}
        props = tuple(p for p in (params.get("properties") or {}) if p not in ("confirmed", "cancelled"))
        docs.append(
            ToolDoc(
                name=name,
                description=str(fn.get("description", "")),
                required=tuple(params.get("required") or ()),
                params=props,
                destructive=entry.destructive,
            )
        )
    return docs


def labelled_examples(path: Path = LABELLED) -> list[Example]:
    out: list[Example] = []
    for it in json.loads(path.read_text(encoding="utf-8")):
        n = normalize(it["text"])
        out.append(
            Example(
                id=it["id"],
                text=it["text"],
                normalized=n.text,
                brain_path=it.get("brain_path", ""),
                accept_tools=tuple(it["accept"]),
                lang_hint=lang_hint(n.tokens),
            )
        )
    return out


_QUOTED = re.compile("[\"\u201c'\u2018\u00ab]([^\"\u201d'\u2019\u00bb]{6,90})[\"\u201d'\u2019\u00bb]")
_NOT_A_PHRASE = re.compile(r"cmd|ctrl|\+|::|\{")
LEAK_COSINE = 0.97


def canonical_phrases(docs: list[ToolDoc]) -> list[Example]:
    """The trigger phrases Emma already ships, quoted in her tools' descriptions.

    CORE-LOCAL §1 defines L1 as "aliases + canonical phrases". Whole phrases
    only (>= 3 tokens); keyboard shortcuts and template fragments dropped.
    """
    seen: dict[tuple[str, str], str] = {}
    for d in docs:
        for m in _QUOTED.findall(d.description):
            n = normalize(m)
            if len(n.tokens) >= 3 and not _NOT_A_PHRASE.search(n.text):
                seen.setdefault((d.name, " ".join(n.words)), m)
    return [
        Example(f"seed:{name}:{i}", orig, text, "SEED", (name,), lang_hint(normalize(orig).tokens))
        for i, ((name, text), orig) in enumerate(seen.items())
    ]


def build_index(
    embedder: Embedder,
    path: Path | str,
    examples: list[Example] | None = None,
    seed_docs: list[ToolDoc] | None = None,
    holdout: list[Example] | None = None,
) -> ExampleIndex:
    """Labelled examples (+ canonical phrases from ``seed_docs``).

    ``holdout``: for EVALUATION on the labelled set — any seed phrase within
    ``LEAK_COSINE`` of a held-out item is left out, so a docstring that quotes a
    test utterance can't score it.
    """
    rows = list(examples if examples is not None else labelled_examples())
    vecs = embedder.embed([embed_text(normalize(e.text)) for e in rows])
    if seed_docs:
        seeds = canonical_phrases(seed_docs)
        svecs = embedder.embed([embed_text(normalize(e.text)) for e in seeds])
        if holdout:
            hvecs = embedder.embed([embed_text(normalize(e.text)) for e in holdout])
            keep = [i for i in range(len(seeds)) if float((hvecs @ svecs[i]).max()) < LEAK_COSINE]
            seeds, svecs = [seeds[i] for i in keep], svecs[keep]
        rows += seeds
        vecs = np.vstack([vecs, svecs])
    index = ExampleIndex(path)
    index.replace_all(rows, vecs)
    return index


def thresholds_from_settings() -> Thresholds:
    from config.settings import settings

    return Thresholds(
        knn_min=settings.BRAIN_KNN_MIN,
        margin_min=settings.BRAIN_MARGIN_MIN,
        max_content_tokens=settings.BRAIN_MAX_CONTENT_TOKENS,
        slot_floor=settings.BRAIN_SLOT_FLOOR,
    )


def vocab(
    apps: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
    playlists: tuple[str, ...] = (),
    connections: tuple[str, ...] = (),
) -> Vocab:
    """Emma's closed sets as plain data: STT aliases, saved pages, contacts.

    The installed-app list and the per-tool listings (notes, playlists,
    connections) come from the caller, which owns the platform reads.
    """
    from core import dictionary, vocabulary

    aliases = {fold(k): v for k, v in vocabulary.alias_map().items()}
    pages = {k: p.title for k, p in dictionary.pages().items()}
    contacts = {c.name: tuple(c.aliases) for c in dictionary.contacts().values() if c.name}
    return Vocab(
        apps=apps,
        aliases=aliases,
        pages=pages,
        notes=notes,
        playlists=playlists,
        connections=connections or tuple(dictionary.connections()),
        contacts=contacts,
    )


def evaluate(
    brain: Brain, examples: list[Example] | None = None
) -> list[tuple[Example, Decision]]:
    """Every labelled example through the brain, kNN leave-one-out."""
    rows = examples if examples is not None else labelled_examples()
    return [(ex, brain.decide(ex.text, exclude_id=ex.id)) for ex in rows]


def is_wrong(
    ex: Example, d: Decision, expected_slots: dict[str, dict[str, object]] | None = None
) -> bool:
    """Wrong tool, wrong argument value, or declining a request that wanted a tool.

    ``expected_slots``: tool -> {param: value} for this item (the Part 4
    fixtures). A local decision on the right tool with a different value for
    any expected parameter is wrong — executing it would do the wrong thing.
    """
    if d.verdict == "local":
        if d.chosen not in ex.accept_tools:
            return True
        want = (expected_slots or {}).get(d.chosen or "")
        if want is not None:
            got = {p: d.slots[p].value for p in want if p in d.slots}
            return got != want
        return False
    if d.verdict in ("none", "clarify", "refuse"):
        return "__none__" not in ex.accept_tools
    return False  # defer is never wrong — it is a coverage miss
