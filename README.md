# sqlassay

Execution-verified text-to-SQL for local models. The thesis, in one line:

> **A predicted query is correct when it returns the gold query's rows — not when it
> looks like the gold query.**

Three deterministic gates, no LLM judge anywhere. A local model proposes SQL; `sqlglot`
decides whether it is one read-only statement; DuckDB decides whether it runs; the result
set decides whether it is right. Every verdict is `PASSED`, `FAILED` or `NOT_RUN`, and the
third is never collapsed into the second.

## Why this exists

Text-to-SQL is the rare LLM task with a **machine-checkable oracle**. There is no judge
model to calibrate and no rubric to argue about: you execute both queries and compare rows.
That makes it the right place to measure what a small local model can actually do, because
the measurement itself is not in question.

The harness is built around the failure that makes most published text-to-SQL numbers hard
to read: *an accuracy figure that silently pools "wrote no SQL", "wrote SQL that crashed"
and "wrote SQL that returned the wrong rows"*. Those are three different findings with
three different fixes. Every run here reports them separately.

## Quick start

```bash
make install
make selftest                      # prove every gate can still go red
.venv/bin/sqlassay demo            # build the demo database and its indexes
.venv/bin/sqlassay ablation        # run all four retrieval arms
```

Needs Ollama running with `qwen3.5:4b-mlx` and `qwen3-embedding:0.6b`. No paid API is used
anywhere in this repository.

## The three gates

| Gate | Question | Failure means |
|---|---|---|
| `parse@1.0.0` | Is this exactly one read-only statement? | The model produced no answer |
| `execute@1.0.0` | Does it run against the database? | The model produced a broken answer |
| `resultset@1.0.0` | Do its rows match the gold's? | The model produced a wrong answer |

A gate that fails stops the pipeline, and every gate after it records **`NOT_RUN`**, never
`FAILED`. Nothing was measured, and a report that showed a failure there would be claiming
evidence it does not have.

### Every gate can be shown to go red

```bash
make selftest
```

```
[PASS] parse@1.0.0: 6 probes fired: empty, prose, fragment, mutating, stacked, valid
[PASS] execute@1.0.0: 5 probes fired: valid, empty_is_an_answer, bad_column, bad_table, mutating_blocked
[PASS] resultset@1.0.0: 7 probes fired: identical, reordered_unordered_gold, reordered_ordered_gold,
       duplicates_matter, empty_is_not_correct, empty_matches_empty, arity_differs
```

Eighteen planted defects. A gate that cannot demonstrate failure is not evidence, so
`selftest` exits non-zero and the CLI says nothing may be reported.

## What result-set comparison actually has to decide

The part that looks like `a == b` and is not. Each of these silently changes the score, and
each is a decision this harness makes explicitly, in
[`normalize.py`](src/sqlassay/normalize.py):

- **Order** is significant only when the **gold** query has a top-level `ORDER BY` — read
  with a parser, not a substring search, so an `ORDER BY` inside a CTE does not wrongly
  make the outer result positional. Read from the prediction instead, a model could make
  its own answer order-insensitive by dropping the `ORDER BY` the question asked for.
- **Duplicates** are compared as a multiset. `SELECT region` and `SELECT DISTINCT region`
  are different questions; a set-based comparator cannot tell them apart.
- **Numbers** — `5`, `5.0` and `Decimal('5.00')` are one answer, rounded to a recorded
  tolerance.
- **Columns** match by **position, not name**: aliasing `SUM(x)` as `total` is not a
  different answer.
- **NULL** is a value, distinct from `0` and from `''`.
- **Ambiguous gold** — `LIMIT` with no `ORDER BY` has no single correct answer. Those items
  are excluded and counted, never scored as model failures.

`SELECT` returning nothing is an **answer** and reaches the comparison as an empty result
set. A query that errored or timed out did not produce one. The two never arrive in the
same shape, because an empty gold can legitimately match the first and never the second.

## Measured: BIRD's own quality review made its answer keys worse, twice

The first thing this harness does is check the **benchmark**, not the model. The gold SQL
is run as if it were the prediction, through all three gates. An answer key that cannot
match itself cannot tell you anything about a model.

BIRD published a [revised dev set on 2025-11-06](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106),
produced by a quality review led by five PhD researchers whose stated goal was to *"minimize
ambiguity"*. Running the same deterministic detector over both revisions measures that
review against a criterion with no human judgement in it.

```bash
sqlassay oracle --suite bird --revision 2024-06-27 --timeout-s 300
sqlassay oracle --suite bird --revision 2025-11-06 --timeout-s 300
```

| defect | 2024-06-27 | 2025-11-06 |
|---|---|---|
| gold depends on the **system clock** | 16 | **39** |
| ambiguous — `ORDER BY` ties across the `LIMIT` boundary | 44 | 23 |
| ambiguous — `LIMIT` with no `ORDER BY` | 6 | 7 |
| gold that fails to parse / execute / is non-deterministic | 0 | 0 |
| **unusable answer keys** | **66 (4.30%)** | **69 (4.50%)** |

Item by item, what the review did to each class:

| defect class | fixed | survived | **introduced** |
|---|---|---|---|
| ambiguous answer keys | 26 | 24 | 6 |
| clock-dependent answer keys | 2 | 14 | **25** |

**The review halved the ambiguity and more than doubled the clock dependence.** Net, the
suite has slightly *more* unusable answer keys than before it.

### The clock-dependent class is the serious one

```sql
AVG(CAST(strftime('%Y','now') - strftime('%Y', c.birth_date) AS REAL))   -- bird-dev-00092
(julianday('now') - julianday(cd.issued)) / 365.25                        -- bird-dev-00178
```

An ambiguous gold has several equally valid answers. A **clock-dependent gold has a
different answer depending on the date you run it**. Scores on those items are not
reproducible across time at all: run BIRD today and in six months and the answer key has
moved underneath you.

Twenty-five of the 39 are in `financial`, which went from 1 to 25 — the review rewrote
those queries to compute ages with `strftime('%Y','now')`.

### This was found by the harness failing, not by looking for it

The controls first came back **INADMISSIBLE**. The gold predictor — which must score 1.0 —
returned `1.000000`, then `0.999335`, then `0.997340` *within a single run*. Four
clock-dependent golds had shifted while the run was in progress.

Two diagnoses were wrong before the right one. Connection poisoning from timeouts:
refuted by 4,512 clean executions. A rare non-deterministic query: refuted by the failures
being reproducible once named. Only then did the clock show up — and it had passed the
oracle's two-execution determinism check, because two executions a second apart cannot see
a clock move.

With the class excluded, the same controls come back **ADMISSIBLE**, and gold scores
`1.000000` in all four independent measurements.

## The controls, and the verdict

```bash
sqlassay controls --seeds 10
```

| control | result | |
|---|---|---|
| `gold_predictor` | PASSED | 1465/1465 = 1.000000 |
| `null_predictor` | PASSED | null 2/1465 = 0.001365, margin 0.998635 |
| `shuffled_gold` | PASSED | deranged 0.000683 against baseline 1.000000 |
| `random_gold` | PASSED | chance 0.002389 ± 0.001171, gold clears the band |

**ADMISSIBLE.** Every band is derived from a measured floor by
[`tools/derive_bands.py`](tools/derive_bands.py), never typed — and **no floor here is
zero**: `SELECT NULL` matches 2 items outright, and another item's gold from the same
database is right 1–7 times in 1,465.

### Does it actually cost a model marks? Two items, measured

A count of ambiguous answer keys is a statement about the benchmark, not about any model.
So the model is run on them and its answers are sorted into three buckets:

```bash
sqlassay demonstrate
```

`qwen3.5:4b-mlx`, full schema, temperature 0, over the 23 tie items in the 2025-11-06
revision:

| bucket | items |
|---|---|
| returned an **equally correct** answer, scored **zero** | **2** |
| returned the gold's own answer, scored correct | 1 |
| returned something genuinely wrong | 16 |
| valid answers not enumerable (`LIMIT > 1`) | 4 |

**Two items out of nineteen.** That is a small number and it is the honest one: this model
is weak, and 16 of its 19 failures here are ordinary mistakes that ambiguity had nothing to
do with. Pooling those together as "failed on ambiguous items" would be the same error the
gates exist to stop.

The conditional is the sharper statistic, and the sample is tiny:

> Of the **3** times the model produced an answer from inside the tie group, **2 were
> scored zero** — not because they were wrong, but because the gold had arbitrarily picked
> a different one.

On `bird-dev-01517` it returned `('SME',)`, one of three equally correct answers. On
`bird-dev-01034`, `(30981,)`, one of two. Both score zero under BIRD's own comparison. With
n=3 this demonstrates that the mechanism is real and does not estimate how often it bites.

### What this does and does not establish

It **does not** discover that BIRD has annotation errors. That is well documented, at much
larger magnitudes, by work that uses human audit — see [`LIMITATIONS.md`](LIMITATIONS.md) §1,
which should be read first. This measures one narrow class that execution alone can decide.

It **does not** say how much any published score is wrong by. Whether a system gained or
lost on these items depends on which valid answer it produced, and that is not measured here.

It **does** say that 30 items in the current recommended dev split cannot distinguish a
correct model from an incorrect one, that 6 of them were created by the pass meant to fix
this, and that both facts are recoverable by anyone in ten minutes with no annotators.

## The retrieval ablation

A vector database here does not answer the question — SQL does. It retrieves **what goes
into the prompt**. Three indexes, three jobs, switched on independently so each one's
contribution is separable:

| Index | Holds | Fixes |
|---|---|---|
| `schema` | one document per table | too many columns to attend to |
| `example` | verified `question → SQL` pairs | the model not knowing the house style |
| `value` | literals from low-cardinality text columns | "Acme" when the data says `ACME CORPORATION INC` |

On BIRD the median full schema is **751 tokens** (max 1,824), which fits a 4B's context
comfortably. So schema retrieval is **not** tested here as a way to make the schema fit —
it is tested as a way to reduce *distraction*, on databases with up to 199 columns. That is
a weaker claim than the usual one for retrieval, and it is the one the measurement can
actually support.

```bash
sqlassay ablation
```

```
arm                        n   acc     parse_f  exec_f  rows_f  note
full                       8   0.625   0        0       3
schema                     8   0.750   0        0       2
schema_examples            8   0.750   0        0       2       DEGENERATE: examples returned nothing
schema_examples_values     8   0.875   0        0       1       DEGENERATE: examples returned nothing
```

**Read the `note` column before the `acc` column.** An arm marked `DEGENERATE` enabled a
retrieval component that returned nothing on every item — here, because no prediction has
been verified yet, so the example index is empty. Its score is not evidence about examples;
it is the previous arm under another name.

**And these are 8 demo items**, not BIRD. The gap between `0.625` and `0.875` is two items.
The real ablation is M5 in [`PLAN.md`](PLAN.md) and has not been run.

## The flywheel, and the contamination it invites

A prediction that passes all three gates is a *verified* `question → SQL` pair, and is worth
retrieving for later questions. That loop is also the most direct route to a meaningless
number: if verified examples enter the index **during** an evaluation pass, later items are
answered by a system earlier items did not have, the run is not one measurement, and an item
whose own answer entered the index is scored against itself.

So the freeze is enforced in the store, at the only place that can write — not by
remembering to do it at the call site:

```python
store.freeze("example", "train split only")
store.add("example", ...)   # FrozenIndexError: index 'example' is closed to writes
```

The freeze records its timestamp and reason in the file, so a report can **state** that the
index was frozen rather than assert it.

## Measured on this machine

Apple M4, 10-core GPU, 16 GB unified memory, Ollama 0.32.9, 2026-09-15.

| Finding | Detail |
|---|---|
| `qwen3.5:4b-mlx` is a reasoning model | Through `/v1/chat/completions` a 300-token budget was consumed **entirely** by the `reasoning` field; `content` came back empty with `finish_reason: length` |
| `think: false` fixes it | Only on the native `/api/chat` endpoint, which is why this client does not use the OpenAI-compatible one |
| Generation throughput | ~23 tok/s with thinking off |
| Resident footprint | 4.0 GB generator + 639 MB embedder ≈ 4.7 GB, so both stay loaded |

Whether reasoning *earns* its tokens on text-to-SQL is an open question and a config axis
(`--think`), not a constant. The default is off because the cheap arm should be the
baseline, not the special case.

## Safety

The model's SQL is never trusted. The DuckDB connection is opened **read-only**, and
independently, only a single `SELECT`/`WITH` is admitted. Both, because they fail
differently: read-only stops the damage, the statement check stops a `DROP TABLE` being
recorded as "the model wrote SQL that failed" — true, but it buries what the model tried
to do. Queries run under a deadline and a timeout is recorded as a timeout, never as an
empty result.

## Status

Scaffold. What works end to end: the three gates, result-set normalisation, the schema and
value indexes, the four ablation arms, the degenerate-arm check, and a demo fixture.

Not built yet, and not claimed:

- **A real benchmark.** Spider 2.0 and BIRD loaders. The 8 demo items are a fixture.
- **Negative controls over the suite**, in the sense `agent-assay` means: a null predictor,
  a chance band measured over seeds, and a shuffled gold set. Until those exist and can be
  shown to fail, no number from this harness is admissible.
- **The example flywheel**, end to end with a train/test split.
- **Repeats.** Every reported number should be n≥5; the current runner does one pass.
