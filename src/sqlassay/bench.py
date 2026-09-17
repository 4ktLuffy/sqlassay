"""The measured run: arms x repeats over a suite, resumable.

Three properties this file exists to guarantee, none of them optional:

1. **Resumable.** Every outcome is appended and flushed as it is produced, and
   a restart skips what is already recorded. An overnight pass that dies at
   item 900 must not cost the first 899. The key is
   ``(arm, repeat, item_id)``, so a half-finished arm resumes mid-arm.

2. **Repeats are first-class.** A single pass is one sample. Ollama is not
   deterministic even at temperature 0 -- already measured on this machine and
   recorded in ``../PLAN.md`` -- so *every* configuration gets repeats, and the
   spread is reported beside the mean rather than hidden by it.

3. **Order is interleaved, not blocked.** Items are run arm-by-arm within a
   repeat rather than all repeats of one arm together, so a run stopped early
   yields a usable comparison across arms instead of one complete arm and
   three empty ones.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlassay.engine import Engine
from sqlassay.models import Item, Outcome
from sqlassay.runner import GoldError, ItemRunner

__all__ = ["BenchKey", "arm_summary", "completed_keys", "iter_plan", "record_outcome"]


@dataclass(frozen=True)
class BenchKey:
    """What identifies one unit of work. The resume key."""

    arm: str
    repeat: int
    item_id: str

    def as_tuple(self) -> tuple[str, int, str]:
        return (self.arm, self.repeat, self.item_id)


def iter_plan(
    arms: Sequence[str], repeats: int, items: Sequence[Item]
) -> Iterator[tuple[BenchKey, Item]]:
    """The full run plan, interleaved by repeat then arm.

    Repeat-major so that a run stopped after one repeat has every arm covered
    once -- a usable paired comparison -- rather than one arm covered N times
    and the rest not at all.
    """
    for repeat in range(repeats):
        for arm in arms:
            for item in items:
                yield BenchKey(arm=arm, repeat=repeat, item_id=item.item_id), item


def completed_keys(path: Path) -> set[tuple[str, int, str]]:
    """Every key already recorded in an outcomes file.

    A malformed trailing line -- the usual result of a process killed
    mid-write -- is skipped rather than fatal, so a crash cannot make the
    whole record unreadable.
    """
    if not path.exists():
        return set()
    done: set[tuple[str, int, str]] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                done.add((row["arm"], int(row["repeat"]), row["item_id"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return done


def record_outcome(
    fh: Any, key: BenchKey, outcome: Outcome, elapsed_s: float, config: Mapping[str, Any]
) -> None:
    """Append one outcome and flush.

    Flushed per item rather than buffered, because the resume guarantee is
    only as good as what actually reached the disk before the process died.
    """
    fh.write(
        json.dumps(
            {
                "arm": key.arm,
                "repeat": key.repeat,
                "item_id": key.item_id,
                "correct": outcome.correct,
                "sql": outcome.prediction.sql,
                "elapsed_s": round(elapsed_s, 3),
                "config": dict(config),
                "gates": [
                    {"gate": g.gate_id, "status": g.status.value, "evidence": g.evidence[:300]}
                    for g in outcome.gates
                ],
                "retrieval": outcome.prediction.usage.get("retrieval"),
            }
        )
        + "\n"
    )
    fh.flush()


def run_plan(
    runner: ItemRunner,
    databases: Mapping[str, Engine],
    arms: Sequence[str],
    repeats: int,
    items: Sequence[Item],
    out: Path,
    *,
    config: Mapping[str, Any],
    on_progress: Any = None,
) -> int:
    """Execute the plan, skipping anything already recorded. Returns items run."""
    done = completed_keys(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    by_id = {i.item_id: i for i in items}
    ran = 0
    with out.open("a", encoding="utf-8") as fh:
        for key, item in iter_plan(arms, repeats, items):
            if key.as_tuple() in done:
                continue
            db = databases.get(item.db_id)
            if db is None:
                continue
            started = time.perf_counter()
            try:
                outcome, elapsed = runner.run(by_id[key.item_id], db, key.arm)
            except GoldError:
                # Excluded and counted upstream by the oracle; if one reaches
                # here the suite was not filtered, and skipping is correct --
                # scoring a model against a broken key is the thing this whole
                # harness refuses to do.
                continue
            except Exception as e:  # noqa: BLE001 - one bad item must not end the night
                elapsed = time.perf_counter() - started
                if on_progress:
                    on_progress(key, None, elapsed, repr(e)[:160])
                continue
            record_outcome(fh, key, outcome, elapsed, config)
            ran += 1
            if on_progress:
                on_progress(key, outcome, elapsed, None)
    return ran


def arm_summary(path: Path) -> dict[str, Any]:
    """Per-arm accuracy with the spread across repeats.

    The mean alone is the number a single pass would have produced. The spread
    is what says whether that number means anything, so they are reported
    together and never separately.
    """
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    by_arm: dict[str, dict[int, list[bool]]] = {}
    for row in rows:
        by_arm.setdefault(row["arm"], {}).setdefault(int(row["repeat"]), []).append(bool(row["correct"]))

    out: dict[str, Any] = {}
    for arm, repeats in sorted(by_arm.items()):
        per_repeat = [sum(v) / len(v) for v in repeats.values() if v]
        counts = [len(v) for v in repeats.values()]
        out[arm] = {
            "repeats": len(per_repeat),
            "n_per_repeat": counts,
            "accuracy_mean": statistics.fmean(per_repeat) if per_repeat else 0.0,
            "accuracy_sd": statistics.stdev(per_repeat) if len(per_repeat) > 1 else 0.0,
            "accuracy_min": min(per_repeat) if per_repeat else 0.0,
            "accuracy_max": max(per_repeat) if per_repeat else 0.0,
        }
    return out
