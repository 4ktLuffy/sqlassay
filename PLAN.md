# sqlassay — plan

Decided 2026-09-15. Supersedes the first sketch in the same file.

## The goal, in one line

> Publish an **admissible** measurement of what a 4B local model does on public
> text-to-SQL, using a harness that has been shown it can go red — and the
> benchmark-integrity findings that fall out of building it.

Two deliverables, not one. The second is the sleeper: the controls that qualify the
model's number also measure the **benchmark's own oracle**, and that result needs no
model at all.

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Control framework | **Depend on `../fireassay`** as a path dependency | `ControlOutcome`, `Band`, `admissibility.assess` already exist and `agent-assay` already reuses them. A third copy would be three dialects of one idea |
| Suite | **Spider 1.0 dev first** (1,034 items, 166 SQLite databases, ~1 GB) | Small enough to run repeats on; published baselines make our number comparable |
| Second suite | **BIRD dev only**, later. Never BIRD train | Not a disk argument — BIRD dev fits. **Floor effects**: frontier models sit near 60% on BIRD, so a 4B risks being pinned to the floor, and an ablation cannot measure a component's contribution against a floor. Spider leaves headroom. BIRD train is ~12 GB and buys nothing |
| Engine | Convert each SQLite database to DuckDB once, at load | One engine, one dialect, one set of type semantics. An `ATTACH` path would leave two |
| Band calibration | Floors **measured** on the suite before any model runs, then frozen | A band written after seeing the model's score is not a control |
| Run shape | **Two nights, not one.** Night 1: 200 stratified items x 4 arms x 5 repeats. Night 2: full 1,034 x 4 arms x 1 pass | Both cost ~5.7 h, so choosing between them was a false economy. Headline uncertainty is dominated by *item count*; the ablation is a *paired* comparison where item variance cancels. Two questions, two shapes. Night 1 licenses night 2's headline |
| Scoring | Unchanged from the scaffold: three gates, execution oracle, no judge | — |

## Measured constraints — do not re-derive

- `qwen3.5:4b-mlx` needs `think: false` on Ollama's native `/api/chat`; the
  OpenAI-compatible endpoint cannot disable reasoning. ~23 tok/s with it off.
- Generator 4.0 GB + embedder 639 MB ≈ 4.7 GB resident. Both stay loaded on 16 GB.
- **Disk: 24 GB free.** Every download in this plan is sized against that.
- Budget: zero paid API.

## Milestones

Each one has a deliverable and a gate. A milestone is not done because the code exists;
it is done when its gate passes.

### M0 — Profile hygiene · *Henos, 30 min*

Delete the 7 empty forks, pin the 6 real repos. Independent of everything below and the
highest impact per minute available anywhere in this plan.

**Gate:** `gh repo list 4ktLuffy` shows 10 repos, all original.

---

### M1 — Suite loader · *1 session + ~30 min machine*

Spider 1.0 dev: download, convert 166 SQLite databases to DuckDB, load the 1,034
question/gold pairs into `Item` records.

**Deliverable:** `sqlassay suite load spider-dev` produces a content-addressed suite.

**Gate:** every database opens read-only; every item has a `db_id` that resolves.

---

### M2 — The gold predictor, and what it says about Spider · *1 session, zero model calls*

**Built before M1, and validated against planted defects first.** A detector whose only
trial run is on data where it finds nothing has not been shown it can go red. So the demo
fixture gains three deliberately broken items — an ambiguous gold, a gold that does not
execute, and a gold that is non-deterministic — and the detector must catch exactly those
three and no others *before* Spider is downloaded.

The positive control, and the first publishable result.

Run **the gold SQL as if it were the prediction**, through all three gates. It must score
`1.000`. Every item where it does not is an item whose oracle is broken:

- gold that does not parse or does not execute,
- gold that is ambiguous (`LIMIT` with no `ORDER BY` — already detected in the scaffold),
- gold that is **non-deterministic** across two executions of the same query.

Those items are excluded and **counted**, never scored against the model. Spider's gold
set is known to contain errors; nobody has published the count under an execution oracle
with an ambiguity check.

**Deliverable:** `reports/oracle_integrity.json` — the excluded items, with the reason
per item, and the resulting admissible suite size.

**Gate:** `gold_predictor` scores exactly `1.000` on the suite **after** exclusions. If it
does not, the harness is wrong and nothing below may run.

> This is the milestone to publish first. It is a finding about a widely-used benchmark,
> it costs no inference, and it cannot be dismissed as "small model does badly".

---

### M3 — Floors measured, then bands frozen · *1 session + ~1 h machine*

Before any band is written, measure what the trivial predictors actually score on this
suite. **The floors are not assumed to be zero** — that lesson is already paid for in
`agent-assay/LIMITATIONS.md` §4.

| Predictor | Mechanism | Why its floor is not zero |
|---|---|---|
| `null_predictor` | always `SELECT NULL` | matches any item whose gold returns a single NULL |
| `empty_predictor` | always `SELECT 1 WHERE FALSE` | matches every item whose gold is legitimately empty |
| `random_gold` | another item's gold SQL, over 10 seeds | two items can share a result set |

**Deliverable:** `configs/expected.yaml`, every band carrying its measured floor and the
derivation, in the style of `agent-assay/src/agent_assay/controls/expected.yaml`.

**Gate:** each band's maximum is the measured floor plus room for one more item — never a
round number someone liked, and never widened later to make a run pass.

---

### M4 — Controls · *1 session*

Five, built on `fireassay.controls.base.ControlOutcome`. Each asserts **the cause, not the
outcome**, and each carries a writable twin.

| Control | Mutation | Expected | Cause asserted | Twin |
|---|---|---|---|---|
| `gold_predictor` | none (positive) | `1.000` | — | — |
| `null_predictor` | always `SELECT NULL` | at floor | `accuracy_at_floor` | base beats it by ≥ margin |
| `random_gold` | another item's gold | in chance band | `band_measured_this_suite` | base above band |
| `shuffled_gold` | rescore against deranged gold | collapses | `resultset_gate_fired`, not parse/execute | base above floor |
| `no_schema` | strip **all** schema from the prompt | collapses | **`execute_failures_spike`** — the model invents table names, so the failure moves to the execute gate | base with schema > 0 |

`no_schema` is the one that matters most for the ablation's credibility. If accuracy is
unchanged without any schema, retrieval was never doing anything and the whole ablation is
theatre. Asserting *which gate* the failures move to is what makes it evidence rather than
a number that went down.

**Gate:** `sqlassay controls run` exits 0 on the gold predictor and exits 2 when any
control is pointed at a deliberately broken harness. Both directions tested.

---

### M5 — The measured run · *overnight, unattended, ~6 h*

Stratified subset × 4 arms × 5 repeats at temperature 0.7, plus a temperature-0 pass.

**Deliverable:** `run/spider.db` plus `reports/` artifacts, and an admissibility verdict
from `fireassay.admissibility.assess` printed **before** any score.

**Gate:** the report refuses to print a headline unless the run is admissible — and if it
is inadmissible, it says so first and says which control failed. An `INADMISSIBLE` banner
is a result, not a failure.

---

### M6 — Evidence discipline · *1 session*

Match what `fireassay` and `agent-assay` already do: `EVIDENCE.md` mapping every claim to
its artifact and run id, `NUMBERS.md` listing every number with the command that
regenerates it, `make readme-check` failing on drift, and `LIMITATIONS.md` **first**.

**Gate:** `make readme-check && make number-audit` exits 0, and every number in the README
is regenerated from a committed artifact rather than typed.

---

### M7 — Distribution · *split by whether it needs a finding*

The work is already invisible; finishing more of it does not fix that. But M7 is three
things with three different readiness conditions, and shipping them together would hold
the two that are ready hostage to the one that is not.

**Now, in parallel with M1–M2 — neither needs an audience or a result:**

1. Split `freetier` into its own repo and publish to PyPI (**the name is available**,
   checked 2026-09-15). The only thing here a stranger would `pip install`; people find a
   package by searching its problem, not by being told about it.
2. Profile README tying the three assays into one thesis: evaluation integrity for RAG
   (`fireassay`), for agents (`agent-assay`), for text-to-SQL (`sqlassay`). This is also
   what makes M0's fork deletion pay off.

**After M2 — needs a finding:**

3. Post **M2's benchmark-integrity result** to r/LocalLLaMA. Not the model's score, and
   not the harness. A first post from an unknown account announcing a tool with no result
   spends the one good first impression on nothing.

## What this plan will not do

- No prompt tuning against the evaluation set. Optimisation belongs in a declared arm.
- No repair loop. No gate may feed back into generation.
- No band widened to make a run pass.
- No headline from a single pass.
- No BIRD train split. No fine-tuning. Both are out of scope and out of disk.

## Risks, and the answer to each

| Risk | Answer |
|---|---|
| The 4B scores near zero on Spider | That is a result, published as one. The inadmissible-run banner in `agent-assay` is the precedent |
| Spider is in the model's training data | Measurable, not fatal: compare against BIRD in M-later, and say what the comparison does and does not establish |
| The gold predictor fails on many items | That **is** M2's deliverable, not a blocker |
| 6 h overnight is optimistic | The run is resumable and records per item; a stopped run loses nothing |
| Disk fills | Spider only. BIRD dev is a later decision with its own check |
