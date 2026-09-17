"""Control tests.

Every control here is a check that something *must* fail. So each one is
tested in both directions: it passes on a healthy suite, and it goes red when
the thing it watches is actually broken. A control with only the first half
is decoration.

The bands are supplied inline rather than read from ``configs/expected.yaml``,
so these tests pin the controls' *logic* and do not silently change meaning
when a band is recalibrated on a new suite.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from fireassay.controls.expected import Band

from sqlassay import normalize
from sqlassay.controls import (
    GoldPredictorControl,
    NullPredictorControl,
    RandomGoldControl,
    ShuffledGoldControl,
    build_context,
)
from sqlassay.engine import DuckDBDatabase
from sqlassay.models import Item

BANDS = {
    "gold_predictor": {"accuracy": Band(eq=1.0)},
    "null_predictor": {"accuracy_max": Band(max=0.10), "min_margin": Band(min=0.5)},
    "shuffled_gold": {"accuracy_max": Band(max=0.20)},
    "random_gold": {"chance_max": Band(max=0.30)},
}


@pytest.fixture
def db(tmp_path: Path) -> DuckDBDatabase:
    path = tmp_path / "c.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'z'),(4,'w'),(5,'v'),(6,'u')")
    con.close()
    return DuckDBDatabase(path, timeout_s=10.0)


@pytest.fixture
def items() -> list[Item]:
    # Distinct answers on purpose: a suite where several items share a result
    # set would put chance high enough to muddy every control below.
    return [
        Item(item_id=f"i{n}", db_id="d", question=f"q{n}", gold_sql=f"SELECT b FROM t WHERE a = {n}")
        for n in range(1, 7)
    ]


def ctx(items: list[Item], db: DuckDBDatabase, seeds: int = 4):
    return build_context(items, {"d": db}, BANDS, revision="test", seeds=seeds)


class TestGoldPredictorControl:
    def test_passes_on_a_healthy_suite(self, items: list[Item], db: DuckDBDatabase) -> None:
        out = GoldPredictorControl().run(ctx(items, db))
        assert out.status == "PASSED", out.detail
        assert out.observed["accuracy"] == 1.0
        assert all(out.cause_assertions.values())

    def test_goes_red_when_the_comparison_is_broken(
        self, items: list[Item], db: DuckDBDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The case only this control can catch: a normalisation rule that a
        # *correct* answer cannot satisfy. Nothing that watches numbers
        # collapse would notice, because this one is supposed to stay at 1.0.
        from sqlassay.gates import resultset as rs

        monkeypatch.setattr(
            rs,
            "compare_result_sets",
            lambda *a, **k: normalize.Comparison(False, "planted", False, 0, 0),
        )
        out = GoldPredictorControl().run(ctx(items, db))
        assert out.status == "FAILED"
        assert out.cause_assertions["no_resultset_failures"] is False

    def test_not_run_on_an_empty_suite(self, db: DuckDBDatabase) -> None:
        out = GoldPredictorControl().run(ctx([], db))
        assert out.status == "NOT_RUN"


class TestNullPredictorControl:
    def test_passes_when_null_sits_at_the_floor(self, items: list[Item], db: DuckDBDatabase) -> None:
        out = NullPredictorControl().run(ctx(items, db))
        assert out.status == "PASSED", out.detail
        assert out.observed["null_accuracy"] == 0.0
        assert out.twin_ok is True

    def test_goes_red_when_null_scores_too_well(self, db: DuckDBDatabase) -> None:
        # A suite whose answers are all a single NULL: SELECT NULL scores 1.0,
        # the margin over gold is 0, and the twin must refuse it.
        nulls = [
            Item(item_id=f"n{n}", db_id="d", question="q", gold_sql="SELECT MAX(a) FROM t WHERE a > 99")
            for n in range(4)
        ]
        out = NullPredictorControl().run(ctx(nulls, db))
        assert out.status == "FAILED"
        assert out.cause_assertions["at_or_below_measured_floor"] is False
        assert out.twin_ok is False

    def test_reports_the_margin_it_measured(self, items: list[Item], db: DuckDBDatabase) -> None:
        out = NullPredictorControl().run(ctx(items, db))
        assert out.observed["margin"] == pytest.approx(1.0)


class TestShuffledGoldControl:
    def test_passes_when_deranging_the_key_collapses_the_score(
        self, items: list[Item], db: DuckDBDatabase
    ) -> None:
        out = ShuffledGoldControl().run(ctx(items, db))
        assert out.status == "PASSED", out.detail
        assert out.observed["accuracy"] == 0.0
        assert out.cause_assertions["resultset_gate_did_the_work"] is True

    def test_the_collapse_must_be_at_the_resultset_gate(
        self, items: list[Item], db: DuckDBDatabase
    ) -> None:
        # Parse and execute counts must be untouched: the permutation changes
        # only what the rows are compared against.
        out = ShuffledGoldControl().run(ctx(items, db))
        assert out.cause_assertions["parse_unchanged"] is True
        assert out.cause_assertions["execute_unchanged"] is True

    def test_goes_red_when_every_item_has_the_same_answer(self, db: DuckDBDatabase) -> None:
        # Deranging a key whose entries are identical changes nothing, so the
        # score does not collapse and the control must refuse the suite.
        same = [
            Item(item_id=f"s{n}", db_id="d", question="q", gold_sql="SELECT COUNT(*) FROM t")
            for n in range(5)
        ]
        out = ShuffledGoldControl().run(ctx(same, db))
        assert out.status == "FAILED"
        assert out.cause_assertions["collapsed_into_band"] is False


class TestRandomGoldControl:
    def test_passes_when_gold_clears_the_measured_band(
        self, items: list[Item], db: DuckDBDatabase
    ) -> None:
        out = RandomGoldControl().run(ctx(items, db))
        assert out.status == "PASSED", out.detail
        assert out.observed["gold_accuracy"] == 1.0
        assert out.observed["chance_mean"] == 0.0

    def test_goes_red_when_chance_equals_gold(self, db: DuckDBDatabase) -> None:
        # Every item has the same answer, so another item's gold is always
        # right: chance reaches 1.0 and gold cannot clear it. A suite like this
        # measures nothing and the control must say so.
        same = [
            Item(item_id=f"s{n}", db_id="d", question="q", gold_sql="SELECT COUNT(*) FROM t")
            for n in range(5)
        ]
        out = RandomGoldControl().run(ctx(same, db))
        assert out.status == "FAILED"
        assert out.cause_assertions["gold_clears_the_band"] is False

    def test_not_run_with_too_few_seeds(self, items: list[Item], db: DuckDBDatabase) -> None:
        out = RandomGoldControl().run(ctx(items, db, seeds=1))
        assert out.status == "NOT_RUN"
        assert "seeds" in out.detail


class TestEveryControlCarriesEvidence:
    @pytest.mark.parametrize(
        "control",
        [GoldPredictorControl(), NullPredictorControl(), ShuffledGoldControl(), RandomGoldControl()],
    )
    def test_detail_is_never_empty(self, control, items: list[Item], db: DuckDBDatabase) -> None:
        assert control.run(ctx(items, db)).detail.strip()
