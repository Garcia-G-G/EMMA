"""The kNN example index: utterances + their embeddings in ``brain.db``.

Raw sqlite3 + the ``sqlite-vec`` extension (already a dependency, as in
``memory/long_term.py``), cosine distance. One row per example:
``(id, text, normalized, brain_path, accept_tools, lang_hint, source)`` + its
vector. ``source`` is ``phrasebook`` (shipped phrasings, BRAIN-1.5),
``labelled`` (the 140) or ``learned`` (BRAIN-2's loop) — so the learning loop
can weigh them differently.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import sqlite_vec

from core.brain.embed import DIM

DEFAULT_PATH = Path.home() / ".emma" / "brain.db"
SOURCES = ("phrasebook", "labelled", "learned")
_COLUMNS = "id, text, normalized, brain_path, accept_tools, lang_hint, source"


@dataclass(frozen=True)
class Example:
    id: str
    text: str
    normalized: str
    brain_path: str
    accept_tools: tuple[str, ...]
    lang_hint: str
    source: str = "labelled"


@dataclass(frozen=True)
class Neighbor:
    example: Example
    cosine: float


class ExampleIndex:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        if self.path != ":memory:":
            # Personal tier once BRAIN-2 appends learned utterances: born 0600.
            Path(self.path).chmod(0o600)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(examples)")}
        if cols and "source" not in cols:
            # A BRAIN-1 index (no `source`). It holds no learned rows — those
            # arrive with BRAIN-2 — so it is a rebuildable cache: start over.
            self.conn.executescript("DROP TABLE examples; DROP TABLE IF EXISTS example_vec;")
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS examples (
                rowid INTEGER PRIMARY KEY,
                id TEXT UNIQUE NOT NULL,
                text TEXT NOT NULL,
                normalized TEXT NOT NULL,
                brain_path TEXT NOT NULL,
                accept_tools TEXT NOT NULL,
                lang_hint TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'labelled'
            );
            CREATE INDEX IF NOT EXISTS examples_source ON examples(source);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS example_vec
                USING vec0(embedding float[{DIM}] distance_metric=cosine);
            """
        )

    def __len__(self) -> int:
        row = self.conn.execute("SELECT count(*) FROM examples").fetchone()
        return int(row[0])

    def count(self, source: str) -> int:
        row = self.conn.execute("SELECT count(*) FROM examples WHERE source = ?", (source,)).fetchone()
        return int(row[0])

    def replace_all(
        self, examples: Iterable[Example], vectors: npt.NDArray[np.float32]
    ) -> None:
        rows = list(examples)
        if len(rows) != len(vectors):
            raise ValueError(f"{len(rows)} examples vs {len(vectors)} vectors")
        bad = {e.source for e in rows} - set(SOURCES)
        if bad:
            raise ValueError(f"unknown source(s): {sorted(bad)}")
        with self.conn:
            self.conn.execute("DELETE FROM examples")
            self.conn.execute("DELETE FROM example_vec")
            for i, (ex, vec) in enumerate(zip(rows, vectors, strict=True), start=1):
                self.conn.execute(
                    f"INSERT INTO examples (rowid, {_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
                    (i, ex.id, ex.text, ex.normalized, ex.brain_path,
                     json.dumps(list(ex.accept_tools)), ex.lang_hint, ex.source),
                )
                self.conn.execute(
                    "INSERT INTO example_vec(rowid, embedding) VALUES (?, ?)",
                    (i, sqlite_vec.serialize_float32(vec.astype(np.float32).tolist())),
                )

    def search(
        self,
        vector: npt.NDArray[np.float32],
        k: int = 10,
        exclude_id: str | None = None,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> list[Neighbor]:
        """Top-``k`` neighbors by cosine, skipping ``exclude_id`` / ``exclude_ids``.

        Exclusion is how evaluation stays honest: the item itself (leave-one-out)
        and any shipped phrasing that near-copies it are not allowed to answer.
        """
        skip = set(exclude_ids) | ({exclude_id} if exclude_id else set())
        want = k + len(skip)
        hits = self.conn.execute(
            "SELECT rowid, distance FROM example_vec WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (sqlite_vec.serialize_float32(vector.astype(np.float32).tolist()), want),
        ).fetchall()
        if not hits:
            return []
        dist = {int(r): float(d) for r, d in hits}
        marks = ",".join("?" * len(dist))
        rows = self.conn.execute(
            f"SELECT rowid, {_COLUMNS} FROM examples WHERE rowid IN ({marks})",
            tuple(dist),
        ).fetchall()
        out: list[Neighbor] = []
        for r in rows:
            if r[1] in skip:
                continue
            ex = Example(r[1], r[2], r[3], r[4], tuple(json.loads(r[5])), r[6], r[7])
            out.append(Neighbor(ex, 1.0 - dist[int(r[0])]))
        out.sort(key=lambda nb: -nb.cosine)
        return out[:k]

    def vectors(self, source: str) -> tuple[list[str], npt.NDArray[np.float32]]:
        """(ids, matrix) of every row from ``source`` — for evaluation holdouts."""
        rows = self.conn.execute(
            "SELECT e.id, v.embedding FROM examples e JOIN example_vec v ON v.rowid = e.rowid "
            "WHERE e.source = ? ORDER BY e.rowid",
            (source,),
        ).fetchall()
        ids = [str(r[0]) for r in rows]
        if not rows:
            return ids, np.zeros((0, DIM), dtype=np.float32)
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return ids, mat

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def close(self) -> None:
        self.conn.close()
