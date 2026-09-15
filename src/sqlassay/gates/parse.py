"""Gate 1: does the prediction parse, and is it a single read-only statement?

This gate exists to keep two very different failures apart. A model that
emitted prose, or a fragment, or three statements, has not produced a wrong
answer -- it has produced no answer. Scoring that as an execution failure
inflates "the model wrote bad SQL" with cases where the model did not write
SQL at all, and the two have different fixes: one is a prompting problem, the
other is a reasoning problem.
"""

from __future__ import annotations

import sqlglot

from sqlassay.engine import statement_is_read_only
from sqlassay.models import GateResult, GateStatus

__all__ = ["ParseGate"]


class ParseGate:
    """Parses the predicted SQL and admits exactly one read-only statement."""

    name = "parse"
    version = "1.0.0"

    def __init__(self, dialect: str = "duckdb") -> None:
        self.dialect = dialect

    def run(self, sql: str) -> GateResult:
        """Judge one prediction's syntax and statement kind."""
        if not sql.strip():
            return GateResult(
                gate=self.name,
                version=self.version,
                status=GateStatus.FAILED,
                evidence="empty prediction: the model produced no SQL",
                detail={"kind": "empty"},
            )
        try:
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
        except Exception as e:
            return GateResult(
                gate=self.name,
                version=self.version,
                status=GateStatus.FAILED,
                evidence=f"parse error: {type(e).__name__}: {str(e)[:200]}",
                detail={"kind": "parse_error"},
            )
        if parsed is None:
            return GateResult(
                gate=self.name,
                version=self.version,
                status=GateStatus.FAILED,
                evidence="parsed to nothing",
                detail={"kind": "empty_parse"},
            )
        ok, reason = statement_is_read_only(sql, dialect=self.dialect)
        if not ok:
            return GateResult(
                gate=self.name,
                version=self.version,
                status=GateStatus.FAILED,
                evidence=f"rejected: {reason}",
                detail={"kind": "not_read_only", "reason": reason},
            )
        return GateResult(
            gate=self.name,
            version=self.version,
            status=GateStatus.PASSED,
            evidence=f"parsed as {reason}",
            detail={"kind": "ok"},
        )

    def selftest(self) -> tuple[bool, str]:
        """Probe the gate with defects it must reject, and one it must accept."""
        probes: list[tuple[str, str, GateStatus]] = [
            ("empty", "", GateStatus.FAILED),
            ("prose", "Sure! Here is the query you asked for.", GateStatus.FAILED),
            ("fragment", "SELECT FROM WHERE", GateStatus.FAILED),
            ("mutating", "DROP TABLE customers", GateStatus.FAILED),
            ("stacked", "SELECT 1; DROP TABLE customers", GateStatus.FAILED),
            ("valid", "SELECT a FROM t WHERE b > 1", GateStatus.PASSED),
        ]
        fired: list[str] = []
        for label, sql, expected in probes:
            got = self.run(sql).status
            if got is not expected:
                return False, f"probe {label!r} expected {expected.value}, got {got.value}"
            fired.append(label)
        return True, f"{len(fired)} probes fired: {', '.join(fired)}"
