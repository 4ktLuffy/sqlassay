"""Building the three indexes, and assembling one prompt context from them.

What each index holds, and why it is a *separate* index rather than one pool:

* **schema** -- one document per table, listing its columns and types. The
  retrieval unit is the table, not the column, because a model given three
  columns of a five-column join writes a query it cannot complete.

* **example** -- one document per *verified* ``question -> SQL`` pair. Verified
  means it passed all three gates. An unverified pair is a guess, and
  few-shot examples that are guesses teach the model the wrong shape.

* **value** -- one document per distinct literal in a low-cardinality text
  column. This is the index that fixes "orders from Acme" when the stored
  literal is ``ACME CORPORATION INC``, which is where text-to-SQL most often
  fails for reasons that have nothing to do with the model's SQL ability.

They are separate because they are retrieved with different ``k`` and because
the ablation needs to switch them on independently. Pooled into one index,
"did value linking help" becomes unanswerable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlassay.retrieval.store import Hit, VectorStore

if TYPE_CHECKING:
    from sqlassay.engine import Engine
    from sqlassay.model.ollama import OllamaEmbedder

__all__ = ["ARMS", "RetrievedContext", "Retriever", "build_schema_index", "build_value_index"]

ARMS: dict[str, tuple[bool, bool, bool]] = {
    # arm name -> (retrieve schema, use examples, use values)
    # 'full' is the baseline: the entire schema, no retrieval at all. It is
    # the only arm that can answer "does schema retrieval help?", because
    # comparing retrieval against nothing measures retrieval against a model
    # that was not told what the tables are.
    "full": (False, False, False),
    "schema": (True, False, False),
    "schema_examples": (True, True, False),
    "schema_examples_values": (True, True, True),
}


@dataclass(frozen=True)
class RetrievedContext:
    """Everything retrieval contributed to one prompt.

    Kept as a record rather than a formatted string so that a report can say
    *which* tables were retrieved for a failed item. "Retrieval was on" does
    not explain a failure; "retrieval returned the wrong three tables" does.
    """

    arm: str
    schema_text: str
    examples: tuple[Hit, ...] = ()
    values: tuple[Hit, ...] = ()
    retrieved_tables: tuple[str, ...] = ()
    full_schema: bool = False

    def as_record(self) -> dict[str, object]:
        """The auditable form, for storing with the prediction."""
        return {
            "arm": self.arm,
            "full_schema": self.full_schema,
            "retrieved_tables": list(self.retrieved_tables),
            "example_ids": [h.doc_id for h in self.examples],
            "value_ids": [h.doc_id for h in self.values],
        }


def _table_documents(db: Engine) -> tuple[list[str], list[str]]:
    """``(table_names, table_documents)`` -- one document per table."""
    by_table: dict[str, list[str]] = {}
    for table, column, dtype in db.schema_rows():
        by_table.setdefault(table, []).append(f"{column} {dtype}")
    names = sorted(by_table)
    docs = [f"Table {t} ({', '.join(by_table[t])})" for t in names]
    return names, docs


def full_schema_text(db: Engine) -> str:
    """Every table, for the ``full`` baseline arm."""
    _, docs = _table_documents(db)
    return "\n".join(docs)


def build_schema_index(
    store: VectorStore, db: Engine, embedder: OllamaEmbedder, *, db_id: str
) -> int:
    """Embed one document per table. Returns the number written."""
    names, docs = _table_documents(db)
    if not names:
        return 0
    vectors = embedder.embed(docs)
    return store.add(
        "schema",
        [f"{db_id}.{n}" for n in names],
        docs,
        vectors,
        [{"db_id": db_id, "table": n} for n in names],
    )


def build_value_index(
    store: VectorStore,
    db: Engine,
    embedder: OllamaEmbedder,
    *,
    db_id: str,
    max_distinct: int = 200,
    max_values_per_db: int = 5000,
) -> int:
    """Embed distinct literals from low-cardinality text columns.

    ``max_distinct`` is the cardinality ceiling above which a column is
    skipped. A column with 40,000 distinct free-text values is not a category
    and embedding it would bury the genuine categories under noise -- as well
    as taking longer than the evaluation it is meant to support.
    """
    written = 0
    doc_ids: list[str] = []
    texts: list[str] = []
    payloads: list[dict[str, object]] = []
    for table, column, dtype in db.schema_rows():
        if "CHAR" not in dtype.upper() and "STRING" not in dtype.upper() and dtype.upper() != "TEXT":
            continue
        try:
            rows = db.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL LIMIT {max_distinct + 1}'
            ).rows
        except Exception:
            # A column that cannot be read is skipped, not fatal: the value
            # index is an enhancement and a partial one is still usable.
            continue
        if len(rows) > max_distinct:
            continue
        for (value,) in rows:
            text = str(value)
            if not text.strip():
                continue
            doc_ids.append(f"{db_id}.{table}.{column}.{text}"[:300])
            texts.append(f"{text} (value of {table}.{column})")
            payloads.append({"db_id": db_id, "table": table, "column": column, "value": text})
        if len(doc_ids) >= max_values_per_db:
            break

    for i in range(0, len(doc_ids), 64):
        chunk = slice(i, i + 64)
        vectors = embedder.embed(texts[chunk])
        written += store.add("value", doc_ids[chunk], texts[chunk], vectors, payloads[chunk])
    return written


@dataclass
class Retriever:
    """Assembles one prompt context, per arm."""

    store: VectorStore
    embedder: OllamaEmbedder
    k_schema: int = 6
    k_examples: int = 3
    k_values: int = 5
    _cache: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def _embed_question(self, question: str) -> list[float]:
        """Embed once per question and reuse across all three indexes.

        The same vector queries all three, so embedding per index would cost
        three times the calls for identical results -- material over an
        overnight pass, and a source of drift if the embedder were ever
        non-deterministic.
        """
        if question not in self._cache:
            self._cache[question] = self.embedder.embed([question])[0]
        return self._cache[question]

    def context(self, arm: str, question: str, db: Engine, *, db_id: str) -> RetrievedContext:
        """Build the retrieval context for one question under one arm."""
        if arm not in ARMS:
            raise KeyError(f"unknown arm {arm!r}; known: {sorted(ARMS)}")
        use_schema, use_examples, use_values = ARMS[arm]

        if not use_schema:
            return RetrievedContext(arm=arm, schema_text=full_schema_text(db), full_schema=True)

        vector = self._embed_question(question)
        schema_hits = [
            h for h in self.store.search("schema", vector, k=self.k_schema)
            if h.payload.get("db_id") == db_id
        ]
        examples: Sequence[Hit] = ()
        values: Sequence[Hit] = ()
        if use_examples:
            examples = self.store.search("example", vector, k=self.k_examples)
        if use_values:
            values = [
                h for h in self.store.search("value", vector, k=self.k_values)
                if h.payload.get("db_id") == db_id
            ]
        return RetrievedContext(
            arm=arm,
            schema_text="\n".join(h.text for h in schema_hits),
            examples=tuple(examples),
            values=tuple(values),
            retrieved_tables=tuple(str(h.payload.get("table", "")) for h in schema_hits),
        )
