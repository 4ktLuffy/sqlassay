"""``shuffled_gold`` -- rescoring against a deranged answer key must collapse it.

The predictions are held fixed and the *answer key* is permuted: item ``i``'s
prediction is scored against item ``σ(i)``'s gold rows, under a derangement so
no item keeps its own. A harness that still reports a high score after this is
not reading the answer key at all.

Predictions here are the **gold** predictions, so this control needs no model
and can run before any model has been measured. That is deliberate: a control
that only becomes available after an expensive run cannot gate that run.

**Assert the cause, not the outcome.** The score must collapse *at the
resultset gate*. If it collapsed because predictions stopped parsing or
executing, something else broke and the collapse would be evidence of the
wrong thing. Parse and execute counts must be **identical** before and after,
since the permutation touches only what the rows are compared against.

**Writable twin:** the same predictions against the *un*-deranged key must
score 1.0.
"""

from __future__ import annotations

import random

from fireassay.controls.base import ControlOutcome

from sqlassay.controls._common import GoldPredictor, band_map, gold_failures, gold_floor
from sqlassay.controls.base import ControlContext
from sqlassay.gates import run_gates
from sqlassay.models import GateStatus, Prediction

__all__ = ["ShuffledGoldControl"]


def _derangement(n: int, seed: int) -> list[int]:
    """A permutation with no fixed point, by rejection.

    Rejection rather than a constructive shuffle because it is obviously
    correct, and ``n`` here is a suite size where the expected number of
    retries is under two.
    """
    if n < 2:
        return list(range(n))
    rng = random.Random(seed)
    order = list(range(n))
    for _ in range(1000):
        rng.shuffle(order)
        if all(i != j for i, j in enumerate(order)):
            return order
    # Fall back to a rotation, which is a derangement for any n >= 2.
    return [(i + 1) % n for i in range(n)]


class ShuffledGoldControl:
    """Permute the answer key; the score must collapse, and for the right reason."""

    kind = "shuffled_gold"
    version = "1.0.0"

    def run(self, ctx: ControlContext) -> ControlOutcome:
        """Rescore gold predictions against a deranged gold set."""
        bands = ctx.expected.get(self.kind, {})
        if len(ctx.items) < 2 or "accuracy_max" not in bands:
            return ControlOutcome(
                kind=self.kind,
                status="NOT_RUN",
                observed={},
                expected=band_map(ctx, self.kind),
                twin_ok=False,
                cause_assertions={},
                detail="fewer than two items, or no accuracy_max band configured",
            )

        baseline = gold_floor(ctx)
        twin_ok = baseline.accuracy == 1.0

        scorable = [i for i in ctx.items if i.db_id in ctx.databases]
        order = _derangement(len(scorable), seed=0)
        predictor = GoldPredictor()

        correct = parse_failed = execute_failed = resultset_failed = scored = 0
        for position, item in enumerate(scorable):
            db = ctx.databases[item.db_id]
            own_rows = ctx.cache.rows_for(db, item)
            other = scorable[order[position]]
            other_rows = ctx.cache.rows_for(ctx.databases[other.db_id], other)
            if own_rows is None or other_rows is None:
                continue
            scored += 1
            outcome = run_gates(
                Prediction(
                    item_id=item.item_id,
                    sql=predictor.predict(item, db),
                    raw="",
                    arm="shuffled_gold",
                ),
                db,
                other_rows,          # the deranged answer key
                item.gold_sql,       # the ordering contract stays the item's own
            )
            correct += int(outcome.correct)
            for name, bump in (("parse", "p"), ("execute", "e"), ("resultset", "r")):
                gate = outcome.gate(name)
                if gate is not None and gate.status is GateStatus.FAILED:
                    if bump == "p":
                        parse_failed += 1
                    elif bump == "e":
                        execute_failed += 1
                    else:
                        resultset_failed += 1

        accuracy = correct / scored if scored else 0.0
        causes = {
            "collapsed_into_band": bands["accuracy_max"].check(accuracy),
            # The cause: the collapse must be the comparison failing, not the
            # predictions ceasing to parse or execute.
            "parse_unchanged": parse_failed == baseline.parse_failed,
            "execute_unchanged": execute_failed == baseline.execute_failed,
            "resultset_gate_did_the_work": resultset_failed > baseline.resultset_failed,
            "underanged_scores_one": twin_ok,
        }
        passed = twin_ok and all(causes.values())
        return ControlOutcome(
            kind=self.kind,
            status="PASSED" if passed else "FAILED",
            observed={
                "accuracy": accuracy,
                "baseline_accuracy": baseline.accuracy,
                "resultset_failed": float(resultset_failed),
            },
            expected=band_map(ctx, self.kind),
            twin_ok=twin_ok,
            cause_assertions=causes,
            detail=(
                f"deranged {correct}/{scored} = {accuracy:.6f} against baseline "
                f"{baseline.accuracy:.6f}; resultset failures "
                f"{baseline.resultset_failed} -> {resultset_failed}"
                + (
                    f"; baseline failed on {list(gold_failures(ctx, baseline))[:10]}"
                    if not twin_ok
                    else ""
                )
            ),
        )
