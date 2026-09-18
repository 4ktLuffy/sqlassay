#!/usr/bin/env python
"""Compute the per-class diff between two BIRD revisions, as a committed artifact.

The README's central claim -- that the 2025-11 quality review fixed some
defects and introduced others -- is a statement about *item sets*, not about
totals. Two revisions can carry the same count while sharing no items, so a
pair of totals cannot support the claim and a reader cannot check it from
them.

This derives the sets from the two committed oracle reports and writes them
out, so ``fixed`` / ``survived`` / ``introduced`` are regenerable rather than
asserted, per class and with the item ids kept.

Usage:  python tools/make_revision_diff.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
OLD = REPORTS / "oracle_integrity_bird_dev.json"
NEW = REPORTS / "oracle_integrity_bird_dev_20251106.json"
OUT = REPORTS / "revision_diff.json"


def excluded_by_kind(report: dict) -> dict[str, set[str]]:
    """``{kind: {item_id}}`` for one oracle report."""
    out: dict[str, set[str]] = {}
    for entry in report["excluded"]:
        out.setdefault(entry["kind"], set()).add(entry["item_id"])
    return out


def main() -> int:
    if not OLD.exists() or not NEW.exists():
        print(f"missing oracle reports: {OLD.name} and/or {NEW.name}")
        return 2
    old = json.loads(OLD.read_text(encoding="utf-8"))
    new = json.loads(NEW.read_text(encoding="utf-8"))

    old_by = excluded_by_kind(old)
    new_by = excluded_by_kind(new)

    # The ambiguity classes are reported together because they are one defect
    # -- an answer key that does not determine its answer -- split by whether
    # a static check or an execution found it. Clock dependence is a different
    # defect and is kept separate.
    groups = {
        "ambiguous": ("ambiguous", "ambiguous_tie"),
        "clock_dependent": ("gold_clock_dependent",),
    }

    classes = {}
    for label, kinds in groups.items():
        a: set[str] = set().union(*(old_by.get(k, set()) for k in kinds))
        b: set[str] = set().union(*(new_by.get(k, set()) for k in kinds))
        classes[label] = {
            "old_count": len(a),
            "new_count": len(b),
            "fixed": sorted(a - b),
            "survived": sorted(a & b),
            "introduced": sorted(b - a),
            "n_fixed": len(a - b),
            "n_survived": len(a & b),
            "n_introduced": len(b - a),
        }

    record = {
        "old_revision": old["revision"],
        "new_revision": new["revision"],
        "n_items": old["n_items"],
        "old_excluded": old["n_excluded"],
        "new_excluded": new["n_excluded"],
        "old_by_kind": old["by_kind"],
        "new_by_kind": new["by_kind"],
        "classes": classes,
    }
    OUT.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    for label, c in classes.items():
        print(
            f"  {label:16} {c['old_count']:3} -> {c['new_count']:3}   "
            f"fixed {c['n_fixed']:3}  survived {c['n_survived']:3}  introduced {c['n_introduced']:3}"
        )
    print(f"  {'TOTAL excluded':16} {old['n_excluded']:3} -> {new['n_excluded']:3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
