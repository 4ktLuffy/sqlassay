#!/usr/bin/env python
"""Derive configs/expected.yaml from a measured floors artifact.

The bands are **computed from the measurement**, not typed by hand. That is
the difference between a control and a threshold someone liked: every number
this writes carries, in a comment beside it, the measured value it came from
and the rule that turned that value into a band.

The rules, and why each one:

* ``null_predictor.accuracy_max`` = the measured null floor **plus room for
  one more item**. Tight enough that a second item a do-nothing agent happens
  to pass shows up as a control failure rather than disappearing into slack.
* ``null_predictor.min_margin`` = 0.5, fixed rather than derived. It asks that
  gold beat null by half the scale; deriving it from the gold score would let
  a broken suite lower its own bar.
* ``shuffled_gold.accuracy_max`` = the random-gold chance mean + 3 sd, floored
  at one item's worth. Rescoring against a deranged key should land at chance,
  and chance here is measured, not assumed to be zero.
* ``random_gold.chance_max`` is a **sanity maximum**, not the band. The band
  itself is re-measured per suite at control time. This only catches a suite
  whose chance level is absurd.

Usage:  python tools/derive_bands.py reports/floors_bird_dev_20251106.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    artifact = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else ROOT / "configs" / "expected.yaml"

    data = json.loads(artifact.read_text(encoding="utf-8"))
    if not data.get("complete"):
        print(
            f"refusing: {artifact} is a partial pass "
            f"({data['chance_band_random_gold']['n_seeds']} seeds). "
            "A band derived from an unfinished measurement is a guess."
        )
        return 2

    n = int(data["n_admissible"])
    floors = {f["predictor"]: f for f in data["floors"]}
    null = floors["null"]
    chance = data["chance_band_random_gold"]

    one_item = 1.0 / n
    null_max = null["accuracy"] + one_item
    shuffled_max = max(chance["mean"] + 3 * chance["sd"], one_item)
    chance_sanity = max(chance["max"] * 4, 0.05)

    text = f"""# Expected bands for the negative controls. DERIVED, NOT TYPED.
#
# Regenerate with:
#     python tools/derive_bands.py {artifact.as_posix()}
#
# Measured on: BIRD dev {data["revision"]}, {n} admissible items,
# chance estimated over {chance["n_seeds"]} seeds.
#
# EVERY MAXIMUM BELOW IS A MEASURED FLOOR, NOT A ZERO. This is the single most
# important thing about this file. On an execution oracle the floors are not
# zero for concrete reasons, and a band set at zero would fail for a reason
# that is a property of the suite rather than a defect in anything -- and
# would then be "fixed" by weakening something real.

gold_predictor:
  # The positive control, and every other control's twin. No slack: the answer
  # key must score itself perfectly or nothing measured against it means
  # anything.
  #
  # NOT derived from the floors artifact -- that pass measures trivial
  # predictors and contains no gold run. 1.0 is the definition of this
  # control, not a measurement of it: the control itself is what measures
  # gold, and its whole job is to fail if gold cannot reach 1.0.
  accuracy: {{eq: 1.0}}

null_predictor:
  # Measured null floor: {null["accuracy"]:.9f} = {null["correct"]}/{n}.
  # Not zero: {null["correct"]} item(s) have a gold returning exactly one NULL,
  # which `SELECT NULL` matches outright.
  # Band = measured floor + one item ({one_item:.9f}), so a second such item
  # surfaces as a control failure instead of vanishing into slack.
  accuracy_max: {{max: {null_max:.9f}}}
  # Fixed, not derived: gold must beat null by half the scale. Deriving this
  # from the gold score would let a broken suite lower its own bar.
  min_margin: {{min: 0.5}}

shuffled_gold:
  # Rescoring against a deranged answer key should land at chance.
  # Measured chance: mean {chance["mean"]:.9f}, sd {chance["sd"]:.9f}
  # over {chance["n_seeds"]} seeds. Band = mean + 3sd, floored at one item.
  accuracy_max: {{max: {shuffled_max:.9f}}}

random_gold:
  # NOT the band -- the band is re-measured per suite at control time, because
  # chance is a property of the suite and freezing it here would hide a suite
  # whose chance level changed. This is only a sanity maximum, set well above
  # the highest seed measured ({chance["max"]:.9f}), to catch a suite where
  # another item's gold is right absurdly often.
  chance_max: {{max: {chance_sanity:.9f}}}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    print(f"  null floor      {null['accuracy']:.9f} ({null['correct']}/{n})  -> accuracy_max {null_max:.9f}")
    print(f"  chance mean     {chance['mean']:.9f} sd {chance['sd']:.9f}  -> shuffled max {shuffled_max:.9f}")
    print(f"  chance sanity   max seed {chance['max']:.9f}  -> chance_max {chance_sanity:.9f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
