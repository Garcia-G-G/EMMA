"""OpenAI embedding helper for the memory store. Single-call async.

Used by :mod:`memory.long_term` for semantic recall and dedup. The
vectors are 1536-dim ``text-embedding-3-small`` embeddings, stored in
the ``facts_vec`` sqlite-vec virtual table.
"""

from __future__ import annotations

import structlog
from openai import AsyncOpenAI

from config.settings import settings

log = structlog.get_logger("emma.memory.embeddings")

_client: AsyncOpenAI | None = None
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536


def _client_singleton() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key(), base_url=settings.openai_base_url())
    return _client


async def embed(text: str) -> list[float]:
    """Return the 1536-dim embedding for `text`. Raises on API failure."""
    if not text.strip():
        raise ValueError("empty text")
    resp = await _client_singleton().embeddings.create(
        model=EMBED_MODEL,
        input=text,
    )
    vec = resp.data[0].embedding
    if len(vec) != EMBED_DIMS:
        raise RuntimeError(f"unexpected embedding dim: {len(vec)}")
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(a) != len(b):
        raise ValueError("dim mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
