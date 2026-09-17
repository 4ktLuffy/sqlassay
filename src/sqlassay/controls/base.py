"""The control framework, over fireassay's ``ControlOutcome``.

The outcome *shape* is fireassay's own class, imported rather than copied, so
a control outcome from this package and one from ``fireassay`` or
``agent-assay`` are the same object to ``fireassay.admissibility.assess``.
Three repositories, one control vocabulary.

The context is ours, because what a control over a **text-to-SQL** system
needs -- a suite of items, a database per ``db_id``, a gold cache, a predictor
-- has nothing in common with what a control over a retrieval system or an
agent needs.

Two rules inherited verbatim, both load-bearing:

1. **A check that cannot run must never read as a check that passed.**
   ``status`` is ``PASSED`` / ``FAILED`` / ``NOT_RUN``, and ``assess`` treats
   ``NOT_RUN`` exactly like ``FAILED`` unless the caller allowed that kind.

2. **Assert the cause, not the outcome.** A control that checked only that
   "the number dropped" is not evidence; it must also assert *why*. Each
   control below carries named boolean ``cause_assertions``, and a control
   whose number lands in its band for the wrong reason still fails.

And one rule that matters more here than anywhere else: **every control whose
expected result is a collapse carries a writable twin** -- the same
measurement performed where it must succeed, demonstrably succeeding.
``twin_ok=False`` forces ``FAILED`` even when the mutant's numbers land
exactly inside the expected band.

On this harness the twin is unusually cheap and unusually strong: it is the
**gold predictor**, which needs no model and must score exactly 1.0. If the
gold cannot score 1.0, nothing else measured here means anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fireassay.controls.base import ControlOutcome
from fireassay.controls.expected import Band

from sqlassay.engine import Engine
from sqlassay.floors import GoldCache
from sqlassay.models import Item

__all__ = ["Band", "Control", "ControlContext", "ControlOutcome", "build_context"]


@dataclass(frozen=True)
class ControlContext:
    """Everything a control may read, and the only way it may touch a suite."""

    items: tuple[Item, ...]
    databases: Mapping[str, Engine]
    cache: GoldCache
    expected: Mapping[str, Mapping[str, Band]]
    revision: str
    seeds: int = 10


def build_context(
    items: Sequence[Item],
    databases: Mapping[str, Engine],
    expected: Mapping[str, Mapping[str, Band]],
    *,
    revision: str,
    cache: GoldCache | None = None,
    seeds: int = 10,
) -> ControlContext:
    """Assemble a :class:`ControlContext`. A constructor with names, no more."""
    return ControlContext(
        items=tuple(items),
        databases=databases,
        cache=cache or GoldCache(),
        expected=expected,
        revision=revision,
        seeds=seeds,
    )


@runtime_checkable
class Control(Protocol):
    """One negative control: a check the harness must be able to fail."""

    kind: str
    version: str

    def run(self, ctx: ControlContext) -> ControlOutcome:
        """Execute the control and report ``PASSED``/``FAILED``/``NOT_RUN``."""
        ...
