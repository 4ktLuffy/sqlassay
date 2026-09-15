"""A small demo database and item set, so the harness is runnable before a
benchmark has been downloaded.

This is a **fixture, not a benchmark.** Eight items cannot measure a model,
and any number produced from it is a smoke test. It exists so the pipeline
can be exercised end to end, and so the three retrieval indexes have
something to be built from on a machine with no network.

The items are chosen to exercise the cases the gates exist for: an ordered
question, a question whose answer is legitimately empty, an aggregate with a
float result, and a question whose entity is spelled differently in the data
than in the question -- which is the one the value index is for.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from sqlassay.models import Item

__all__ = ["DEMO_DB_ID", "build_demo_database", "defective_items", "demo_items"]

DEMO_DB_ID = "demo_sales"

_DDL = """
CREATE TABLE customers (
    customer_id INTEGER, name VARCHAR, region VARCHAR, tier VARCHAR
);
CREATE TABLE orders (
    order_id INTEGER, customer_id INTEGER, amount DECIMAL(10,2), status VARCHAR, placed_on DATE
);
INSERT INTO customers VALUES
    (1, 'ACME CORPORATION INC', 'North', 'gold'),
    (2, 'Globex PLC',           'North', 'silver'),
    (3, 'Initech LLC',          'South', 'gold'),
    (4, 'Umbrella Group',       'South', 'bronze');
INSERT INTO orders VALUES
    (10, 1, 1200.50, 'shipped',   '2026-01-15'),
    (11, 1,  300.00, 'cancelled', '2026-02-01'),
    (12, 2,  875.25, 'shipped',   '2026-02-11'),
    (13, 3, 2400.00, 'shipped',   '2026-03-02'),
    (14, 3,  150.75, 'pending',   '2026-03-19'),
    (15, 4,  620.00, 'shipped',   '2026-04-07');
"""


def build_demo_database(path: Path | str) -> Path:
    """Create the demo DuckDB file, replacing any existing one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    con = duckdb.connect(str(p))
    con.execute(_DDL)
    con.close()
    return p


def demo_items() -> tuple[Item, ...]:
    """Eight items. A smoke test, not a measurement."""
    return (
        Item(
            item_id="demo-001",
            db_id=DEMO_DB_ID,
            question="How many customers are there?",
            gold_sql="SELECT COUNT(*) FROM customers",
            evidence="scalar aggregate, no ordering",
        ),
        Item(
            item_id="demo-002",
            db_id=DEMO_DB_ID,
            question="List the regions of all customers.",
            gold_sql="SELECT region FROM customers",
            evidence="duplicates are data: 4 rows, not 2 distinct regions",
        ),
        Item(
            item_id="demo-003",
            db_id=DEMO_DB_ID,
            question="List customer names ordered by name descending.",
            gold_sql="SELECT name FROM customers ORDER BY name DESC",
            evidence="order is significant; the resultset gate compares positionally",
        ),
        Item(
            item_id="demo-004",
            db_id=DEMO_DB_ID,
            question="What is the total amount of all shipped orders?",
            gold_sql="SELECT SUM(amount) FROM orders WHERE status = 'shipped'",
            evidence="DECIMAL sum; exercises numeric normalisation",
        ),
        Item(
            item_id="demo-005",
            db_id=DEMO_DB_ID,
            question="Which orders are in the 'refunded' status?",
            gold_sql="SELECT order_id FROM orders WHERE status = 'refunded'",
            evidence="the correct answer is an empty result set",
        ),
        Item(
            item_id="demo-006",
            db_id=DEMO_DB_ID,
            question="How many orders did Acme place?",
            gold_sql=(
                "SELECT COUNT(*) FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
                "WHERE c.name = 'ACME CORPORATION INC'"
            ),
            evidence="value linking: the question says Acme, the data says ACME CORPORATION INC",
        ),
        Item(
            item_id="demo-007",
            db_id=DEMO_DB_ID,
            question="What is the average order amount per region, ordered by region?",
            gold_sql=(
                "SELECT c.region, AVG(o.amount) FROM orders o "
                "JOIN customers c ON c.customer_id = o.customer_id "
                "GROUP BY c.region ORDER BY c.region"
            ),
            evidence="join, group, float average, explicit ordering",
        ),
        Item(
            item_id="demo-010",
            db_id=DEMO_DB_ID,
            question="What is the earliest region alphabetically?",
            gold_sql="SELECT region FROM customers ORDER BY region LIMIT 1",
            evidence=(
                "the ORDER BY key ties across the LIMIT boundary, but both tied rows "
                "project to 'North' -- harmless, and must NOT be excluded"
            ),
        ),
        Item(
            item_id="demo-009",
            db_id=DEMO_DB_ID,
            question="What is the largest refunded order amount?",
            gold_sql="SELECT MAX(amount) FROM orders WHERE status = 'refunded'",
            evidence="gold returns a single NULL; a comparator using SQL's NULL != NULL breaks here",
        ),
        Item(
            item_id="demo-008",
            db_id=DEMO_DB_ID,
            question="Name the gold-tier customers in the South region.",
            gold_sql="SELECT name FROM customers WHERE tier = 'gold' AND region = 'South'",
            evidence="two-predicate filter returning a single row",
        ),
    )


def defective_items() -> tuple[Item, ...]:
    """Four items with **deliberately broken answer keys**.

    These exist to prove the oracle-integrity check can go red. A detector
    whose only trial is on data where it finds nothing has not been shown to
    work -- so the check is run against these before it is ever run against a
    real benchmark, and it must flag exactly these four and none of the clean
    items.

    One per failure mode in ``sqlassay.oracle.OracleKind``, except
    ``GOLD_DOES_NOT_MATCH_ITSELF``, which cannot be planted in an *item*: it
    is a defect in the harness, not in a benchmark, and is provoked instead by
    mutating the comparison (see ``tests/test_oracle.py``).
    """
    return (
        Item(
            item_id="broken-ambiguous",
            db_id=DEMO_DB_ID,
            question="Show me two customers.",
            gold_sql="SELECT name FROM customers LIMIT 2",
            evidence="LIMIT with no ORDER BY: the engine may return any two rows",
        ),
        Item(
            item_id="broken-unparseable",
            db_id=DEMO_DB_ID,
            question="How many customers are there?",
            gold_sql="SELCT COUNT(*) FROM customers",
            evidence="typo in the gold: SELCT",
        ),
        Item(
            item_id="broken-not-read-only",
            db_id=DEMO_DB_ID,
            question="Remove the cancelled orders.",
            gold_sql="DELETE FROM orders WHERE status = 'cancelled'",
            evidence="gold is a mutation, not a query",
        ),
        Item(
            item_id="broken-does-not-execute",
            db_id=DEMO_DB_ID,
            question="What is each customer's credit limit?",
            gold_sql="SELECT credit_limit FROM customers",
            evidence="gold references a column that does not exist",
        ),
        Item(
            item_id="broken-tied-boundary",
            db_id=DEMO_DB_ID,
            question="Name a customer from the earliest region alphabetically.",
            gold_sql="SELECT name FROM customers ORDER BY region LIMIT 1",
            evidence=(
                "two customers are in 'North'; ORDER BY region does not break the tie, so "
                "the gold's answer is one of two equally valid names"
            ),
        ),
        Item(
            item_id="broken-nondeterministic",
            db_id=DEMO_DB_ID,
            question="Give me a random score.",
            gold_sql="SELECT random() AS score",
            evidence="two executions of this gold return different rows",
        ),
    )
