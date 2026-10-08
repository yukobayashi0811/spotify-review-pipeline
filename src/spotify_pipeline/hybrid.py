from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .classify import ModelConfig, run_enrichment
from .codex_benchmark import deterministic_shards, read_sample, write_source_csv
from .constants import SOURCE_FIELDS
from .io_utils import atomic_write_text, canonical, file_sha, row_sha, write_csv, write_json
from .prepare import ingest
from .providers import Provider
from .storage import Store


ROUTING_POLICY_VERSION = "hybrid-routing-v4"


@dataclass(frozen=True)
class RoutingPolicy:
    """Deterministic escalation policy applied only to local-model output."""

    routing_mode: str = "risk"
    uncertainty_metric: str = "mean_margin"
    high_severity_floor: int = 4
    escalate_needs_review: bool = True
    escalate_cancellation: bool = True
    escalate_unclear: bool = True
    escalate_negative_other: bool = True
    escalate_high_risk_complaints: bool = True
    high_risk_topics: tuple[str, ...] = ("access", "billing", "support")
    escalate_rating_contradictions: bool = True
    max_escalation_fraction: float = 1.0

    def validate(self) -> None:
        if not 0.0 <= self.max_escalation_fraction <= 1.0:
            raise ValueError("max_escalation_fraction must be between 0 and 1")
        if self.routing_mode not in {"risk", "uncertainty"}:
            raise ValueError("routing_mode must be 'risk' or 'uncertainty'")
        if self.uncertainty_metric not in {
            "minimum_confidence",
            "minimum_margin",
            "mean_margin",
        }:
            raise ValueError("Unsupported uncertainty_metric")

    @property
    def fingerprint(self) -> str:
        payload = {"version": ROUTING_POLICY_VERSION, **asdict(self)}
        digest = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:12]
        return f"{ROUTING_POLICY_VERSION}-{digest}"


def escalation_reasons(
    source: dict[str, str], local_result: dict[str, Any], policy: RoutingPolicy
) -> list[str]:
    """Return stable, inspectable reasons for escalating one exact-text group."""

    reasons: list[str] = []
    severity = int(local_result["severity"])
    topic = str(local_result["topic"])
    intent = str(local_result["intent"])
    sentiment = float(local_result["sentiment"])
    if policy.escalate_needs_review and bool(local_result["needs_review"]):
        reasons.append("local_needs_review")
    if severity >= policy.high_severity_floor:
        reasons.append("high_severity")
    if policy.escalate_cancellation and intent == "cancellation":
        reasons.append("cancellation")
    if policy.escalate_unclear and intent == "unclear":
        reasons.append("unclear_intent")
    if policy.escalate_negative_other and topic == "other" and sentiment < 0:
        reasons.append("negative_other")
    if (
        policy.escalate_high_risk_complaints
        and topic in policy.high_risk_topics
        and intent in {"complaint", "cancellation"}
    ):
        reasons.append("high_risk_topic")
    if policy.escalate_rating_contradictions:
        try:
            rating = int(float(source["review_rating"]))
        except (TypeError, ValueError):
            rating = 0
        if rating in {1, 2} and (intent == "praise" or sentiment > 0.25):
            reasons.append("low_rating_positive_label")
        if rating in {4, 5} and (
            intent in {"complaint", "cancellation"} or sentiment < -0.25
        ):
            reasons.append("high_rating_negative_label")
    return reasons


def repair_local_consistency(
    local_result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Apply narrow label-consistency repairs without reading source ratings or gold labels."""

    repaired = dict(local_result)
    reasons: list[str] = []
    if (
        repaired["intent"] == "complaint"
        and float(repaired["sentiment"]) >= 0.4
        and int(repaired["severity"]) == 1
        and repaired["topic"] in {"other", "catalog"}
    ):
        repaired["intent"] = "praise"
        reasons.append("positive_low_severity_complaint_to_praise")
    return repaired, reasons


def hybrid_label_config(
    local_config: ModelConfig, escalation_config: ModelConfig, policy: RoutingPolicy
) -> str:
    raw = canonical(
        {
            "local": local_config.label_config,
            "escalation": escalation_config.label_config,
            "policy": policy.fingerprint,
        }
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"hybrid:{policy.fingerprint}:{digest}"


def run_hybrid_checkpoint(
    *,
    input_path: str | Path,
    output_dir: str | Path,
    prompt_path: str | Path,
    sample_size: int,
    local_provider: Provider,
    local_config: ModelConfig,
    escalation_provider: Provider,
    escalation_config: ModelConfig,
    escalation_provider_factory: Callable[[Path], Provider] | None = None,
    policy: RoutingPolicy | None = None,
) -> dict[str, Any]:
    """Run resumable local-first classification and escalate only risky exact texts.

    The golden labels are never opened. Local, escalation, and final states are
    retained separately so every overwrite is reconstructable.
    """

    if not 1 <= sample_size <= 10_000:
        raise ValueError("sample_size must be between 1 and 10,000")
    local_config.validate()
    escalation_config.validate()
    policy = policy or RoutingPolicy()
    policy.validate()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    prompt = Path(prompt_path)
    rows = read_sample(input_path, sample_size)
    sample_path = output / "sample.csv"
    if sample_path.exists():
        prior_sha = file_sha(sample_path)
        candidate_path = output / ".sample-candidate.csv"
        write_source_csv(candidate_path, rows)
        candidate_sha = file_sha(candidate_path)
        candidate_path.unlink()
        if candidate_sha != prior_sha:
            raise ValueError("Refusing to resume with a different hybrid sample")
    else:
        write_source_csv(sample_path, rows)

    started = time.perf_counter()
    run_id = f"hybrid-{uuid.uuid4()}"
    local_dir = output / "local"
    local_dir.mkdir(exist_ok=True)
    local_state = local_dir / "state.db"
    ingest(sample_path, local_state, local_dir / "ingestion.json")
    local_started = time.perf_counter()
    local_summary = run_enrichment(
        state_path=local_state,
        provider=local_provider,
        prompt_path=prompt,
        config=local_config,
        run_id=f"{run_id}-local",
        phase="resume" if _has_completed(local_state) else "initial",
    )
    local_wall = time.perf_counter() - local_started

    with Store(local_state) as store:
        local_by_text = _results_by_exact_text(store)
        local_calls = list(store.call_rows())
        local_status = store.status_counts()
    diagnostics_by_review = _collect_local_diagnostics(local_provider, local_calls)

    source_groups = _group_source_rows(rows)
    decisions: list[dict[str, Any]] = []
    effective_local_by_text: dict[str, dict[str, Any]] = {}
    for text, group in source_groups.items():
        if not text.strip():
            continue
        raw_local_result = local_by_text.get(text)
        if raw_local_result is None:
            raise RuntimeError(f"Local classification missing exact text for {group[0]['review_id']}")
        local_result, consistency_repairs = repair_local_consistency(raw_local_result)
        effective_local_by_text[text] = local_result
        reasons = escalation_reasons(group[0], local_result, policy)
        diagnostics = diagnostics_by_review.get(group[0]["review_id"])
        if policy.routing_mode == "uncertainty":
            if diagnostics is None:
                raise RuntimeError(
                    "Uncertainty routing requires saved per-review local diagnostics. "
                    "Use a fresh output directory for the first uncertainty-routed run."
                )
            reasons.append("uncertainty_priority")
        decisions.append(
            {
                "representative_review_id": group[0]["review_id"],
                "text_sha256": group[0]["text_sha256"],
                "exact_text_group_size": len(group),
                "route_candidate": bool(reasons),
                "route_to_luna": False,
                "route_capped": False,
                "route_reasons": reasons,
                "consistency_repairs": consistency_repairs,
                "raw_local_topic": raw_local_result["topic"],
                "raw_local_intent": raw_local_result["intent"],
                "raw_local_sentiment": raw_local_result["sentiment"],
                "raw_local_severity": raw_local_result["severity"],
                "raw_local_needs_review": raw_local_result["needs_review"],
                "local_topic": local_result["topic"],
                "local_intent": local_result["intent"],
                "local_sentiment": local_result["sentiment"],
                "local_severity": local_result["severity"],
                "local_needs_review": local_result["needs_review"],
                "topic_confidence": _diagnostic_value(diagnostics, "topic_confidence"),
                "topic_margin": _diagnostic_value(diagnostics, "topic_margin"),
                "intent_confidence": _diagnostic_value(diagnostics, "intent_confidence"),
                "intent_margin": _diagnostic_value(diagnostics, "intent_margin"),
                "severity_confidence": _diagnostic_value(diagnostics, "severity_confidence"),
                "severity_margin": _diagnostic_value(diagnostics, "severity_margin"),
                "minimum_confidence": _diagnostic_value(diagnostics, "minimum_confidence"),
                "minimum_margin": _diagnostic_value(diagnostics, "minimum_margin"),
                "mean_margin": _diagnostic_value(diagnostics, "mean_margin"),
            }
        )

    nonempty_rows = sum(bool(row["review_text"].strip()) for row in rows)
    route_capacity_rows = int(nonempty_rows * policy.max_escalation_fraction)
    routed_rows_selected = 0
    candidates = sorted(
        (decision for decision in decisions if decision["route_candidate"]),
        key=lambda decision: _routing_priority(decision, policy),
    )
    for decision in candidates:
        group_size = int(decision["exact_text_group_size"])
        if routed_rows_selected + group_size <= route_capacity_rows:
            decision["route_to_luna"] = True
            routed_rows_selected += group_size
        else:
            decision["route_capped"] = True
    selected_text_hashes = {
        decision["text_sha256"] for decision in decisions if decision["route_to_luna"]
    }
    routed_representatives = [
        {field: group[0][field] for field in SOURCE_FIELDS}
        for text, group in source_groups.items()
        if text.strip() and group[0]["text_sha256"] in selected_text_hashes
    ]

    escalation_dir = output / "escalation"
    escalation_dir.mkdir(exist_ok=True)
    escalation_state = escalation_dir / "state.db"
    escalation_calls: list[dict[str, Any]] = []
    escalation_summary: dict[str, Any] = {
        "pending_unique_at_start": 0,
        "processed_unique": 0,
        "completed_total": 0,
        "failed_records": 0,
    }
    escalation_wall = 0.0
    escalation_runtime: dict[str, Any] | None = None
    escalation_by_text: dict[str, dict[str, Any]] = {}
    escalation_path = escalation_dir / "routed.csv"
    if routed_representatives:
        write_source_csv(escalation_path, routed_representatives)
        ingest(escalation_path, escalation_state, escalation_dir / "ingestion.json")
        if escalation_config.max_workers == 1:
            escalation_started = time.perf_counter()
            escalation_summary = run_enrichment(
                state_path=escalation_state,
                provider=escalation_provider,
                prompt_path=prompt,
                config=escalation_config,
                run_id=f"{run_id}-luna",
                phase="resume" if _has_completed(escalation_state) else "initial",
            )
            escalation_wall = time.perf_counter() - escalation_started
        else:
            if escalation_provider_factory is None:
                raise ValueError(
                    "Parallel escalation requires escalation_provider_factory"
                )
            escalation_summary, escalation_wall, escalation_runtime = (
                _run_parallel_escalation(
                    rows=routed_representatives,
                    central_state=escalation_state,
                    escalation_dir=escalation_dir,
                    prompt_path=prompt,
                    config=escalation_config,
                    run_id=f"{run_id}-luna",
                    provider_factory=escalation_provider_factory,
                )
            )
        with Store(escalation_state) as store:
            escalation_by_text = _results_by_exact_text(store)
            escalation_calls = list(store.call_rows())
            escalation_status = store.status_counts()
    else:
        write_source_csv(escalation_path, [])
        escalation_status = {}

    final_dir = output / "final"
    final_dir.mkdir(exist_ok=True)
    final_state = final_dir / "state.db"
    ingest(sample_path, final_state, final_dir / "ingestion.json")
    label_config = hybrid_label_config(local_config, escalation_config, policy)
    decision_by_text_sha = {item["text_sha256"]: item for item in decisions}
    changed = 0
    with Store(final_state) as store:
        for text, group in source_groups.items():
            if not text.strip():
                continue
            decision = decision_by_text_sha[group[0]["text_sha256"]]
            raw_local_result = local_by_text[text]
            local_result = effective_local_by_text[text]
            if decision["route_to_luna"]:
                result = escalation_by_text.get(text)
                if result is None:
                    raise RuntimeError(
                        f"Escalation result missing exact text for {group[0]['review_id']}"
                    )
                final_source = "luna"
                if _label_tuple(local_result) != _label_tuple(result):
                    changed += len(group)
            else:
                result = local_result
                final_source = "local"
            representative = group[0]
            store.save_completed(representative, result, label_config, attempt_count=1)
            for duplicate in group[1:]:
                store.save_cached_record(
                    duplicate, result, label_config, representative["review_id"]
                )
            decision.update(
                {
                    "final_source": final_source,
                    "final_topic": result["topic"],
                    "final_intent": result["intent"],
                    "final_sentiment": result["sentiment"],
                    "final_severity": result["severity"],
                    "final_needs_review": result["needs_review"],
                    "classification_changed": _label_tuple(local_result)
                    != _label_tuple(result),
                    "final_changed_from_raw_local": _label_tuple(raw_local_result)
                    != _label_tuple(result),
                }
            )
        for component_call in local_calls + escalation_calls:
            call = dict(component_call)
            component_label_config = call["label_config"]
            metadata = dict(call.get("provider_metadata") or {})
            metadata["component_label_config"] = component_label_config
            call["provider_metadata"] = metadata
            call["label_config"] = label_config
            exists = store.connection.execute(
                "SELECT 1 FROM calls WHERE request_id=?", (call["request_id"],)
            ).fetchone()
            if not exists:
                store.log_call(call)
        store.set_metadata(
            "hybrid_run",
            {
                "run_id": run_id,
                "label_config": label_config,
                "policy": {"version": ROUTING_POLICY_VERSION, **asdict(policy)},
                "local_label_config": local_config.label_config,
                "escalation_label_config": escalation_config.label_config,
            },
        )
        final_records = list(store.completed_records())
        final_status = store.status_counts()
        exact_cache_hits = sum(bool(record["cache_source_id"]) for record in final_records)
        final_call_count = len(list(store.call_rows()))

    _write_routing_decisions(output / "routing_decisions.csv", decisions)
    atomic_write_text(
        final_dir / "records.jsonl",
        "".join(canonical(record) + "\n" for record in final_records),
    )
    reason_counts = Counter(
        reason for decision in decisions for reason in decision["route_reasons"]
    )
    routed_unique = len(routed_representatives)
    routed_rows = sum(
        decision["exact_text_group_size"]
        for decision in decisions
        if decision["route_to_luna"]
    )
    runtime = {
        "local": _runtime_metadata(local_provider),
        "escalation": escalation_runtime or _runtime_metadata(escalation_provider),
    }
    summary = {
        "run_id": run_id,
        "development_only": True,
        "grading_eligible": False,
        "golden_labels_loaded": False,
        "input_path": str(Path(input_path).resolve()),
        "input_sha256": file_sha(input_path),
        "sample_path": str(sample_path.resolve()),
        "sample_sha256": file_sha(sample_path),
        "sample_rows": len(rows),
        "nonempty_rows": sum(bool(row["review_text"].strip()) for row in rows),
        "distinct_nonempty_texts": len(decisions),
        "routing_policy": {"version": ROUTING_POLICY_VERSION, **asdict(policy)},
        "routing_policy_fingerprint": policy.fingerprint,
        "hybrid_label_config": label_config,
        "routed_unique_texts": routed_unique,
        "routed_rows": routed_rows,
        "routing_candidate_unique_texts": len(candidates),
        "routing_candidate_rows": sum(
            int(decision["exact_text_group_size"]) for decision in candidates
        ),
        "routing_cap_rows": route_capacity_rows,
        "routing_capped_unique_texts": sum(
            bool(decision["route_capped"]) for decision in decisions
        ),
        "routing_fraction_unique": routed_unique / len(decisions) if decisions else 0.0,
        "route_reason_counts": dict(sorted(reason_counts.items())),
        "classification_changed_rows": changed,
        "consistency_repaired_unique_texts": sum(
            bool(decision["consistency_repairs"]) for decision in decisions
        ),
        "local_status": local_status,
        "escalation_status": escalation_status,
        "final_status": final_status,
        "final_exact_text_cache_hits": exact_cache_hits,
        "final_call_count": final_call_count,
        "local_wall_seconds": round(local_wall, 6),
        "escalation_wall_seconds": round(escalation_wall, 6),
        "total_wall_seconds": round(time.perf_counter() - started, 6),
        "local_run": local_summary,
        "escalation_run": escalation_summary,
        "runtime": runtime,
        "artifacts": {
            "routing_decisions": str((output / "routing_decisions.csv").resolve()),
            "local_state": str(local_state.resolve()),
            "escalation_state": str(escalation_state.resolve()),
            "final_state": str(final_state.resolve()),
            "final_records": str((final_dir / "records.jsonl").resolve()),
        },
    }
    cache_mode = (
        "warm"
        if local_summary["processed_unique"] == 0
        and escalation_summary["processed_unique"] == 0
        else "cold_or_partial_resume"
    )
    summary["cache_mode"] = cache_mode
    runs_dir = output / "runs"
    runs_dir.mkdir(exist_ok=True)
    run_report = runs_dir / f"{run_id}.json"
    summary["artifacts"]["run_report"] = str(run_report.resolve())
    write_json(run_report, summary)
    if cache_mode == "warm":
        write_json(output / "hybrid_warm_report.json", summary)
    else:
        write_json(output / "hybrid_cold_report.json", summary)
    write_json(output / "hybrid_report.json", summary)
    return summary


def _run_parallel_escalation(
    *,
    rows: list[dict[str, str]],
    central_state: Path,
    escalation_dir: Path,
    prompt_path: Path,
    config: ModelConfig,
    run_id: str,
    provider_factory: Callable[[Path], Provider],
) -> tuple[dict[str, Any], float, dict[str, Any]]:
    """Classify deterministic shards concurrently, then merge into one checkpoint.

    Every worker owns a separate SQLite file and trace directory. The parent is
    the only writer to the central escalation database, avoiding shared-database
    write contention while retaining resumability after an interruption.
    """

    workers = config.max_workers
    shards = deterministic_shards(rows, workers)
    shard_root = escalation_dir / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    worker_config = replace(config, max_workers=1)

    def run_worker(index: int, shard: list[dict[str, str]]) -> dict[str, Any]:
        worker_dir = shard_root / f"worker-{index:02d}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        input_path = worker_dir / "input.csv"
        if input_path.exists():
            candidate_path = worker_dir / ".input-candidate.csv"
            write_source_csv(candidate_path, shard)
            if file_sha(candidate_path) != file_sha(input_path):
                candidate_path.unlink()
                raise ValueError(f"Refusing to resume changed escalation shard {index}")
            candidate_path.unlink()
        else:
            write_source_csv(input_path, shard)
        state_path = worker_dir / "state.db"
        ingest(input_path, state_path, worker_dir / "ingestion.json")
        provider = provider_factory(escalation_dir / "traces" / f"worker-{index:02d}")
        result = run_enrichment(
            state_path=state_path,
            provider=provider,
            prompt_path=prompt_path,
            config=worker_config,
            run_id=f"{run_id}-worker-{index:02d}",
            phase="resume" if _has_completed(state_path) else "initial",
        )
        with Store(state_path) as store:
            records = list(store.completed_records())
            calls = list(store.call_rows())
            statuses = store.status_counts()
        return {
            "worker_index": index,
            "input_rows": len(shard),
            "summary": result,
            "records": records,
            "calls": calls,
            "statuses": statuses,
            "runtime": _runtime_metadata(provider),
        }

    started = time.perf_counter()
    outputs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(run_worker, index, shard)
            for index, shard in enumerate(shards, 1)
            if shard
        ]
        for future in as_completed(futures):
            outputs.append(future.result())
    wall = time.perf_counter() - started

    with Store(central_state) as store:
        source_by_id = {
            row["review_id"]: dict(row)
            for row in store.connection.execute("SELECT * FROM source_rows")
        }
        for output in sorted(outputs, key=lambda item: item["worker_index"]):
            for record in output["records"]:
                source = source_by_id[record["review_id"]]
                store.save_completed(
                    source,
                    _result_from_record(record),
                    config.label_config,
                    int(record.get("attempt_count") or 1),
                )
            for component_call in output["calls"]:
                call = dict(component_call)
                call["label_config"] = config.label_config
                exists = store.connection.execute(
                    "SELECT 1 FROM calls WHERE request_id=?", (call["request_id"],)
                ).fetchone()
                if not exists:
                    store.log_call(call)
        completed_total = store.completed_count()
        status_counts = store.status_counts()

    worker_summaries = [
        {
            "worker_index": output["worker_index"],
            "input_rows": output["input_rows"],
            "processed_unique": output["summary"]["processed_unique"],
            "completed_total": output["summary"]["completed_total"],
            "failed_records": output["summary"]["failed_records"],
        }
        for output in sorted(outputs, key=lambda item: item["worker_index"])
    ]
    summary = {
        "run_id": run_id,
        "phase": "resume" if any(
            output["summary"]["phase"] == "resume" for output in outputs
        ) else "initial",
        "pending_unique_at_start": sum(
            output["summary"]["pending_unique_at_start"] for output in outputs
        ),
        "processed_unique": sum(
            output["summary"]["processed_unique"] for output in outputs
        ),
        "failed_records": sum(
            output["summary"]["failed_records"] for output in outputs
        ),
        "fallback_unique": sum(
            output["summary"]["fallback_unique"] for output in outputs
        ),
        "cache_hits_total": sum(
            output["summary"]["cache_hits_total"] for output in outputs
        ),
        "completed_total": completed_total,
        "spend_ledger_usd_including_unknown_reservations": sum(
            output["summary"]["spend_ledger_usd_including_unknown_reservations"]
            for output in outputs
        ),
        "unsettled_reservation_usd": sum(
            output["summary"]["unsettled_reservation_usd"] for output in outputs
        ),
        "unknown_charge_batches_reserved_at_cap": sum(
            output["summary"]["unknown_charge_batches_reserved_at_cap"]
            for output in outputs
        ),
        "evidence_quote_repairs": sum(
            output["summary"]["evidence_quote_repairs"] for output in outputs
        ),
        "max_workers_configured": workers,
        "worker_summaries": worker_summaries,
        "status_counts": status_counts,
        "note": "Deterministic shards ran concurrently with one SQLite checkpoint per worker and a parent-only central merge.",
    }
    runtime = {
        "provider": "parallel-sharded",
        "max_workers": workers,
        "workers": [
            {
                "worker_index": output["worker_index"],
                "runtime": output["runtime"],
            }
            for output in sorted(outputs, key=lambda item: item["worker_index"])
        ],
    }
    return summary, wall, runtime


def _has_completed(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    with Store(state_path) as store:
        return store.completed_count() > 0


def _result_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_id": record["review_id"],
        "topic": record["topic"],
        "intent": record["intent"],
        "sentiment": record["sentiment"],
        "severity": record["severity"],
        "entities": record["entities"],
        "evidence_quote": record["evidence_quote"],
        "needs_review": record["needs_review"],
    }


def _results_by_exact_text(store: Store) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for record in store.completed_records():
        results.setdefault(record["review_text"], _result_from_record(record))
    return results


def _group_source_rows(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        source = dict(row)
        source["source_sha256"] = row_sha(source)
        source["text_sha256"] = hashlib.sha256(
            source["review_text"].encode("utf-8")
        ).hexdigest()
        groups.setdefault(source["review_text"], []).append(source)
    return groups


def _label_tuple(result: dict[str, Any]) -> tuple[Any, ...]:
    return (
        result["topic"],
        result["intent"],
        float(result["sentiment"]),
        int(result["severity"]),
        bool(result["needs_review"]),
    )


def _runtime_metadata(provider: Provider) -> dict[str, Any]:
    getter = getattr(provider, "runtime_metadata", None)
    if callable(getter):
        return getter()
    return {"provider": provider.name}


def _routing_priority(
    decision: dict[str, Any], policy: RoutingPolicy | None = None
) -> tuple[Any, ...]:
    policy = policy or RoutingPolicy()
    reasons = set(decision["route_reasons"])
    if policy.routing_mode == "uncertainty":
        uncertainty = decision.get(policy.uncertainty_metric)
        if not isinstance(uncertainty, (int, float)):
            uncertainty = float("-inf")
        return (
            float(uncertainty),
            float(decision.get("minimum_confidence") or 0.0),
            -int("high_severity" in reasons),
            -int("cancellation" in reasons),
            decision["text_sha256"],
        )
    return (
        -int("high_severity" in reasons),
        -int("cancellation" in reasons),
        -int("high_risk_topic" in reasons),
        -int("local_needs_review" in reasons),
        -int(decision["local_severity"]),
        decision["text_sha256"],
    )


def _collect_local_diagnostics(
    provider: Provider, calls: list[dict[str, Any]]
) -> dict[str, dict[str, float]]:
    """Collect current or saved local probability diagnostics without a new model call."""

    del provider
    diagnostics: dict[str, dict[str, float]] = {}
    for call in calls:
        metadata = call.get("provider_metadata") or {}
        saved = metadata.get("review_diagnostics") or {}
        if isinstance(saved, dict):
            saved_items = sorted(saved.items(), key=lambda item: item[0])
            source_ids = call.get("review_ids") or []
            compact_ids = [f"R{index:03d}" for index in range(1, len(saved_items) + 1)]
            if (
                [review_id for review_id, _ in saved_items] == compact_ids
                and len(source_ids) == len(saved_items)
                and all(isinstance(review_id, str) for review_id in source_ids)
            ):
                mapped_items = zip(source_ids, (values for _, values in saved_items), strict=True)
            else:
                mapped_items = saved_items
            for review_id, values in mapped_items:
                if isinstance(review_id, str) and isinstance(values, dict):
                    diagnostics[review_id] = {
                        str(key): float(value)
                        for key, value in values.items()
                        if isinstance(value, (int, float))
                    }
    return diagnostics


def _diagnostic_value(
    diagnostics: dict[str, float] | None, key: str
) -> float | None:
    if diagnostics is None:
        return None
    value = diagnostics.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _write_routing_decisions(path: Path, decisions: list[dict[str, Any]]) -> None:
    rows = []
    for decision in decisions:
        row = dict(decision)
        row["route_reasons"] = ";".join(decision["route_reasons"])
        row["consistency_repairs"] = ";".join(decision["consistency_repairs"])
        rows.append(row)
    fields = [
        "representative_review_id",
        "text_sha256",
        "exact_text_group_size",
        "route_candidate",
        "route_to_luna",
        "route_capped",
        "route_reasons",
        "consistency_repairs",
        "raw_local_topic",
        "raw_local_intent",
        "raw_local_sentiment",
        "raw_local_severity",
        "raw_local_needs_review",
        "local_topic",
        "local_intent",
        "local_sentiment",
        "local_severity",
        "local_needs_review",
        "topic_confidence",
        "topic_margin",
        "intent_confidence",
        "intent_margin",
        "severity_confidence",
        "severity_margin",
        "minimum_confidence",
        "minimum_margin",
        "mean_margin",
        "final_source",
        "final_topic",
        "final_intent",
        "final_sentiment",
        "final_severity",
        "final_needs_review",
        "classification_changed",
        "final_changed_from_raw_local",
    ]
    write_csv(path, fields, rows)
