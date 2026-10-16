"""Local embeddings for the memory store (Step 42). No network, no OpenAI.

Used by :mod:`memory.long_term` for note recall, dedup and backfill. The vectors
come from memory's own pinned model (``memory.local_embed.MEMORY_MODEL``, 384-dim,
onnxruntime on the Mac), stored in the ``facts_vec`` sqlite-vec table. The model
id is recorded in the database, so a model change re-embeds instead of mixing.
"""

from __future__ import annotations

import asyncio
import threading

from core.brain.embed import Embedder
from memory.local_embed import MEMORY_MODEL, embedder

EMBED_MODEL = f"{MEMORY_MODEL.repo}@{MEMORY_MODEL.revision}"
EMBED_DIMS = MEMORY_MODEL.dim

_lock = threading.Lock()
_embedder: Embedder | None = None


class EmbedderMissingError(RuntimeError):
    """The memory model isn't installed (installer step 5.3)."""


def _get() -> Embedder:
    global _embedder
    with _lock:
        if _embedder is None:
            _embedder = embedder()
        return _embedder


def available() -> bool:
    return _get().available()


def embed_sync(text: str) -> list[float]:
    if not text.strip():
        raise ValueError("empty text")
    e = _get()
    if not e.available():
        raise EmbedderMissingError(f"memory embedder not installed: {e.model_dir}")
    return [float(x) for x in e.embed_one(text)]


async def embed(text: str) -> list[float]:
    """Return the 384-dim embedding for `text`, computed on this Mac."""
    return await asyncio.to_thread(embed_sync, text)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(a) != len(b):
        raise ValueError("dim mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
