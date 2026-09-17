"""Benchmark-runner tests, centred on resumability.

Resumability is the property an overnight run depends on, and the only way to
know it works is to simulate the crash. So these tests write a partial
outcomes file and check that a restart skips exactly what is recorded and
nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlassay.bench import arm_summary, completed_keys, iter_plan
from sqlassay.models import Item


def items(n: int) -> list[Item]:
    return [Item(item_id=f"i{k}", db_id="d", question="q", gold_sql="SELECT 1") for k in range(n)]


def write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class TestPlan:
    def test_covers_every_combination_once(self) -> None:
        plan = list(iter_plan(["a", "b"], 3, items(4)))
        assert len(plan) == 2 * 3 * 4
        assert len({k.as_tuple() for k, _ in plan}) == 24

    def test_is_repeat_major_so_a_stopped_run_still_compares_arms(self) -> None:
        # The first slice must contain every arm, not every repeat of one arm:
        # a run killed early should yield a usable paired comparison.
        plan = list(iter_plan(["a", "b"], 3, items(2)))
        first_repeat = [k for k, _ in plan[:4]]
        assert {k.arm for k in first_repeat} == {"a", "b"}
        assert {k.repeat for k in first_repeat} == {0}


class TestResume:
    def test_no_file_means_nothing_done(self, tmp_path: Path) -> None:
        assert completed_keys(tmp_path / "absent.jsonl") == set()

    def test_reads_back_exactly_what_was_recorded(self, tmp_path: Path) -> None:
        p = tmp_path / "o.jsonl"
        write(p, [
            {"arm": "full", "repeat": 0, "item_id": "i0", "correct": True},
            {"arm": "full", "repeat": 0, "item_id": "i1", "correct": False},
            {"arm": "schema", "repeat": 1, "item_id": "i0", "correct": True},
        ])
        assert completed_keys(p) == {
            ("full", 0, "i0"), ("full", 0, "i1"), ("schema", 1, "i0"),
        }

    def test_a_torn_trailing_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        # The usual result of a process killed mid-write. It must cost one
        # item, not the whole night's record.
        p = tmp_path / "o.jsonl"
        p.write_text(
            json.dumps({"arm": "full", "repeat": 0, "item_id": "i0", "correct": True})
            + '\n{"arm": "full", "repeat": 0, "item_i',
            encoding="utf-8",
        )
        assert completed_keys(p) == {("full", 0, "i0")}

    def test_resume_skips_only_what_is_present(self, tmp_path: Path) -> None:
        p = tmp_path / "o.jsonl"
        write(p, [{"arm": "a", "repeat": 0, "item_id": "i0", "correct": True}])
        done = completed_keys(p)
        remaining = [k for k, _ in iter_plan(["a"], 1, items(3)) if k.as_tuple() not in done]
        assert [k.item_id for k in remaining] == ["i1", "i2"]


class TestArmSummary:
    def test_reports_spread_alongside_the_mean(self, tmp_path: Path) -> None:
        p = tmp_path / "o.jsonl"
        write(p, [
            {"arm": "full", "repeat": 0, "item_id": "i0", "correct": True},
            {"arm": "full", "repeat": 0, "item_id": "i1", "correct": True},
            {"arm": "full", "repeat": 1, "item_id": "i0", "correct": True},
            {"arm": "full", "repeat": 1, "item_id": "i1", "correct": False},
        ])
        s = arm_summary(p)["full"]
        assert s["repeats"] == 2
        assert s["accuracy_mean"] == 0.75
        assert s["accuracy_sd"] > 0
        assert (s["accuracy_min"], s["accuracy_max"]) == (0.5, 1.0)

    def test_a_single_repeat_reports_zero_spread_not_an_error(self, tmp_path: Path) -> None:
        # n=1 has no spread to report. It must say so with a number rather
        # than raising, so a one-pass run is still summarisable -- and its
        # sd of 0.0 is visibly the absence of evidence, not a tight result.
        p = tmp_path / "o.jsonl"
        write(p, [{"arm": "full", "repeat": 0, "item_id": "i0", "correct": True}])
        assert arm_summary(p)["full"]["accuracy_sd"] == 0.0

    def test_empty_file_summarises_to_nothing(self, tmp_path: Path) -> None:
        p = tmp_path / "o.jsonl"
        p.write_text("", encoding="utf-8")
        assert arm_summary(p) == {}

    def test_arms_are_kept_separate(self, tmp_path: Path) -> None:
        p = tmp_path / "o.jsonl"
        write(p, [
            {"arm": "full", "repeat": 0, "item_id": "i0", "correct": True},
            {"arm": "schema", "repeat": 0, "item_id": "i0", "correct": False},
        ])
        s = arm_summary(p)
        assert s["full"]["accuracy_mean"] == 1.0
        assert s["schema"]["accuracy_mean"] == 0.0
