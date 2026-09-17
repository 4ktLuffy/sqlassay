"""Shared machinery for the controls."""

from __future__ import annotations

from collections.abc import Sequence

from sqlassay.controls.base import ControlContext
from sqlassay.floors import FloorResult, Predictor, measure_floor
from sqlassay.models import Item

__all__ = ["GoldPredictor", "gold_floor", "run_predictor"]


class GoldPredictor:
    """Proposes the item's own gold SQL.

    The twin for every other control here, and the cheapest strong one
    available on an execution oracle: it needs no model, and it must score
    exactly 1.0. If it does not, the harness is broken and nothing else
    measured against it means anything.
    """

    name = "gold"

    def predict(self, item: Item, db: object) -> str:
        return item.gold_sql


def run_predictor(predictor: Predictor, ctx: ControlContext) -> FloorResult:
    """Measure one predictor over the control context's suite."""
    return measure_floor(predictor, ctx.items, ctx.databases, ctx.cache)


def gold_floor(ctx: ControlContext) -> FloorResult:
    """The twin measurement: the gold predictor over the same suite."""
    return run_predictor(GoldPredictor(), ctx)


def gold_failures(ctx: ControlContext, result: FloorResult) -> tuple[str, ...]:
    """Which items the gold predictor failed to match.

    An aggregate says a twin slipped; it does not say *where*. Naming the
    items is the difference between "the harness is unstable" and a
    reproducible defect someone can go and look at -- and on a suite of 1504
    a single unnamed failure is unfindable.
    """
    matched = set(result.matched_items)
    return tuple(i.item_id for i in ctx.items if i.db_id in ctx.databases and i.item_id not in matched)


def band_map(ctx: ControlContext, kind: str) -> dict[str, object]:
    """The bands for one control kind, as plain JSON for the outcome record."""
    return {name: band.model_dump() for name, band in ctx.expected.get(kind, {}).items()}


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0
