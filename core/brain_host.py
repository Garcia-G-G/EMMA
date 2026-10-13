"""The brain's host side: everything that turns Emma's live state into plain data.

``core/brain`` may not import ``tools`` (the registry pulls in every tool and,
with them, the platform). This module may — it is the adapter the import rule
forces. It builds ``ToolDoc``s from the registry and the example index from
the labelled set.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.brain.embed import Embedder
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


def build_index(
    embedder: Embedder, path: Path | str, examples: list[Example] | None = None
) -> ExampleIndex:
    rows = examples if examples is not None else labelled_examples()
    index = ExampleIndex(path)
    index.replace_all(rows, embedder.embed([embed_text(normalize(e.text)) for e in rows]))
    return index


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
