"""Tests for result-set normalisation.

Half of these are *negative* tests: they pin the cases where the comparison
must say NO. A comparator that only has passing tests has not been shown it
can go red, and on this harness that is the whole point.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sqlassay.normalize import (
    compare_result_sets,
    gold_is_ambiguous,
    gold_is_clock_dependent,
    normalize_value,
    order_is_significant,
)


class TestNormalizeValue:
    def test_numeric_spellings_collapse(self) -> None:
        assert normalize_value(5) == normalize_value(5.0) == normalize_value(Decimal("5.00"))

    def test_bool_is_not_an_int(self) -> None:
        # bool subclasses int in Python; an unguarded numeric branch would
        # make a boolean column compare equal to an integer one.
        assert normalize_value(True) != normalize_value(1)
        assert normalize_value(False) != normalize_value(0)

    def test_null_is_distinct_from_zero_and_empty(self) -> None:
        assert normalize_value(None) is None
        assert normalize_value(None) != normalize_value(0)
        assert normalize_value(None) != normalize_value("")

    def test_char_padding_is_stripped_but_case_is_kept(self) -> None:
        assert normalize_value("Active  ") == "Active"
        assert normalize_value("Active") != normalize_value("active")

    def test_dates_become_iso_strings(self) -> None:
        assert normalize_value(date(2026, 9, 15)) == "2026-09-15"

    def test_float_tolerance_is_applied(self) -> None:
        assert normalize_value(1.00001) == normalize_value(1.00002)
        assert normalize_value(1.0001) != normalize_value(1.0002)

    def test_nested_containers_become_hashable(self) -> None:
        assert hash(normalize_value({"b": 2, "a": 1})) is not None
        # STRUCT field order must not matter.
        assert normalize_value({"a": 1, "b": 2}) == normalize_value({"b": 2, "a": 1})


class TestOrderSignificance:
    def test_top_level_order_by_is_significant(self) -> None:
        assert order_is_significant("SELECT a FROM t ORDER BY a") is True

    def test_order_by_inside_a_subquery_is_not(self) -> None:
        # The classic false positive: a substring check on 'order by' says
        # True here and would then compare positionally against a result
        # whose order the outer query never promised.
        sql = "SELECT x FROM (SELECT a AS x FROM t ORDER BY a) AS s"
        assert order_is_significant(sql) is False

    def test_order_by_inside_a_cte_is_not(self) -> None:
        sql = "WITH c AS (SELECT a FROM t ORDER BY a) SELECT a FROM c"
        assert order_is_significant(sql) is False

    def test_unparseable_sql_does_not_raise(self) -> None:
        assert order_is_significant("this is not sql ((") is False


class TestGoldAmbiguity:
    def test_limit_without_order_by_is_ambiguous(self) -> None:
        ambiguous, reason = gold_is_ambiguous("SELECT a FROM t LIMIT 5")
        assert ambiguous is True
        assert "ORDER BY" in reason

    def test_limit_with_order_by_is_determinate(self) -> None:
        ambiguous, _ = gold_is_ambiguous("SELECT a FROM t ORDER BY a LIMIT 5")
        assert ambiguous is False

    def test_no_limit_is_determinate(self) -> None:
        ambiguous, _ = gold_is_ambiguous("SELECT a FROM t")
        assert ambiguous is False


class TestCompareResultSets:
    def test_identical_unordered_matches(self) -> None:
        c = compare_result_sets([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], order_sensitive=False)
        assert c.equal is True

    def test_same_rows_wrong_order_fails_when_order_matters(self) -> None:
        c = compare_result_sets([(2,), (1,)], [(1,), (2,)], order_sensitive=True)
        assert c.equal is False
        assert "row 0" in c.reason

    def test_duplicates_are_not_collapsed(self) -> None:
        # A set-based comparator calls these equal. They are different answers:
        # one is SELECT region, the other SELECT DISTINCT region.
        c = compare_result_sets([("x",), ("x",)], [("x",)], order_sensitive=False)
        assert c.equal is False

    def test_column_count_mismatch_is_named_as_such(self) -> None:
        c = compare_result_sets([(1, 2)], [(1,)], order_sensitive=False)
        assert c.equal is False
        assert "column count" in c.reason

    def test_row_count_mismatch_is_named_as_such(self) -> None:
        c = compare_result_sets([(1,), (2,)], [(1,)], order_sensitive=False)
        assert c.equal is False
        assert "row count" in c.reason

    def test_both_empty_matches(self) -> None:
        assert compare_result_sets([], [], order_sensitive=False).equal is True

    def test_empty_against_nonempty_fails(self) -> None:
        # The single most important negative case: a model that returns
        # nothing must never score as correct.
        assert compare_result_sets([], [(1,)], order_sensitive=False).equal is False

    def test_null_rows_compare_without_raising(self) -> None:
        # Mixed None/str/float in one column would raise TypeError under a
        # naive sorted() comparison.
        c = compare_result_sets(
            [(None,), ("a",), (1.5,)], [(1.5,), (None,), ("a",)], order_sensitive=False
        )
        assert c.equal is True

    def test_numeric_spellings_match_across_engines(self) -> None:
        c = compare_result_sets([(Decimal("5.00"),)], [(5,)], order_sensitive=False)
        assert c.equal is True

    @pytest.mark.parametrize("order_sensitive", [True, False])
    def test_reason_is_never_empty(self, order_sensitive: bool) -> None:
        c = compare_result_sets([(1,)], [(2,)], order_sensitive=order_sensitive)
        assert c.equal is False
        assert c.reason.strip()


class TestFailureEvidence:
    """The evidence a reader sees on a mismatch must be the data, not internals."""

    def test_unordered_mismatch_reports_the_row_not_the_sort_key(self) -> None:
        c = compare_result_sets([(2.0,)], [(5.0,)], order_sensitive=False)
        assert c.equal is False
        # The row, as it would appear in a result set.
        assert "(5.0,)" in c.reason
        # Not the internal (type-rank, string) key form.
        assert "'5.0000000000'" not in c.reason

    def test_both_sides_are_described_when_both_differ(self) -> None:
        c = compare_result_sets([("x",)], [("y",)], order_sensitive=False)
        assert "in gold not predicted" in c.reason
        assert "predicted not in gold" in c.reason


class TestAmbiguityScoping:
    """A LIMIT is judged against its own select's ORDER BY, not the outermost.

    Regression tests for a detector bug measured on BIRD dev: matching any
    LIMIT in the tree against the top-level ORDER BY flagged 23 determinate
    items, 79% of everything the check reported.
    """

    def test_ordered_subquery_limit_is_determinate(self) -> None:
        sql = "SELECT a FROM t WHERE id = (SELECT id FROM u ORDER BY score DESC LIMIT 1)"
        ambiguous, _ = gold_is_ambiguous(sql)
        assert ambiguous is False

    def test_unordered_subquery_limit_is_ambiguous(self) -> None:
        # Same shape, no ORDER BY in the subquery: genuinely under-determined.
        sql = "SELECT a FROM t WHERE id = (SELECT id FROM u LIMIT 1)"
        ambiguous, reason = gold_is_ambiguous(sql)
        assert ambiguous is True
        assert "subquery" in reason

    def test_outer_limit_still_caught_when_subquery_is_ordered(self) -> None:
        sql = "SELECT a FROM (SELECT a FROM u ORDER BY a LIMIT 5) LIMIT 2"
        ambiguous, _ = gold_is_ambiguous(sql)
        assert ambiguous is True

    def test_ordered_outer_limit_is_determinate(self) -> None:
        assert gold_is_ambiguous("SELECT a FROM t ORDER BY a LIMIT 5")[0] is False


class TestClockDependence:
    """A gold whose answer changes with the date is not an answer key.

    Found the expensive way: four such items made the control suite's twin
    unreproducible forty minutes into a run, after passing a two-execution
    determinism check that had no chance of catching them.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a FROM t WHERE JULIANDAY('now') - JULIANDAY(b) > 100",
            "SELECT a FROM t WHERE julianday('now') > 1",
            "SELECT a FROM t WHERE DATE('now') = b",
            "SELECT a FROM t WHERE b < CURRENT_DATE",
            "SELECT a FROM t WHERE b < current_timestamp",
            "SELECT STRFTIME('%Y', 'now') FROM t",
        ],
    )
    def test_clock_functions_are_flagged(self, sql: str) -> None:
        dependent, reason = gold_is_clock_dependent(sql)
        assert dependent is True
        assert reason.strip()

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a FROM t",
            # A data literal, not a clock call. Flagging this would exclude a
            # perfectly good item for containing four letters.
            "SELECT a FROM t WHERE status = 'now'",
            "SELECT a FROM t WHERE label LIKE '%now%'",
            # DATE() on a column is fine; only DATE('now') is not.
            "SELECT DATE(birth_date) FROM t",
            "SELECT JULIANDAY(b) - JULIANDAY(c) FROM t",
        ],
    )
    def test_ordinary_sql_is_not_flagged(self, sql: str) -> None:
        assert gold_is_clock_dependent(sql)[0] is False
