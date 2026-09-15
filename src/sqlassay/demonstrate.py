"""Does the ambiguity actually cost a model marks? Measured, not argued.

The oracle report says 30 BIRD dev items cannot distinguish a correct model
from an incorrect one. That is a statement about the answer keys. It is not
yet a statement about any model, and the obvious question -- *does this
actually happen?* -- is left unanswered by a count.

So: run a model on the ambiguous items, execute what it writes, and sort the
results into three buckets.

* **matched gold** -- scored correct, by luck as much as anything.
* **matched an alternative** -- the model returned a result set that is
  *equally correct* under the question as asked, and every execution-accuracy
  implementation, BIRD's own included, scores it zero.
* **matched neither** -- the model is simply wrong, and the ambiguity is not
  what cost it the mark.

Only the middle bucket is evidence. The first and third say nothing about
ambiguity at all, and reporting them together with it -- "the model failed N
ambiguous items" -- would be the same pooling error the gates exist to stop.

Restricted to ``LIMIT 1`` ties, where the valid answers are enumerable
exactly. See ``oracle.valid_alternatives``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlassay.engine import Engine, ExecutionError
from sqlassay.model.extract import extract_sql
from sqlassay.model.ollama import OllamaChat
from sqlassay.model.prompt import SYSTEM_PROMPT, build_user_prompt
from sqlassay.models import Item
from sqlassay.normalize import compare_result_sets, order_is_significant
from sqlassay.oracle import valid_alternatives
from sqlassay.retrieval.indexes import RetrievedContext, full_schema_text

__all__ = ["Bucket", "DemonstrationResult", "demonstrate_item"]


class Bucket:
    """The three outcomes. Strings rather than an enum, for the JSON artifact."""

    GOLD = "matched_gold"
    ALTERNATIVE = "matched_alternative"
    NEITHER = "matched_neither"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class DemonstrationResult:
    """One item's outcome, with everything needed to check it by hand."""

    item_id: str
    bucket: str
    predicted_sql: str
    n_alternatives: int
    detail: str

    def as_record(self) -> dict[str, Any]:
        """The committed artifact form."""
        return {
            "item_id": self.item_id,
            "bucket": self.bucket,
            "n_alternatives": self.n_alternatives,
            "predicted_sql": self.predicted_sql,
            "detail": self.detail,
        }


def demonstrate_item(chat: OllamaChat, db: Engine, item: Item) -> DemonstrationResult:
    """Ask the model, execute the answer, and sort it into a bucket."""
    alternatives, why = valid_alternatives(db, item.gold_sql)
    if not alternatives:
        return DemonstrationResult(item.item_id, Bucket.NOT_RUN, "", 0, why)

    ctx = RetrievedContext(arm="full", schema_text=full_schema_text(db), full_schema=True)
    response = chat.complete(SYSTEM_PROMPT, build_user_prompt(item.question, ctx, hint=item.hint))
    sql = extract_sql(response.content)
    if not sql:
        return DemonstrationResult(
            item.item_id, Bucket.NEITHER, "", len(alternatives), "no SQL produced"
        )

    try:
        predicted = db.execute(sql).rows
    except ExecutionError as e:
        return DemonstrationResult(
            item.item_id, Bucket.NEITHER, sql, len(alternatives), f"did not execute: {e}"
        )

    order_sensitive = order_is_significant(item.gold_sql, dialect=db.dialect)
    try:
        gold = db.execute(item.gold_sql).rows
    except ExecutionError as e:  # pragma: no cover - the oracle already ran this
        return DemonstrationResult(item.item_id, Bucket.NOT_RUN, sql, len(alternatives), f"gold failed: {e}")

    if compare_result_sets(predicted, gold, order_sensitive=order_sensitive).equal:
        return DemonstrationResult(
            item.item_id, Bucket.GOLD, sql, len(alternatives), "returned the gold's own answer"
        )

    # Every alternative is a one-row result; compare under the same rules the
    # scorer uses, not with `in`, so a numeric or NULL spelling difference does
    # not hide a match that the scorer itself would have made.
    for alt in alternatives:
        if compare_result_sets(predicted, alt, order_sensitive=order_sensitive).equal:
            return DemonstrationResult(
                item.item_id,
                Bucket.ALTERNATIVE,
                sql,
                len(alternatives),
                f"returned {predicted!r}, equally correct among {len(alternatives)}, scored zero",
            )

    return DemonstrationResult(
        item.item_id, Bucket.NEITHER, sql, len(alternatives), f"returned {str(predicted)[:120]}"
    )


def summarise(results: Sequence[DemonstrationResult]) -> dict[str, Any]:
    """Counts per bucket, and the items in the one bucket that is evidence."""
    from collections import Counter

    counts = Counter(r.bucket for r in results)
    return {
        "n": len(results),
        "buckets": dict(counts),
        "penalised_by_ambiguity": [
            r.as_record() for r in results if r.bucket == Bucket.ALTERNATIVE
        ],
    }


def write_report(results: Sequence[DemonstrationResult], out: Path, extra: dict[str, Any]) -> None:
    """Write the artifact every number in the README is regenerated from."""
    record = {**extra, **summarise(results), "results": [r.as_record() for r in results]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")
