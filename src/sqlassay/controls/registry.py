"""The control registry, and the order they run in.

Ordered cheapest-and-most-fundamental first. ``gold_predictor`` leads because
it is every other control's twin: if the answer key cannot score itself, the
rest measure nothing and there is no point spending minutes finding out.
"""

from __future__ import annotations

from sqlassay.controls.base import Control
from sqlassay.controls.gold_predictor import GoldPredictorControl
from sqlassay.controls.null_predictor import NullPredictorControl
from sqlassay.controls.random_gold import RandomGoldControl
from sqlassay.controls.shuffled_gold import ShuffledGoldControl

__all__ = ["CONTROLS", "all_controls"]

CONTROLS: tuple[type, ...] = (
    GoldPredictorControl,
    NullPredictorControl,
    ShuffledGoldControl,
    RandomGoldControl,   # last: it runs the suite once per seed
)


def all_controls() -> tuple[Control, ...]:
    """One instance of every control, in run order."""
    return tuple(cls() for cls in CONTROLS)
