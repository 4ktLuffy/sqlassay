# EVIDENCE.md

Every claim in [`README.md`](README.md) and [`LIMITATIONS.md`](LIMITATIONS.md), mapped to the
committed artifact it is read from and the command that regenerates that artifact. The values
themselves are **not repeated here**: `make readme-check` recomputes them from these artifacts
and diffs the documents, and a second hand-written copy of a number would be one more place
for it to be wrong.

Artifacts live in [`reports/`](reports/) and are committed. The BIRD download they are
computed from is not — it is a build input, and `corpus/bird/dev.zip.sha256` records which
copy produced them.

```bash
make docs        # readme-check, then number-audit
```

`make readme-check` reads `reports/` only: no database, no model, no network. A reviewer with
a clean clone can check every headline figure before running anything.

## The artifacts, and what regenerates each

| Artifact | Regenerate with | Needs |
|---|---|---|
| [`reports/oracle_integrity_bird_dev.json`](reports/oracle_integrity_bird_dev.json) | `sqlassay oracle --suite bird --revision 2024-06-27 --timeout-s 300` | BIRD download |
| [`reports/oracle_integrity_bird_dev_20251106.json`](reports/oracle_integrity_bird_dev_20251106.json) | `sqlassay oracle --suite bird --revision 2025-11-06 --timeout-s 300` | BIRD download |
| [`reports/revision_diff.json`](reports/revision_diff.json) | `python tools/make_revision_diff.py` | the two above only |
| [`reports/floors_bird_dev_20251106.json`](reports/floors_bird_dev_20251106.json) | `sqlassay floors --seeds 10` | BIRD download |
| [`configs/expected.yaml`](configs/expected.yaml) | `python tools/derive_bands.py reports/floors_bird_dev_20251106.json` | the floors artifact only |
| [`reports/controls_bird_dev_20251106.json`](reports/controls_bird_dev_20251106.json) | `sqlassay controls --seeds 10` | BIRD download |
| [`reports/ambiguity_cost.json`](reports/ambiguity_cost.json) | `sqlassay demonstrate` | BIRD download **and** a running Ollama |

The order matters and is not a convenience: `floors` must follow `oracle` because a floor
measured over unusable items is not a floor for the suite being scored; `controls` must follow
`floors` because its bands are derived from them.

## Claim by claim

| Claim | Artifact | Field |
|---|---|---|
| BIRD 2025-11 has 39 clock-dependent golds, 2024-06 has 16 | both oracle reports | `by_kind.gold_clock_dependent` |
| The review fixed 2 clock-dependent golds and introduced 25 | `revision_diff.json` | `classes.clock_dependent.{n_fixed,n_introduced}` |
| The review fixed 26 ambiguous golds and introduced 6 | `revision_diff.json` | `classes.ambiguous.{n_fixed,n_introduced}` |
| The revision it recommends carries more unusable keys, 66 to 69 | `revision_diff.json` | `old_excluded`, `new_excluded` |
| Every BIRD gold parses, executes, and is deterministic | both oracle reports | `by_kind` has no `gold_failed` / `gold_unparseable` / `gold_nondeterministic` |
| The suite is ADMISSIBLE at 1,465 items | `controls_bird_dev_20251106.json` | `admissible`, `n_items` |
| Gold scores 1.000000 in all four independent measurements | same | `controls[*].observed` |
| The null floor is 2/1465, not zero | `floors_bird_dev_20251106.json` | `floors[predictor=null]` |
| Chance is 0.002389 ± 0.001171 over 10 seeds | same | `chance_band_random_gold` |
| Every band is derived from a measured floor | `configs/expected.yaml` | header comment names the artifact and rule |
| 2 of 19 enumerable items scored zero for a correct answer | `ambiguity_cost.json` | `buckets`, `penalised_by_ambiguity` |

## What has no artifact, and is labelled as such

[`NUMBERS.md`](NUMBERS.md) carries every figure a machine does not check: schema sizes
measured once by a sweep, the 154-second gold, the demo fixture's ablation, this project's own
two detector defects, and the external papers cited in `LIMITATIONS.md` §1.

`make number-audit` fails if any number in the documents is neither diffed against an artifact
nor listed there, so the boundary between the two cannot drift silently.

## What is not claimed

There is **no model accuracy number on BIRD** in this repository. The ablation table in the
README is the eight-item demo fixture and says so. M5 in [`PLAN.md`](PLAN.md) — the measured
run — has not been executed.
