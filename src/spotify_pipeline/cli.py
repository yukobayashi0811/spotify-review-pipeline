from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .classify import ControlledInterrupt, ModelConfig
from .costs import replay_cost
from .evaluate import export_predictions, sanitize_golden_source, score_golden
from .export import export_grading, save_checkpoint
from .full_run import run_full
from .frozen_run import run_frozen
from .io_utils import atomic_write_text, canonical, file_sha, write_json
from .orchestrator import run_pipeline
from .pipeline_run import run_pipeline as run_final_pipeline
from .prepare import ingest
from .providers import LocalMLXProvider, OpenAIResponsesProvider, Provider
from .rank import rerank
from .storage import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify-pipeline",
        description="Auditable Spotify review assignment pipeline. Offline commands never call an API.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Ingest and profile a CSV without model calls")
    prepare.add_argument("--input", required=True, type=Path)
    prepare.add_argument("--state", required=True, type=Path)
    prepare.add_argument("--report", required=True, type=Path)

    full = commands.add_parser(
        "full-run", help="Run or resume the leakage-safe full-corpus hybrid pipeline"
    )
    full.add_argument("--input", required=True, type=Path)
    full.add_argument("--config", required=True, type=Path)
    full.add_argument("--mode", choices=("frozen", "training"), default="frozen")

    run = commands.add_parser(
        "run",
        help=(
            "Final pipeline (pipeline-v4) for any input CSV: prepare -> local classify -> "
            "risk-routed Luna -> blind Luna verify -> group -> rank -> Luna memo -> export. "
            "Makes GPT-6 Luna calls through the Codex CLI unless every stage is cached."
        ),
    )
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--config", type=Path, default=Path("config/pipeline.json"))
    run.add_argument("--output", type=Path, help="Output directory (default: runs/<input stem>)")
    run.add_argument(
        "--interrupt-after-luna-batches", type=int,
        help="Resume demonstration: stop after this many saved Luna batches (exit 75); "
             "rerun the same command without this flag to resume",
    )

    dev_run = commands.add_parser(
        "dev-run", help="Legacy development orchestrator (local MLX or OpenAI API provider)"
    )
    _add_run_arguments(dev_run)

    pilot = commands.add_parser("pilot", help="Legacy development cold/warm pilot (superseded by run)")
    _add_run_arguments(pilot)
    pilot.add_argument("--mode", required=True, choices=("cold", "warm"))
    pilot.add_argument("--cost-dir", required=True, type=Path)

    rank = commands.add_parser("rank", help="Deterministically rerank from saved state")
    rank.add_argument("--state", required=True, type=Path)
    rank.add_argument("--output", required=True, type=Path)
    rank.add_argument("--summary", required=True, type=Path)

    checkpoint = commands.add_parser("checkpoint", help="Save a resume checkpoint snapshot")
    checkpoint.add_argument("--state", required=True, type=Path)
    checkpoint.add_argument("--name", required=True, choices=("before", "after"))
    checkpoint.add_argument("--output", required=True, type=Path)

    export = commands.add_parser("export-grading", help="Create the grading adapter/export")
    export.add_argument("--state", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--allow-incomplete", action="store_true")

    sanitize = commands.add_parser("golden-sanitize", help="Create label-free prediction input")
    sanitize.add_argument("--golden", required=True, type=Path)
    sanitize.add_argument("--output", required=True, type=Path)

    predictions = commands.add_parser("golden-export-predictions")
    predictions.add_argument("--state", required=True, type=Path)
    predictions.add_argument("--output", required=True, type=Path)

    score = commands.add_parser("golden-score", help="Score saved predictions after labeling")
    score.add_argument("--golden", required=True, type=Path)
    score.add_argument("--predictions", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.add_argument("--cases", required=True, type=Path)

    cost = commands.add_parser("cost-replay", help="Default offline cost replay; never calls an API")
    cost.add_argument("--calls", required=True, type=Path)
    cost.add_argument("--rates", required=True, type=Path)
    cost.add_argument("--usage", required=True, type=Path)
    cost.add_argument("--report", required=True, type=Path)
    cost.add_argument("--run-metadata", type=Path)
    cost.add_argument("--projected-rows", type=int, default=660_622)
    cost.add_argument("--projected-nonempty", type=int, default=660_609)
    cost.add_argument("--projected-distinct", type=int, default=484_189)
    cost.add_argument("--budget-usd", type=float, default=25.0)
    cost.add_argument("--conservative-multiplier", type=float, default=1.25)
    return parser


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider", choices=("local-mlx", "openai"), default="local-mlx",
        help="Execution provider. local-mlx is offline and is the default.",
    )
    parser.add_argument(
        "--execute-paid", action="store_true",
        help="Required acknowledgement only when --provider openai is selected",
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--phase", choices=("initial", "resume"), default="initial")
    parser.add_argument(
        "--stop-after",
        choices=("prepare", "classify", "verify", "group", "rank", "recommend"),
        default="recommend",
    )
    parser.add_argument(
        "--model",
        help="Exact model ID. Defaults to local Gemma for local-mlx or gpt-6-luna for openai.",
    )
    parser.add_argument(
        "--model-revision",
        default="475b9088d29754a3379866cf5aeb6b41acd313c2",
        help="Pinned local Hugging Face snapshot revision",
    )
    parser.add_argument("--local-temperature", type=float, default=0.0)
    parser.add_argument("--effort", default="none")
    parser.add_argument("--fallback-model")
    parser.add_argument("--fallback-effort", default="low")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=6000)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--spend-limit-usd", type=float, default=25)
    parser.add_argument("--fallback-fraction", type=float, default=0.02)
    parser.add_argument("--interrupt-after-batches", type=int)


def model_config(args: argparse.Namespace) -> ModelConfig:
    default_model = (
        "mlx-community/gemma-4-e4b-it-4bit"
        if args.provider == "local-mlx"
        else "gpt-6-luna"
    )
    return ModelConfig(
        model=args.model or default_model,
        effort=args.effort,
        fallback_model=args.fallback_model,
        fallback_effort=args.fallback_effort,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        max_attempts=args.max_attempts,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
        spend_limit_usd=args.spend_limit_usd,
        fallback_fraction=args.fallback_fraction,
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = ingest(args.input, args.state, args.report)
        elif args.command == "full-run":
            result = (
                run_frozen(args.input, args.config)
                if args.mode == "frozen"
                else run_full(args.input, args.config)
            )
        elif args.command == "run":
            result = run_final_pipeline(
                args.input, args.config, output_dir=args.output,
                interrupt_after_luna_batches=args.interrupt_after_luna_batches,
            )
        elif args.command == "dev-run":
            provider = _provider_for_args(args)
            result = run_pipeline(
                input_csv=args.input,
                state_path=args.state,
                artifacts_dir=args.artifacts,
                provider=provider,
                config=model_config(args),
                phase=args.phase,
                stop_after=args.stop_after,
                interrupt_after_batches=args.interrupt_after_batches,
            )
        elif args.command == "pilot":
            provider = _provider_for_args(args)
            result = _run_pilot(args, provider)
        elif args.command == "rank":
            result = {"ranking": rerank(state_path=args.state, output_csv=args.output, summary_json=args.summary)}
        elif args.command == "checkpoint":
            result = save_checkpoint(args.state, args.output)
            with Store(args.state) as store:
                store.set_metadata(f"checkpoint_{args.name}", result)
        elif args.command == "export-grading":
            result = export_grading(
                state_path=args.state,
                output_dir=args.output,
                allow_incomplete=args.allow_incomplete,
            )
        elif args.command == "golden-sanitize":
            result = sanitize_golden_source(args.golden, args.output)
        elif args.command == "golden-export-predictions":
            export_predictions(args.state, args.output)
            result = {"saved": str(args.output)}
        elif args.command == "golden-score":
            result = score_golden(args.golden, args.predictions, args.output, args.cases)
        elif args.command == "cost-replay":
            result = replay_cost(
                calls_path=args.calls,
                rates_path=args.rates,
                usage_path=args.usage,
                report_path=args.report,
                run_metadata_path=args.run_metadata,
                projected_rows=args.projected_rows,
                projected_nonempty=args.projected_nonempty,
                projected_distinct=args.projected_distinct,
                budget_usd=args.budget_usd,
                conservative_multiplier=args.conservative_multiplier,
            )
        else:
            raise AssertionError(args.command)
    except ControlledInterrupt as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(75) from error
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _provider_for_args(args: argparse.Namespace) -> Provider:
    if args.max_workers != 1:
        raise SystemExit(
            "Concurrent dispatch is not enabled for pipeline runs. The measured two-worker "
            "local probe did not meet the 10% adoption threshold; use --max-workers 1."
        )
    if args.provider == "openai":
        if not args.execute_paid:
            raise SystemExit(
                "Refusing paid API calls. Add --execute-paid after reviewing the spend, "
                "output, worker, and fallback limits."
            )
        return OpenAIResponsesProvider()
    if args.execute_paid:
        raise SystemExit("--execute-paid is not used with the offline local-mlx provider")
    if args.fallback_model and args.fallback_model != (args.model or "mlx-community/gemma-4-e4b-it-4bit"):
        raise SystemExit(
            "The in-process local-mlx provider cannot switch to a different fallback model. "
            "Leave --fallback-model unset or run the fallback as a separate bounded job."
        )
    if not 0 <= args.local_temperature <= 1:
        raise SystemExit("--local-temperature must be between 0 and 1")
    return LocalMLXProvider(
        model_revision=args.model_revision,
        temperature=args.local_temperature,
    )


def _run_pilot(args: argparse.Namespace, provider: Provider) -> dict[str, Any]:
    config = model_config(args)
    expected_cost_100_sha = "c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e"
    actual_sha = file_sha(args.input)
    if actual_sha != expected_cost_100_sha:
        raise ValueError(
            "The required pilot must use cost_100.csv unchanged; "
            f"expected {expected_cost_100_sha}, received {actual_sha}"
        )
    if config.max_workers != 1:
        raise ValueError("The required 100-review pilot must start with --max-workers 1")
    state_exists = args.state.exists()
    if args.mode == "cold" and state_exists:
        with Store(args.state) as store:
            if store.completed_count() or store.connection.execute("SELECT COUNT(*) FROM result_cache").fetchone()[0]:
                raise ValueError("Cold pilot requires a new state path with an empty result cache")
    if args.mode == "warm" and not state_exists:
        raise ValueError("Warm pilot requires the saved cold-run state")
    run_id = f"pilot-{args.mode}-{uuid.uuid4()}"
    started = time.perf_counter()
    with Store(args.state) as store:
        before_enrich_calls = int(
            store.connection.execute("SELECT COUNT(*) FROM calls WHERE role='enrich'").fetchone()[0]
        )
        before_completed = store.completed_count()
    summary = run_pipeline(
        input_csv=args.input,
        state_path=args.state,
        artifacts_dir=args.artifacts,
        provider=provider,
        config=config,
        phase=args.phase,
        stop_after=args.stop_after,
        interrupt_after_batches=args.interrupt_after_batches,
        run_id=run_id,
    )
    wall = round(time.perf_counter() - started, 6)
    cost_dir = args.cost_dir
    cost_dir.mkdir(parents=True, exist_ok=True)
    _copy_pilot_artifacts(args.artifacts, cost_dir / f"pilot_{args.mode}")
    with Store(args.state) as store:
        after_enrich_calls = int(
            store.connection.execute("SELECT COUNT(*) FROM calls WHERE role='enrich'").fetchone()[0]
        )
        source_count = store.source_count()
        unique_texts = int(
            store.connection.execute(
                "SELECT COUNT(DISTINCT review_text) FROM source_rows WHERE TRIM(review_text)<>''"
            ).fetchone()[0]
        )
        completed = store.completed_count()
        quarantined = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM records WHERE status='quarantined'"
            ).fetchone()[0]
        )
        direct_cache_hits = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL"
            ).fetchone()[0]
        )
        pilot_ids = [
            row[0] for row in store.connection.execute("SELECT review_id FROM source_rows ORDER BY rowid")
        ]
        ingestion = store.get_metadata("ingestion_report")
        _export_pilot_records(store, cost_dir / "pilot_records.jsonl")
        _export_pilot_calls(store, cost_dir / "pilot_calls.jsonl")
    metadata_path = cost_dir / "pilot_run.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    runtime_metadata = (
        provider.runtime_metadata()
        if callable(getattr(provider, "runtime_metadata", None))
        else {"provider": provider.name}
    )
    metadata.update(
        {
            "pilot_rows": 100,
            "input_sha256": ingestion["file_sha256"],
            "pilot_ids": pilot_ids,
            "source_records": source_count,
            "unique_nonempty_texts": unique_texts,
            "completed_records": completed,
            "quarantined_or_failed_records": quarantined,
            "direct_exact_text_cache_hits": direct_cache_hits,
            "model": config.model,
            "provider": provider.name,
            "effort": config.effort,
            "batch_size": config.batch_size,
            "worker_limit": config.max_workers,
            f"{args.mode}_wall_seconds": wall,
            f"{args.mode}_stage_wall_seconds": summary.get("stage_wall_seconds", {}),
            f"{args.mode}_verification_selected": summary.get("stages", {})
            .get("verify", {})
            .get("selected"),
            f"{args.mode}_new_enrichment_calls": after_enrich_calls - before_enrich_calls,
            f"{args.mode}_saved_result_reuses": before_completed if args.mode == "warm" else 0,
            "controls": {
                "spend_limit_usd": config.spend_limit_usd,
                "max_output_tokens": config.max_output_tokens,
                "max_workers": config.max_workers,
                "fallback_fraction": config.fallback_fraction,
            },
            f"{args.mode}_provider_runtime": runtime_metadata,
        }
    )
    if args.mode == "cold":
        metadata["provider_runtime"] = runtime_metadata
    write_json(metadata_path, metadata)
    return {"mode": args.mode, "wall_seconds": wall, "run_id": run_id, "summary": summary}


def _export_pilot_records(store: Store, path: Path) -> None:
    lines = []
    for row in store.connection.execute("SELECT * FROM records ORDER BY review_id"):
        value = dict(row)
        if value["status"] == "quarantined":
            item = {
                "review_id": value["review_id"],
                "source_sha256": value["source_sha256"],
                "status": "quarantined",
                "reason": value["reason"],
            }
        else:
            item = {
                "review_id": value["review_id"],
                "source_sha256": value["source_sha256"],
                "status": "completed",
                "topic": value["topic"],
                "intent": value["intent"],
                "sentiment": value["sentiment"],
                "severity": value["severity"],
                "entities": json.loads(value["entities_json"]),
                "evidence_quote": value["evidence_quote"],
                "needs_review": bool(value["needs_review"]),
                "label_config": value["label_config"],
            }
            if value["cache_source_id"]:
                item["cache_source_id"] = value["cache_source_id"]
        lines.append(canonical(item))
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _copy_pilot_artifacts(source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "claims.csv",
        "group_summary.json",
        "ingestion_report.json",
        "issues.csv",
        "membership.csv",
        "memo.md",
        "memo_evidence_pack.json",
        "memo_payload.json",
        "ranking.csv",
        "ranking.json",
        "run_summary.json",
        "verification_report.json",
        "checkpoint_after.json",
    ):
        source = source_dir / name
        if source.exists():
            shutil.copy2(source, destination_dir / name)


def _export_pilot_calls(store: Store, path: Path) -> None:
    lines = []
    for event in store.call_rows():
        lines.append(canonical(event))
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


if __name__ == "__main__":
    main()
