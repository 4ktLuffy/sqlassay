"""Gate tests, including the pipeline's NOT_RUN behaviour.

The selftests are pulled into pytest rather than left as a CLI-only command,
because a probe suite that only runs when someone remembers to run it is not
a gate on anything.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from sqlassay.engine import DuckDBDatabase, ExecutionError
from sqlassay.gates import ExecuteGate, ParseGate, ResultSetGate, run_gates
from sqlassay.models import GateStatus, Prediction


@pytest.fixture
def db(tmp_path: Path) -> DuckDBDatabase:
    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
    con.close()
    return DuckDBDatabase(path, timeout_s=10.0)


class TestSelftests:
    """Every gate must demonstrate failure on planted defects."""

    @pytest.mark.parametrize("gate", [ParseGate(), ExecuteGate(), ResultSetGate()])
    def test_probes_fire(self, gate: ParseGate | ExecuteGate | ResultSetGate) -> None:
        ok, detail = gate.selftest()
        assert ok, detail
        assert "probes fired" in detail


class TestParseGate:
    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT a FROM t", GateStatus.PASSED),
            ("WITH c AS (SELECT 1 AS x) SELECT x FROM c", GateStatus.PASSED),
            ("", GateStatus.FAILED),
            ("I'm sorry, I can't help with that.", GateStatus.FAILED),
            ("DROP TABLE t", GateStatus.FAILED),
            ("UPDATE t SET a = 1", GateStatus.FAILED),
            ("SELECT 1; DROP TABLE t", GateStatus.FAILED),
        ],
    )
    def test_verdicts(self, sql: str, expected: GateStatus) -> None:
        assert ParseGate().run(sql).status is expected

    def test_every_failure_carries_evidence(self) -> None:
        for sql in ("", "nonsense ((", "DELETE FROM t"):
            r = ParseGate().run(sql)
            assert r.status is GateStatus.FAILED
            assert r.evidence.strip(), f"no evidence for {sql!r}"


class TestExecuteGate:
    def test_empty_result_is_a_pass_with_rows(self, db: DuckDBDatabase) -> None:
        # The load-bearing case: an empty result set is an answer and must
        # reach the comparison gate as data, not as None.
        verdict, rows = ExecuteGate().run(db, "SELECT a FROM t WHERE a > 99")
        assert verdict.status is GateStatus.PASSED
        assert rows is not None
        assert rows.rows == ()

    def test_mutating_statement_is_refused_before_execution(self, db: DuckDBDatabase) -> None:
        verdict, rows = ExecuteGate().run(db, "DELETE FROM t")
        assert verdict.status is GateStatus.FAILED
        assert verdict.detail["kind"] == "not_read_only"
        assert rows is None

    def test_read_only_connection_is_the_second_defence(self, db: DuckDBDatabase) -> None:
        # Bypassing the statement check entirely, DuckDB itself must still refuse.
        with pytest.raises((ExecutionError, duckdb.Error)):
            db._conn.execute("DELETE FROM t")  # noqa: SLF001


class TestPipeline:
    def test_downstream_gates_are_not_run_not_failed(self, db: DuckDBDatabase) -> None:
        # A prediction that does not parse has not produced a wrong answer.
        # The gates after it must say NOT_RUN, or the accuracy number pools
        # "wrote no SQL" with "wrote the wrong SQL".
        pred = Prediction(item_id="i", sql="this is not sql", raw="this is not sql", arm="full")
        outcome = run_gates(pred, db, [(1,)], "SELECT a FROM t")
        assert [g.status for g in outcome.gates] == [
            GateStatus.FAILED,
            GateStatus.NOT_RUN,
            GateStatus.NOT_RUN,
        ]
        assert outcome.correct is False

    def test_always_three_gates(self, db: DuckDBDatabase) -> None:
        for sql in ("SELECT a FROM t", "DROP TABLE t", "SELECT nope FROM t", ""):
            outcome = run_gates(
                Prediction(item_id="i", sql=sql, raw=sql, arm="full"), db, [(1,), (2,)], "SELECT a FROM t"
            )
            assert len(outcome.gates) == 3, f"{sql!r} produced {len(outcome.gates)} gates"

    def test_correct_requires_all_three_passed(self, db: DuckDBDatabase) -> None:
        pred = Prediction(item_id="i", sql="SELECT a FROM t", raw="", arm="full")
        assert run_gates(pred, db, [(1,), (2,)], "SELECT a FROM t").correct is True
        assert run_gates(pred, db, [(9,)], "SELECT a FROM t").correct is False
