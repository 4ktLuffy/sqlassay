"""Oracle-integrity tests.

The structure of this file is the argument for the check itself:

* ``TestCleanItemsSurvive`` -- the check does not fire on good items.
* ``TestPlantedDefectsAreCaught`` -- it fires on every planted defect, and
  names the *right* reason for each. A detector that flags everything is as
  useless as one that flags nothing.
* ``TestGoldPredictorCanGoRed`` -- the stage that cannot be planted in an item
  is provoked by mutating the comparison instead. Without this, the gold
  predictor is a check that has never been observed to fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlassay import normalize
from sqlassay.demo import DEMO_DB_ID, build_demo_database, defective_items, demo_items
from sqlassay.engine import DuckDBDatabase
from sqlassay.models import Item
from sqlassay.oracle import OracleKind, check_item, check_suite, limit_boundary_is_tied


@pytest.fixture
def db(tmp_path: Path) -> DuckDBDatabase:
    return DuckDBDatabase(build_demo_database(tmp_path / "demo.duckdb"), timeout_s=10.0)


class TestCleanItemsSurvive:
    def test_every_clean_item_is_admissible(self, db: DuckDBDatabase) -> None:
        report = check_suite(demo_items(), {DEMO_DB_ID: db})
        assert report.excluded == (), [
            (v.item_id, v.kind.value, v.evidence) for v in report.excluded
        ]
        assert len(report.admissible_items) == len(demo_items())

    def test_gold_verified_count_equals_admissible(self, db: DuckDBDatabase) -> None:
        report = check_suite(demo_items(), {DEMO_DB_ID: db})
        assert report.n_gold_verified == len(report.admissible_items)

    def test_a_null_returning_gold_is_admissible(self, db: DuckDBDatabase) -> None:
        item = next(i for i in demo_items() if i.item_id == "demo-009")
        assert check_item(db, item).kind is OracleKind.OK


class TestPlantedDefectsAreCaught:
    @pytest.mark.parametrize(
        ("item_id", "expected"),
        [
            ("broken-ambiguous", OracleKind.AMBIGUOUS),
            ("broken-unparseable", OracleKind.GOLD_UNPARSEABLE),
            ("broken-not-read-only", OracleKind.GOLD_UNPARSEABLE),
            ("broken-does-not-execute", OracleKind.GOLD_FAILED),
            ("broken-nondeterministic", OracleKind.GOLD_NONDETERMINISTIC),
        ],
    )
    def test_each_defect_gets_its_own_reason(
        self, db: DuckDBDatabase, item_id: str, expected: OracleKind
    ) -> None:
        item = next(i for i in defective_items() if i.item_id == item_id)
        verdict = check_item(db, item)
        assert verdict.kind is expected, verdict.evidence
        assert verdict.admissible is False
        assert verdict.evidence.strip()

    def test_detector_flags_exactly_the_planted_defects(self, db: DuckDBDatabase) -> None:
        items = (*demo_items(), *defective_items())
        report = check_suite(items, {DEMO_DB_ID: db})
        excluded = {v.item_id for v in report.excluded}
        assert excluded == {i.item_id for i in defective_items()}
        assert len(report.admissible_items) == len(demo_items())

    def test_missing_database_excludes_rather_than_crashes(self, db: DuckDBDatabase) -> None:
        orphan = Item(item_id="orphan", db_id="no_such_db", question="q", gold_sql="SELECT 1")
        report = check_suite([orphan], {DEMO_DB_ID: db})
        assert report.excluded[0].kind is OracleKind.GOLD_FAILED
        assert "no database loaded" in report.excluded[0].evidence


class TestGoldPredictorCanGoRed:
    """The stage no item can plant: mutate the comparison and it must bite."""

    def test_sql_null_semantics_leaking_in_breaks_reflexivity(
        self, db: DuckDBDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A realistic bug class: implementing comparison with SQL's own
        # semantics, where NULL != NULL. Every gold that returns a NULL then
        # stops matching itself. The gold predictor is the only thing in this
        # harness that would notice.
        real = normalize.normalize_value

        def null_never_equals_null(value: object, **kw: object) -> object:
            if value is None:
                return object()  # a fresh identity every call
            return real(value, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(normalize, "normalize_value", null_never_equals_null)

        item = next(i for i in demo_items() if i.item_id == "demo-009")
        verdict = check_item(db, item)
        # Measured, not assumed: the mutation trips the *determinism* check at
        # step 4, not the gold-predictor at step 5, because two executions of
        # the same gold now produce two different identities for the same NULL.
        # Being caught earlier and more cheaply is the right outcome; asserting
        # a disjunction over both stages would hide which one actually fired.
        assert verdict.kind is OracleKind.GOLD_NONDETERMINISTIC, (
            f"expected the determinism check to fire; got {verdict.kind.value}: {verdict.evidence}"
        )
        assert verdict.admissible is False

    def test_a_comparator_that_never_agrees_is_caught_on_every_item(
        self, db: DuckDBDatabase, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The blunt mutation, to prove the plumbing: if the comparison always
        # disagrees, no item may survive. A check that still passed here would
        # not be reading the comparison at all.
        from sqlassay.gates import resultset as rs

        def never_equal(*a: object, **kw: object) -> normalize.Comparison:
            return normalize.Comparison(
                equal=False,
                reason="planted: comparator always disagrees",
                order_sensitive=False,
                predicted_rows=0,
                gold_rows=0,
            )

        monkeypatch.setattr(rs, "compare_result_sets", never_equal)
        report = check_suite(demo_items(), {DEMO_DB_ID: db})
        assert report.admissible_items == (), "a comparator that never agrees left items admissible"
        # And it must fail at step 5 specifically -- the gold-predictor stage --
        # since the determinism check calls the comparison through a different
        # module and is untouched by this patch. That separation is the point:
        # the two stages fail independently and can be told apart.
        assert report.by_kind == {OracleKind.GOLD_DOES_NOT_MATCH_ITSELF.value: len(demo_items())}


class TestBoundaryTies:
    """A tie the ORDER BY does not break, and one that does not matter.

    The pair is the point. A check that flagged every boundary tie would
    exclude 26 perfectly good BIRD items along with the 39 bad ones; a check
    that flagged none would miss the largest category of under-determined
    answer key in the suite.
    """

    def test_a_tie_with_differing_projections_is_excluded(self, db: DuckDBDatabase) -> None:
        item = next(i for i in defective_items() if i.item_id == "broken-tied-boundary")
        verdict = check_item(db, item)
        assert verdict.kind is OracleKind.AMBIGUOUS_TIE, verdict.evidence
        assert "equally valid" in verdict.evidence

    def test_a_tie_with_identical_projections_is_kept(self, db: DuckDBDatabase) -> None:
        # Both tied rows project to 'North', so the engine's choice cannot
        # change the result set. Excluding this item would be a false positive.
        item = next(i for i in demo_items() if i.item_id == "demo-010")
        assert check_item(db, item).kind is OracleKind.OK

    def test_probe_declines_rather_than_excludes_on_select_star(
        self, db: DuckDBDatabase
    ) -> None:
        # The check may exclude only on positive evidence, never on its own
        # inability to look.
        tied, reason = limit_boundary_is_tied(db, "SELECT * FROM customers ORDER BY region LIMIT 1")
        assert tied is False
        assert "SELECT *" in reason

    def test_no_limit_is_not_probed(self, db: DuckDBDatabase) -> None:
        tied, reason = limit_boundary_is_tied(db, "SELECT name FROM customers ORDER BY region")
        assert tied is False
        assert "no top-level LIMIT" in reason

    def test_a_limit_that_does_not_truncate_is_not_a_tie(self, db: DuckDBDatabase) -> None:
        tied, reason = limit_boundary_is_tied(
            db, "SELECT name FROM customers ORDER BY region LIMIT 99"
        )
        assert tied is False
        assert "does not truncate" in reason
