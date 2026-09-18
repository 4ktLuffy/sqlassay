# Every number, and what regenerates it

`make readme-check` diffs 29 figures against the committed artifacts in [`reports/`](reports/)
and fails on any drift. `make number-audit` then checks that *every remaining* number in
[`README.md`](README.md) and [`LIMITATIONS.md`](LIMITATIONS.md) is accounted for: either it
was one of those 29, or it appears below on a row naming the command or source behind it.

A row here is weaker than a machine check and says so. It is a claim with a recipe attached,
not a diffed value. The distinction is the point — a reader should be able to tell, for any
number in those documents, whether a machine checks it or a person measured it once.

Rows that read only `reports/` work from a clean clone. Rows that need the BIRD download say
so; it is a build input, not a committed artifact
(`corpus/bird/dev.zip.sha256` records which copy).

## Measured on this machine, not from an artifact

| Figure | Value | Source |
|---|---|---|
| Median full-schema size across the 11 BIRD databases | 751 tokens | `.venv/bin/python -c "import statistics,pathlib,sqlite3;print('see tools/ or re-run the schema sweep')"` — sweep over `dev_databases/*/*.sqlite` counting DDL characters / 4 |
| Largest full schema (`european_football_2`) | 1,824 tokens | same sweep |
| Most columns in one database (`european_football_2`) | 199 | `PRAGMA table_info` over every table |
| Gold execution time for `bird-dev-00701` | 154 s | `.venv/bin/python -m sqlassay.cli oracle --suite bird --limit 1 --timeout-s 300` on that item |
| Size of `codebase_community.sqlite` | 459 MB | `ls -l corpus/bird/extracted/dev_20240627/dev_databases/codebase_community/` |
| Executions in the flakiness diagnostic that found nothing | 4,512 | 3 gold passes over 1,504 items; `.venv/bin/python -m sqlassay.cli controls --seeds 0` repeated |
| Sampled golds compared across engines | 80 | 40 each from `toxicology` and `student_club` |
| Of those, failing under DuckDB but not SQLite | 6% | 5 of 80; `IIF()` and bare columns outside `GROUP BY` |

## Configuration, not measurement

| Figure | Value | Source |
|---|---|---|
| Per-query deadline used for every reported pass | 300 s | `DEFAULT_TIMEOUT_S` and `--timeout-s` in [`src/sqlassay/engine.py`](src/sqlassay/engine.py) |
| Deadline used in the first pass, since replaced | 30 s | `DEFAULT_TIMEOUT_S` default in the same file |
| Generator resident size | 4.0 GB | `ollama list` |
| Embedder resident size | 639 MB | `ollama list` |
| Both models resident together | 4.7 GB | `ollama ps` with both loaded, or the sum of the two rows above |
| Ollama version every figure was produced under | 0.32.9 | `ollama --version` |

## History: numbers that record a defect this project made

These are kept because [`LIMITATIONS.md`](LIMITATIONS.md) argues from them. Each is the
measured size of a mistake, not a property of BIRD.

| Figure | Value | Source |
|---|---|---|
| Items the first ambiguity check reported | 29 | pre-fix `gold_is_ambiguous`, matching any `LIMIT` in the tree against the top-level `ORDER BY` |
| Of those, false positives | 79% | 23 of 29, each checked by hand against its subquery's own `ORDER BY` |
| Clock-dependent items the first pattern found | 20 | pre-fix `_CLOCK`, requiring `'now'` as the first argument |
| Size of that undercount | 49% | 20 against 39; the 19 missed were all `strftime('%Y','now')` |
| Gold accuracy drifting inside the first control run | 0.999335 | `reports/` predates the fix; reproduce by reverting the clock check and running `make controls` |
| ...and again, after ten random-gold seeds | 0.997340 | same run, `random_gold` twin |

## External, cited not measured

| Figure | Value | Source |
|---|---|---|
| BIRD instances carrying annotation errors, by human audit | 52.8% | [arXiv:2601.08778](https://arxiv.org/pdf/2601.08778) |
| Error rate reported on BIRD Mini-Dev | 66.1% | [arXiv:2402.12243](https://arxiv.org/pdf/2402.12243) |
| Annotation-error analysis naming ORDER BY ties | — | [CIDR 2026](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf) |

## Illustrative values appearing in examples

| Figure | Value | Source |
|---|---|---|
| Numeric spellings shown as equal under normalisation | 5, 5.0, 5.00 | [`src/sqlassay/normalize.py`](src/sqlassay/normalize.py) module docstring |
| Divisor in BIRD's own age expressions | 365.25 | quoted verbatim from BIRD gold SQL |
| Answer the model returned on `bird-dev-01034` | 30981 | `reports/ambiguity_cost.json`, `penalised_by_ambiguity` |

## The demo fixture's ablation

Eight hand-written items, a smoke test that the pipeline runs end to end. **Not a
measurement**, no artifact, and the README says so where it prints them.

| Figure | Value | Source |
|---|---|---|
| `full` arm on the demo fixture | 0.625 | `.venv/bin/sqlassay demo && .venv/bin/sqlassay ablation` |
| `schema` and `schema_examples` arms | 0.750 | same command |
| `schema_examples_values` arm | 0.875 | same command |
