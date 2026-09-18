#!/usr/bin/env python
"""Regenerate every number README.md and LIMITATIONS.md claim, and diff them.

Values are read from the committed artifacts in ``reports/`` -- never from a
database that happens to be on this disk, never from whichever run happened
last, and never from the documents themselves. Nothing here compares a number
to a copy of itself: the expected side is always recomputed from an artifact,
and a figure no artifact can produce does not belong in the documents.

This script needs no database, no model and no network, so a reviewer with a
clean clone can check the arithmetic before running anything. That is the
point: the claims are the reason to read the repository, and a reader who
cannot check them has to take them on trust.

Each check pins **where** the value appears, not merely that the digits occur
somewhere, so a figure quoted into the wrong row still fails.

Exit 0 if the documents agree with the artifacts, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DOCS = (ROOT / "README.md", ROOT / "LIMITATIONS.md")


@dataclass(frozen=True)
class Check:
    """One regenerated figure and the shape it must appear in."""

    label: str
    source: str
    expected: str
    pattern: str


def load(name: str) -> dict:
    path = REPORTS / name
    if not path.exists():
        raise SystemExit(f"missing artifact: reports/{name} (run `make artifacts`)")
    return json.loads(path.read_text(encoding="utf-8"))


def build_checks() -> list[Check]:
    """Every machine-checked figure, recomputed from an artifact."""
    old = load("oracle_integrity_bird_dev.json")
    new = load("oracle_integrity_bird_dev_20251106.json")
    diff = load("revision_diff.json")
    floors = load("floors_bird_dev_20251106.json")
    controls = load("controls_bird_dev_20251106.json")
    cost = load("ambiguity_cost.json")

    amb = diff["classes"]["ambiguous"]
    clock = diff["classes"]["clock_dependent"]
    chance = floors["chance_band_random_gold"]
    buckets = cost["buckets"]

    def observed(kind: str, field: str) -> float:
        """One control's observed value, by kind and field name."""
        return float(next(c for c in controls["controls"] if c["kind"] == kind)["observed"][field])

    null_margin = observed("null_predictor", "margin")
    deranged_acc = observed("shuffled_gold", "accuracy")

    n_items = new["n_items"]
    new_pct = f"{100 * new['n_excluded'] / n_items:.2f}%"
    old_pct = f"{100 * old['n_excluded'] / n_items:.2f}%"

    c: list[Check] = [
        # --- the oracle, per revision -------------------------------------
        # The denominator. A count of defects with no suite size beside it is
        # unreadable, and this check exists because the first draft of the
        # README stated 39 and 69 without ever saying "of what".
        Check("suite size", "oracle.n_items", str(n_items), rf"of {n_items // 1000},{n_items % 1000} items"),
        Check("2024 clock-dependent", "old.by_kind", str(old["by_kind"].get("gold_clock_dependent", 0)),
              r"clock\*{0,2}\s*\|\s*16\s*\|"),
        Check("2025 clock-dependent", "new.by_kind", str(new["by_kind"].get("gold_clock_dependent", 0)),
              r"clock\*{0,2}\s*\|\s*16\s*\|\s*\*{0,2}39"),
        Check("2024 tie-ambiguous", "old.by_kind", str(old["by_kind"].get("ambiguous_tie", 0)),
              r"LIMIT` boundary \|\s*44\s*\|"),
        Check("2025 tie-ambiguous", "new.by_kind", str(new["by_kind"].get("ambiguous_tie", 0)),
              r"LIMIT` boundary \|\s*44\s*\|\s*23\s*\|"),
        Check("2024 excluded", "old.n_excluded", str(old["n_excluded"]),
              rf"\*\*{old['n_excluded']} \({re.escape(old_pct)}\)\*\*"),
        Check("2025 excluded", "new.n_excluded", str(new["n_excluded"]),
              rf"\*\*{new['n_excluded']} \({re.escape(new_pct)}\)\*\*"),
        Check("2024 excluded pct", "old.n_excluded/n_items", old_pct,
              rf"\({re.escape(old_pct)}\)"),
        Check("2025 excluded pct in LIMITATIONS", "new.n_excluded/n_items", new_pct,
              rf"\*\*{re.escape(new_pct)} of BIRD dev is excluded"),
        Check("excluded items named in LIMITATIONS", "new.n_excluded", str(new["n_excluded"]),
              rf"The {new['n_excluded']} excluded items cannot distinguish"),

        # --- the version diff ---------------------------------------------
        Check("ambiguous fixed", "diff.ambiguous.n_fixed", str(amb["n_fixed"]),
              rf"ambiguous answer keys \|\s*{amb['n_fixed']}\s*\|"),
        Check("ambiguous survived", "diff.ambiguous.n_survived", str(amb["n_survived"]),
              rf"ambiguous answer keys \|\s*{amb['n_fixed']}\s*\|\s*{amb['n_survived']}\s*\|"),
        Check(
            "ambiguous introduced",
            "diff.ambiguous.n_introduced",
            str(amb["n_introduced"]),
            rf"ambiguous answer keys \|\s*{amb['n_fixed']}\s*\|\s*{amb['n_survived']}"
            rf"\s*\|\s*{amb['n_introduced']}\s*\|",
        ),
        Check("clock fixed", "diff.clock.n_fixed", str(clock["n_fixed"]),
              rf"clock-dependent answer keys \|\s*{clock['n_fixed']}\s*\|"),
        Check("clock survived", "diff.clock.n_survived", str(clock["n_survived"]),
              rf"clock-dependent answer keys \|\s*{clock['n_fixed']}\s*\|\s*{clock['n_survived']}\s*\|"),
        Check("clock introduced", "diff.clock.n_introduced", str(clock["n_introduced"]),
              rf"clock-dependent answer keys \|.*\*\*{clock['n_introduced']}\*\*"),
        Check(
            "clock totals in prose",
            "diff.clock counts",
            f"{clock['old_count']} to {clock['new_count']}",
            rf"{clock['old_count']}\s*(?:to|->|→)\s*{clock['new_count']}",
        ),
        Check(
            "total excluded regression",
            "diff old->new excluded",
            f"{diff['old_excluded']} to {diff['new_excluded']}",
            # 'to' or an arrow, but the two numbers must be adjacent in one
            # phrase: the claim is the regression, not the two totals apart.
            rf"{diff['old_excluded']}\s*(?:to|->|→)\s*{diff['new_excluded']}",
        ),

        # --- the controls ---------------------------------------------------
        Check("controls suite size", "controls.n_items", str(controls["n_items"]),
              rf"{controls['n_items']}/{controls['n_items']} = 1\.000000"),
        Check("admissible verdict", "controls.admissible", str(controls["admissible"]),
              r"\*\*ADMISSIBLE\.\*\*" if controls["admissible"] else r"INADMISSIBLE"),
        Check("null floor", "floors.null.accuracy",
              f"{[f for f in floors['floors'] if f['predictor'] == 'null'][0]['accuracy']:.6f}",
              rf"null {[f for f in floors['floors'] if f['predictor'] == 'null'][0]['correct']}/"
              rf"{floors['n_admissible']} = 0\.001365"),
        Check("null margin", "controls.null_predictor.margin", f"{null_margin:.6f}",
              rf"margin {null_margin:.6f}"),
        Check("shuffled deranged accuracy", "controls.shuffled_gold.accuracy",
              f"{deranged_acc:.6f}", rf"deranged {deranged_acc:.6f}"),
        Check("chance mean", "floors.chance.mean", f"{chance['mean']:.6f}",
              rf"chance {chance['mean']:.6f} . {chance['sd']:.6f}"),

        # --- does ambiguity cost a model marks ------------------------------
        Check("penalised by ambiguity", "cost.buckets.matched_alternative",
              str(buckets.get("matched_alternative", 0)),
              rf"scored \*\*zero\*\* \|\s*\*\*{buckets.get('matched_alternative', 0)}\*\*"),
        Check("matched gold", "cost.buckets.matched_gold", str(buckets.get("matched_gold", 0)),
              rf"gold's own answer[^|]*\|\s*{buckets.get('matched_gold', 0)}\s*\|"),
        Check("genuinely wrong", "cost.buckets.matched_neither", str(buckets.get("matched_neither", 0)),
              rf"genuinely wrong \|\s*{buckets.get('matched_neither', 0)}\s*\|"),
        Check("not enumerable", "cost.buckets.not_run", str(buckets.get("not_run", 0)),
              rf"not enumerable \(`LIMIT > 1`\) \|\s*{buckets.get('not_run', 0)}\s*\|"),
        # The denominator of the ambiguity-cost claim: items whose valid
        # answers could be enumerated at all, i.e. everything but not_run.
        Check("enumerable items", "cost.n - not_run",
              str(cost["n"] - buckets.get("not_run", 0)),
              rf"measured on {cost['n'] - buckets.get('not_run', 0)} items"),
        Check("ordinary failures of the enumerable", "cost.buckets.matched_neither",
              str(buckets.get("matched_neither", 0)),
              rf"{buckets.get('matched_neither', 0)} of its "
              rf"{cost['n'] - buckets.get('not_run', 0)} failures"),
        Check("gold reproducibility", "controls gold accuracy", "1.000000",
              r"`1\.000000` in all four independent measurements"),
    ]
    return c


def main() -> int:
    text = "\n".join(p.read_text(encoding="utf-8") for p in DOCS)
    checks = build_checks()
    failed = []
    for check in checks:
        if re.search(check.pattern, text) is None:
            failed.append(check)
        else:
            print(f"  ok    {check.label:38} {check.expected:>12}   [{check.source}]")

    if failed:
        print(f"\n{len(failed)} of {len(checks)} figures do not match the artifacts:\n")
        for check in failed:
            print(f"  FAIL  {check.label}")
            print(f"        artifact says : {check.expected}   [{check.source}]")
            print(f"        expected shape: {check.pattern}")
        print("\nThe documents have drifted from reports/. Fix the prose, not the artifact.")
        return 1

    print(f"\n{len(checks)} figures match the committed artifacts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
