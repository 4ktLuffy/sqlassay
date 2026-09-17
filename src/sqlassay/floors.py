"""Trivial predictors, and the floors they establish.

**The floors are not zero.** That is the whole reason this module exists, and
it is a lesson already paid for once in ``agent-assay/LIMITATIONS.md`` §4,
where two metrics turned out to have non-zero floors that a band set at zero
would have failed against for the wrong reason.

On an execution oracle the floors are not zero for concrete, checkable
reasons:

* ``SELECT NULL`` matches every item whose gold returns a single NULL -- and
  BIRD is full of ``MAX(...)`` over an empty filter.
* A query returning no rows matches every item whose gold is legitimately
  empty.
* Another item's gold, drawn from the *same database*, can coincide with this
  item's answer outright -- two questions about the same table often have the
  same result set.

So each floor is **measured on the suite being scored**, through the same
three gates the model is scored by. A floor measured with a different
instrument is not a floor for this instrument.

Every predictor here is deterministic given its seed and makes no model call.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlassay.engine import Engine, ExecutionError
from sqlassay.gates import run_gates
from sqlassay.models import GateStatus, Item, Outcome, Prediction

__all__ = [
    "ConstantCountPredictor",
    "EmptyPredictor",
    "FloorResult",
    "GoldCache",
    "NullPredictor",
    "Predictor",
    "RandomGoldPredictor",
    "measure_floor",
]


@runtime_checkable
class Predictor(Protocol):
    """Something that proposes SQL for an item without consulting a model."""

    name: str

    def predict(self, item: Item, db: Engine) -> str:
        """The SQL this predictor proposes. Deterministic."""
        ...


class NullPredictor:
    """Always ``SELECT NULL``.

    The canonical do-nothing agent. Its floor is the share of items whose gold
    returns exactly one NULL, which on a suite full of aggregates over empty
    filters is emphatically not zero.
    """

    name = "null"

    def predict(self, item: Item, db: Engine) -> str:
        return "SELECT NULL"


class EmptyPredictor:
    """Always a query that returns no rows.

    Distinct from :class:`NullPredictor`: one row containing NULL and zero
    rows are different answers, and they match different items. Keeping them
    as separate predictors is what makes the two floors separately visible.
    """

    name = "empty"

    def predict(self, item: Item, db: Engine) -> str:
        return "SELECT NULL WHERE 1 = 0"


class ConstantCountPredictor:
    """Always ``SELECT COUNT(*)`` over the database's first table.

    A plausible-looking wrong answer rather than an obviously empty one. It
    exists because a suite where "count something" scores well by accident
    would inflate every reported number, and only a predictor that *looks*
    like an answer can reveal that.
    """

    name = "constant_count"

    def predict(self, item: Item, db: Engine) -> str:
        tables = sorted({t for t, _c, _ty in db.schema_rows()})
        if not tables:
            return "SELECT NULL"
        return f'SELECT COUNT(*) FROM "{tables[0]}"'


@dataclass
class RandomGoldPredictor:
    """Another item's gold SQL, drawn from the **same database**.

    Same database on purpose. A gold drawn from a different database would
    reference tables that do not exist here, fail at the execute gate, and
    produce a "floor" that is really a measurement of cross-database schema
    mismatch. Drawing within the database keeps the prediction *plausible*,
    which is what a chance band is supposed to be.

    The item's own gold is excluded, so this can never accidentally score a
    correct answer for the trivial reason.
    """

    seed: int
    name: str = field(default="", init=False)
    _pools: dict[str, list[Item]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.name = f"random_gold:seed={self.seed}"

    def index(self, items: Sequence[Item]) -> None:
        """Build the per-database pools this predictor draws from."""
        pools: dict[str, list[Item]] = {}
        for item in items:
            pools.setdefault(item.db_id, []).append(item)
        self._pools = {k: sorted(v, key=lambda i: i.item_id) for k, v in pools.items()}

    def predict(self, item: Item, db: Engine) -> str:
        pool = [i for i in self._pools.get(item.db_id, ()) if i.item_id != item.item_id]
        if not pool:
            return "SELECT NULL"
        rng = random.Random(f"{self.seed}:{item.item_id}")
        return rng.choice(pool).gold_sql


class GoldCache:
    """Gold result sets, executed once per item and reused.

    Every floor pass needs each item's gold rows, and re-executing them per
    predictor would multiply the cost of the cheapest part of the measurement
    by the number of predictors. An item whose gold cannot be executed is
    recorded as ``None`` and skipped by every predictor alike, so no predictor
    is scored over a different item set than any other -- which would make the
    floors incomparable with each other and with the model's score.
    """

    def __init__(self) -> None:
        self._rows: dict[str, tuple[tuple[object, ...], ...] | None] = {}

    def rows_for(self, db: Engine, item: Item) -> tuple[tuple[object, ...], ...] | None:
        """The gold's rows, executing it on first use."""
        if item.item_id not in self._rows:
            try:
                self._rows[item.item_id] = db.execute(item.gold_sql).rows
            except ExecutionError:
                self._rows[item.item_id] = None
        return self._rows[item.item_id]

    def __len__(self) -> int:
        return len(self._rows)


@dataclass(frozen=True)
class FloorResult:
    """One predictor's floor on one suite."""

    predictor: str
    n: int
    correct: int
    parse_failed: int
    execute_failed: int
    resultset_failed: int
    matched_items: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        """Execution accuracy. The floor itself."""
        return self.correct / self.n if self.n else 0.0

    def as_record(self) -> dict[str, object]:
        """The committed artifact form.

        ``matched_items`` is capped at 40 because a floor that matches
        hundreds of items is a finding about the suite, and the first forty
        are enough to see why without making the artifact unreadable.
        """
        return {
            "predictor": self.predictor,
            "n": self.n,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "parse_failed": self.parse_failed,
            "execute_failed": self.execute_failed,
            "resultset_failed": self.resultset_failed,
            "matched_items": list(self.matched_items[:40]),
            "matched_items_truncated": max(0, len(self.matched_items) - 40),
        }


def measure_floor(
    predictor: Predictor,
    items: Sequence[Item],
    databases: Mapping[str, Engine],
    cache: GoldCache,
) -> FloorResult:
    """Run one predictor over the suite, through the real gates.

    Uses :func:`sqlassay.gates.run_gates` rather than comparing result sets
    directly, so a floor and a model score are produced by the same
    instrument. A floor measured by a shortcut would be a different number
    wearing the same name.
    """
    correct = 0
    parse_failed = execute_failed = resultset_failed = 0
    matched: list[str] = []

    for item in items:
        db = databases.get(item.db_id)
        if db is None:
            continue
        gold = cache.rows_for(db, item)
        if gold is None:
            continue
        outcome: Outcome = run_gates(
            Prediction(
                item_id=item.item_id,
                sql=predictor.predict(item, db),
                raw="",
                arm=predictor.name,
            ),
            db,
            gold,
            item.gold_sql,
        )
        if outcome.correct:
            correct += 1
            matched.append(item.item_id)
        for gate, counter in (("parse", "p"), ("execute", "e"), ("resultset", "r")):
            g = outcome.gate(gate)
            if g is not None and g.status is GateStatus.FAILED:
                if counter == "p":
                    parse_failed += 1
                elif counter == "e":
                    execute_failed += 1
                else:
                    resultset_failed += 1

    n = sum(1 for i in items if i.db_id in databases and cache.rows_for(databases[i.db_id], i) is not None)
    return FloorResult(
        predictor=predictor.name,
        n=n,
        correct=correct,
        parse_failed=parse_failed,
        execute_failed=execute_failed,
        resultset_failed=resultset_failed,
        matched_items=tuple(matched),
    )
