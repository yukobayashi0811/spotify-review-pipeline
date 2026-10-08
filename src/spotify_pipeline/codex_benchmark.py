from __future__ import annotations

import csv
import json
import platform
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .classify import ModelConfig, run_enrichment
from .constants import SOURCE_FIELDS
from .io_utils import atomic_write_text, canonical, file_sha, write_csv, write_json
from .orchestrator import run_pipeline
from .prepare import ingest
from .providers import CodexExecProvider
from .storage import Store


ACKNOWLEDGEMENT = "RUN-DEVELOPMENT-CODEX-SMOKE"
MAX_DEVELOPMENT_SAMPLE = 10_000


def _execution_fingerprint(expected_cli_version: str) -> str:
    version_slug = expected_cli_version.removeprefix("codex-cli ").replace(" ", "-")
    return (
        f"codex-cli-{version_slug}:subscription:ephemeral:"
        "empty-read-only:tools-disabled:provider-output-cap-unavailable"
    )


def read_sample(path: str | Path, sample_size: int) -> list[dict[str, str]]:
    if not 1 <= sample_size <= MAX_DEVELOPMENT_SAMPLE:
        raise ValueError(f"sample_size must be between 1 and {MAX_DEVELOPMENT_SAMPLE}")
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if tuple(reader.fieldnames or ())[: len(SOURCE_FIELDS)] != SOURCE_FIELDS:
            raise ValueError(f"Unexpected source schema: {reader.fieldnames}")
        rows: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("Malformed source row")
            row = {field: raw[field] for field in SOURCE_FIELDS}
            if row["review_id"] in seen_ids:
                raise ValueError(f"Duplicate review_id in benchmark sample: {row['review_id']}")
            seen_ids.add(row["review_id"])
            rows.append(row)
            if len(rows) == sample_size:
                break
    if len(rows) != sample_size:
        raise ValueError(f"Requested {sample_size} rows but source contains only {len(rows)}")
    return rows


def deterministic_shards(
    rows: list[dict[str, str]], workers: int
) -> list[list[dict[str, str]]]:
    if workers not in {1, 3}:
        raise ValueError("Development benchmark supports exactly 1 or 3 workers")
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(row["review_text"], []).append((index, row))
    ordered_groups = sorted(
        groups.values(),
        key=lambda group: (-len(group), min(index for index, _ in group)),
    )
    shards: list[list[tuple[int, dict[str, str]]]] = [[] for _ in range(workers)]
    loads = [0 for _ in range(workers)]
    for group in ordered_groups:
        target = min(range(workers), key=lambda index: (loads[index], index))
        shards[target].extend(group)
        loads[target] += len(group)
    return [[row for _, row in sorted(shard)] for shard in shards]


def write_source_csv(path: str | Path, rows: list[dict[str, str]]) -> None:
    write_csv(path, list(SOURCE_FIELDS), rows)


def _worker(
    *,
    worker_index: int,
    rows: list[dict[str, str]],
    output: Path,
    prompt_path: Path,
    executable: str,
    expected_cli_version: str,
    model: str,
    effort: str,
    batch_size: int,
    timeout_seconds: float,
    experiment_id: str,
) -> dict[str, Any]:
    worker_dir = output / f"worker-{worker_index:02d}"
    worker_dir.mkdir(parents=True, exist_ok=False)
    source_path = worker_dir / "input.csv"
    state_path = worker_dir / "state.db"
    write_source_csv(source_path, rows)
    ingestion = ingest(source_path, state_path, worker_dir / "ingestion.json")
    provider = CodexExecProvider(
        trace_root=worker_dir / "traces",
        executable=executable,
        expected_version=expected_cli_version,
        ephemeral=True,
    )
    config = ModelConfig(
        model=model,
        effort=effort,
        batch_size=batch_size,
        max_workers=1,
        max_attempts=1,
        max_output_tokens=4000,
        timeout_seconds=timeout_seconds,
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=_execution_fingerprint(expected_cli_version),
    )
    started = time.perf_counter()
    result = run_enrichment(
        state_path=state_path,
        provider=provider,
        prompt_path=prompt_path,
        config=config,
        run_id=f"{experiment_id}-worker-{worker_index:02d}",
        phase="initial",
    )
    wall_seconds = time.perf_counter() - started
    with Store(state_path) as store:
        records = list(store.completed_records())
        calls = list(store.call_rows())
        statuses = store.status_counts()
    atomic_write_text(
        worker_dir / "development_records.jsonl",
        "".join(canonical(record) + "\n" for record in records),
    )
    atomic_write_text(
        worker_dir / "agent_interactions.jsonl",
        "".join(canonical(call) + "\n" for call in calls),
    )
    runtime = provider.runtime_metadata()
    write_json(worker_dir / "runtime.json", runtime)
    summary = {
        "worker_index": worker_index,
        "input_rows": len(rows),
        "input_sha256": file_sha(source_path),
        "completed_records": len(records),
        "status_counts": statuses,
        "wall_seconds": round(wall_seconds, 6),
        "call_count": len(calls),
        "input_tokens": sum(call["input_tokens"] or 0 for call in calls),
        "cached_input_tokens": sum(call["cached_input_tokens"] or 0 for call in calls),
        "output_tokens": sum(call["output_tokens"] or 0 for call in calls),
        "token_usage_complete": all(
            call["input_tokens"] is not None and call["output_tokens"] is not None
            for call in calls
        ),
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
        "run_enrichment": result,
        "ingestion_counts": ingestion["counts"],
        "records_path": str(worker_dir / "development_records.jsonl"),
        "interactions_path": str(worker_dir / "agent_interactions.jsonl"),
    }
    write_json(worker_dir / "summary.json", summary)
    return {"summary": summary, "records": records, "calls": calls}


def run_development_benchmark(
    *,
    input_path: str | Path,
    output_dir: str | Path,
    prompt_path: str | Path,
    sample_size: int,
    workers: int,
    model: str,
    effort: str,
    executable: str,
    expected_cli_version: str,
    batch_size: int,
    timeout_seconds: float,
    acknowledgement: str,
) -> dict[str, Any]:
    if acknowledgement != ACKNOWLEDGEMENT:
        raise RuntimeError(
            "Development Codex execution requires the explicit acknowledgement "
            f"{ACKNOWLEDGEMENT!r}"
        )
    if workers not in {1, 3}:
        raise ValueError("workers must be 1 or 3")
    if not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to overwrite nonempty experiment directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    rows = read_sample(input_path, sample_size)
    shards = deterministic_shards(rows, workers)
    experiment_id = f"codex-dev-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    plan = {
        "experiment_id": experiment_id,
        "development_only": True,
        "grading_eligible": False,
        "purpose": "Measure subscription-authenticated Codex classification speed and format quality.",
        "input_path": str(Path(input_path).resolve()),
        "input_sha256": file_sha(input_path),
        "sample_policy": f"first {sample_size} rows; no golden labels loaded",
        "sample_size": sample_size,
        "workers": workers,
        "model": model,
        "effort": effort,
        "batch_size": batch_size,
        "timeout_seconds": timeout_seconds,
        "expected_cli_version": expected_cli_version,
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
        "api_credentials_used": False,
        "provider_output_token_cap": "unavailable_in_codex_exec_0.159.3",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "acknowledgement": acknowledgement,
    }
    write_json(output / "experiment_plan.json", plan)
    write_source_csv(output / "sample.csv", rows)
    manifest: list[dict[str, Any]] = []
    for worker_index, shard in enumerate(shards, start=1):
        for row in shard:
            manifest.append(
                {
                    "worker_index": worker_index,
                    "review_id": row["review_id"],
                    "exact_text_group_size": sum(
                        candidate["review_text"] == row["review_text"] for candidate in rows
                    ),
                }
            )
    write_csv(
        output / "shard_manifest.csv",
        ["worker_index", "review_id", "exact_text_group_size"],
        manifest,
    )

    started = time.perf_counter()
    worker_outputs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _worker,
                worker_index=index,
                rows=shard,
                output=output,
                prompt_path=Path(prompt_path),
                executable=executable,
                expected_cli_version=expected_cli_version,
                model=model,
                effort=effort,
                batch_size=batch_size,
                timeout_seconds=timeout_seconds,
                experiment_id=experiment_id,
            )
            for index, shard in enumerate(shards, start=1)
            if shard
        ]
        for future in as_completed(futures):
            worker_outputs.append(future.result())
    wall_seconds = time.perf_counter() - started

    records = [record for value in worker_outputs for record in value["records"]]
    calls = [call for value in worker_outputs for call in value["calls"]]
    expected_ids = [row["review_id"] for row in rows]
    by_id = {record["review_id"]: record for record in records}
    returned_ids = [record["review_id"] for record in records]
    duplicates = sorted({review_id for review_id in returned_ids if returned_ids.count(review_id) > 1})
    unknown = sorted(set(returned_ids) - set(expected_ids))
    missing = sorted(set(expected_ids) - set(returned_ids))
    quote_failures = sorted(
        record["review_id"]
        for record in records
        if record["evidence_quote"] not in record["review_text"]
    )
    valid = not duplicates and not unknown and not missing and not quote_failures
    ordered_records = [by_id[review_id] for review_id in expected_ids if review_id in by_id]
    ordered_calls = sorted(calls, key=lambda call: (call["run_id"], call["request_id"]))
    atomic_write_text(
        output / "development_records.jsonl",
        "".join(canonical(record) + "\n" for record in ordered_records),
    )
    write_csv(
        output / "predictions.csv",
        [
            "review_id",
            "topic",
            "intent",
            "sentiment",
            "severity",
            "entities",
            "evidence_quote",
            "needs_review",
        ],
        [
            {
                "review_id": record["review_id"],
                "topic": record["topic"],
                "intent": record["intent"],
                "sentiment": record["sentiment"],
                "severity": record["severity"],
                "entities": json.dumps(record["entities"], ensure_ascii=False),
                "evidence_quote": record["evidence_quote"],
                "needs_review": str(record["needs_review"]).lower(),
            }
            for record in ordered_records
        ],
    )
    atomic_write_text(
        output / "agent_interactions.jsonl",
        "".join(canonical(call) + "\n" for call in ordered_calls),
    )
    merge_report = {
        "valid": valid,
        "expected_records": sample_size,
        "merged_records": len(records),
        "missing_review_ids": missing,
        "unknown_review_ids": unknown,
        "duplicate_review_ids": duplicates,
        "evidence_quote_failures": quote_failures,
        "exact_text_groups_split_across_workers": [],
    }
    write_json(output / "merge_report.json", merge_report)
    worker_summaries = sorted(
        (value["summary"] for value in worker_outputs), key=lambda value: value["worker_index"]
    )
    report = {
        **plan,
        "wall_seconds": round(wall_seconds, 6),
        "worker_summaries": worker_summaries,
        "completed_records": len(records),
        "call_count": len(calls),
        "input_tokens": sum(call["input_tokens"] or 0 for call in calls),
        "cached_input_tokens": sum(call["cached_input_tokens"] or 0 for call in calls),
        "output_tokens": sum(call["output_tokens"] or 0 for call in calls),
        "token_usage_complete": all(
            call["input_tokens"] is not None and call["output_tokens"] is not None
            for call in calls
        ),
        "merge_valid": valid,
        "grading_export_written": False,
        "golden_labels_loaded": False,
        "prediction_export_written": True,
    }
    write_json(output / "benchmark_report.json", report)
    if not valid:
        raise RuntimeError(f"Development benchmark merge validation failed: {merge_report}")
    return report


def replay_development_warm(
    *,
    output_dir: str | Path,
    prompt_path: str | Path,
    executable: str,
    expected_cli_version: str,
    acknowledgement: str,
) -> dict[str, Any]:
    """Prove exact-result-cache reuse without making another model call."""
    if acknowledgement != ACKNOWLEDGEMENT:
        raise RuntimeError(
            "Development Codex execution requires the explicit acknowledgement "
            f"{ACKNOWLEDGEMENT!r}"
        )
    output = Path(output_dir)
    plan_path = output / "experiment_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"Missing cold experiment plan: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not plan.get("development_only") or plan.get("grading_eligible") is not False:
        raise RuntimeError("Refusing warm replay for an unrecognized experiment directory")
    summaries: list[dict[str, Any]] = []
    started = time.perf_counter()
    for worker_index in range(1, int(plan["workers"]) + 1):
        worker_dir = output / f"worker-{worker_index:02d}"
        state_path = worker_dir / "state.db"
        if not state_path.is_file():
            raise FileNotFoundError(f"Missing worker state: {state_path}")
        with Store(state_path) as store:
            calls_before = len(list(store.call_rows()))
            completed_before = store.completed_count()
        provider = CodexExecProvider(
            trace_root=worker_dir / "warm-traces",
            executable=executable,
            expected_version=expected_cli_version,
            ephemeral=True,
        )
        config = ModelConfig(
            model=plan["model"],
            effort=plan["effort"],
            batch_size=int(plan["batch_size"]),
            max_workers=1,
            max_attempts=1,
            max_output_tokens=4000,
            timeout_seconds=float(plan["timeout_seconds"]),
            spend_limit_usd=1.0,
            fallback_model=None,
            fallback_fraction=0.0,
            input_rate_per_million=0.0,
            cached_input_rate_per_million=0.0,
            output_rate_per_million=0.0,
            execution_fingerprint=_execution_fingerprint(expected_cli_version),
        )
        worker_started = time.perf_counter()
        result = run_enrichment(
            state_path=state_path,
            provider=provider,
            prompt_path=prompt_path,
            config=config,
            run_id=f"{plan['experiment_id']}-warm-worker-{worker_index:02d}",
            phase="resume",
        )
        with Store(state_path) as store:
            calls_after = len(list(store.call_rows()))
            completed_after = store.completed_count()
        summaries.append(
            {
                "worker_index": worker_index,
                "wall_seconds": round(time.perf_counter() - worker_started, 6),
                "completed_before": completed_before,
                "completed_after": completed_after,
                "new_model_calls": calls_after - calls_before,
                "run_enrichment": result,
            }
        )
    report = {
        "development_only": True,
        "grading_eligible": False,
        "experiment_id": plan["experiment_id"],
        "mode": "warm_exact_result_cache_replay",
        "workers": plan["workers"],
        "sample_size": plan["sample_size"],
        "wall_seconds": round(time.perf_counter() - started, 6),
        "new_model_calls": sum(item["new_model_calls"] for item in summaries),
        "completed_before": sum(item["completed_before"] for item in summaries),
        "completed_after": sum(item["completed_after"] for item in summaries),
        "worker_summaries": summaries,
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
    }
    write_json(output / "warm_replay.json", report)
    if report["new_model_calls"] != 0 or report["completed_after"] != plan["sample_size"]:
        raise RuntimeError(f"Warm exact-cache replay failed: {report}")
    return report


def merge_development_worker_states(output_dir: str | Path) -> dict[str, Any]:
    """Merge isolated classification states into one validated downstream state."""
    output = Path(output_dir)
    plan = json.loads((output / "experiment_plan.json").read_text(encoding="utf-8"))
    sample_path = output / "sample.csv"
    consolidated = output / "consolidated"
    state_path = consolidated / "state.db"
    if state_path.exists():
        raise FileExistsError(f"Refusing to overwrite consolidated state: {state_path}")
    consolidated.mkdir(parents=True, exist_ok=True)
    ingestion = ingest(sample_path, state_path, consolidated / "ingestion.json")
    originals: list[dict[str, Any]] = []
    cached: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for worker_index in range(1, int(plan["workers"]) + 1):
        worker_state = output / f"worker-{worker_index:02d}" / "state.db"
        with Store(worker_state) as worker:
            for record in worker.completed_records():
                (cached if record.get("cache_source_id") else originals).append(record)
            calls.extend(worker.call_rows())
    with Store(state_path) as store:
        for record in originals:
            source_row = store.connection.execute(
                "SELECT * FROM source_rows WHERE review_id=?", (record["review_id"],)
            ).fetchone()
            if source_row is None:
                raise RuntimeError(f"Worker returned unknown review_id: {record['review_id']}")
            source = dict(source_row)
            if source["source_sha256"] != record["source_sha256"]:
                raise RuntimeError(f"Source hash mismatch for {record['review_id']}")
            store.save_completed(
                source,
                record,
                record["label_config"],
                int(record["attempt_count"]),
            )
        for record in cached:
            source_row = store.connection.execute(
                "SELECT * FROM source_rows WHERE review_id=?", (record["review_id"],)
            ).fetchone()
            if source_row is None:
                raise RuntimeError(f"Worker returned unknown cached review_id: {record['review_id']}")
            source = dict(source_row)
            cache_source_id = record.get("cache_source_id")
            cache_source = store.record_for_id(cache_source_id) if cache_source_id else None
            if not cache_source or cache_source.get("review_text") != source["review_text"]:
                raise RuntimeError(f"Invalid direct cache provenance for {record['review_id']}")
            store.save_cached_record(
                source,
                record,
                record["label_config"],
                cache_source_id,
            )
        for event in calls:
            store.log_call(event)
        store.set_metadata(
            "codex_worker_merge",
            {
                "experiment_id": plan["experiment_id"],
                "workers": plan["workers"],
                "source_states": [f"worker-{index:02d}/state.db" for index in range(1, int(plan["workers"]) + 1)],
            },
        )
        completed = store.completed_count()
        merged_calls = len(list(store.call_rows()))
        cache_hits = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL"
            ).fetchone()[0]
        )
    report = {
        "development_only": True,
        "grading_eligible": False,
        "experiment_id": plan["experiment_id"],
        "expected_records": plan["sample_size"],
        "completed_records": completed,
        "merged_calls": merged_calls,
        "direct_exact_text_cache_hits": cache_hits,
        "input_sha256": ingestion["file_sha256"],
        "state_path": str(state_path),
        "valid": completed == int(plan["sample_size"]),
    }
    write_json(consolidated / "merge_report.json", report)
    if not report["valid"]:
        raise RuntimeError(f"Consolidated state validation failed: {report}")
    return report


def run_development_downstream(
    *,
    output_dir: str | Path,
    executable: str,
    expected_cli_version: str,
    acknowledgement: str,
) -> dict[str, Any]:
    """Run verify, group, rank, and memo after the sharded classification merge."""
    if acknowledgement != ACKNOWLEDGEMENT:
        raise RuntimeError(
            "Development Codex execution requires the explicit acknowledgement "
            f"{ACKNOWLEDGEMENT!r}"
        )
    output = Path(output_dir)
    plan = json.loads((output / "experiment_plan.json").read_text(encoding="utf-8"))
    merge = merge_development_worker_states(output)
    downstream = output / "downstream"
    downstream.mkdir(parents=True, exist_ok=False)
    provider = CodexExecProvider(
        trace_root=downstream / "traces",
        executable=executable,
        expected_version=expected_cli_version,
        ephemeral=True,
    )
    config = ModelConfig(
        model=plan["model"],
        effort=plan["effort"],
        batch_size=int(plan["batch_size"]),
        max_workers=1,
        max_attempts=1,
        max_output_tokens=4000,
        timeout_seconds=float(plan["timeout_seconds"]),
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=_execution_fingerprint(expected_cli_version),
    )
    started = time.perf_counter()
    summary = run_pipeline(
        input_csv=output / "sample.csv",
        state_path=output / "consolidated" / "state.db",
        artifacts_dir=downstream / "artifacts",
        provider=provider,
        config=config,
        phase="resume",
        stop_after="recommend",
        run_id=f"{plan['experiment_id']}-downstream",
    )
    runtime = provider.runtime_metadata()
    write_json(downstream / "runtime.json", runtime)
    report = {
        "development_only": True,
        "grading_eligible": False,
        "experiment_id": plan["experiment_id"],
        "classification_merge": merge,
        "downstream_wall_seconds": round(time.perf_counter() - started, 6),
        "summary": summary,
        "provider_runtime": runtime,
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
    }
    write_json(downstream / "end_to_end_report.json", report)
    return report


def _resume_worker(
    *,
    worker_index: int,
    output: Path,
    prompt_path: Path,
    executable: str,
    expected_cli_version: str,
    plan: dict[str, Any],
    resume_id: str,
) -> dict[str, Any]:
    worker_dir = output / f"worker-{worker_index:02d}"
    state_path = worker_dir / "state.db"
    with Store(state_path) as store:
        completed_before = store.completed_count()
        calls_before = len(list(store.call_rows()))
    provider = CodexExecProvider(
        trace_root=worker_dir / "traces",
        executable=executable,
        expected_version=expected_cli_version,
        ephemeral=True,
    )
    config = ModelConfig(
        model=plan["model"],
        effort=plan["effort"],
        batch_size=int(plan["batch_size"]),
        max_workers=1,
        max_attempts=1,
        max_output_tokens=4000,
        timeout_seconds=float(plan["timeout_seconds"]),
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=_execution_fingerprint(expected_cli_version),
    )
    started = time.perf_counter()
    result = run_enrichment(
        state_path=state_path,
        provider=provider,
        prompt_path=prompt_path,
        config=config,
        run_id=f"{plan['experiment_id']}-{resume_id}-worker-{worker_index:02d}",
        phase="resume",
    )
    wall_seconds = time.perf_counter() - started
    with Store(state_path) as store:
        records = list(store.completed_records())
        calls = list(store.call_rows())
        statuses = store.status_counts()
    atomic_write_text(
        worker_dir / "development_records.jsonl",
        "".join(canonical(record) + "\n" for record in records),
    )
    atomic_write_text(
        worker_dir / "agent_interactions.jsonl",
        "".join(canonical(call) + "\n" for call in calls),
    )
    write_json(worker_dir / f"runtime-{resume_id}.json", provider.runtime_metadata())
    summary = {
        "worker_index": worker_index,
        "resume_id": resume_id,
        "completed_before": completed_before,
        "completed_after": len(records),
        "new_calls": len(calls) - calls_before,
        "status_counts": statuses,
        "wall_seconds": round(wall_seconds, 6),
        "run_enrichment": result,
    }
    write_json(worker_dir / f"summary-{resume_id}.json", summary)
    return {"summary": summary, "records": records, "calls": calls}


def resume_development_benchmark(
    *,
    output_dir: str | Path,
    prompt_path: str | Path,
    executable: str,
    expected_cli_version: str,
    acknowledgement: str,
) -> dict[str, Any]:
    """Continue only pending records in an interrupted sharded experiment."""
    if acknowledgement != ACKNOWLEDGEMENT:
        raise RuntimeError(
            "Development Codex execution requires the explicit acknowledgement "
            f"{ACKNOWLEDGEMENT!r}"
        )
    output = Path(output_dir)
    plan = json.loads((output / "experiment_plan.json").read_text(encoding="utf-8"))
    if plan.get("expected_cli_version") != expected_cli_version:
        raise RuntimeError("CLI version differs from the interrupted experiment")
    resume_id = f"resume-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    worker_outputs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=int(plan["workers"])) as executor:
        futures = [
            executor.submit(
                _resume_worker,
                worker_index=index,
                output=output,
                prompt_path=Path(prompt_path),
                executable=executable,
                expected_cli_version=expected_cli_version,
                plan=plan,
                resume_id=resume_id,
            )
            for index in range(1, int(plan["workers"]) + 1)
        ]
        for future in as_completed(futures):
            worker_outputs.append(future.result())
    wall_seconds = time.perf_counter() - started

    with (output / "sample.csv").open(encoding="utf-8-sig", newline="") as stream:
        source_rows = list(csv.DictReader(stream, strict=True))
    records = [record for value in worker_outputs for record in value["records"]]
    calls = [call for value in worker_outputs for call in value["calls"]]
    expected_ids = [row["review_id"] for row in source_rows]
    returned_ids = [record["review_id"] for record in records]
    duplicates = sorted({review_id for review_id in returned_ids if returned_ids.count(review_id) > 1})
    missing = sorted(set(expected_ids) - set(returned_ids))
    unknown = sorted(set(returned_ids) - set(expected_ids))
    by_id = {record["review_id"]: record for record in records}
    ordered_records = [by_id[review_id] for review_id in expected_ids if review_id in by_id]
    quote_failures = sorted(
        record["review_id"]
        for record in ordered_records
        if record["evidence_quote"] not in record["review_text"]
    )
    valid = not duplicates and not missing and not unknown and not quote_failures
    atomic_write_text(
        output / "development_records.jsonl",
        "".join(canonical(record) + "\n" for record in ordered_records),
    )
    atomic_write_text(
        output / "agent_interactions.jsonl",
        "".join(
            canonical(call) + "\n"
            for call in sorted(calls, key=lambda call: (call["run_id"], call["request_id"]))
        ),
    )
    write_csv(
        output / "predictions.csv",
        [
            "review_id", "topic", "intent", "sentiment", "severity",
            "entities", "evidence_quote", "needs_review",
        ],
        [
            {
                "review_id": record["review_id"],
                "topic": record["topic"],
                "intent": record["intent"],
                "sentiment": record["sentiment"],
                "severity": record["severity"],
                "entities": json.dumps(record["entities"], ensure_ascii=False),
                "evidence_quote": record["evidence_quote"],
                "needs_review": str(record["needs_review"]).lower(),
            }
            for record in ordered_records
        ],
    )
    merge_report = {
        "valid": valid,
        "expected_records": len(expected_ids),
        "merged_records": len(records),
        "missing_review_ids": missing,
        "unknown_review_ids": unknown,
        "duplicate_review_ids": duplicates,
        "evidence_quote_failures": quote_failures,
        "exact_text_groups_split_across_workers": [],
    }
    write_json(output / "merge_report.json", merge_report)
    report = {
        "development_only": True,
        "grading_eligible": False,
        "experiment_id": plan["experiment_id"],
        "resume_id": resume_id,
        "sample_size": plan["sample_size"],
        "workers": plan["workers"],
        "model": plan["model"],
        "effort": plan["effort"],
        "batch_size": plan["batch_size"],
        "wall_seconds": round(wall_seconds, 6),
        "wall_seconds_scope": "this successful resume invocation only",
        "cumulative_call_totals": True,
        "completed_records": len(records),
        "call_count_total": len(calls),
        "input_tokens_total": sum(call["input_tokens"] or 0 for call in calls),
        "output_tokens_total": sum(call["output_tokens"] or 0 for call in calls),
        "merge_valid": valid,
        "worker_summaries": sorted(
            (value["summary"] for value in worker_outputs), key=lambda value: value["worker_index"]
        ),
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
    }
    write_json(output / f"{resume_id}.json", report)
    write_json(output / "benchmark_report.json", report)
    if not valid:
        raise RuntimeError(f"Resumed benchmark remains incomplete: {merge_report}")
    return report
