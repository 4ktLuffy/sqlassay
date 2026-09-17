"""``null_predictor`` -- an agent that proposes nothing must score at the floor.

The floor is **measured, not assumed to be zero**. On BIRD dev 2025-11-06 the
null predictor scores 2/1504: two items whose gold returns exactly one NULL,
which ``SELECT NULL`` matches outright. A band set at zero would fail for a
reason that is a property of the suite rather than a defect in anything, and
would then be "fixed" by weakening something real.

**Writable twin:** the gold predictor must beat the null predictor by a stated
margin on the same items. Without it, a suite nobody can score on -- where
gold and null both sit at zero -- would pass this control by collapsing
correctly, which is exactly the failure ``agent-assay`` recorded against
``qwen2.5:7b``.
"""

from __future__ import annotations

from fireassay.controls.base import ControlOutcome

from sqlassay.controls._common import band_map, gold_floor, run_predictor
from sqlassay.controls.base import ControlContext
from sqlassay.floors import NullPredictor

__all__ = ["NullPredictorControl"]


class NullPredictorControl:
    """Proposing nothing must land at the measured floor, and gold must beat it."""

    kind = "null_predictor"
    version = "1.0.0"

    def run(self, ctx: ControlContext) -> ControlOutcome:
        """Measure the null predictor and its twin."""
        bands = ctx.expected.get(self.kind, {})
        if not ctx.items or "accuracy_max" not in bands or "min_margin" not in bands:
            return ControlOutcome(
                kind=self.kind,
                status="NOT_RUN",
                observed={},
                expected=band_map(ctx, self.kind),
                twin_ok=False,
                cause_assertions={},
                detail="no items, or no accuracy_max/min_margin band configured",
            )

        null = run_predictor(NullPredictor(), ctx)
        gold = gold_floor(ctx)
        margin = gold.accuracy - null.accuracy
        twin_ok = bands["min_margin"].check(margin)

        causes = {
            "at_or_below_measured_floor": bands["accuracy_max"].check(null.accuracy),
            "gold_beats_null_by_margin": twin_ok,
            # The cause, not just the outcome: a null predictor that scored low
            # because its SQL failed to parse would be measuring the parse gate,
            # not the floor. `SELECT NULL` must reach the comparison every time.
            "null_sql_always_executed": null.parse_failed == 0 and null.execute_failed == 0,
        }
        passed = twin_ok and all(causes.values())
        return ControlOutcome(
            kind=self.kind,
            status="PASSED" if passed else "FAILED",
            observed={
                "null_accuracy": null.accuracy,
                "gold_accuracy": gold.accuracy,
                "margin": margin,
            },
            expected=band_map(ctx, self.kind),
            twin_ok=twin_ok,
            cause_assertions=causes,
            detail=(
                f"null {null.correct}/{null.n} = {null.accuracy:.6f}; "
                f"gold {gold.accuracy:.6f}; margin {margin:.6f}"
            ),
        )
