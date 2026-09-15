"""Gate 2: does the query actually run against the database?

The distinction this gate preserves is between *ran and returned nothing* and
*did not run*. An empty result set is an answer -- sometimes the correct one --
and must reach the comparison gate as data. A query that errored or timed out
produced no answer at all, and the two must never arrive at the comparison in
the same shape, because one of them can legitimately match an empty gold and
the other never can.
"""

from __future__ import annotations

from sqlassay.engine import Engine, ExecutionError, ExecutionResult
from sqlassay.models import GateResult, GateStatus

__all__ = ["ExecuteGate"]


class ExecuteGate:
    """Executes a parsed prediction and captures its rows, or why there are none."""

    name = "execute"
    version = "1.0.0"

    def run(self, db: Engine, sql: str) -> tuple[GateResult, ExecutionResult | None]:
        """Execute one prediction.

        Returns the verdict and, on success, the rows. The rows are returned
        beside the result rather than stuffed into ``detail`` because they are
        data for the next gate, not evidence for a reader, and a report that
        inlined a 10,000-row result would be unreadable.
        """
        try:
            result = db.execute(sql)
        except ExecutionError as e:
            return (
                GateResult(
                    gate=self.name,
                    version=self.version,
                    status=GateStatus.FAILED,
                    evidence=f"{e.kind}: {e}",
                    detail={"kind": e.kind},
                ),
                None,
            )
        return (
            GateResult(
                gate=self.name,
                version=self.version,
                status=GateStatus.PASSED,
                evidence=(
                    f"executed in {result.elapsed_s:.3f}s: "
                    f"{len(result.rows)} row(s), {len(result.columns)} column(s)"
                ),
                detail={
                    "kind": "ok",
                    "rows": len(result.rows),
                    "columns": len(result.columns),
                    "elapsed_s": round(result.elapsed_s, 4),
                },
            ),
            result,
        )

    def selftest(self) -> tuple[bool, str]:
        """Probe against a scratch in-memory database.

        Builds a real file-backed database in a temp directory rather than
        mocking, because what this gate is asserting is the behaviour of
        DuckDB under a read-only connection -- which a mock would assert
        nothing about.
        """
        import tempfile
        from pathlib import Path

        import duckdb

        from sqlassay.engine import DuckDBDatabase

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.duckdb"
            con = duckdb.connect(str(path))
            con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
            con.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
            con.close()

            with DuckDBDatabase(path, timeout_s=5.0) as db:
                probes: list[tuple[str, str, GateStatus]] = [
                    ("valid", "SELECT a FROM t ORDER BY a", GateStatus.PASSED),
                    ("empty_is_an_answer", "SELECT a FROM t WHERE a > 99", GateStatus.PASSED),
                    ("bad_column", "SELECT nonexistent FROM t", GateStatus.FAILED),
                    ("bad_table", "SELECT a FROM no_such_table", GateStatus.FAILED),
                    ("mutating_blocked", "DELETE FROM t", GateStatus.FAILED),
                ]
                fired: list[str] = []
                for label, sql, expected in probes:
                    verdict, rows = self.run(db, sql)
                    if verdict.status is not expected:
                        return False, f"probe {label!r} expected {expected.value}, got {verdict.status.value}"
                    # The load-bearing probe: an empty result must arrive as an
                    # empty result set, not as None, or an empty gold could
                    # never be matched by a correct prediction.
                    if label == "empty_is_an_answer" and (rows is None or rows.rows != ()):
                        return False, "probe 'empty_is_an_answer' did not return an empty result set"
                    fired.append(label)
        return True, f"{len(fired)} probes fired: {', '.join(fired)}"
