# Limitations

What this does not establish, and which of its numbers to distrust. Read this before the
README's results, not after. Ordered by how much each one limits a claim in the README.

## 1. This finding is not novel, and the number is far smaller than published ones

**BIRD's annotation quality is a well-studied problem and this repository did not discover
it.** Prior work, found *after* the measurement was taken and before anything was
published:

- [Pervasive Annotation Errors Break Text-to-SQL Benchmarks and Leaderboards](https://arxiv.org/pdf/2601.08778)
  (VLDB 2026) audits BIRD and reports **52.8% of instances carrying annotation errors**.
- [Text-to-SQL Benchmarks are Broken](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf)
  (CIDR 2026) names **"sorting failures (ties in ORDER BY)"** explicitly as a category —
  the same defect this harness detects.
- [Understanding the Effects of Noise in BIRD-Bench](https://arxiv.org/pdf/2402.12243)
  reports a 66.1% error rate on the Mini-Dev subset.

The 4.50% here is **not a competing estimate**. It counts two narrow classes —
under-determination that is provable by execution alone — where the published figures count
all annotation errors, including wrong gold SQL, schema mismatches and ambiguous questions,
which need human judgement to identify. Anyone comparing 4.50% against 52.8% is comparing a
mechanical lower bound with a human audit.

What is unique here is the **instrument, not the number**: a deterministic, reproducible,
zero-human-labour detector for the subset that can be decided by running the query. That is
a smaller claim than the one this README made in its first draft, which asserted the count
was unpublished. That assertion was never checked before it was written down, which is
precisely the failure this project exists to catch, and it is recorded here rather than
quietly deleted.

## 2. The oracle-integrity figure is a lower bound, not a count

**4.50% of BIRD dev is excluded. The true share of unusable answer keys is higher**, and
three known gaps all push the same way:

- **Only the top-level `LIMIT` is probed for ties.** A `LIMIT` inside a subquery whose
  `ORDER BY` ties is not checked, because the subquery's rows are not observable from the
  outer projection without rewriting the query in ways that change what it means.
- **`SELECT *` cannot be probed.** The projection is not a fixed column list, so the tie
  group cannot be compared. One BIRD item is skipped for this.
- **Non-determinism is tested with two executions.** That catches `random()` and
  unstable scans. It will not catch a query that disagrees with itself one time in fifty.

No gap is known that would push the number *down*: every exclusion is on positive evidence
— a tie actually observed in the data, with distinct projections actually present.

## 3. It says nothing about how much any published BIRD score is wrong by

The 69 excluded items cannot distinguish a correct model from an incorrect one. Whether a
given system gained or lost from them depends on which answer it happened to produce, and
**that is not measured here.** A reader who converts 4.50% into an error bar on someone's
leaderboard number is doing something this report does not support.

## 4. The dialect is a decision, and it decides the result

BIRD is run on **SQLite**, the dialect its gold was written in. Measured on 80 sampled
golds that execute correctly under SQLite, **5 fail under DuckDB** (`IIF()`, and bare
columns outside `GROUP BY`) — so running the same check with DuckDB would report roughly
6% of BIRD's answer keys as broken. That number would be about the engine, not the
benchmark. Any reproduction that swaps the engine is measuring something else.

## 5. An earlier version of the ambiguity check was wrong by 79%

The first implementation matched any `LIMIT` in the parse tree against the *top-level*
`ORDER BY`, so a subquery that ordered and then limited flagged its outer query. On BIRD
dev it reported 29 ambiguous items, of which **23 were false positives** — a detector whose
output was three-quarters noise, and which looked entirely plausible until each flagged
item was checked by hand.

It is recorded here rather than quietly fixed because it is the strongest available
argument for the rest of this file: the check now has regression tests in both directions
(`tests/test_normalize.py::TestAmbiguityScoping`), and the tie check has tests that a real
tie is caught *and* a harmless one is not.

## 6. The retrieval ablation has never been run on BIRD

The four-arm table in the README is **8 demo items**, written by hand as a fixture. It is a
smoke test that the pipeline works end to end. It is not a measurement, no control has
certified it, and two of its four arms are marked `DEGENERATE` because the example index
was empty.

## 7. The model was measured on 19 items, and on nothing else

`sqlassay demonstrate` runs `qwen3.5:4b-mlx` over the 19 tie items whose valid answers are
enumerable, and that is the **entire** extent of model measurement in this repository. Its
headline — 2 items scored zero for a correct answer — rests on **n=3** answers that landed
inside a tie group. It shows the mechanism is real; it estimates nothing.

Four of the 23 tie items are skipped because `LIMIT > 1` admits *combinations* of tied rows
rather than single rows, and enumerating those correctly is a different problem. They may
well be penalised too; it is not measured.

No accuracy number, no ablation, no repeats, no controls. M3–M5 in [`PLAN.md`](PLAN.md) are
not done, and nothing here should be read as a claim about what a small local model can do
on BIRD.

## 8. The clock-dependence check is static, and that is a real limit

It matches the SQL forms — `julianday('now')`, `strftime(..., 'now')`, `CURRENT_DATE` and
friends. It cannot see a gold whose answer drifts for some other reason, and it would miss
a clock reference built by string concatenation at runtime. All 39 matched fragments on
BIRD dev were checked by hand and none is a false positive, but the count is a **lower
bound on time-dependence**, not a census of it.

An earlier version of the pattern required `'now'` to be the *first* argument, so it missed
every `strftime('%Y','now')` and reported 20 instead of 39 — a 49% undercount that looked
entirely plausible. The regression is pinned in `tests/test_normalize.py::TestClockDependence`.

## 9. One item's gold takes 154 seconds

`bird-dev-00701` executes in 154 s and returns one row. At the 30 s deadline used in the
first pass it was recorded as a failure; at 300 s it is admissible. It is correct SQL over
a 459 MB database, and it is reported as slow rather than broken — but it means a full pass
carries a two-and-a-half-minute item, and a reproduction with a shorter deadline will get a
different suite size for a reason that has nothing to do with BIRD.
