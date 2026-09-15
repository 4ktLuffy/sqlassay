"""Database engines: read-only, time-bounded, and never trusting the model.

**Why there is more than one engine.**

A benchmark's gold SQL is written in a dialect, and running it in a different
one manufactures findings. Measured on BIRD dev, 2026-09-15: of 80 sampled
gold queries that execute correctly under SQLite, **5 fail under DuckDB** --
``IIF()``, which DuckDB does not have, and SQLite's tolerance of selecting a
bare column that is not in the ``GROUP BY``, which DuckDB rejects as the
standard requires.

Had the oracle checked BIRD's answer keys with DuckDB, it would have reported
roughly 6% of them as broken. That number would have been about the *engine*,
not about the benchmark, and it would have been wrong in the most damaging
possible way: a confident, specific, publishable falsehood.

So each suite runs on the engine its gold was written for, and the dialect
travels with the database rather than being a global setting.

**Two independent defences against the model's SQL**, in both engines:

1. The connection is opened **read-only**, enforced by the database.
2. Only a single ``SELECT``/``WITH`` is admitted. A read-only connection
   already refuses a ``DROP``, but as a *runtime error* -- which would be
   recorded as "the model wrote SQL that failed", burying the fact that it
   tried to mutate. Those are different findings.

A per-query deadline matters because a model's accidental cross join does not
error, it simply runs until the machine gives up, and one such item would
stall an overnight pass. Neither engine has a statement timeout, so the query
runs on a worker thread and is stopped with ``interrupt()``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import duckdb
import sqlglot
from sqlglot import exp

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DuckDBDatabase",
    "Engine",
    "ExecutionError",
    "ExecutionResult",
    "SQLiteDatabase",
    "open_database",
    "statement_is_read_only",
]

DEFAULT_TIMEOUT_S = 30.0
"""Seconds a single query may run before it is interrupted.

Sized for an overnight pass: at 30s a hundred pathological items cost under an
hour rather than stalling indefinitely. A query stopped by this deadline is
recorded as ``timeout``, never as an empty result -- an empty result set is an
*answer*, and a query that never finished did not produce one.
"""

_READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)


class ExecutionError(RuntimeError):
    """Raised when a query cannot be executed. Carries a ``kind`` for the record."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def statement_is_read_only(sql: str, *, dialect: str = "duckdb") -> tuple[bool, str]:
    """Is this exactly one read-only statement?

    Returns ``(ok, reason)``. The reason is recorded either way, because
    "admitted as read-only" is itself a claim worth auditing.

    A parse failure is *not* judged here -- the parse gate owns that verdict,
    and reporting it twice would let one defect look like two.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except Exception as e:
        return False, f"unparseable: {type(e).__name__}"
    if not statements:
        return False, "no statement"
    if len(statements) > 1:
        return False, f"{len(statements)} statements; exactly one is required"
    root = statements[0]
    if isinstance(root, _READ_ONLY_ROOTS):
        return True, f"read-only {type(root).__name__.lower()}"
    if isinstance(root, exp.With):
        return True, "read-only with"
    return False, f"non-read-only statement: {type(root).__name__.upper()}"


@dataclass(frozen=True)
class ExecutionResult:
    """Rows and column names from one successful query.

    ``columns`` is carried for the report only. It is deliberately not used by
    the comparison, which matches by position -- see ``normalize`` -- so a
    model aliasing ``SUM(x)`` as ``total`` is not scored wrong for it.
    """

    rows: tuple[tuple[Any, ...], ...]
    columns: tuple[str, ...]
    elapsed_s: float


@runtime_checkable
class Engine(Protocol):
    """One read-only database, in one dialect."""

    path: Path
    dialect: str
    timeout_s: float

    def execute(self, sql: str) -> ExecutionResult:
        """Run one read-only query under the deadline."""
        ...

    def schema_rows(self) -> Sequence[tuple[str, str, str]]:
        """``(table, column, type)`` for every column, for the schema index."""
        ...

    def close(self) -> None:
        """Close the underlying connection."""
        ...


class _BaseDatabase:
    """Shared deadline and read-only admission. Subclasses supply the driver.

    The threading dance lives here rather than in each engine because getting
    it subtly different in two places is how one engine ends up silently
    without a deadline.
    """

    path: Path
    dialect: str
    timeout_s: float

    def _run(self, sql: str) -> tuple[tuple[tuple[Any, ...], ...], tuple[str, ...]]:
        raise NotImplementedError

    def _interrupt(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def execute(self, sql: str) -> ExecutionResult:
        """Run one read-only query under the deadline.

        Raises :class:`ExecutionError` with ``kind`` in ``{not_read_only,
        timeout, error}``. Those three stay separate all the way into the
        report: a model that wrote a ``DELETE``, one whose query was too slow,
        and one with a typo are three different findings.
        """
        ok, reason = statement_is_read_only(sql, dialect=self.dialect)
        if not ok:
            raise ExecutionError("not_read_only", reason)

        box: dict[str, Any] = {}
        started = threading.Event()

        def work() -> None:
            started.set()
            try:
                box["rows"], box["columns"] = self._run(sql)
            except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
                box["error"] = e

        t = threading.Thread(target=work, daemon=True, name="sqlassay-query")
        t0 = time.perf_counter()
        t.start()
        started.wait(timeout=5.0)
        t.join(timeout=self.timeout_s)
        elapsed = time.perf_counter() - t0

        if t.is_alive():
            self._interrupt()
            raise ExecutionError("timeout", f"exceeded {self.timeout_s}s deadline")
        if "error" in box:
            err = box["error"]
            raise ExecutionError("error", f"{type(err).__name__}: {err}")
        return ExecutionResult(
            rows=box.get("rows", ()), columns=box.get("columns", ()), elapsed_s=elapsed
        )


class DuckDBDatabase(_BaseDatabase):
    """A read-only DuckDB database. The engine for suites written in DuckDB SQL."""

    dialect = "duckdb"

    def __init__(self, path: Path | str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        if not self.path.exists():
            raise FileNotFoundError(f"no database at {self.path}")
        self._conn = duckdb.connect(str(self.path), read_only=True)

    def _run(self, sql: str) -> tuple[tuple[tuple[Any, ...], ...], tuple[str, ...]]:
        cur = self._conn.execute(sql)
        return (
            tuple(tuple(r) for r in cur.fetchall()),
            tuple(d[0] for d in (cur.description or ())),
        )

    def _interrupt(self) -> None:
        self._conn.interrupt()

    def close(self) -> None:
        self._conn.close()

    def schema_rows(self) -> Sequence[tuple[str, str, str]]:
        """``(table, column, type)`` for every column."""
        return self._conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()


class SQLiteDatabase(_BaseDatabase):
    """A read-only SQLite database. The engine for BIRD, whose gold is SQLite SQL.

    Opened through a ``file:`` URI with ``mode=ro``, which is SQLite's own
    read-only guarantee rather than a promise made in Python. The URI is built
    with :meth:`Path.as_uri`, because this machine's corpus path contains a
    space and hand-built URIs fail on it -- a real failure hit while writing
    this class, not a hypothetical.
    """

    dialect = "sqlite"

    def __init__(self, path: Path | str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        if not self.path.exists():
            raise FileNotFoundError(f"no database at {self.path}")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        # check_same_thread=False because queries run on a worker thread for
        # the deadline; only one query is ever in flight per instance.
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        # SQLite raises on malformed text in some BIRD databases otherwise.
        self._conn.text_factory = lambda b: b.decode("utf-8", errors="replace")

    def _run(self, sql: str) -> tuple[tuple[tuple[Any, ...], ...], tuple[str, ...]]:
        cur = self._conn.execute(sql)
        rows = tuple(tuple(r) for r in cur.fetchall())
        cols = tuple(d[0] for d in (cur.description or ()))
        cur.close()
        return rows, cols

    def _interrupt(self) -> None:
        self._conn.interrupt()

    def close(self) -> None:
        self._conn.close()

    def schema_rows(self) -> Sequence[tuple[str, str, str]]:
        """``(table, column, type)`` for every column, via ``PRAGMA table_info``."""
        out: list[tuple[str, str, str]] = []
        tables = [
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            for row in self._conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                out.append((table, str(row[1]), str(row[2] or "")))
        return out


def open_database(path: Path | str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Engine:
    """Open a database, choosing the engine from the file extension.

    Extension-based rather than content-sniffing because the extension is what
    the suite loader already knows, and a wrong guess here would silently pick
    the wrong dialect -- the exact failure this module exists to prevent.
    """
    p = Path(path)
    if p.suffix in {".sqlite", ".sqlite3", ".db"}:
        return SQLiteDatabase(p, timeout_s=timeout_s)
    return DuckDBDatabase(p, timeout_s=timeout_s)
