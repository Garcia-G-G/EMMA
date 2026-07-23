"""Enrolled voice profiles (Prompt 35.1).

A tiny table in the existing ``~/.emma/memory.db`` holding one voice embedding per
enrolled person (the user in V1; the schema is generic for future "esta es la voz de
mi hermana"). Personal-tier data — embeddings live in memory.db, never the Keychain,
and we NEVER persist raw audio (only the ~1 KB float32 embedding).

Identification is a cosine-similarity match against the enrolled embeddings. The
embedding is computed by ``embed_audio`` via the optional ``resemblyzer`` dependency;
if it isn't installed, ``embed_audio`` raises :class:`SpeakerIDUnavailable` and the
caller degrades to "always the user".
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import structlog

from config.settings import settings

log = structlog.get_logger("emma.voice_profiles")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS voice_profiles ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "name TEXT NOT NULL UNIQUE,"
    "embedding BLOB NOT NULL,"
    "enrolled_at REAL NOT NULL,"
    "confidence REAL DEFAULT 1.0,"
    "last_seen REAL)"
)


class SpeakerIDUnavailable(RuntimeError):  # noqa: N818 — deliberate API name (caught by callers)
    """Raised when resemblyzer isn't installed — caller must degrade gracefully."""


@dataclass
class ProfileRecord:
    name: str
    enrolled_at: float
    confidence: float
    last_seen: float | None


def _connect() -> sqlite3.Connection:
    # Same DB file as long-term memory; own thin connection so we don't depend on
    # long_term's schema side effects.
    path = Path(settings.MEMORY_DB_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    return conn


def init() -> None:
    _connect().close()


# ---- embedding helper -------------------------------------------------------

_encoder = None


def embed_audio(samples: np.ndarray, sample_rate: int) -> bytes:
    """Return the 256-dim speaker embedding for `samples` as float32 bytes (~1 KB).

    Raises :class:`SpeakerIDUnavailable` if resemblyzer isn't installed.
    """
    global _encoder
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
    except ImportError as exc:
        raise SpeakerIDUnavailable("resemblyzer no está instalado") from exc
    if _encoder is None:
        _encoder = VoiceEncoder(verbose=False)
    wav = preprocess_wav(samples.astype(np.float32), source_sr=sample_rate)
    emb = _encoder.embed_utterance(wav).astype(np.float32)
    return bytes(emb.tobytes())


def _vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ---- store API (all writes off-thread) --------------------------------------


def _enroll_sync(name: str, embedding: bytes) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO voice_profiles(name,embedding,enrolled_at,confidence) VALUES(?,?,?,1.0) "
            "ON CONFLICT(name) DO UPDATE SET embedding=excluded.embedding, "
            "enrolled_at=excluded.enrolled_at, confidence=confidence+0.5",
            (name.strip().lower(), embedding, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


async def enroll(name: str, embedding: bytes) -> None:
    await asyncio.to_thread(_enroll_sync, name, embedding)


def _identify_sync(embedding: bytes, threshold: float) -> str | None:
    q = _vec(embedding)
    conn = _connect()
    try:
        best_name, best_score = None, -1.0
        for row in conn.execute("SELECT name, embedding FROM voice_profiles"):
            score = _cosine(q, _vec(row["embedding"]))
            if score > best_score:
                best_name, best_score = row["name"], score
        return best_name if best_score >= threshold else None
    finally:
        conn.close()


async def identify(embedding: bytes, threshold: float = 0.62) -> str | None:
    return await asyncio.to_thread(_identify_sync, embedding, threshold)


def _list_sync() -> list[ProfileRecord]:
    conn = _connect()
    try:
        return [
            ProfileRecord(r["name"], r["enrolled_at"], r["confidence"], r["last_seen"])
            for r in conn.execute("SELECT name,enrolled_at,confidence,last_seen FROM voice_profiles ORDER BY name")
        ]
    finally:
        conn.close()


async def list_profiles() -> list[ProfileRecord]:
    return await asyncio.to_thread(_list_sync)


def _delete_sync(name: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM voice_profiles WHERE name=?", (name.strip().lower(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


async def delete_profile(name: str) -> bool:
    return bool(await asyncio.to_thread(_delete_sync, name))


def _mark_seen_sync(name: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE voice_profiles SET last_seen=? WHERE name=?", (time.time(), name.strip().lower()))
        conn.commit()
    finally:
        conn.close()


async def mark_seen(name: str) -> None:
    await asyncio.to_thread(_mark_seen_sync, name)


def count_sync() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0])
    finally:
        conn.close()
