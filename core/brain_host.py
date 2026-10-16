"""The brain's host side: everything that turns Emma's live state into plain data.

``core/brain`` may not import ``tools`` (the registry pulls in every tool and,
with them, the platform). This module may — it is the adapter the import rule
forces. It builds ``ToolDoc``s from the registry and the example index from
the labelled set.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from core.brain import phrasebook
from core.brain.brain import Brain
from core.brain.decision import Decision
from core.brain.embed import Embedder
from core.brain.gate import Thresholds
from core.brain.index import Example, ExampleIndex
from core.brain.normalize import fold, lang_hint, normalize
from core.brain.retrieve import LLM_TOOL, ToolDoc, embed_text
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
                reversible=entry.reversible,
            )
        )
    return docs


# L3 (time) is served since Step 43 Part 3: its rows vote their real tool.
_NOT_LOCAL = frozenset({"LLM", "READ"})
# The exam's clock (a Thursday, 9:00): an item that names a time resolves against
# it, so an expected value like "tomorrow at nine" is a fixed ISO string.
EXAM_NOW = datetime(2026, 10, 15, 9, 0)


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


def index_rows(examples: list[Example]) -> list[Example]:
    """Labelled rows as the INDEX holds them: an item the brain isn't meant to serve
    (LLM / READ / L3) votes ``__llm__`` — repeating it must defer, not pick the
    first of its accepted tools with a residue full of junk (review, BRAIN-1.5)."""
    return [
        dataclasses.replace(e, accept_tools=(LLM_TOOL,)) if e.brain_path in _NOT_LOCAL else e
        for e in examples
    ]


LEAK_COSINE = 0.97


def phrasebook_examples(tools: list[str] | None = None) -> list[Example]:
    """Every phrasebook phrasing, filled, as an index row (``source='phrasebook'``)."""
    book = phrasebook.load()
    out: list[Example] = []
    for tool in tools if tools is not None else list(book):
        for i, (lang, text) in enumerate(phrasebook.expansions(tool, book)):
            n = normalize(text)
            out.append(Example(f"pb:{tool}:{i}", text, n.text, "PHRASEBOOK", (tool,),
                               "en" if lang == "en" else lang_hint(n.tokens), "phrasebook"))
    return out


def build_index(
    embedder: Embedder,
    path: Path | str,
    examples: list[Example] | None = None,
    with_phrasebook: bool = True,
) -> ExampleIndex:
    """Labelled examples (+ the phrasebook). Never the LLM tool descriptions."""
    rows = index_rows(list(examples if examples is not None else labelled_examples()))
    if with_phrasebook:
        rows += phrasebook_examples()
    vecs = embedder.embed([embed_text(normalize(e.text)) for e in rows])
    index = ExampleIndex(path)
    index.replace_all(rows, vecs)
    return index


def index_fingerprint() -> str:
    """Everything the stored rows depend on: the data files, the model revision,
    and the FULL source of every module that shapes a row or its vector (fill
    tables, normalization, ids, embed text). Any change rebuilds; a boot with no
    change doesn't."""
    import hashlib

    from core.brain import embed, index, retrieve
    from core.brain import normalize as normalize_mod

    h = hashlib.sha256()
    for f in (phrasebook.PATH, LABELLED, Path(phrasebook.__file__), Path(normalize_mod.__file__),
              Path(index.__file__), Path(retrieve.__file__), Path(embed.__file__), Path(__file__)):
        h.update(f.read_bytes())
    h.update(embed.REVISION.encode())
    return h.hexdigest()


def open_index(embedder: Embedder, path: Path | str) -> ExampleIndex:
    """The persisted index, rebuilt only when its inputs changed (a boot costs ~0 s,
    a rebuild 20-150 s on an M3 under load)."""
    fp = index_fingerprint()
    index = ExampleIndex(path)
    if len(index) and index.get_meta("fingerprint") == fp:
        return index
    index.close()
    index = build_index(embedder, path)
    index.set_meta("fingerprint", fp)
    return index


def holdouts(
    index: ExampleIndex, embedder: Embedder, examples: list[Example]
) -> dict[str, frozenset[str]]:
    """Per labelled item: the phrasebook rows too close to it to count (cos >= 0.97).

    The phrasebook is written not to copy the 140 (a test checks exact matches);
    this also keeps a near-copy from answering for the item in evaluation.
    """
    ids, mat = index.vectors("phrasebook")
    if not ids:
        return {e.id: frozenset() for e in examples}
    vecs = embedder.embed([embed_text(normalize(e.text)) for e in examples])
    sims = vecs @ mat.T
    return {
        e.id: frozenset(ids[j] for j in np.nonzero(sims[i] >= LEAK_COSINE)[0])
        for i, e in enumerate(examples)
    }


def thresholds_from_settings() -> Thresholds:
    from config.settings import settings

    return Thresholds(
        knn_min=settings.BRAIN_KNN_MIN,
        margin_min=settings.BRAIN_MARGIN_MIN,
        max_content_tokens=settings.BRAIN_MAX_CONTENT_TOKENS,
        slot_floor=settings.BRAIN_SLOT_FLOOR,
        relaxed_agreement=settings.BRAIN_RELAXED_AGREEMENT,
        bm25_single_min=settings.BRAIN_BM25_SINGLE_MIN,
        bm25_single_gap=settings.BRAIN_BM25_SINGLE_GAP,
        knn_single_min=settings.BRAIN_KNN_SINGLE_MIN,
        # Master plan rule 9: argument completeness is the whole safety layer.
        # Never read from settings — no .env line, env var or flag can turn it off.
        require_slots=True,
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
    user = dictionary.tables().get("user", {})
    own_time = user.get("time", {}) if isinstance(user, dict) else {}
    time_phrases = {fold(k): int(v) for k, v in own_time.items() if isinstance(v, int)}
    return Vocab(
        apps=apps,
        aliases=aliases,
        pages=pages,
        notes=notes,
        playlists=playlists,
        connections=connections or tuple(dictionary.connections()),
        contacts=contacts,
        time_phrases=time_phrases,
    )


# Tools whose success changes a listing the vocabulary holds.
CHANGES_LISTINGS = frozenset({"create_note", "append_to_note", "merge_notes", "delete_note"})


async def listings() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(notes, playlists) from the tools' own read-only listings. A failing
    listing is empty, never an error: its requests just keep deferring."""
    from tools import music, notes_tool

    got = await asyncio.gather(notes_tool.note_titles(), music.playlist_names(),
                               return_exceptions=True)
    notes, playlists = (g if isinstance(g, tuple) else () for g in got)
    return notes, playlists


def evaluate(
    brain: Brain,
    examples: list[Example] | None = None,
    held: dict[str, frozenset[str]] | None = None,
) -> list[tuple[Example, Decision]]:
    """Every labelled example through the brain: itself excluded (leave-one-out)
    and any phrasebook near-copy of it (``held``, from ``holdouts``)."""
    rows = examples if examples is not None else labelled_examples()
    held = held or {}
    return [
        (ex, brain.decide(ex.text, exclude_id=ex.id, exclude_ids=held.get(ex.id, frozenset()),
                          now=EXAM_NOW))
        for ex in rows
    ]


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
