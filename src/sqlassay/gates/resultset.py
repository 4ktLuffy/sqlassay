"""Gate 3: does the prediction's result set match the gold's?

This gate does not compare SQL text. Two spellings of the same query are the
same answer, and text comparison would score a correct model wrong -- a defect
in the harness reported as a finding about the model.

The ordering contract is read off the **gold** query, never the prediction.
If it were read off the prediction, a model could make its own answer
order-insensitive simply by omitting the ``ORDER BY`` the question asked for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlassay.models import GateResult, GateStatus
from sqlassay.normalize import DEFAULT_FLOAT_DECIMALS, compare_result_sets, order_is_significant

__all__ = ["ResultSetGate"]


class ResultSetGate:
    """Compares predicted rows against gold rows under the normalisation rules."""

    name = "resultset"
    version = "1.0.0"

    def __init__(self, *, decimals: int = DEFAULT_FLOAT_DECIMALS, dialect: str = "duckdb") -> None:
        self.decimals = decimals
        self.dialect = dialect

    def run(
        self,
        predicted: Sequence[Sequence[Any]],
        gold: Sequence[Sequence[Any]],
        *,
        gold_sql: str,
    ) -> GateResult:
        """Judge one prediction's rows against the gold rows."""
        order_sensitive = order_is_significant(gold_sql, dialect=self.dialect)
        c = compare_result_sets(
            predicted, gold, order_sensitive=order_sensitive, decimals=self.decimals
        )
        return GateResult(
            gate=self.name,
            version=self.version,
            status=GateStatus.PASSED if c.equal else GateStatus.FAILED,
            evidence=c.reason,
            detail={
                "order_sensitive": order_sensitive,
                "decimals": self.decimals,
                "predicted_rows": c.predicted_rows,
                "gold_rows": c.gold_rows,
            },
        )

    def selftest(self) -> tuple[bool, str]:
        """Probe the comparison with the four ways it is usually wrong."""
        probes: list[tuple[str, list[tuple[Any, ...]], list[tuple[Any, ...]], str, GateStatus]] = [
            ("identical", [(1,), (2,)], [(1,), (2,)], "SELECT a FROM t", GateStatus.PASSED),
            # Order must be ignored when gold does not order.
            ("reordered_unordered_gold", [(2,), (1,)], [(1,), (2,)], "SELECT a FROM t", GateStatus.PASSED),
            # ...and must bite when it does.
            (
                "reordered_ordered_gold",
                [(2,), (1,)],
                [(1,), (2,)],
                "SELECT a FROM t ORDER BY a",
                GateStatus.FAILED,
            ),
            # A set-based comparator passes this. It must not.
            ("duplicates_matter", [("x",), ("x",)], [("x",)], "SELECT r FROM t", GateStatus.FAILED),
            # The single most important negative: returning nothing is not correct.
            ("empty_is_not_correct", [], [(1,)], "SELECT a FROM t", GateStatus.FAILED),
            # ...but an empty gold legitimately matches an empty prediction.
            ("empty_matches_empty", [], [], "SELECT a FROM t WHERE 1=0", GateStatus.PASSED),
            ("arity_differs", [(1, 2)], [(1,)], "SELECT a FROM t", GateStatus.FAILED),
        ]
        fired: list[str] = []
        for label, pred, gold, gold_sql, expected in probes:
            got = self.run(pred, gold, gold_sql=gold_sql).status
            if got is not expected:
                return False, f"probe {label!r} expected {expected.value}, got {got.value}"
            fired.append(label)
        return True, f"{len(fired)} probes fired: {', '.join(fired)}"
