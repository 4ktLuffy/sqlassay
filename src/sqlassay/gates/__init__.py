"""The three gates, and the pipeline that runs them in order.

The pipeline short-circuits: when a gate fails, the gates after it record
``NOT_RUN`` with the reason rather than ``FAILED``. This is the difference
between "we checked and it was wrong" and "we never checked", and collapsing
them is how a text-to-SQL accuracy number stops being interpretable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlassay.engine import Engine
from sqlassay.gates.base import Gate, skipped
from sqlassay.gates.execute import ExecuteGate
from sqlassay.gates.parse import ParseGate
from sqlassay.gates.resultset import ResultSetGate
from sqlassay.models import GateResult, GateStatus, Outcome, Prediction

__all__ = ["ExecuteGate", "Gate", "ParseGate", "ResultSetGate", "run_gates"]


def run_gates(
    prediction: Prediction,
    db: Engine,
    gold_rows: Sequence[Sequence[Any]],
    gold_sql: str,
    *,
    parse: ParseGate | None = None,
    execute: ExecuteGate | None = None,
    resultset: ResultSetGate | None = None,
) -> Outcome:
    """Run parse -> execute -> resultset over one prediction.

    Always returns exactly three gate results, in order, whatever happened.
    A record with a missing gate would be a hole that downstream aggregation
    could not distinguish from a pass.
    """
    # The dialect comes from the database, never from a default. A gate
    # parsing SQLite gold as DuckDB would reject valid queries and the failure
    # would be recorded against the model -- see engine.py on the 6% of BIRD
    # golds this exact mistake would have condemned.
    parse = parse or ParseGate(dialect=db.dialect)
    execute = execute or ExecuteGate()
    resultset = resultset or ResultSetGate(dialect=db.dialect)

    results: list[GateResult] = []

    p = parse.run(prediction.sql)
    results.append(p)
    if p.status is not GateStatus.PASSED:
        results.append(skipped(execute.name, execute.version, f"{parse.name} failed"))
        results.append(skipped(resultset.name, resultset.version, f"{parse.name} failed"))
        return Outcome(prediction.item_id, prediction.arm, prediction, tuple(results))

    e, rows = execute.run(db, prediction.sql)
    results.append(e)
    if e.status is not GateStatus.PASSED or rows is None:
        results.append(skipped(resultset.name, resultset.version, f"{execute.name} failed"))
        return Outcome(prediction.item_id, prediction.arm, prediction, tuple(results))

    results.append(resultset.run(rows.rows, gold_rows, gold_sql=gold_sql))
    return Outcome(prediction.item_id, prediction.arm, prediction, tuple(results))
