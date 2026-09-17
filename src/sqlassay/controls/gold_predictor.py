"""``gold_predictor`` -- the positive control, and everything else's twin.

The gold SQL is proposed as if it were the model's answer and scored by the
same three gates. It must come back **exactly 1.0**.

This is not a formality. It is the only check in the harness that can catch a
normalisation rule a *correct* answer cannot satisfy -- a comparison that
treats NULL the way SQL does, an ordering contract read from the wrong query,
a numeric tolerance that rejects two spellings of the same number. Every one of
those makes the model look worse and none of them would show up in any
control that only watches numbers collapse.

It has no writable twin because it **is** the twin: a control whose expected
result is a collapse is only evidence if this one passes first.
"""

from __future__ import annotations

from fireassay.controls.base import ControlOutcome

from sqlassay.controls._common import band_map, gold_floor
from sqlassay.controls.base import ControlContext

__all__ = ["GoldPredictorControl"]


class GoldPredictorControl:
    """The answer key must be able to score itself."""

    kind = "gold_predictor"
    version = "1.0.0"

    def run(self, ctx: ControlContext) -> ControlOutcome:
        """Score the gold against itself over the whole suite."""
        if not ctx.items:
            return ControlOutcome(
                kind=self.kind,
                status="NOT_RUN",
                observed={},
                expected=band_map(ctx, self.kind),
                twin_ok=False,
                cause_assertions={},
                detail="no items in the suite",
            )

        result = gold_floor(ctx)
        bands = ctx.expected.get(self.kind, {})
        accuracy_ok = bands["accuracy"].check(result.accuracy) if "accuracy" in bands else False

        causes = {
            # Not just "the number is 1.0" -- each gate must be individually
            # clean, so a compensating pair of failures cannot average out.
            "no_parse_failures": result.parse_failed == 0,
            "no_execute_failures": result.execute_failed == 0,
            "no_resultset_failures": result.resultset_failed == 0,
            "every_item_scored": result.correct == result.n,
        }
        passed = accuracy_ok and all(causes.values())
        return ControlOutcome(
            kind=self.kind,
            status="PASSED" if passed else "FAILED",
            observed={"accuracy": result.accuracy, "n": float(result.n)},
            expected=band_map(ctx, self.kind),
            twin_ok=True,
            cause_assertions=causes,
            detail=(
                f"gold scored {result.correct}/{result.n} = {result.accuracy:.6f}; "
                f"parse_failed={result.parse_failed} execute_failed={result.execute_failed} "
                f"resultset_failed={result.resultset_failed}"
            ),
        )
