"""Floor tests.

The point of this file is the **positive control**. A floor measurement that
reports zero is indistinguishable from a floor measurement that is broken, and
on BIRD every trivial predictor lands at or near zero — so without a case
where the floor must be 1.0, the whole module could be silently inert and
every band derived from it would be meaningless.

So each predictor is given a suite it *must* score 1.0 on, and one it must
score 0.0 on. Both directions, every time.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from sqlassay.engine import DuckDBDatabase
from sqlassay.floors import (
    ConstantCountPredictor,
    EmptyPredictor,
    GoldCache,
    NullPredictor,
    RandomGoldPredictor,
    measure_floor,
)
from sqlassay.models import Item


@pytest.fixture
def db(tmp_path: Path) -> DuckDBDatabase:
    path = tmp_path / "f.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y'), (3, 'x')")
    con.close()
    return DuckDBDatabase(path, timeout_s=10.0)


def item(iid: str, gold: str) -> Item:
    return Item(item_id=iid, db_id="d", question="q", gold_sql=gold)


class TestNullPredictor:
    def test_scores_one_where_gold_is_a_single_null(self, db: DuckDBDatabase) -> None:
        # The positive control. Without it, a floor of 0.0 proves nothing.
        items = [item("null-gold", "SELECT NULL"), item("null-gold-2", "SELECT MAX(a) FROM t WHERE a > 99")]
        r = measure_floor(NullPredictor(), items, {"d": db}, GoldCache())
        assert r.accuracy == 1.0, r

    def test_scores_zero_on_ordinary_items(self, db: DuckDBDatabase) -> None:
        r = measure_floor(NullPredictor(), [item("real", "SELECT a FROM t")], {"d": db}, GoldCache())
        assert r.accuracy == 0.0


class TestEmptyPredictor:
    def test_scores_one_where_gold_is_empty(self, db: DuckDBDatabase) -> None:
        r = measure_floor(
            EmptyPredictor(), [item("empty", "SELECT a FROM t WHERE a > 99")], {"d": db}, GoldCache()
        )
        assert r.accuracy == 1.0

    def test_a_single_null_is_not_an_empty_result(self, db: DuckDBDatabase) -> None:
        # One row containing NULL and zero rows are different answers. If these
        # two predictors could match each other's items, their floors would
        # not be separately interpretable.
        r = measure_floor(EmptyPredictor(), [item("null", "SELECT NULL")], {"d": db}, GoldCache())
        assert r.accuracy == 0.0


class TestConstantCountPredictor:
    def test_scores_one_where_gold_counts_the_first_table(self, db: DuckDBDatabase) -> None:
        r = measure_floor(
            ConstantCountPredictor(), [item("count", "SELECT COUNT(*) FROM t")], {"d": db}, GoldCache()
        )
        assert r.accuracy == 1.0


class TestRandomGoldPredictor:
    def test_never_draws_the_item_s_own_gold(self, db: DuckDBDatabase) -> None:
        items = [item(f"i{n}", f"SELECT a FROM t WHERE a = {n}") for n in range(1, 4)]
        p = RandomGoldPredictor(seed=0)
        p.index(items)
        for it in items:
            assert p.predict(it, db) != it.gold_sql

    def test_is_deterministic_for_a_seed(self, db: DuckDBDatabase) -> None:
        items = [item(f"i{n}", f"SELECT a FROM t WHERE a = {n}") for n in range(1, 6)]
        a, b = RandomGoldPredictor(seed=3), RandomGoldPredictor(seed=3)
        a.index(items)
        b.index(items)
        assert [a.predict(i, db) for i in items] == [b.predict(i, db) for i in items]

    def test_different_seeds_differ(self, db: DuckDBDatabase) -> None:
        items = [item(f"i{n}", f"SELECT a FROM t WHERE a = {n}") for n in range(1, 12)]
        a, b = RandomGoldPredictor(seed=0), RandomGoldPredictor(seed=1)
        a.index(items)
        b.index(items)
        assert [a.predict(i, db) for i in items] != [b.predict(i, db) for i in items]

    def test_draws_only_from_the_same_database(self, db: DuckDBDatabase) -> None:
        # A gold from another database would fail at the execute gate and the
        # "floor" would really be measuring schema mismatch.
        here = [item("a", "SELECT a FROM t"), item("b", "SELECT b FROM t")]
        there = [Item(item_id="x", db_id="other", question="q", gold_sql="SELECT z FROM elsewhere")]
        p = RandomGoldPredictor(seed=0)
        p.index([*here, *there])
        assert p.predict(here[0], db) == "SELECT b FROM t"


class TestGoldCache:
    def test_executes_each_gold_once(self, db: DuckDBDatabase) -> None:
        cache = GoldCache()
        it = item("i", "SELECT a FROM t")
        first = cache.rows_for(db, it)
        assert cache.rows_for(db, it) is first
        assert len(cache) == 1

    def test_unexecutable_gold_is_cached_as_none(self, db: DuckDBDatabase) -> None:
        # Recorded rather than raised, so every predictor skips the same items
        # and their floors stay comparable with each other.
        assert GoldCache().rows_for(db, item("bad", "SELECT nope FROM t")) is None
