"""The vector store: three indexes in one SQLite file, and a freeze that bites.

**Why SQLite and not a server.** The runs this harness produces already live
in SQLite. Putting the indexes in the same file means a run and the exact
index state that produced it cannot drift apart, and there is no service to
be up or down at 3am.

**Why the freeze exists.** The example index is designed to grow: a prediction
that passes all three gates is a verified ``question -> SQL`` pair and is
worth retrieving for later questions. That flywheel is also the most direct
route to a meaningless number. If verified examples are added *during* an
evaluation pass, then later items are answered by a system that earlier items
did not have, the run is not one measurement, and an item whose own answer
entered the index is scored against itself.

So freezing is enforced here, at the only place that can write, rather than
by remembering to do it at the call site. A frozen index raises on write. The
freeze is recorded in the file with the reason and the timestamp, so a report
can state that the index was frozen rather than assert it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite_vec

__all__ = ["FrozenIndexError", "Hit", "IndexKind", "VectorStore"]

IndexKind = str
"""One of ``schema``, ``example`` or ``value``. A str rather than an Enum so a
caller can add a fourth index without editing this module."""


class FrozenIndexError(RuntimeError):
    """Raised on any attempt to write to a frozen index."""


@dataclass(frozen=True)
class Hit:
    """One retrieved document, with the distance that retrieved it."""

    doc_id: str
    text: str
    payload: dict[str, Any]
    distance: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    rowid    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    doc_id   TEXT NOT NULL,
    text     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (kind, doc_id)
);
CREATE INDEX IF NOT EXISTS docs_kind ON docs(kind);

CREATE TABLE IF NOT EXISTS index_state (
    kind        TEXT PRIMARY KEY,
    frozen_at   TEXT,
    reason      TEXT,
    n_docs      INTEGER NOT NULL DEFAULT 0,
    embed_model TEXT,
    dim         INTEGER
);
"""


class VectorStore:
    """Three vector indexes over one SQLite file."""

    def __init__(self, path: Path | str, *, dim: int, embed_model: str) -> None:
        self.path = Path(path)
        self.dim = dim
        self.embed_model = embed_model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self.db.close()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _vec_table(self, kind: IndexKind) -> str:
        """Create this kind's vector table on demand and return its name.

        The width comes from the embedder, which discovers it rather than
        assuming it -- a table built at one width and vectors written at
        another fails inside sqlite-vec with an error that names neither.
        """
        table = f"vec_{kind}"
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{self.dim}])"
        )
        return table

    def is_frozen(self, kind: IndexKind) -> tuple[bool, str]:
        """``(frozen, reason)`` for one index."""
        row = self.db.execute(
            "SELECT frozen_at, reason FROM index_state WHERE kind = ?", (kind,)
        ).fetchone()
        if row is None or row[0] is None:
            return False, "not frozen"
        return True, f"frozen at {row[0]}: {row[1]}"

    def freeze(self, kind: IndexKind, reason: str) -> None:
        """Freeze one index against further writes, recording why.

        Idempotent: freezing an already-frozen index keeps the original
        timestamp and reason, so the record says when it *first* closed.
        """
        frozen, _ = self.is_frozen(kind)
        if frozen:
            return
        self.db.execute(
            "INSERT INTO index_state (kind, frozen_at, reason, n_docs, embed_model, dim) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET frozen_at = excluded.frozen_at, reason = excluded.reason",
            (kind, datetime.now(UTC).isoformat(), reason, self.count(kind), self.embed_model, self.dim),
        )
        self.db.commit()

    def count(self, kind: IndexKind) -> int:
        """How many documents this index holds."""
        row = self.db.execute("SELECT COUNT(*) FROM docs WHERE kind = ?", (kind,)).fetchone()
        return int(row[0]) if row else 0

    def add(
        self,
        kind: IndexKind,
        doc_ids: Sequence[str],
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """Add documents to one index. Raises if that index is frozen.

        The freeze check is first, before any validation, so that a frozen
        index reports *frozen* rather than reporting a length mismatch in a
        write that was never going to be allowed.
        """
        frozen, reason = self.is_frozen(kind)
        if frozen:
            raise FrozenIndexError(f"index {kind!r} is closed to writes: {reason}")
        if not (len(doc_ids) == len(texts) == len(vectors)):
            raise ValueError(
                f"length mismatch: {len(doc_ids)} ids, {len(texts)} texts, {len(vectors)} vectors"
            )
        pays = list(payloads or [{} for _ in doc_ids])
        table = self._vec_table(kind)
        written = 0
        for doc_id, text, vector, payload in zip(doc_ids, texts, vectors, pays, strict=True):
            if len(vector) != self.dim:
                raise ValueError(f"vector for {doc_id!r} has width {len(vector)}, store expects {self.dim}")
            cur = self.db.execute(
                "INSERT INTO docs (kind, doc_id, text, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, doc_id) DO UPDATE SET text = excluded.text, payload = excluded.payload "
                "RETURNING rowid",
                (kind, doc_id, text, json.dumps(payload)),
            )
            rowid = int(cur.fetchone()[0])
            self.db.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
            self.db.execute(
                f"INSERT INTO {table} (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(list(vector))),
            )
            written += 1
        self.db.execute(
            "INSERT INTO index_state (kind, n_docs, embed_model, dim) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET n_docs = excluded.n_docs",
            (kind, self.count(kind), self.embed_model, self.dim),
        )
        self.db.commit()
        return written

    def search(self, kind: IndexKind, vector: Sequence[float], k: int = 5) -> list[Hit]:
        """The ``k`` nearest documents in one index.

        An index that holds nothing returns an empty list rather than raising.
        "Retrieved nothing" is a legitimate state that the no-retrieval arm
        depends on, and it must be representable without an error.
        """
        if self.count(kind) == 0:
            return []
        table = self._vec_table(kind)
        rows = self.db.execute(
            f"SELECT v.rowid, v.distance, d.doc_id, d.text, d.payload "
            f"FROM {table} v JOIN docs d ON d.rowid = v.rowid "
            f"WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (sqlite_vec.serialize_float32(list(vector)), k),
        ).fetchall()
        return [
            Hit(doc_id=r[2], text=r[3], payload=json.loads(r[4]), distance=float(r[1]))
            for r in rows
        ]
