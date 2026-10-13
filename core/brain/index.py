"""The kNN example index: labelled utterances + their embeddings in ``brain.db``.

Raw sqlite3 + the ``sqlite-vec`` extension (already a dependency, as in
``memory/long_term.py``), cosine distance. One row per example:
``(id, text, normalized, brain_path, accept_tools, lang_hint)`` + its vector.
Today the rows are the 140 labelled items; BRAIN-2's learning loop appends.
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


@dataclass(frozen=True)
class Example:
    id: str
    text: str
    normalized: str
    brain_path: str
    accept_tools: tuple[str, ...]
    lang_hint: str


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
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS examples (
                rowid INTEGER PRIMARY KEY,
                id TEXT UNIQUE NOT NULL,
                text TEXT NOT NULL,
                normalized TEXT NOT NULL,
                brain_path TEXT NOT NULL,
                accept_tools TEXT NOT NULL,
                lang_hint TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS example_vec
                USING vec0(embedding float[{DIM}] distance_metric=cosine);
            """
        )

    def __len__(self) -> int:
        row = self.conn.execute("SELECT count(*) FROM examples").fetchone()
        return int(row[0])

    def replace_all(
        self, examples: Iterable[Example], vectors: npt.NDArray[np.float32]
    ) -> None:
        rows = list(examples)
        if len(rows) != len(vectors):
            raise ValueError(f"{len(rows)} examples vs {len(vectors)} vectors")
        with self.conn:
            self.conn.execute("DELETE FROM examples")
            self.conn.execute("DELETE FROM example_vec")
            for i, (ex, vec) in enumerate(zip(rows, vectors, strict=True), start=1):
                self.conn.execute(
                    "INSERT INTO examples VALUES (?,?,?,?,?,?,?)",
                    (i, ex.id, ex.text, ex.normalized, ex.brain_path,
                     json.dumps(list(ex.accept_tools)), ex.lang_hint),
                )
                self.conn.execute(
                    "INSERT INTO example_vec(rowid, embedding) VALUES (?, ?)",
                    (i, sqlite_vec.serialize_float32(vec.astype(np.float32).tolist())),
                )

    def search(
        self, vector: npt.NDArray[np.float32], k: int = 10, exclude_id: str | None = None
    ) -> list[Neighbor]:
        """Top-``k`` neighbors by cosine. ``exclude_id`` = leave-one-out (eval)."""
        want = k + (1 if exclude_id else 0)
        hits = self.conn.execute(
            "SELECT rowid, distance FROM example_vec WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (sqlite_vec.serialize_float32(vector.astype(np.float32).tolist()), want),
        ).fetchall()
        out: list[Neighbor] = []
        for rowid, dist in hits:
            r = self.conn.execute(
                "SELECT id, text, normalized, brain_path, accept_tools, lang_hint "
                "FROM examples WHERE rowid = ?",
                (rowid,),
            ).fetchone()
            if r is None or r[0] == exclude_id:
                continue
            ex = Example(r[0], r[1], r[2], r[3], tuple(json.loads(r[4])), r[5])
            out.append(Neighbor(ex, 1.0 - float(dist)))
        return out[:k]

    def close(self) -> None:
        self.conn.close()
