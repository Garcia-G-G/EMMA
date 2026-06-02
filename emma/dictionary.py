"""CLI: seed memory + vocabulary from config/dictionary.toml.

Run via ``python -m emma.dictionary seed`` from the installer (step 7.7).
Idempotent: facts dedup semantically inside ``memory.remember`` (Phase 14b),
and glossary terms already present in the vocabulary library are skipped.
"""

from __future__ import annotations

import argparse
import asyncio

from core import dictionary, vocabulary
from memory.long_term import initialize, remember


async def seed() -> dict[str, int]:
    initialize()  # sync in this codebase — do NOT await
    vocabulary.reload()  # refresh so the idempotency check sees current entries
    seeded_facts = 0
    seeded_vocab = 0

    # 1. Facts → memory.long_term (semantic dedup makes re-seeding a no-op).
    for fact in dictionary.facts():
        await remember(fact.text, kind=fact.kind, confidence=fact.confidence, source="dictionary")
        seeded_facts += 1

    # 2. Terms → vocabulary library (so STT/TTS handle the acronyms). Skip any
    #    term already present so re-running doesn't duplicate sections.
    existing = set(vocabulary._entries)
    for term in dictionary.terms().values():
        if term.key in existing:
            continue
        aliases = [term.expansion.lower(), term.expansion]
        vocabulary.append_entry(canonical=term.key, aliases=aliases, description=term.context[:160])
        seeded_vocab += 1

    # 3. Contacts → memory as durable facts (public fields only).
    for c in dictionary.contacts().values():
        if not c.email:
            continue
        await remember(
            f"{c.relation.capitalize() or 'Contacto'} {c.name} — correo {c.email}",
            kind="contact",
            confidence=0.95,
            source="dictionary",
        )
        seeded_facts += 1

    return {"facts": seeded_facts, "vocab": seeded_vocab}


def _cli() -> int:
    p = argparse.ArgumentParser(prog="emma.dictionary")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="seed memory + vocabulary from dictionary.toml")
    sub.add_parser("reload", help="reload the dictionary cache only")
    args = p.parse_args()
    if args.cmd == "seed":
        res = asyncio.run(seed())
        print(f"Seeded: facts={res['facts']}, vocab={res['vocab']}")
        return 0
    if args.cmd == "reload":
        n = dictionary.reload()
        print(f"Reloaded {n} entries")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
