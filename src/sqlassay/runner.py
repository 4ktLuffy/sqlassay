"""One item, end to end: retrieve, generate, extract, then gate.

The order matters and is fixed here rather than at the call site. Generation
happens before any gate sees the prediction, and no gate can feed back into
generation -- there is no repair loop. A harness that lets the model retry
after seeing a gate's complaint is measuring the loop, not the model, and the
number it produces cannot be compared with anyone else's.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from sqlassay.engine import Engine, ExecutionError
from sqlassay.gates import ExecuteGate, ParseGate, ResultSetGate, run_gates
from sqlassay.model.extract import extract_sql
from sqlassay.model.ollama import OllamaChat
from sqlassay.model.prompt import SYSTEM_PROMPT, build_user_prompt
from sqlassay.models import Item, Outcome, Prediction
from sqlassay.retrieval.indexes import Retriever

__all__ = ["GoldError", "ItemRunner", "degenerate_components", "gold_rows_for", "summarise"]


class GoldError(RuntimeError):
    """The gold query itself could not be executed.

    Raised rather than scored. If the answer key does not run, nothing about
    the model can be concluded from that item, and recording it as a model
    failure would be a harness defect reported as a finding.
    """


def gold_rows_for(db: Engine, item: Item) -> tuple[tuple[object, ...], ...]:
    """Execute the gold SQL, or raise :class:`GoldError`."""
    try:
        return db.execute(item.gold_sql).rows
    except ExecutionError as e:
        raise GoldError(f"{item.item_id}: gold SQL did not execute: {e}") from e


@dataclass
class ItemRunner:
    """Runs one arm over one item."""

    chat: OllamaChat
    retriever: Retriever
    parse: ParseGate
    execute: ExecuteGate
    resultset: ResultSetGate

    def run(self, item: Item, db: Engine, arm: str) -> tuple[Outcome, float]:
        """Produce and judge one prediction. Returns the outcome and wall time."""
        t0 = time.perf_counter()
        ctx = self.retriever.context(arm, item.question, db, db_id=item.db_id)
        response = self.chat.complete(SYSTEM_PROMPT, build_user_prompt(item.question, ctx, hint=item.hint))
        sql = extract_sql(response.content)
        prediction = Prediction(
            item_id=item.item_id,
            sql=sql,
            raw=response.content,
            reasoning=response.reasoning,
            usage={**response.usage, "retrieval": ctx.as_record()},
            arm=arm,
        )
        gold = gold_rows_for(db, item)
        outcome = run_gates(
            prediction,
            db,
            gold,
            item.gold_sql,
            parse=self.parse,
            execute=self.execute,
            resultset=self.resultset,
        )
        return outcome, time.perf_counter() - t0


def degenerate_components(outcomes: Sequence[Outcome], arm: str) -> tuple[str, ...]:
    """Retrieval components this arm switched on that never returned anything.

    This is the ``NOT_RUN`` rule applied one level up, at the arm. An arm that
    enables example retrieval against an *empty* example index is not a
    measurement of whether examples help -- it is the previous arm, run again
    under a different name. Printing the two side by side with the same score
    invites exactly the wrong conclusion: that examples were tried and made no
    difference, when they were never tried at all.

    Measured from what retrieval actually returned per item, not from the
    index count, because an index can be non-empty and still return nothing
    for every question in the suite -- which is equally degenerate and equally
    invisible in a count.
    """
    from sqlassay.retrieval.indexes import ARMS

    if arm not in ARMS or not outcomes:
        return ()
    _, wants_examples, wants_values = ARMS[arm]
    records = [o.prediction.usage.get("retrieval") or {} for o in outcomes]
    dead: list[str] = []
    if wants_examples and not any(r.get("example_ids") for r in records):
        dead.append("examples")
    if wants_values and not any(r.get("value_ids") for r in records):
        dead.append("values")
    return tuple(dead)


def summarise(outcomes: Sequence[Outcome], arm: str = "") -> dict[str, object]:
    """Aggregate one arm's outcomes.

    Reports the gate breakdown alongside execution accuracy, never accuracy
    alone. Two arms can reach the same accuracy with completely different
    failure profiles -- one failing to parse, the other returning wrong rows --
    and only the breakdown says which problem to go and fix.

    ``degenerate`` names any retrieval component the arm asked for and never
    received. A non-empty ``degenerate`` makes the arm's score uninterpretable
    as a statement about that component, and the reporting layer must say so
    rather than print the number beside the arms that were genuinely run.
    """
    from sqlassay.models import GateStatus

    n = len(outcomes)
    if n == 0:
        return {"n": 0, "degenerate": []}

    def count(gate: str, status: GateStatus) -> int:
        return sum(1 for o in outcomes if (g := o.gate(gate)) and g.status is status)

    correct = sum(1 for o in outcomes if o.correct)
    return {
        "n": n,
        "degenerate": list(degenerate_components(outcomes, arm)),
        "execution_accuracy": correct / n,
        "correct": correct,
        "parse_failed": count("parse", GateStatus.FAILED),
        "execute_failed": count("execute", GateStatus.FAILED),
        "resultset_failed": count("resultset", GateStatus.FAILED),
        "resultset_not_run": count("resultset", GateStatus.NOT_RUN),
    }
