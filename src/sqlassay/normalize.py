"""Result-set normalisation and comparison: the part of a text-to-SQL harness
that is actually hard, and the part that is usually wrong.

Comparing two result sets looks like ``a == b`` and is not. Six decisions sit
underneath it, and each one silently changes the score:

1. **Order.** Two queries that return the same rows in a different order are
   the same answer *unless the question asked for an ordering*. So order
   significance is read off the gold query's own syntax -- a top-level
   ``ORDER BY`` -- and never assumed. Sorting both sides unconditionally
   scores a model correct on "top 5 by revenue" when it returned the bottom
   five; comparing positionally scores it wrong for returning the right rows.
   Both are common and both are wrong.

2. **Duplicates.** Rows are compared as a *multiset*, not a set. ``SELECT
   region`` and ``SELECT DISTINCT region`` are different questions, and
   collapsing to a set makes them indistinguishable.

3. **Numbers.** ``5``, ``5.0`` and ``Decimal('5.00')`` are one answer. Float
   equality across engines is not reliable, so all numerics are rounded to a
   fixed number of decimals. The tolerance is a named parameter, stored with
   the run, because a result that only matches at 2dp and not at 6dp is a
   different claim.

4. **Column identity.** Compared by **position, not name.** A model that
   aliases ``SUM(x)`` as ``total`` rather than ``sum_x`` has not answered a
   different question. Names are therefore ignored entirely; arity is not,
   and a differing column count fails with that as the stated reason.

5. **NULL.** ``None`` is a value distinct from ``0`` and from ``''``. It is
   never coerced, and it sorts as its own key.

6. **Ambiguous gold.** A gold query with ``LIMIT`` and no ``ORDER BY`` has no
   single correct answer -- the engine may return any rows. That is a defect
   in the *item*, not in the model, and :func:`gold_is_ambiguous` exists so
   such items can be excluded and counted rather than scored as failures.

Every function here is pure and deterministic. Nothing in this module touches
a database or a model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

import sqlglot
from sqlglot import exp

__all__ = [
    "DEFAULT_FLOAT_DECIMALS",
    "Comparison",
    "NormBool",
    "compare_result_sets",
    "gold_is_ambiguous",
    "normalize_row",
    "normalize_value",
    "order_is_significant",
]

class NormBool(Enum):
    """The normalised form of a SQL boolean.

    A bare ``bool`` cannot be used here. ``bool`` subclasses ``int`` in
    Python, so ``True == 1`` and ``True == 1.0`` are both true, and returning
    the boolean unchanged leaves a boolean column comparing equal to an
    integer one even though the numeric branch never touched it. Guarding the
    ``isinstance`` check is necessary and *not sufficient*; the value has to
    leave the numeric type family altogether.

    An ``Enum`` rather than a tagged tuple or a sentinel string because it is
    hashable, orderable via its name, and cannot collide with anything a
    database can put in a cell.
    """

    TRUE = True
    FALSE = False


DEFAULT_FLOAT_DECIMALS = 4
"""Decimals every numeric is rounded to before comparison.

Four is a choice, not a law: it is tight enough that a genuinely different
aggregate shows up, and loose enough to absorb the last-bit differences two
engines produce for the same ``AVG``. It is recorded with every run so a
number measured at 4dp is never compared against one measured at 6dp.
"""


def normalize_value(value: Any, *, decimals: int = DEFAULT_FLOAT_DECIMALS) -> Any:
    """Reduce one cell to a hashable, engine-independent form.

    Booleans are checked before the numeric branch *and* mapped out of the
    numeric type family entirely, to :class:`NormBool`. Ordering the branches
    alone is not enough: ``bool`` subclasses ``int``, so a returned ``True``
    still compares equal to ``1`` and to ``1.0``. This was caught by
    ``test_bool_is_not_an_int`` against an earlier version of this function
    that did exactly that, which is the case for keeping the negative tests.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return NormBool.TRUE if value else NormBool.FALSE
    if isinstance(value, Decimal | float | int):
        return round(float(value), decimals)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, str):
        # Trailing whitespace is CHAR padding, not content. Case is content
        # and is left alone: 'Active' and 'active' may be different rows.
        return value.strip()
    if isinstance(value, dict):
        # DuckDB STRUCT. Sorted by key so two engines' field ordering cannot
        # produce a spurious mismatch.
        return tuple(sorted((str(k), normalize_value(v, decimals=decimals)) for k, v in value.items()))
    if isinstance(value, list | tuple | set):
        # DuckDB LIST. Order inside a list is content and is preserved.
        return tuple(normalize_value(v, decimals=decimals) for v in value)
    return str(value)


def normalize_row(row: Sequence[Any], *, decimals: int = DEFAULT_FLOAT_DECIMALS) -> tuple[Any, ...]:
    """Normalise every cell of one row, preserving column position."""
    return tuple(normalize_value(v, decimals=decimals) for v in row)


def _sort_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """A total order over normalised rows, for order-insensitive comparison.

    Python refuses to compare ``None`` with ``str`` and ``str`` with ``float``,
    so each cell is keyed by ``(type rank, string form)``. The ordering itself
    carries no meaning -- it only has to be *stable and total* so that two
    equal multisets sort identically.
    """
    out: list[Any] = []
    for cell in row:
        if cell is None:
            out.append((0, ""))
        elif isinstance(cell, NormBool):
            out.append((1, cell.name))
        elif isinstance(cell, float):
            out.append((2, f"{cell:.10f}"))
        else:
            out.append((3, str(cell)))
    return tuple(out)


def order_is_significant(sql: str, *, dialect: str = "duckdb") -> bool:
    """Does this query's *own* result order carry meaning?

    True when the outermost select carries an ``ORDER BY``. An ``ORDER BY``
    inside a subquery or CTE orders an intermediate, not the answer, and does
    not make the caller's row order significant -- so only the top-level node
    is inspected, and a naive ``'order by' in sql.lower()`` would get this
    wrong on exactly the queries that matter.

    An unparseable query returns ``False``. This function's job is to describe
    the gold query, not to judge it; parse failure is the parse gate's verdict
    to deliver, and raising here would move that decision into the wrong place.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return False
    if parsed is None:
        return False
    return parsed.args.get("order") is not None


def gold_is_ambiguous(sql: str, *, dialect: str = "duckdb") -> tuple[bool, str]:
    """Is this gold query's answer under-determined by its own text?

    ``LIMIT`` without a top-level ``ORDER BY`` lets the engine return any *n*
    rows it likes, so there is no single correct result set to compare
    against. Such an item cannot score a model and must be excluded and
    counted, never silently failed.

    Returns ``(ambiguous, reason)`` so the caller can record *why* an item was
    excluded rather than only that it was.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as e:
        return False, f"not parsed here; parse gate owns this verdict ({type(e).__name__})"
    if parsed is None:
        return False, "empty parse"

    # Each LIMIT is judged against the ORDER BY of *its own* select, not the
    # outermost one. A subquery that orders and then limits is determinate,
    # and the outer query inherits no ambiguity from it. Measured cost of
    # getting this wrong: on BIRD dev an earlier version of this function used
    # `find_all(exp.Limit)` against the top-level `order` and flagged 23 items
    # that were perfectly well-determined -- 79% of everything it reported.
    for limit_node in parsed.find_all(exp.Limit):
        owner = limit_node.parent
        if owner is None:
            continue
        if owner.args.get("order") is None:
            where = "the query" if owner is parsed else "a subquery"
            return True, f"LIMIT without an ORDER BY in {where}: the engine may return any rows"
    return False, "determinate"


@dataclass(frozen=True)
class Comparison:
    """The verdict of one result-set comparison, and why.

    ``reason`` is written for a human reading a failure in a report. It names
    the first concrete difference -- arity, cardinality, or an example row
    present on one side only -- rather than saying that the sets differed.
    """

    equal: bool
    reason: str
    order_sensitive: bool
    predicted_rows: int
    gold_rows: int


def compare_result_sets(
    predicted: Iterable[Sequence[Any]],
    gold: Iterable[Sequence[Any]],
    *,
    order_sensitive: bool,
    decimals: int = DEFAULT_FLOAT_DECIMALS,
) -> Comparison:
    """Compare two result sets under the rules documented at module level.

    ``order_sensitive`` is passed in rather than derived here, because the
    caller knows which query defines the ordering contract (the gold one) and
    this function must not re-decide it from the prediction -- a model could
    otherwise make its own answer order-insensitive by dropping the ORDER BY.
    """
    pred_rows = [normalize_row(r, decimals=decimals) for r in predicted]
    gold_rows = [normalize_row(r, decimals=decimals) for r in gold]

    def result(equal: bool, reason: str) -> Comparison:
        return Comparison(
            equal=equal,
            reason=reason,
            order_sensitive=order_sensitive,
            predicted_rows=len(pred_rows),
            gold_rows=len(gold_rows),
        )

    if not pred_rows and not gold_rows:
        return result(True, "both empty")

    pred_arity = {len(r) for r in pred_rows}
    gold_arity = {len(r) for r in gold_rows}
    if pred_rows and gold_rows and pred_arity != gold_arity:
        return result(
            False,
            f"column count differs: predicted {sorted(pred_arity)}, gold {sorted(gold_arity)}",
        )

    if len(pred_rows) != len(gold_rows):
        return result(False, f"row count differs: predicted {len(pred_rows)}, gold {len(gold_rows)}")

    if order_sensitive:
        for i, (p, g) in enumerate(zip(pred_rows, gold_rows, strict=True)):
            if p != g:
                return result(False, f"ordered comparison: first difference at row {i}: {p!r} != {g!r}")
        return result(True, f"ordered comparison: {len(gold_rows)} rows identical in sequence")

    pred_counts = Counter(_sort_key(r) for r in pred_rows)
    gold_counts = Counter(_sort_key(r) for r in gold_rows)
    if pred_counts == gold_counts:
        return result(True, f"unordered comparison: {len(gold_rows)} rows match as a multiset")

    # The multiset is keyed by _sort_key, which is an internal total ordering
    # and is unreadable. Map each key back to a real row so the evidence shows
    # the data, not the key -- a reader debugging a failure needs the row.
    examples: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in (*gold_rows, *pred_rows):
        examples.setdefault(_sort_key(row), row)

    missing = gold_counts - pred_counts
    extra = pred_counts - gold_counts
    parts = []
    if missing:
        key = next(iter(missing))
        parts.append(f"{sum(missing.values())} row(s) in gold not predicted, e.g. {examples[key]!r}")
    if extra:
        key = next(iter(extra))
        parts.append(f"{sum(extra.values())} row(s) predicted not in gold, e.g. {examples[key]!r}")
    return result(False, "unordered comparison: " + "; ".join(parts))
