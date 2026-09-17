"""Oracle integrity: is this item's answer key fit to score anything against?

The positive control. Every other check in this harness asks whether the model
was right; this one asks whether the question was answerable, and it runs
before any model does.

The mechanism is to **use the gold SQL as if it were the prediction** and send
it through all three gates. It must score correct. An item where the answer key
cannot match itself cannot tell you anything about a model, and scoring a model
against it manufactures a failure out of a defect in the benchmark.

Four ways an item fails, in the order they are cheapest to detect:

1. **Ambiguous** -- a ``LIMIT`` whose own select has no ``ORDER BY``. The
   engine may return any rows, so there is no single correct result set.
   Static, free.
1b. **Ambiguous by tie** -- ``ORDER BY`` is present, but the key *ties across
   the ``LIMIT`` boundary* while the projected columns differ. The gold
   returns one of several equally valid answers, and a model that returns a
   different one is marked wrong by every execution-accuracy implementation
   including the benchmark's own. Requires execution; no static check can see
   it. Measured on BIRD dev: 39 items, against 6 caught by the static check.
2. **Gold does not parse**, or is not a single read-only statement.
3. **Gold does not execute** against its own database.
0. **Clock-dependent** -- the gold calls ``JULIANDAY('now')``, ``CURRENT_DATE``
   or similar, so its answer changes with the date. Checked first and
   statically: two executions a second apart will not catch it. Measured on
   BIRD dev 2025-11-06: 20 items.
4. **Gold is non-deterministic** -- executing it twice returns different rows.
   Caught by running it twice and comparing, which is the only way to catch a
   tie-break or a ``random()`` that no static check can see.

Every failure is **excluded and counted, with its reason**, never scored. A
report that quietly dropped them would overstate the suite size, and one that
scored them would understate the model.

This module makes no model calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import sqlglot
from sqlglot import exp

from sqlassay.engine import Engine, ExecutionError
from sqlassay.gates import ParseGate, run_gates
from sqlassay.models import GateStatus, Item, Prediction
from sqlassay.normalize import (
    compare_result_sets,
    gold_is_ambiguous,
    gold_is_clock_dependent,
    order_is_significant,
)

__all__ = [
    "OracleKind",
    "OracleReport",
    "OracleVerdict",
    "check_item",
    "check_suite",
    "limit_boundary_is_tied",
    "valid_alternatives",
]

DEFAULT_DETERMINISM_RUNS = 2
"""How many times the gold query is executed to test determinism.

Two is the minimum that can disagree, and it doubles the cost of the cheapest
part of the pipeline rather than of the expensive part. It will not catch a
query that is non-deterministic one time in fifty; that is a stated limit of
this check, not a thing the number hides.
"""


class OracleKind(StrEnum):
    """Why an item is or is not fit to score against."""

    OK = "ok"
    AMBIGUOUS = "ambiguous"
    AMBIGUOUS_TIE = "ambiguous_tie"
    GOLD_CLOCK_DEPENDENT = "gold_clock_dependent"
    GOLD_TOO_SLOW = "gold_too_slow"
    GOLD_UNPARSEABLE = "gold_unparseable"
    GOLD_FAILED = "gold_failed"
    GOLD_NONDETERMINISTIC = "gold_nondeterministic"
    GOLD_DOES_NOT_MATCH_ITSELF = "gold_does_not_match_itself"


@dataclass(frozen=True)
class OracleVerdict:
    """One item's fitness, and the evidence for it."""

    item_id: str
    kind: OracleKind
    evidence: str

    @property
    def admissible(self) -> bool:
        """Can this item score a model at all?"""
        return self.kind is OracleKind.OK


@dataclass(frozen=True)
class OracleReport:
    """The suite's fitness: what survives, what was excluded, and why."""

    verdicts: tuple[OracleVerdict, ...]

    @property
    def admissible_items(self) -> tuple[str, ...]:
        """Item ids fit to score a model against."""
        return tuple(v.item_id for v in self.verdicts if v.admissible)

    @property
    def excluded(self) -> tuple[OracleVerdict, ...]:
        """Every excluded item, with its reason."""
        return tuple(v for v in self.verdicts if not v.admissible)

    @property
    def by_kind(self) -> Mapping[str, int]:
        """How many items fell into each kind."""
        return dict(Counter(v.kind.value for v in self.verdicts))

    @property
    def n_gold_verified(self) -> int:
        """Items whose gold reached the gold-predictor stage and matched itself.

        **This equals ``n_admissible`` by construction** -- an item only
        becomes admissible by passing that stage -- so the equality is not
        evidence of anything and is not reported as though it were. It is
        exposed because the count itself is worth stating: it says how many
        items were actually pushed through all three gates, as opposed to
        excluded earlier by the static ambiguity check, which is cheaper and
        proves less.

        The real check on this stage is a **mutation**: break a normalisation
        rule and the gold predictor must stop matching itself. That cannot be
        a property of a report; it lives in
        ``tests/test_oracle.py::TestGoldPredictorCanGoRed``.
        """
        return sum(1 for v in self.verdicts if v.kind is OracleKind.OK)

    def as_record(self) -> dict[str, object]:
        """The committed artifact form."""
        return {
            "n_items": len(self.verdicts),
            "n_admissible": len(self.admissible_items),
            "n_excluded": len(self.excluded),
            "by_kind": dict(self.by_kind),
            "n_gold_verified": self.n_gold_verified,
            "excluded": [
                {"item_id": v.item_id, "kind": v.kind.value, "evidence": v.evidence}
                for v in self.excluded
            ],
        }


def limit_boundary_is_tied(db: Engine, sql: str) -> tuple[bool, str]:
    """Does this query's ``ORDER BY`` fail to break the tie at its ``LIMIT``?

    The gold is rewritten to expose its own sort key -- the ``ORDER BY``
    expressions are appended to the projection -- and run without the
    ``LIMIT``. If the key at the last kept row equals the key at the first
    dropped row, the engine's choice between them is arbitrary.

    That alone is not a defect. If the two rows *project* identically, either
    choice yields the same result set and the item is fine; on BIRD dev that
    is 26 of the 65 boundary ties. Only a tie where the projections differ
    makes the answer key one of several equally valid answers.

    Returns ``(tied, reason)``. Anything that cannot be probed -- ``SELECT *``,
    an unparseable order key, an execution failure on the rewritten query --
    returns ``False`` with the reason, because this check may exclude an item
    only on positive evidence, never on its own inability to look.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=db.dialect)
    except Exception as e:
        return False, f"not probed: parse failed ({type(e).__name__})"
    if tree is None:
        return False, "not probed: empty parse"

    limit_node, order_node = tree.args.get("limit"), tree.args.get("order")
    if limit_node is None or order_node is None:
        return False, "no top-level LIMIT with ORDER BY"
    try:
        n = int(limit_node.expression.this)
    except Exception:
        return False, "not probed: LIMIT is not a literal integer"
    if n < 1:
        return False, "not probed: non-positive LIMIT"

    projection = list(tree.args.get("expressions") or [])
    if not projection or any(isinstance(e, exp.Star) for e in projection):
        return False, "not probed: SELECT * has no stable projection to compare"
    n_proj = len(projection)

    probe = tree.copy()
    probe.set("expressions", projection + [o.this.copy() for o in order_node.expressions])
    probe.set("limit", None)
    try:
        rows = db.execute(probe.sql(dialect=db.dialect)).rows
    except ExecutionError as e:
        return False, f"not probed: rewritten query failed ({e})"

    if len(rows) <= n:
        return False, f"LIMIT {n} does not truncate {len(rows)} rows"

    boundary_key = rows[n - 1][n_proj:]
    if rows[n][n_proj:] != boundary_key:
        return False, f"ORDER BY key differs across the LIMIT {n} boundary"

    # The whole tie group, not just the adjacent pair. Comparing only rows
    # n-1 and n under-counts: if those two project identically but a third row
    # sharing the key projects differently, the engine may return that one
    # instead and the answer is still under-determined.
    group = [r for r in rows if r[n_proj:] == boundary_key]
    projections = {r[:n_proj] for r in group}
    if len(projections) == 1:
        return False, (
            f"key ties across the LIMIT {n} boundary over {len(group)} rows, "
            f"but all of them project identically"
        )

    kept = rows[n - 1][:n_proj]
    alternative = next(p for p in projections if p != kept)
    return True, (
        f"ORDER BY key {boundary_key!r} ties across the LIMIT {n} boundary over {len(group)} rows "
        f"with {len(projections)} distinct projections: gold returns {kept!r} "
        f"but {alternative!r} is equally valid"
    )


def valid_alternatives(db: Engine, sql: str) -> tuple[frozenset[tuple[Any, ...]], str]:
    """Every result set that is equally correct for a ``LIMIT 1`` boundary tie.

    Restricted to ``LIMIT 1`` on purpose. There, the answer is one row drawn
    from the tie group, so the valid result sets are exactly the group's
    distinct projections, one row each -- enumerable and checkable. For
    ``LIMIT n > 1`` the valid answers are *combinations* drawn from the group,
    and enumerating them is both expensive and easy to get subtly wrong, so
    this declines rather than guessing.

    Returns ``(alternatives, reason)``. An empty set means "not enumerable
    here", never "there are none".
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=db.dialect)
    except Exception as e:
        return frozenset(), f"not enumerable: parse failed ({type(e).__name__})"
    if tree is None:
        return frozenset(), "not enumerable: empty parse"

    limit_node, order_node = tree.args.get("limit"), tree.args.get("order")
    if limit_node is None or order_node is None:
        return frozenset(), "not enumerable: no top-level LIMIT with ORDER BY"
    try:
        n = int(limit_node.expression.this)
    except Exception:
        return frozenset(), "not enumerable: LIMIT is not a literal integer"
    if n != 1:
        return frozenset(), f"not enumerable: LIMIT {n} admits combinations, not single rows"

    projection = list(tree.args.get("expressions") or [])
    if not projection or any(isinstance(e, exp.Star) for e in projection):
        return frozenset(), "not enumerable: SELECT * has no stable projection"
    n_proj = len(projection)

    probe = tree.copy()
    probe.set("expressions", projection + [o.this.copy() for o in order_node.expressions])
    probe.set("limit", None)
    try:
        rows = probe_rows = db.execute(probe.sql(dialect=db.dialect)).rows
    except ExecutionError as e:
        return frozenset(), f"not enumerable: rewritten query failed ({e})"
    if not probe_rows:
        return frozenset(), "not enumerable: query returns no rows"

    key = rows[0][n_proj:]
    group = [r[:n_proj] for r in rows if r[n_proj:] == key]
    alternatives = frozenset((row,) for row in group)
    return alternatives, f"{len(alternatives)} equally correct single-row answers"


def check_item(
    db: Engine, item: Item, *, determinism_runs: int = DEFAULT_DETERMINISM_RUNS
) -> OracleVerdict:
    """Decide whether one item's answer key is fit to score against."""

    def verdict(kind: OracleKind, evidence: str) -> OracleVerdict:
        return OracleVerdict(item_id=item.item_id, kind=kind, evidence=evidence)

    # 0. Clock dependence. Static, free, and checked first because an answer
    #    key that changes with the date is unusable regardless of anything
    #    else that might also be wrong with it.
    clocked, clock_reason = gold_is_clock_dependent(item.gold_sql)
    if clocked:
        return verdict(OracleKind.GOLD_CLOCK_DEPENDENT, clock_reason)

    # 1. Static ambiguity. Free, and the most common real defect.
    ambiguous, reason = gold_is_ambiguous(item.gold_sql, dialect=db.dialect)
    if ambiguous:
        return verdict(OracleKind.AMBIGUOUS, reason)

    # 2. The gold must itself be one read-only statement.
    parsed = ParseGate(dialect=db.dialect).run(item.gold_sql)
    if parsed.status is not GateStatus.PASSED:
        return verdict(OracleKind.GOLD_UNPARSEABLE, parsed.evidence)

    # 3. It must execute.
    runs = []
    for i in range(max(1, determinism_runs)):
        try:
            runs.append(db.execute(item.gold_sql).rows)
        except ExecutionError as e:
            # A gold that exceeds the deadline is slow, not broken, and the
            # two must not be pooled. Measured on BIRD dev: the single item
            # that timed out at 30s completes in 154s and returns one row.
            # Reporting it as a broken answer key would be a false finding
            # about the benchmark caused by our own deadline.
            kind = OracleKind.GOLD_TOO_SLOW if e.kind == "timeout" else OracleKind.GOLD_FAILED
            return verdict(kind, f"execution {i + 1}: {e}")

    # 4. It must execute to the *same* answer twice. Compared under the same
    #    rules the model is scored by, so a query the comparison would call
    #    stable is not failed here for a difference the comparison ignores.
    order_sensitive = order_is_significant(item.gold_sql, dialect=db.dialect)
    first = runs[0]
    for i, rows in enumerate(runs[1:], start=2):
        c = compare_result_sets(rows, first, order_sensitive=order_sensitive)
        if not c.equal:
            return verdict(
                OracleKind.GOLD_NONDETERMINISTIC,
                f"run {i} differs from run 1: {c.reason}",
            )

    # 4b. A tie the ORDER BY does not break is ambiguity that no static check
    #     can see, and it is the larger category in practice.
    tied, tie_reason = limit_boundary_is_tied(db, item.gold_sql)
    if tied:
        return verdict(OracleKind.AMBIGUOUS_TIE, tie_reason)

    # 5. The gold predictor: gold SQL as the prediction, through all three
    #    gates. Anything that reaches here and fails is a defect in this
    #    harness -- a normalisation rule that a correct answer cannot satisfy.
    outcome = run_gates(
        Prediction(item_id=item.item_id, sql=item.gold_sql, raw=item.gold_sql, arm="oracle"),
        db,
        first,
        item.gold_sql,
    )
    if not outcome.correct:
        failing = next(
            (g for g in outcome.gates if g.status is not GateStatus.PASSED), outcome.gates[-1]
        )
        return verdict(
            OracleKind.GOLD_DOES_NOT_MATCH_ITSELF,
            f"{failing.gate_id} said {failing.status.value}: {failing.evidence}",
        )

    return verdict(OracleKind.OK, f"gold executes deterministically and matches itself ({len(first)} rows)")


def check_suite(
    items: Iterable[Item],
    databases: Mapping[str, Engine],
    *,
    determinism_runs: int = DEFAULT_DETERMINISM_RUNS,
) -> OracleReport:
    """Check every item. Items whose database is missing are excluded, not skipped."""
    verdicts: list[OracleVerdict] = []
    for item in items:
        db = databases.get(item.db_id)
        if db is None:
            verdicts.append(
                OracleVerdict(
                    item_id=item.item_id,
                    kind=OracleKind.GOLD_FAILED,
                    evidence=f"no database loaded for db_id {item.db_id!r}",
                )
            )
            continue
        verdicts.append(check_item(db, item, determinism_runs=determinism_runs))
    return OracleReport(verdicts=tuple(verdicts))


def admissible_subset(items: Sequence[Item], report: OracleReport) -> tuple[Item, ...]:
    """The items a model may be scored against, in the suite's original order."""
    keep = set(report.admissible_items)
    return tuple(i for i in items if i.item_id in keep)
