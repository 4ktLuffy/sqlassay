"""``random_gold`` -- a plausible wrong answer must land in the chance band.

Another item's gold SQL, drawn from the **same database**, is a far better
chance baseline than random text: it parses, it executes, and it returns a
real result set. What it almost never does is answer *this* question. The
share of times it does anyway is the chance level of this suite, and it is not
zero -- two questions over the same table often have the same answer.

**The band is measured here, per suite, over seeds. It is never fixed in
``expected.yaml``.** What is fixed there is the measurement's parameters and a
sanity maximum on where chance may legitimately sit, so that a suite whose
chance level is absurdly high is caught rather than accommodated.

**Writable twin:** the gold predictor must sit above the band's upper edge.
A suite where chance and gold are indistinguishable cannot measure anything.
"""

from __future__ import annotations

import statistics

from fireassay.controls.base import ControlOutcome

from sqlassay.controls._common import band_map, gold_failures, gold_floor, run_predictor
from sqlassay.controls.base import ControlContext
from sqlassay.floors import RandomGoldPredictor

__all__ = ["RandomGoldControl"]


class RandomGoldControl:
    """Measure the chance band over seeds, then check gold clears it."""

    kind = "random_gold"
    version = "1.0.0"

    def run(self, ctx: ControlContext) -> ControlOutcome:
        """Measure chance over ``ctx.seeds`` seeds and compare gold against it."""
        bands = ctx.expected.get(self.kind, {})
        if not ctx.items or ctx.seeds < 2:
            return ControlOutcome(
                kind=self.kind,
                status="NOT_RUN",
                observed={},
                expected=band_map(ctx, self.kind),
                twin_ok=False,
                cause_assertions={},
                detail=f"no items, or seeds={ctx.seeds} is too few to estimate a band",
            )

        accuracies = []
        parse_failures = execute_failures = 0
        for seed in range(ctx.seeds):
            predictor = RandomGoldPredictor(seed=seed)
            predictor.index(ctx.items)
            result = run_predictor(predictor, ctx)
            accuracies.append(result.accuracy)
            parse_failures += result.parse_failed
            execute_failures += result.execute_failed

        chance_mean = statistics.fmean(accuracies)
        chance_sd = statistics.stdev(accuracies) if len(accuracies) > 1 else 0.0
        upper = chance_mean + 3 * chance_sd
        gold = gold_floor(ctx)

        causes = {
            # The cause: a random gold that failed to *execute* would be
            # measuring cross-database mismatch, not chance. Drawing within the
            # database is supposed to keep every prediction runnable.
            "random_gold_mostly_executed": execute_failures < len(ctx.items) * ctx.seeds * 0.25,
            "random_gold_parses": parse_failures == 0,
            "gold_clears_the_band": gold.accuracy > upper,
        }
        sanity = bands.get("chance_max")
        if sanity is not None:
            causes["chance_below_sanity_maximum"] = sanity.check(chance_mean)

        twin_ok = causes["gold_clears_the_band"]
        passed = twin_ok and all(causes.values())
        return ControlOutcome(
            kind=self.kind,
            status="PASSED" if passed else "FAILED",
            observed={
                "chance_mean": chance_mean,
                "chance_sd": chance_sd,
                "chance_max_seed": max(accuracies),
                "gold_accuracy": gold.accuracy,
            },
            expected=band_map(ctx, self.kind),
            twin_ok=twin_ok,
            cause_assertions=causes,
            detail=(
                f"chance over {ctx.seeds} seeds: mean {chance_mean:.6f} sd {chance_sd:.6f}; "
                f"band upper edge {upper:.6f}; gold {gold.accuracy:.6f}"
                + (
                    f"; gold failed on {list(gold_failures(ctx, gold))[:10]}"
                    if gold.accuracy < 1.0
                    else ""
                )
            ),
        )
