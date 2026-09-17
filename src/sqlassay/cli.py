"""The command line.

Five commands, in the order you would use them:

``selftest``   prove every gate can still go red
``demo``       build the demo database and its indexes
``index``      build the schema and value indexes for a database
``run``        run one arm over the items
``ablation``   run all four arms and print the comparison
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from sqlassay.controls import DEFAULT_EXPECTED_PATH, all_controls, build_context, load_expected
from sqlassay.demo import DEMO_DB_ID, build_demo_database, defective_items, demo_items
from sqlassay.demonstrate import demonstrate_item, write_report
from sqlassay.demonstrate import summarise as demo_summary
from sqlassay.engine import DuckDBDatabase
from sqlassay.floors import (
    ConstantCountPredictor,
    EmptyPredictor,
    FloorResult,
    GoldCache,
    NullPredictor,
    RandomGoldPredictor,
    measure_floor,
)
from sqlassay.gates import ExecuteGate, ParseGate, ResultSetGate
from sqlassay.model.ollama import DEFAULT_BASE_URL, OllamaChat, OllamaEmbedder
from sqlassay.oracle import check_suite
from sqlassay.retrieval import ARMS, Retriever, VectorStore, build_schema_index, build_value_index
from sqlassay.runner import GoldError, ItemRunner, summarise
from sqlassay.suites import BIRD_DEFAULT_ROOT, load_bird_dev

app = typer.Typer(add_completion=False, help="Execution-verified text-to-SQL for local models.")

DEFAULT_DB = Path("run/demo_sales.duckdb")
DEFAULT_INDEX = Path("run/indexes.db")


@app.command()
def selftest() -> None:
    """Run every gate's planted-defect probes.

    Exits non-zero if any gate cannot demonstrate failure. A green run from a
    gate that cannot go red is not evidence, so this is a precondition for
    reporting anything, not a diagnostic.
    """
    gates: list[ParseGate | ExecuteGate | ResultSetGate] = [ParseGate(), ExecuteGate(), ResultSetGate()]
    failed = False
    for g in gates:
        ok, detail = g.selftest()
        mark = "PASS" if ok else "FAIL"
        typer.echo(f"[{mark}] {g.name}@{g.version}: {detail}")
        failed |= not ok
    if failed:
        typer.echo("\nAt least one gate could not be shown to go red. Nothing may be reported.")
        raise typer.Exit(code=2)
    typer.echo("\nAll gates demonstrated failure on planted defects.")


@app.command()
def oracle(
    suite: str = typer.Option("demo", help="Which suite to check: demo | bird."),
    db_path: Path = typer.Option(DEFAULT_DB, help="demo suite only."),
    bird_root: Path = typer.Option(BIRD_DEFAULT_ROOT, help="bird suite only: extracted dev.zip."),
    revision: str = typer.Option("2024-06-27", help="bird suite only: 2024-06-27 | 2025-11-06."),
    include_defects: bool = typer.Option(False, help="demo suite only: add the broken fixtures."),
    limit: int = typer.Option(0, help="Check only the first N items. 0 checks all."),
    timeout_s: float = typer.Option(
        300.0,
        help="Per-query deadline. Generous by default so a slow-but-valid gold is not "
        "recorded as a defect in the benchmark.",
    ),
    out: Path = typer.Option(Path("run/oracle_integrity.json")),
) -> None:
    """Check every item's answer key. Makes no model calls.

    Runs the gold SQL as if it were the prediction, through all three gates.
    An item whose answer key cannot match itself cannot score a model, and is
    excluded and counted with its reason rather than failed against.
    """
    import time as _t

    if suite == "bird":
        loaded = load_bird_dev(bird_root, timeout_s=timeout_s, revision=revision)
        items = loaded.items[: limit or None]
        databases: dict[str, object] = dict(loaded.databases)
        closer = loaded.close
    elif suite == "demo":
        items = ((*demo_items(), *defective_items()) if include_defects else demo_items())[
            : limit or None
        ]
        db = DuckDBDatabase(db_path, timeout_s=timeout_s)
        databases = {DEMO_DB_ID: db}
        closer = db.close
    else:
        typer.echo(f"unknown suite {suite!r}; known: demo, bird")
        raise typer.Exit(code=2)

    t0 = _t.perf_counter()
    try:
        report = check_suite(items, databases)  # type: ignore[arg-type]
    finally:
        closer()
    elapsed = _t.perf_counter() - t0

    for v in report.excluded[:40]:
        typer.echo(f"  EXCLUDED  {v.item_id:22} {v.kind.value:26} {v.evidence[:58]}")
    if len(report.excluded) > 40:
        typer.echo(f"  ... and {len(report.excluded) - 40} more, all in {out}")

    record = report.as_record()
    record["suite"] = suite
    record["revision"] = revision if suite == "bird" else None
    record["elapsed_s"] = round(elapsed, 2)
    record["timeout_s"] = timeout_s
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")

    typer.echo("\n" + json.dumps(record["by_kind"], indent=2))
    n_excl = len(report.excluded)
    pct = 100.0 * n_excl / len(report.verdicts) if report.verdicts else 0.0
    typer.echo(
        f"\n{len(report.admissible_items)} of {len(report.verdicts)} items can score a model. "
        f"{n_excl} excluded ({pct:.2f}%). {elapsed:.1f}s. Written to {out}."
    )


@app.command()
def controls(
    revision: str = typer.Option("2025-11-06"),
    bird_root: Path = typer.Option(BIRD_DEFAULT_ROOT),
    oracle_report: Path = typer.Option(Path("reports/oracle_integrity_bird_dev_20251106.json")),
    expected: Path = typer.Option(DEFAULT_EXPECTED_PATH),
    seeds: int = typer.Option(10, help="Seeds for the random-gold chance band."),
    limit: int = typer.Option(0),
    allow_not_run: list[str] = typer.Option([], help="Control kinds whose NOT_RUN is tolerated."),
    out: Path = typer.Option(Path("reports/controls_bird_dev_20251106.json")),
) -> None:
    """Run every negative control and print the admissibility verdict.

    Exits 2 when the run is inadmissible. An INADMISSIBLE verdict is a result,
    not a crash: it says the numbers from this configuration may not be
    reported, and which control refused them.
    """
    from fireassay.admissibility import assess
    from fireassay.models import Run

    report = json.loads(oracle_report.read_text(encoding="utf-8"))
    excluded = {e["item_id"] for e in report["excluded"]}
    bands = load_expected(expected)
    loaded = load_bird_dev(bird_root, timeout_s=300.0, revision=revision)
    items = [i for i in loaded.items if i.item_id not in excluded][: limit or None]

    ctx = build_context(items, loaded.databases, bands, revision=revision, seeds=seeds)
    typer.echo(f"suite: {len(items)} admissible items, revision {revision}\n")

    outcomes = []
    try:
        for control in all_controls():
            outcome = control.run(ctx)
            outcomes.append(outcome)
            typer.echo(f"[{outcome.status:8}] {outcome.kind}@{control.version}: {outcome.detail}")
            for name, ok in outcome.cause_assertions.items():
                typer.echo(f"             {'ok ' if ok else 'NO '} {name}")
    finally:
        loaded.close()

    run = Run(
        id=f"controls-{revision}",
        suite_id=f"bird-dev-{revision}",
        suite_hash="",
        config_id="controls",
        config_hash="",
        env_json={},
        started_at="",
        finished_at=None,
        status="complete",
    )
    verdict = assess(run, [], outcomes, allow_not_run=allow_not_run)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "revision": revision,
                "n_items": len(items),
                "admissible": verdict.admissible,
                "failed_controls": list(verdict.failed_controls),
                "not_run_controls": list(verdict.not_run_controls),
                "controls": [o.model_dump() for o in outcomes],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    typer.echo("")
    if verdict.admissible:
        typer.echo("ADMISSIBLE: every control passed. Numbers from this configuration may be reported.")
    else:
        typer.echo("INADMISSIBLE: numbers from this configuration may NOT be reported.")
        if verdict.failed_controls:
            typer.echo(f"  failed : {', '.join(verdict.failed_controls)}")
        if verdict.not_run_controls:
            typer.echo(f"  not run: {', '.join(verdict.not_run_controls)}")
    typer.echo(f"Written to {out}.")
    if not verdict.admissible:
        raise typer.Exit(code=2)


@app.command()
def floors(
    revision: str = typer.Option("2025-11-06"),
    bird_root: Path = typer.Option(BIRD_DEFAULT_ROOT),
    oracle_report: Path = typer.Option(Path("reports/oracle_integrity_bird_dev_20251106.json")),
    seeds: int = typer.Option(10, help="Seeds for the random-gold chance band."),
    limit: int = typer.Option(0),
    out: Path = typer.Option(Path("reports/floors_bird_dev_20251106.json")),
) -> None:
    """Measure what trivial predictors score. Makes no model call.

    Every band in configs/expected.yaml is derived from this, and is written
    only after these numbers exist. A band chosen before its floor is measured
    is a guess with a threshold attached.
    """
    import statistics

    report = json.loads(oracle_report.read_text(encoding="utf-8"))
    excluded = {e["item_id"] for e in report["excluded"]}
    loaded = load_bird_dev(bird_root, timeout_s=300.0, revision=revision)
    items = [i for i in loaded.items if i.item_id not in excluded][: limit or None]
    typer.echo(f"admissible items: {len(items)} (of {len(loaded.items)}, {len(excluded)} excluded)")

    cache = GoldCache()
    results: list[FloorResult] = []
    out.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:
        """Rewrite the artifact after every predictor.

        A pass killed at seed 5 of 10 previously lost all ten, because the
        artifact was written once at the end. Each predictor is a whole sweep
        of the suite and is worth keeping on its own, so the partial file is
        always valid and always says how many seeds it actually holds.
        """
        band_so_far = [r.accuracy for r in results if r.predictor.startswith("random_gold")]
        out.write_text(
            json.dumps(
                {
                    "revision": revision,
                    "n_admissible": len(items),
                    "complete": len(band_so_far) >= seeds,
                    "chance_band_random_gold": {
                        "n_seeds": len(band_so_far),
                        "mean": statistics.fmean(band_so_far) if band_so_far else 0.0,
                        "sd": statistics.stdev(band_so_far) if len(band_so_far) > 1 else 0.0,
                        "min": min(band_so_far) if band_so_far else 0.0,
                        "max": max(band_so_far) if band_so_far else 0.0,
                    },
                    "floors": [r.as_record() for r in results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    try:
        for predictor in (NullPredictor(), EmptyPredictor(), ConstantCountPredictor()):
            r = measure_floor(predictor, items, loaded.databases, cache)
            results.append(r)
            flush()
            typer.echo(f"  {r.predictor:24} accuracy={r.accuracy:.6f}  ({r.correct}/{r.n})")

        for seed in range(seeds):
            rp = RandomGoldPredictor(seed=seed)
            rp.index(items)
            r = measure_floor(rp, items, loaded.databases, cache)
            results.append(r)
            flush()
            typer.echo(f"  {r.predictor:24} accuracy={r.accuracy:.6f}  ({r.correct}/{r.n})")
    finally:
        loaded.close()

    flush()
    chance = json.loads(out.read_text(encoding="utf-8"))["chance_band_random_gold"]
    typer.echo(
        f"\nrandom-gold chance band over {chance['n_seeds']} seeds: "
        f"mean {chance['mean']:.6f}, sd {chance['sd']:.6f}, "
        f"range [{chance['min']:.6f}, {chance['max']:.6f}]"
    )
    typer.echo(f"Written to {out}.")


@app.command()
def demonstrate(
    revision: str = typer.Option("2025-11-06", help="Which BIRD revision's ambiguous items."),
    bird_root: Path = typer.Option(BIRD_DEFAULT_ROOT),
    oracle_report: Path = typer.Option(Path("reports/oracle_integrity_bird_dev_20251106.json")),
    model: str = typer.Option("qwen3.5:4b-mlx"),
    base_url: str = typer.Option(DEFAULT_BASE_URL),
    think: bool = typer.Option(False),
    out: Path = typer.Option(Path("reports/ambiguity_cost.json")),
) -> None:
    """Measure whether the ambiguity actually costs a model marks.

    Runs the model on the items the oracle excluded, and counts how many
    times it returns an answer that is **equally correct** and scored zero.
    """
    report = json.loads(oracle_report.read_text(encoding="utf-8"))
    wanted = [e["item_id"] for e in report["excluded"] if e["kind"] == "ambiguous_tie"]
    chat = OllamaChat(model, base_url=base_url, think=think)
    loaded = load_bird_dev(bird_root, timeout_s=300.0, revision=revision)
    by_id = {i.item_id: i for i in loaded.items}

    results = []
    try:
        for iid in wanted:
            item = by_id.get(iid)
            if item is None:
                continue
            r = demonstrate_item(chat, loaded.databases[item.db_id], item)
            results.append(r)
            typer.echo(f"  {r.item_id:22} {r.bucket:22} {r.detail[:70]}")
    finally:
        loaded.close()

    write_report(results, out, {"revision": revision, "model": chat.config})
    s = demo_summary(results)
    typer.echo("\n" + json.dumps(s["buckets"], indent=2))
    n = len(s["penalised_by_ambiguity"])
    typer.echo(
        f"\n{n} item(s) where the model returned an equally correct answer and scored zero. "
        f"Written to {out}."
    )


@app.command()
def demo(
    db_path: Path = typer.Option(DEFAULT_DB, help="Where to write the demo DuckDB file."),
    index_path: Path = typer.Option(DEFAULT_INDEX, help="Where to write the vector indexes."),
    embed_model: str = typer.Option("qwen3-embedding:0.6b"),
    base_url: str = typer.Option(DEFAULT_BASE_URL),
) -> None:
    """Build the demo database and its schema and value indexes."""
    path = build_demo_database(db_path)
    typer.echo(f"demo database: {path}")
    _index(path, DEMO_DB_ID, index_path, embed_model, base_url)


@app.command()
def index(
    db_path: Path = typer.Argument(..., help="DuckDB file to index."),
    db_id: str = typer.Option(..., help="Identifier used to scope retrieval to this database."),
    index_path: Path = typer.Option(DEFAULT_INDEX),
    embed_model: str = typer.Option("qwen3-embedding:0.6b"),
    base_url: str = typer.Option(DEFAULT_BASE_URL),
) -> None:
    """Build the schema and value indexes for one database."""
    _index(db_path, db_id, index_path, embed_model, base_url)


def _index(db_path: Path, db_id: str, index_path: Path, embed_model: str, base_url: str) -> None:
    embedder = OllamaEmbedder(embed_model, base_url=base_url)
    with (
        DuckDBDatabase(db_path) as db,
        VectorStore(index_path, dim=embedder.dim, embed_model=embed_model) as store,
    ):
        n_schema = build_schema_index(store, db, embedder, db_id=db_id)
        n_values = build_value_index(store, db, embedder, db_id=db_id)
        typer.echo(f"schema index: {n_schema} table(s)")
        typer.echo(f"value index:  {n_values} literal(s)")
        # Frozen at build time. The example index is the one that grows, and
        # it must be frozen before a measured run rather than after -- see
        # retrieval/store.py on why this is enforced in the store.
        store.freeze("schema", "built from the database schema; fixed for this run")


@app.command()
def run(
    arm: str = typer.Option("full", help=f"One of: {', '.join(ARMS)}"),
    db_path: Path = typer.Option(DEFAULT_DB),
    index_path: Path = typer.Option(DEFAULT_INDEX),
    model: str = typer.Option("qwen3.5:4b-mlx"),
    embed_model: str = typer.Option("qwen3-embedding:0.6b"),
    base_url: str = typer.Option(DEFAULT_BASE_URL),
    think: bool = typer.Option(False, help="Leave the model's reasoning mode on."),
    limit: int = typer.Option(0, help="Run only the first N items. 0 runs all."),
    out: Path = typer.Option(Path("run/outcomes.jsonl"), help="Where to append per-item records."),
) -> None:
    """Run one arm over the demo items and print the gate breakdown."""
    if arm not in ARMS:
        typer.echo(f"unknown arm {arm!r}; known: {', '.join(ARMS)}")
        raise typer.Exit(code=2)
    summary = _run_arm(arm, db_path, index_path, model, embed_model, base_url, think, limit, out)
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def ablation(
    db_path: Path = typer.Option(DEFAULT_DB),
    index_path: Path = typer.Option(DEFAULT_INDEX),
    model: str = typer.Option("qwen3.5:4b-mlx"),
    embed_model: str = typer.Option("qwen3-embedding:0.6b"),
    base_url: str = typer.Option(DEFAULT_BASE_URL),
    think: bool = typer.Option(False),
    limit: int = typer.Option(0),
    out: Path = typer.Option(Path("run/outcomes.jsonl")),
) -> None:
    """Run all four arms and print them side by side.

    The comparison is the point. A single arm's accuracy says nothing about
    whether retrieval helped, which is the question this harness was built to
    answer.
    """
    rows = {}
    for name in ARMS:
        typer.echo(f"--- arm: {name} ---")
        rows[name] = _run_arm(name, db_path, index_path, model, embed_model, base_url, think, limit, out)
    typer.echo("\narm                        n   acc     parse_f  exec_f  rows_f  note")
    degenerate: list[str] = []
    for name, s in rows.items():
        if not s.get("n"):
            continue
        raw = s.get("degenerate") or []
        dead = [str(x) for x in raw] if isinstance(raw, list) else []
        note = "" if not dead else f"DEGENERATE: {', '.join(dead)} returned nothing"
        if dead:
            degenerate.append(name)
        typer.echo(
            f"{name:26} {s['n']:<3} {s['execution_accuracy']:.3f}   "
            f"{s['parse_failed']:<7}  {s['execute_failed']:<6}  {s['resultset_failed']}       {note}"
        )
    if degenerate:
        typer.echo(
            "\nAn arm marked DEGENERATE enabled a retrieval component that returned nothing on "
            "every item. Its score is not evidence about that component -- it is the previous arm "
            "under another name, and the two must not be read as a comparison."
        )
    typer.echo(
        "\nThese are 8 demo items. That is a smoke test, not a measurement: "
        "no control has certified this suite and no band has been calibrated on it."
    )


def _run_arm(
    arm: str,
    db_path: Path,
    index_path: Path,
    model: str,
    embed_model: str,
    base_url: str,
    think: bool,
    limit: int,
    out: Path,
) -> dict[str, object]:
    chat = OllamaChat(model, base_url=base_url, think=think)
    embedder = OllamaEmbedder(embed_model, base_url=base_url)
    items = demo_items()[: limit or None]
    out.parent.mkdir(parents=True, exist_ok=True)

    with (
        DuckDBDatabase(db_path) as db,
        VectorStore(index_path, dim=embedder.dim, embed_model=embed_model) as store,
        out.open("a", encoding="utf-8") as fh,
    ):
        runner = ItemRunner(
            chat=chat,
            retriever=Retriever(store=store, embedder=embedder),
            parse=ParseGate(),
            execute=ExecuteGate(),
            resultset=ResultSetGate(),
        )
        outcomes = []
        for item in items:
            try:
                outcome, elapsed = runner.run(item, db, arm)
            except GoldError as e:
                # Excluded and counted, never scored: a broken answer key is
                # a defect in the item, not a failure by the model.
                typer.echo(f"  {item.item_id}  EXCLUDED  {e}")
                continue
            outcomes.append(outcome)
            mark = "ok " if outcome.correct else "BAD"
            last = outcome.gates[-1]
            typer.echo(f"  {item.item_id}  {mark}  {elapsed:5.1f}s  {last.gate}: {last.evidence[:70]}")
            fh.write(
                json.dumps(
                    {
                        "item_id": outcome.item_id,
                        "arm": arm,
                        "correct": outcome.correct,
                        "sql": outcome.prediction.sql,
                        "config": chat.config,
                        "gates": [
                            {"gate": g.gate_id, "status": g.status.value, "evidence": g.evidence}
                            for g in outcome.gates
                        ],
                        "retrieval": outcome.prediction.usage.get("retrieval"),
                    }
                )
                + "\n"
            )
    return summarise(outcomes, arm)


if __name__ == "__main__":
    app()
