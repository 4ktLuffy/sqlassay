"""Extraction tests.

Extraction sits before the parse gate and is the step most likely to produce a
*false finding about the model*: a fence left unstripped, or a query cut in
half, reads downstream as "the model wrote bad SQL". These tests pin the
wrappers small models actually emit, and pin the refusal to repair anything.
"""

from __future__ import annotations

import pytest

from sqlassay.model.extract import extract_sql


class TestWrappers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("SELECT a FROM t", "SELECT a FROM t"),
            ("```sql\nSELECT a FROM t\n```", "SELECT a FROM t"),
            ("```\nSELECT a FROM t\n```", "SELECT a FROM t"),
            ("Sure! Here is the SQL: SELECT a FROM t", "SELECT a FROM t"),
            ("SELECT a FROM t;", "SELECT a FROM t"),
            ("SELECT a FROM t;\nThis returns all rows.", "SELECT a FROM t"),
            ("  \n SELECT a FROM t \n ", "SELECT a FROM t"),
            ("WITH c AS (SELECT 1 AS x) SELECT x FROM c", "WITH c AS (SELECT 1 AS x) SELECT x FROM c"),
        ],
    )
    def test_strips_what_models_wrap_sql_in(self, raw: str, expected: str) -> None:
        assert extract_sql(raw) == expected


class TestRefusals:
    def test_empty_stays_empty(self) -> None:
        assert extract_sql("") == ""
        assert extract_sql("   \n  ") == ""

    def test_non_sql_passes_through_unchanged(self) -> None:
        # Deliberately NOT emptied. An extraction miss and a model that
        # produced nothing are different defects, and returning "" here would
        # make them indistinguishable in the record.
        assert extract_sql("I cannot answer that.") == "I cannot answer that."

    def test_broken_sql_is_not_repaired(self) -> None:
        # A fixer here would silently improve the model's score, and the
        # report would then be measuring the fixer.
        assert extract_sql("SELECT FROM WHERE") == "SELECT FROM WHERE"

    def test_stacked_statements_keep_only_the_first(self) -> None:
        # The parse gate independently refuses stacked statements, so a miss
        # here can never become a second statement actually running.
        assert extract_sql("SELECT 1; DROP TABLE t") == "SELECT 1"
