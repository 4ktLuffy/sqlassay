"""BIRD dev loader.

BIRD ships 1,534 questions over 11 SQLite databases, with a difficulty label
per item and an "evidence" hint that its own protocol supplies to the model.

**The databases are used in place, as SQLite.** They are not converted to
DuckDB, and that is the single most important decision in this module: BIRD's
gold SQL is written in SQLite's dialect, and measured on this machine 5 of 80
sampled golds that run correctly under SQLite fail under DuckDB (``IIF()``,
and bare columns outside ``GROUP BY``). Converting would have made the oracle
report roughly 6% of BIRD's answer keys as broken -- a specific, confident,
publishable falsehood caused entirely by the engine. See ``engine.py``.

Nothing here is copied into the repository. The download is a build input with
a recorded checksum, not an artifact.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlassay.engine import DEFAULT_TIMEOUT_S, Engine, SQLiteDatabase
from sqlassay.models import Item

__all__ = ["BIRD_DEFAULT_ROOT", "BIRD_REVISIONS", "BirdSuite", "load_bird_dev"]

BIRD_DEFAULT_ROOT = Path.home() / "AI Engineering" / "corpus" / "bird" / "extracted" / "dev_20240627"
"""Where ``dev.zip`` was extracted. Outside the repository on purpose.

A 346 MB download is a build input, not a committed artifact, and a checkout
that carried it would be unusable. ``dev.zip.sha256`` beside the archive
records exactly which copy produced a given run.
"""


@dataclass(frozen=True)
class BirdSuite:
    """Loaded BIRD items and the databases they are asked against.

    The databases are held open for the life of the suite. Opening a SQLite
    connection per item would dominate the wall clock of a pass over 1,534
    items against 11 files.
    """

    items: tuple[Item, ...]
    databases: dict[str, Engine]
    root: Path

    def close(self) -> None:
        """Close every database connection."""
        for db in self.databases.values():
            db.close()

    def __enter__(self) -> BirdSuite:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stratified(self, per_difficulty: dict[str, int], *, seed: int = 0) -> tuple[Item, ...]:
        """A reproducible sample, ``n`` items per difficulty label.

        Sampling is seeded and sorted before selection so the same request
        returns the same items on any machine. A subset that changed between
        runs would make two runs incomparable while looking identical.

        Asking for more items than a stratum holds takes the whole stratum
        rather than raising -- the caller wants a sample, and a suite that
        refuses to be sampled because ``challenging`` is short is unhelpful.
        The returned tuple says what was actually taken.
        """
        import random

        out: list[Item] = []
        for label, n in sorted(per_difficulty.items()):
            pool = sorted(
                (i for i in self.items if i.difficulty == label), key=lambda i: i.item_id
            )
            rng = random.Random(f"{seed}:{label}")
            out.extend(pool if n >= len(pool) else rng.sample(pool, n))
        return tuple(sorted(out, key=lambda i: i.item_id))


BIRD_REVISIONS = {
    # The June 2024 release, as shipped in dev.zip.
    "2024-06-27": None,
    # The November 2025 quality-reviewed release. Ships questions and SQL only
    # -- the databases are unchanged, so it is read against the same files.
    "2025-11-06": Path.home()
    / "AI Engineering"
    / "corpus"
    / "bird"
    / "dev_20251106.json",
}
"""The two published revisions of BIRD dev.

Both are carried because the interesting measurement is the **difference**.
The 2025-11 release was produced by a quality review whose stated goal was to
"minimize ambiguity"; running the same deterministic detector over both says
how much mechanically-detectable ambiguity that review removed, with no human
judgement anywhere in the comparison.
"""


def load_bird_dev(
    root: Path | str = BIRD_DEFAULT_ROOT,
    *,
    db_ids: Sequence[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    revision: str = "2024-06-27",
) -> BirdSuite:
    """Load BIRD dev from an extracted ``dev.zip``.

    ``db_ids`` restricts to a subset of databases, for a fast pass while
    developing. An item whose database is excluded is dropped here rather than
    left to fail later as a missing-database exclusion, which would inflate
    the oracle report with items nobody intended to check.

    ``revision`` selects which published question/SQL file to read. The
    databases are the same in both, so only the item file changes.
    """
    root = Path(root)
    if revision not in BIRD_REVISIONS:
        raise KeyError(f"unknown revision {revision!r}; known: {sorted(BIRD_REVISIONS)}")
    override = BIRD_REVISIONS[revision]
    dev_json = Path(override) if override is not None else root / "dev.json"
    db_root = root / "dev_databases"
    if not dev_json.exists():
        raise FileNotFoundError(f"no item file at {dev_json} for revision {revision!r}.")
    if not db_root.is_dir():
        raise FileNotFoundError(f"no dev_databases/ at {db_root}. Extract dev_databases.zip there.")

    raw = json.loads(dev_json.read_text(encoding="utf-8"))
    wanted = set(db_ids) if db_ids else None

    databases: dict[str, Engine] = {}
    for path in sorted(db_root.glob("*/*.sqlite")):
        if "__MACOSX" in str(path):
            continue
        db_id = path.parent.name
        if wanted is not None and db_id not in wanted:
            continue
        databases[db_id] = SQLiteDatabase(path, timeout_s=timeout_s)

    items: list[Item] = []
    for entry in raw:
        db_id = str(entry["db_id"])
        if db_id not in databases:
            continue
        items.append(
            Item(
                item_id=f"bird-dev-{int(entry['question_id']):05d}",
                db_id=db_id,
                question=str(entry["question"]).strip(),
                gold_sql=str(entry["SQL"]).strip(),
                # BIRD's "evidence" is external knowledge FOR THE MODEL, so it
                # maps to `hint`, never to `evidence`. See models.Item.
                hint=str(entry.get("evidence") or "").strip(),
                difficulty=str(entry.get("difficulty") or "").strip(),
                evidence=f"BIRD dev {revision} question_id={entry['question_id']}",
            )
        )
    return BirdSuite(items=tuple(items), databases=databases, root=root)
