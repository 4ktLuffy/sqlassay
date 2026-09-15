"""The record types. Deliberately small, and deliberately explicit about the
difference between *did not run* and *ran and failed*.

The distinction this module exists to preserve: a prediction that never
reached the database because it would not parse is not a wrong answer, it is
an unattempted one, and averaging the two together is how a text-to-SQL
number stops meaning anything. Every stage records its own verdict, and the
final score is computed from the conjunction rather than from a single
boolean the pipeline collapsed early.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GateStatus(StrEnum):
    """The only three things a gate may say.

    ``NOT_RUN`` is not a synonym for ``FAILED``. A gate that never executed
    because an upstream gate stopped the pipeline has measured nothing, and a
    report that shows it as a failure is claiming evidence it does not have.
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict on one prediction, with the evidence behind it.

    ``evidence`` is the load-bearing field. The status is the claim; the
    evidence is what lets a reader check the claim without re-running the
    pipeline. A gate that returns ``FAILED`` with an empty evidence string is
    a bug, and ``tests/test_gates.py`` pins that.
    """

    gate: str
    version: str
    status: GateStatus
    evidence: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def gate_id(self) -> str:
        """The ``name@version`` string stored with every result.

        Versioned for the same reason scorers are in ``agent-assay``: a
        result-set comparison under ``resultset@1.0.0`` and one under
        ``resultset@1.1.0`` are not the same measurement, and a report that
        pools them is comparing across a changed instrument.
        """
        return f"{self.gate}@{self.version}"


@dataclass(frozen=True)
class Item:
    """One benchmark question: the database it is asked against, and the gold SQL.

    ``gold_sql`` is the answer key and is executed, never compared as text.
    Two spellings of the same query are the same answer, and string equality
    would score a correct model wrong -- which is a defect in the harness
    being reported as a finding about the model.

    ``evidence`` and ``hint`` are **not** the same thing, and the distinction
    matters because BIRD uses the word "evidence" for the second one:

    * ``evidence`` is a note *about the item*, for a human reading a report.
      It never reaches a model.
    * ``hint`` is external knowledge *given to the model* in the prompt --
      BIRD's ``evidence`` field, e.g. "eligible free rate = Free Meal Count /
      Enrollment". Many BIRD items are unanswerable without it, and BIRD's own
      protocol supplies it, so withholding it would measure something other
      than what the benchmark's published numbers measure.

    Mapping BIRD's ``evidence`` onto ``evidence`` here would silently feed a
    field meant for readers into the prompt, or silently withhold one the
    benchmark assumes is present. Either would make our number incomparable
    with everyone else's for a reason nobody could see.

    ``difficulty`` is the suite's own stratification label where it has one
    (BIRD: ``simple``/``moderate``/``challenging``), used to sample a subset
    with headroom above the floor rather than one dominated by items no small
    model answers.
    """

    item_id: str
    db_id: str
    question: str
    gold_sql: str
    evidence: str = ""
    hint: str = ""
    difficulty: str = ""


@dataclass(frozen=True)
class Prediction:
    """What the model produced for one item, before any gate has judged it.

    ``raw`` is kept alongside ``sql`` because the extraction step is itself a
    thing that can be wrong: when a gate fails it must be possible to tell a
    model that wrote bad SQL from an extractor that cut good SQL in half.
    """

    item_id: str
    sql: str
    raw: str
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    arm: str = ""


@dataclass(frozen=True)
class Outcome:
    """The full record for one item: prediction plus every gate's verdict.

    ``correct`` is a conjunction over the three gates rather than a field any
    single stage can set, so there is no code path that marks an item correct
    without the result sets having actually matched.
    """

    item_id: str
    arm: str
    prediction: Prediction
    gates: tuple[GateResult, ...]

    @property
    def correct(self) -> bool:
        """True only if every gate ran and every gate passed."""
        return len(self.gates) == 3 and all(g.status is GateStatus.PASSED for g in self.gates)

    def gate(self, name: str) -> GateResult | None:
        """The named gate's result, or ``None`` if it is not in this record."""
        return next((g for g in self.gates if g.gate == name), None)
