"""``spotify-pipeline run``: the staged pipeline for an arbitrary input CSV.

Stages (each cached by a fingerprint of its inputs and settings, so an unchanged
re-run skips it and makes no model call):

1. prepare        code: ingest, hash every row, profile, quarantine empty texts,
                  build the exact-text index.
2. classify_local frozen local classifier (TF-IDF/linear, sealed artifact) on
                  every pending distinct text, batches of 50.
3. route          code: risk rule (support topic, billing/privacy/fraud terms,
                  or severity >= 4) ordered by local error probability x rows,
                  capped at a declared fraction of distinct texts.
4. classify_luna  GPT-6 Luna (Codex CLI, subscription) re-labels only the
                  routed texts, batches of <= 50; a routed text whose Luna call
                  fails keeps its local label and the failure is recorded.
5. verify         GPT-6 Luna, blind to the first-pass labels, on a declared
                  hash-ordered sample; code compares and reports disagreements.
6. group          code: pinned MiniLM embeddings + seeded k-means per topic;
                  Luna names/reviews bounded representatives only.
7. rank           code: priority_score = severity_sum, tie by issue_id.
8. memo_evidence  code: actionable ranking, topic rollup, placeholders for the
                  full-corpus-only grouping comparison and sensitivity model.
9. recommend      GPT-6 Luna memo role from saved aggregates + typical evidence;
                  code validates every ID and number (two-call cap).
10. export        code: grading/ contract export.

All model calls pass through ``GuardedProvider`` (model allowlist, bounded
transient retries with backoff, per-role call caps, one API-equivalent spend
ledger).
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .classify import ControlledInterrupt, ModelConfig, _call_event, _failed_event, run_enrichment
from .distilled import DISTILLED_MODEL_VERSION, DistilledTextProvider
from .downstream_analysis import build_actionable_and_topic_outputs
from .export import export_grading
from .group import run_grouping
from .guard import CallCapReachedError, GuardedProvider, SpendCapReachedError
from .io_utils import canonical, file_sha, write_csv, write_json
from .prepare import ingest
from .providers import CodexExecProvider, Provider, ProviderUnavailableError
from .rank import rerank
from .recommend import run_recommendation
from .sensitivity import MIN_LOCAL_VERIFIED, run_sensitivity_analysis
from .schemas import (
    compact_review_batch,
    enrichment_schema_for_count,
    repair_and_validate_enrichment_payload,
    restore_source_review_ids,
)
from .storage import Store
from .verify import VERIFY_PROMPT_VERSION, run_verification


PIPELINE_VERSION = "pipeline-v4"
ROUTE_POLICY = "risk-route-v1"
ROUTE_BUCKET = "R_PIPELINE"
HIGH_RISK = re.compile(
    r"\b(?:charge[ds]?|charged|refund|billing|payment|privacy|private|hacked|"
    r"stolen|fraud|unauthorized|support|customer\s+service)\b",
    re.IGNORECASE,
)
ROLES = ("enrich", "verify", "group", "memo")


def run_pipeline(
    input_path: str | Path,
    config_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    luna_provider_factory: Callable[[Path], Provider] | None = None,
    interrupt_after_luna_batches: int | None = None,
) -> dict[str, Any]:
    started_all = time.perf_counter()
    config_file = Path(config_path).resolve()
    config = json.loads(config_file.read_text(encoding="utf-8"))
    root = config_file.parent.parent
    input_csv = Path(input_path).resolve()
    output = (
        Path(output_dir).resolve()
        if output_dir
        else _resolve(root, str(config["output_dir"]).format(input_stem=input_csv.stem))
    )
    output.mkdir(parents=True, exist_ok=True)
    state = output / "state.db"
    grading = output / "grading"
    run_id = f"{PIPELINE_VERSION}-{uuid.uuid4()}"
    run_log = output / "run_log.jsonl"
    summary: dict[str, Any] = {
        "run_id": run_id,
        "pipeline_version": PIPELINE_VERSION,
        "input": str(input_csv),
        "output": str(output),
        "config": str(config_file),
        "stages": {},
    }

    artifact = _resolve(root, config["local_artifact_path"])
    manifest = json.loads(_resolve(root, config["local_manifest_path"]).read_text(encoding="utf-8"))
    artifact_sha = file_sha(artifact)
    if manifest.get("artifact_sha256") != artifact_sha:
        raise RuntimeError("The local classifier artifact does not match its manifest")
    luna_settings = config["luna"]
    workers = int(luna_settings.get("workers", 1))
    if not 1 <= workers <= 8:
        raise ValueError("luna.workers must be between 1 and 8")
    model = str(luna_settings["model"])
    effort = str(luna_settings["effort"])
    expected_cli = str(luna_settings["expected_cli_version"])
    prompts = {name: root / "prompts" / f"{name}.md" for name in ("classify", "verify", "group", "memo")}
    prompt_sha = {name: file_sha(path) for name, path in prompts.items()}
    source_sha = file_sha(input_csv)

    guard_holder: dict[str, GuardedProvider] = {}

    def luna() -> GuardedProvider:
        """Create the Luna provider only when a stage actually needs a model call."""
        if "guard" not in guard_holder:
            trace_root = output / "luna_traces"
            if luna_provider_factory is not None:
                raw = luna_provider_factory(trace_root)
            else:
                raw = CodexExecProvider(
                    trace_root=trace_root,
                    executable=str(luna_settings.get("codex_executable", "codex")),
                    expected_version=expected_cli,
                    ephemeral=True,
                )

            def log_transient(record: dict[str, Any]) -> None:
                # Retried transient failures are kept out of the contract call log
                # (they carry no review IDs and no usage) and saved separately.
                _append(output / "transient_failures.jsonl", {"run_id": run_id, **record})

            guard_holder["guard"] = GuardedProvider(
                raw,
                allowlist={model: (effort,)},
                max_calls_by_role=caps,
                max_transient_retries=int(luna_settings.get("max_transient_retries", 2)),
                backoff_base_seconds=float(luna_settings.get("backoff_base_seconds", 2.0)),
                on_failed_attempt=log_transient,
                spend_limit_usd=float(config["budget"]["spend_limit_usd_api_equivalent"]),
                rates_per_token={
                    key: float(value) / 1_000_000
                    for key, value in config["budget"]["rates_usd_per_million"].items()
                },
            )
        return guard_holder["guard"]

    def stage(name: str, fingerprint: dict[str, Any], action: Callable[[], Any]) -> dict[str, Any]:
        input_sha = hashlib.sha256(canonical(fingerprint).encode()).hexdigest()
        with Store(state) as store:
            cached = store.connection.execute(
                "SELECT output_sha256,details_json FROM stage_runs "
                "WHERE stage=? AND input_sha256=? AND status='completed' "
                "ORDER BY stage_run_id DESC LIMIT 1", (name, input_sha),
            ).fetchone()
            if cached:
                value = json.loads(cached["details_json"])
                value["source_wall_seconds"] = value.get("wall_seconds")
                value["wall_seconds"] = 0.0
                value["skipped_unchanged"] = True
                summary["stages"][name] = value
                _progress(f"stage {name}: unchanged, skipped (no model call)")
                _append(run_log, {
                    "run_id": run_id, "stage": name, "status": "skipped_unchanged",
                    "input_sha256": input_sha, "output_sha256": cached["output_sha256"],
                })
                return value
            previous_failure = store.connection.execute(
                "SELECT COUNT(*) FROM stage_runs WHERE stage=? AND input_sha256=? "
                "AND status IN ('failed','running')", (name, input_sha),
            ).fetchone()[0]
            with store.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO stage_runs(run_id,stage,input_sha256,status) VALUES(?,?,?,'running')",
                    (run_id, name, input_sha),
                )
                stage_id = int(cursor.lastrowid)
        _append(run_log, {
            "run_id": run_id, "stage": name, "status": "started", "input_sha256": input_sha,
            "resume_after_failure": bool(previous_failure),
        })
        _progress(f"stage {name}: started" + (" (resuming after an earlier stop)" if previous_failure else ""))
        started = time.perf_counter()
        try:
            raw = action()
            details = raw if isinstance(raw, dict) else {"result": raw}
            details["wall_seconds"] = round(time.perf_counter() - started, 6)
            details["resumed_after_failure"] = bool(previous_failure)
            output_sha = hashlib.sha256(canonical(details).encode()).hexdigest()
            with Store(state) as store:
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE stage_runs SET output_sha256=?,status='completed',details_json=?,"
                        "finished_at=CURRENT_TIMESTAMP WHERE stage_run_id=?",
                        (output_sha, canonical(details), stage_id),
                    )
            summary["stages"][name] = details
            _progress(f"stage {name}: completed in {details['wall_seconds']:.1f} s")
            _append(run_log, {
                "run_id": run_id, "stage": name, "status": "completed",
                "input_sha256": input_sha, "output_sha256": output_sha,
                "wall_seconds": details["wall_seconds"],
            })
            return details
        except BaseException as error:
            with Store(state) as store:
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE stage_runs SET status='failed',details_json=?,"
                        "finished_at=CURRENT_TIMESTAMP WHERE stage_run_id=?",
                        (canonical({"error": f"{type(error).__name__}: {error}"}), stage_id),
                    )
            _progress(f"stage {name}: stopped ({type(error).__name__}: {str(error)[:200]})")
            _append(run_log, {
                "run_id": run_id, "stage": name, "status": "failed",
                "input_sha256": input_sha, "error": f"{type(error).__name__}: {error}"[:2000],
                "wall_seconds": round(time.perf_counter() - started, 6),
            })
            summary["status"] = "failed"
            summary["failed_stage"] = name
            summary["error"] = f"{type(error).__name__}: {error}"[:2000]
            _write_summary(output, summary, started_all, state, run_id, guard_holder)
            raise

    # 1. prepare ------------------------------------------------------------
    def prepare() -> dict[str, Any]:
        report = ingest(input_csv, state, output / "ingestion.json")
        with Store(state) as store:
            distinct = store.rebuild_texts()
            store.set_metadata("input_csv", str(input_csv))
        return {
            "file_sha256": report["file_sha256"],
            "counts": report["counts"],
            "distinct_nonempty_texts": distinct,
        }

    prepared = stage("prepare", {"input": source_sha, "version": PIPELINE_VERSION}, prepare)
    distinct_texts = int(prepared["distinct_nonempty_texts"])

    # Call caps derived from the declared fractions (hard upper bounds).
    routing = config["routing"]
    verification = config["verification"]
    route_limit = math.floor(distinct_texts * float(routing["max_fraction_of_distinct_texts"]))
    with Store(state) as store:
        completed_or_nonempty = int(store.connection.execute(
            "SELECT COUNT(*) FROM source_rows WHERE TRIM(review_text)<>''"
        ).fetchone()[0])
    verify_target = min(
        int(verification["maximum"]),
        max(int(verification["minimum"]),
            round(completed_or_nonempty * float(verification["sample_fraction"]))),
        completed_or_nonempty,
    )
    caps = {
        # Each failed 50-review batch can split once into 10s and then 1s.
        "enrich": math.ceil(route_limit / 50) * 3 + 2 if route_limit else 0,
        "verify": math.ceil(verify_target / 50) * 3 + 2,
        "group": int(config["luna"].get("max_group_calls", 30)),
        "memo": 2,
    }
    summary["declared_controls"] = {
        "route_policy": ROUTE_POLICY,
        "max_route_fraction_of_distinct_texts": float(routing["max_fraction_of_distinct_texts"]),
        "route_limit_texts": route_limit,
        "verification_sample_rows": verify_target,
        "max_calls_by_role": caps,
        "spend_limit_usd_api_equivalent": float(config["budget"]["spend_limit_usd_api_equivalent"]),
        "luna_workers": workers,
        "batch_size_max": 50,
        "max_transient_retries": int(luna_settings.get("max_transient_retries", 2)),
        "invalid_output_retry": "one split retry per failed batch (50 -> 10 -> 1), then quarantine or keep local label",
    }

    local_config = ModelConfig(
        model=DISTILLED_MODEL_VERSION, effort="none", batch_size=50, max_workers=1,
        max_attempts=1, max_output_tokens=1, timeout_seconds=60, spend_limit_usd=1,
        input_rate_per_million=0, cached_input_rate_per_million=0, output_rate_per_million=0,
        execution_fingerprint=(
            f"artifact-{artifact_sha}:sklearn-{manifest['scikit_learn_version']}:final-local-v2"
        ),
    )
    rates = config["budget"]["rates_usd_per_million"]
    fingerprint_suffix = (
        f"{expected_cli.replace(' ', '-')}:subscription:ephemeral:empty-read-only:"
        f"tools-disabled:{PIPELINE_VERSION}"
    )

    def luna_config(batch: int, output_cap: int) -> ModelConfig:
        return ModelConfig(
            model=model, effort=effort, batch_size=batch, max_workers=1, max_attempts=1,
            max_output_tokens=output_cap, timeout_seconds=float(luna_settings["timeout_seconds"]),
            spend_limit_usd=float(config["budget"]["spend_limit_usd_api_equivalent"]),
            fallback_model=None, fallback_fraction=0,
            input_rate_per_million=float(rates["input_tokens"]),
            cached_input_rate_per_million=float(rates["cached_input_tokens"]),
            output_rate_per_million=float(rates["output_tokens"]),
            execution_fingerprint=fingerprint_suffix,
        )

    # Enrichment phase for this invocation: "resume" when an earlier invocation
    # already made enrichment calls (an interrupted run), otherwise "initial".
    with Store(state) as store:
        prior_enrich = int(store.connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id<>? AND role='enrich'", (run_id,)
        ).fetchone()[0])
        classification_done = store.connection.execute(
            "SELECT 1 FROM stage_runs WHERE stage='classify_local' AND status='completed' LIMIT 1"
        ).fetchone() is not None
        if store.get_metadata("checkpoint_before") is None:
            store.set_metadata("checkpoint_before", {"completed_ids": [], "note": "no interruption"})
        if prior_enrich and not classification_done:
            # Saved work at the moment of the interruption (checkpoint before resume).
            before = {"completed_ids": store.completed_ids(), "captured": "start of resumed invocation"}
            if not store.get_metadata("checkpoint_before", {}).get("completed_ids"):
                store.set_metadata("checkpoint_before", before)
                write_json(output / "checkpoint_before.json", before)
    phase = "resume" if prior_enrich else "initial"
    summary["enrichment_phase"] = phase

    # 2. route ------------------------------------------------------------------
    def route() -> dict[str, Any]:
        decisions = _route(
            state=state, artifact=artifact, artifact_sha=artifact_sha, limit=route_limit,
        )
        write_json(output / "routing.json", decisions)
        return {key: decisions[key] for key in (
            "policy", "distinct_texts", "eligible_texts", "limit", "routed_texts",
            "routed_rows",
        )}

    stage("route", {
        "input": source_sha, "artifact": artifact_sha, "policy": ROUTE_POLICY,
        "limit": route_limit,
    }, route)
    routing_sha = file_sha(output / "routing.json")

    # 3. classify_luna ------------------------------------------------------------
    enrich_config = luna_config(50, 6000)

    def classify_luna() -> dict[str, Any]:
        decisions = json.loads((output / "routing.json").read_text(encoding="utf-8"))
        hashes = {row["text_sha256"] for row in decisions["routed"]}
        result: dict[str, Any] = {"routed_texts": len(hashes), "workers": workers, "phase": phase}
        if not hashes:
            return result
        prompt_text = prompts["classify"].read_text(encoding="utf-8")
        result["parallel"] = _luna_parallel(
            state=state, provider=luna(), prompt=prompt_text, config=enrich_config,
            run_id=run_id, phase=phase, hashes=hashes, workers=workers,
            interrupt_after=interrupt_after_luna_batches if phase == "initial" else None,
        )
        # Batches that failed validation are retried serially by splitting 50 -> 10 -> 1.
        serial = run_enrichment(
            state_path=state, provider=luna(), prompt_path=prompts["classify"],
            config=enrich_config, run_id=run_id, phase=phase, restrict_text_sha256=hashes,
        )
        result["serial_recovery"] = {key: serial[key] for key in (
            "pending_unique_at_start", "processed_unique", "failed_records",
        )}
        result["label_config"] = enrich_config.label_config
        return result

    stage("classify_luna", {
        "route": routing_sha, "model": model, "effort": effort, "prompt": prompt_sha["classify"],
        "label_config": enrich_config.label_config,
    }, classify_luna)

    # 4. classify_local -----------------------------------------------------------
    def classify_local() -> dict[str, Any]:
        with Store(state) as store:
            luna_done = {
                str(row[0]) for row in store.connection.execute(
                    "SELECT t.text_sha256 FROM texts t JOIN records r "
                    "ON r.review_id=t.representative_review_id "
                    "WHERE r.status='completed' AND r.label_config=?",
                    (enrich_config.label_config,),
                )
            }
            all_hashes = {str(row[0]) for row in store.connection.execute("SELECT text_sha256 FROM texts")}
        provider = DistilledTextProvider(artifact_path=artifact, expected_sha256=artifact_sha)
        local = run_enrichment(
            state_path=state, provider=provider, prompt_path=prompts["classify"],
            config=local_config, run_id=run_id, phase=phase,
            restrict_text_sha256=all_hashes - luna_done,
        )
        decisions = json.loads((output / "routing.json").read_text(encoding="utf-8"))
        routed = {row["text_sha256"] for row in decisions["routed"]}
        outcome = {
            "routed_texts": len(routed),
            "routed_labeled_by_luna": len(routed & luna_done),
            "routed_kept_local_after_luna_failure": sorted(routed - luna_done),
        }
        write_json(output / "routing_outcome.json", outcome)
        with Store(state) as store:
            after = {"completed_ids": store.completed_ids()}
            store.set_metadata("checkpoint_after", after)
            quarantined = int(store.connection.execute(
                "SELECT COUNT(*) FROM records WHERE status='quarantined'"
            ).fetchone()[0])
            completed = store.completed_count()
            write_json(output / "checkpoint_before.json", store.get_metadata("checkpoint_before"))
        write_json(output / "checkpoint_after.json", after)
        return {
            **{key: local[key] for key in (
                "pending_unique_at_start", "processed_unique", "failed_records", "cache_hits_total",
            )},
            "label_config": local_config.label_config,
            "completed_total": completed,
            "quarantined_total": quarantined,
            "routed_labeled_by_luna": outcome["routed_labeled_by_luna"],
            "routed_kept_local_after_luna_failure": len(outcome["routed_kept_local_after_luna_failure"]),
        }

    stage("classify_local", {
        "route": routing_sha, "artifact": artifact_sha, "prompt": prompt_sha["classify"],
        "label_config": local_config.label_config, "luna_label_config": enrich_config.label_config,
    }, classify_local)

    # 5. verify -------------------------------------------------------------------
    verify_config = luna_config(50, 6000)

    def verify() -> dict[str, Any]:
        report_path = output / "verification" / "verification_report.json"
        design = _stratified_sample(
            state=state, target=verify_target, seed=str(verification["seed"]),
            luna_label_config=enrich_config.label_config,
            luna_share=float(verification.get("luna_stratum_share", 0.25)),
        )
        report = run_verification(
            state_path=state, provider=luna(), prompt_path=prompts["verify"],
            config=verify_config, run_id=run_id, output_path=report_path,
            sample_fraction=float(verification["sample_fraction"]),
            minimum_sample=int(verification["minimum"]),
            maximum_sample=int(verification["maximum"]),
            seed=str(verification["seed"]), selected_ids=design["ids"],
        )
        with Store(state) as store:
            with store.transaction() as connection:
                connection.executemany(
                    "UPDATE verifier_records SET final_source=?,stratum=?,sample_weight=? WHERE review_id=?",
                    [(item["final_source"], item["final_source"], item["weight"], review_id)
                     for review_id, item in design["by_id"].items()],
                )
        report["metrics"] = report.get("agreement", {})
        report["sampling"] = {key: value for key, value in design.items() if key not in ("ids", "by_id")}
        report["by_label_source"] = _verification_by_source(
            state, f"{model}:{effort}:verify-{VERIFY_PROMPT_VERSION}"
        )
        report["role_independence"] = (
            "Separate GPT-6 Luna verifier prompt; the first-pass labels are never sent. "
            "Agreement on the gpt-6-luna stratum is a same-model test-retest, not independent accuracy."
        )
        write_json(report_path, report)
        _write_disagreements(state, output / "verification" / "disagreements.csv")
        return {
            "sampled": len(design["ids"]),
            "metrics": report["metrics"],
            "by_label_source": report["by_label_source"],
        }

    stage("verify", {
        "classified": _records_fingerprint(state), "model": model, "effort": effort,
        "prompt": prompt_sha["verify"], "sample": verify_target, "seed": verification["seed"],
        "design": "stratified-by-label-source-v1",
    }, verify)

    # 6. group ----------------------------------------------------------------
    group_config = luna_config(50, 4000)
    grouping = stage("group", {
        "classified": _records_fingerprint(state), "model": model, "effort": effort,
        "prompt": prompt_sha["group"], "design": "embedding-group-v3",
    }, lambda: run_grouping(
        state_path=state, provider=luna(), prompt_path=prompts["group"],
        config=group_config, run_id=run_id, artifacts_dir=output,
    ))

    # 7. rank -----------------------------------------------------------------
    ranking_result = stage("rank", {
        "grouping": grouping.get("input_hash"), "classified": _records_fingerprint(state),
        "formula": "severity_sum-v1",
    }, lambda: {"ranking": rerank(
        state_path=state, output_csv=output / "ranking.csv",
        summary_json=output / "ranking.json",
    )})

    # 8. memo evidence --------------------------------------------------------
    def memo_evidence() -> dict[str, Any]:
        derived = build_actionable_and_topic_outputs(state_path=state, artifacts_dir=output)
        ranking = ranking_result["ranking"]
        write_json(output / "grouping_comparison.json", {
            "adopted": "embedding_group_v3",
            "scope": (
                "One grouping method is run here. The method was chosen earlier on the full "
                "corpus by comparing TF-IDF clustering (v1) with embedding clustering (v2/v3); "
                "that comparison is archived in artifacts/full_run_v2/grouping_comparison.json."
            ),
            "issue_count": len(ranking),
        })
        with Store(state) as store:
            local_verified = int(store.connection.execute(
                "SELECT COUNT(*) FROM verifier_records WHERE final_source='local'"
            ).fetchone()[0])
        if local_verified >= MIN_LOCAL_VERIFIED and len(ranking) >= 2:
            sensitivity = run_sensitivity_analysis(
                state_path=state, output_json=output / "sensitivity_analysis.json",
                output_md=output / "sensitivity_analysis.md",
                iterations=int(config.get("sensitivity_iterations", 1000)),
            )
            sensitivity_status = "estimated"
            stability = sensitivity["rank_stability"]
        else:
            write_json(output / "sensitivity_analysis.json", {
                "status": "not_estimated_for_this_input",
                "reason": f"fewer than {MIN_LOCAL_VERIFIED} verified local-labeled texts ({local_verified})",
                "rank_stability": {
                    "baseline_top_issue_id": ranking[0]["issue_id"] if ranking else None,
                    "note": "No verifier-based correction was fitted; baseline ranks are unadjusted.",
                },
                "issues": ranking,
                "topics": derived["topic_rollup"],
                "limitations": [
                    "No sensitivity correction was fitted for this input; treat baseline ranks as unadjusted.",
                ],
            })
            sensitivity_status = "not_estimated"
            stability = None
        return {
            "actionable_issues": len(derived["actionable_ranking"]),
            "topics": len(derived["topic_rollup"]),
            "sensitivity": sensitivity_status,
            "rank_stability": stability,
        }

    stage("memo_evidence", {
        "ranking": ranking_result["ranking"], "version": PIPELINE_VERSION,
        "verification": _file_or_none(output / "verification" / "verification_report.json"),
    }, memo_evidence)

    # 9. recommend ------------------------------------------------------------
    memo_config = luna_config(50, 6000)
    memo = stage("recommend", {
        "ranking": ranking_result["ranking"], "model": model, "effort": effort,
        "prompt": prompt_sha["memo"], "schema": "memo-v3",
        "verification": _file_or_none(output / "verification" / "verification_report.json"),
    }, lambda: run_recommendation(
        state_path=state, provider=luna(), prompt_path=prompts["memo"],
        config=memo_config, run_id=run_id, artifacts_dir=output,
    ))

    # 10. export --------------------------------------------------------------
    stage("export", {
        "ranking": ranking_result["ranking"], "memo": memo.get("input_hash"),
        "classified": _records_fingerprint(state), "version": PIPELINE_VERSION,
    }, lambda: export_grading(state_path=state, output_dir=grading))

    summary["status"] = "completed"
    _write_summary(output, summary, started_all, state, run_id, guard_holder)
    _write_manifest(
        output=output, root=root, run_id=run_id, input_csv=input_csv, source_sha=source_sha,
        config_file=config_file, prompt_sha=prompt_sha, artifact_sha=artifact_sha,
        labels={
            "local": local_config.label_config,
            "luna_enrich": enrich_config.label_config,
        },
        model=model, effort=effort, expected_cli=expected_cli,
    )
    return summary


# --- helpers -------------------------------------------------------------------


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _progress(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _append(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"at_unix": round(time.time(), 3), **value}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")


def _file_or_none(path: Path) -> str | None:
    return file_sha(path) if path.is_file() else None


def _records_fingerprint(state: Path) -> str:
    digest = hashlib.sha256()
    with Store(state) as store:
        for row in store.connection.execute(
            "SELECT review_id,status,topic,intent,severity,label_config FROM records "
            "ORDER BY review_id"
        ):
            digest.update(canonical(list(row)).encode("utf-8"))
    return digest.hexdigest()


def _route(*, state: Path, artifact: Path, artifact_sha: str, limit: int) -> dict[str, Any]:
    """Score every distinct text with the local classifier and pick risky ones for Luna.

    This is a router, not an enrichment call: its predictions are used only to
    decide routing and are not saved as labels.
    """
    provider = DistilledTextProvider(artifact_path=artifact, expected_sha256=artifact_sha)
    scored: list[dict[str, Any]] = []
    with Store(state) as store:
        cursor = store.connection.execute(
            "SELECT text_sha256,representative_review_id,review_text,group_size FROM texts "
            "ORDER BY first_source_rowid"
        )
        while True:
            chunk = [dict(row) for row in cursor.fetchmany(50)]
            if not chunk:
                break
            result = provider.call(
                role="enrich", model=DISTILLED_MODEL_VERSION, effort="none", developer_prompt="",
                user_payload={"reviews": [
                    {"review_id": row["representative_review_id"], "review_text": row["review_text"]}
                    for row in chunk
                ]},
                schema=enrichment_schema_for_count(len(chunk)), max_output_tokens=1,
                timeout_seconds=60.0,
            )
            predictions = {str(row["review_id"]): row for row in result.payload["records"]}
            diagnostics = (result.provider_metadata or {}).get("review_diagnostics") or {}
            for row in chunk:
                review_id = str(row["representative_review_id"])
                prediction = predictions[review_id]
                rule_hit = bool(HIGH_RISK.search(row["review_text"]))
                severity = int(prediction["severity"])
                eligible = prediction["topic"] == "support" or rule_hit or severity >= 4
                if not eligible:
                    continue
                risk_weight = 3 if rule_hit or severity >= 4 else (
                    2 if prediction["intent"] in {"complaint", "cancellation"} else 1
                )
                p_err = float((diagnostics.get(review_id) or {}).get("p_err", 0.0))
                scored.append({
                    "text_sha256": row["text_sha256"],
                    "representative_review_id": review_id,
                    "group_size": int(row["group_size"]),
                    "local_topic": prediction["topic"], "local_intent": prediction["intent"],
                    "local_severity": severity, "rule_hit": rule_hit,
                    "p_err": round(p_err, 6), "risk_weight": risk_weight,
                    "priority": round(p_err * int(row["group_size"]) * risk_weight, 6),
                })
        distinct = int(store.connection.execute("SELECT COUNT(*) FROM texts").fetchone()[0])
    scored.sort(key=lambda row: (-row["priority"], row["text_sha256"]))
    chosen = scored[:limit]
    with Store(state) as store:
        with store.transaction() as connection:
            connection.execute("DELETE FROM routing WHERE route_bucket=?", (ROUTE_BUCKET,))
            connection.executemany(
                "INSERT OR REPLACE INTO routing(text_sha256,route_bucket,selected_order,reasons_json) "
                "VALUES(?,?,?,?)",
                [
                    (row["text_sha256"], ROUTE_BUCKET, order,
                     canonical({key: row[key] for key in ("p_err", "priority", "rule_hit", "local_severity", "local_topic")}))
                    for order, row in enumerate(chosen, 1)
                ],
            )
    return {
        "policy": ROUTE_POLICY,
        "rule": (
            "eligible if the local router predicts topic=support or severity>=4, or a "
            "billing/privacy/fraud/support term matches; ordered by p_err x rows x risk weight "
            "(3 for rule/severity>=4, 2 complaint/cancellation, 1 otherwise), then text hash; "
            "capped at floor(max_fraction x distinct texts)"
        ),
        "distinct_texts": distinct,
        "eligible_texts": len(scored),
        "limit": limit,
        "routed_texts": len(chosen),
        "routed_rows": sum(row["group_size"] for row in chosen),
        "routed": chosen,
    }


def _luna_parallel(
    *, state: Path, provider: Any, prompt: str, config: ModelConfig, run_id: str, phase: str,
    hashes: set[str], workers: int, interrupt_after: int | None,
) -> dict[str, Any]:
    """Send routed texts to Luna in batches of <= 50 with up to ``workers`` requests in flight.

    Only the main thread touches SQLite: each finished request is validated, logged
    and saved before the next one is admitted. A batch that fails validation is
    left pending for the serial split retry. ``interrupt_after`` stops admitting new
    batches after that many saved batches (a controlled interruption for the
    resume demonstration); in-flight requests are drained and saved first.
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    with Store(state) as store:
        pending = list(store.iter_pending_unique(config.label_config, hashes))
    batches = [pending[offset:offset + 50] for offset in range(0, len(pending), 50)]
    stats = {
        "pending_texts_at_start": len(pending), "batches": len(batches),
        "succeeded_batches": 0, "failed_batches": 0, "interrupted": False,
    }
    provider_error: BaseException | None = None
    started = time.perf_counter()
    with Store(state) as store, ThreadPoolExecutor(max_workers=workers) as pool:
        queue = iter(batches)
        futures: dict[Any, tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]] = {}

        def submit() -> bool:
            batch = next(queue, None)
            if batch is None:
                return False
            rows, by_alias, aliases = compact_review_batch(batch)
            future = pool.submit(
                provider.call, role="enrich", model=config.model, effort=config.effort,
                developer_prompt=prompt, user_payload={"reviews": rows},
                schema=enrichment_schema_for_count(len(batch)),
                max_output_tokens=config.max_output_tokens, timeout_seconds=config.timeout_seconds,
            )
            futures[future] = (batch, by_alias, aliases)
            return True

        stop = False
        while len(futures) < workers and submit():
            pass
        while futures:
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done:
                batch, by_alias, aliases = futures.pop(future)
                review_ids = [row["review_id"] for row in batch]
                try:
                    result = future.result()
                except ProviderUnavailableError as error:
                    event = _failed_event(
                        run_id=run_id, role="enrich", review_ids=review_ids, model=config.model,
                        effort=config.effort, phase=phase, label_config=config.label_config,
                        error=f"{type(error).__name__}: {error}"[:2000], provider=provider.name,
                    )
                    event["request_id"] = error.request_id
                    event["duration_seconds"] = error.duration_seconds
                    store.log_call(event)
                    stats["failed_batches"] += 1
                    provider_error, stop = error, True
                    continue
                except Exception as error:  # timeout, cap or other failure
                    store.log_call(_failed_event(
                        run_id=run_id, role="enrich", review_ids=review_ids, model=config.model,
                        effort=config.effort, phase=phase, label_config=config.label_config,
                        error=f"{type(error).__name__}: {error}"[:2000], provider=provider.name,
                    ))
                    stats["failed_batches"] += 1
                    if isinstance(error, (CallCapReachedError, SpendCapReachedError)):
                        provider_error, stop = error, True
                    continue
                try:
                    alias_values, _repaired = repair_and_validate_enrichment_payload(
                        result.payload, by_alias
                    )
                    values = restore_source_review_ids(alias_values, aliases)
                except Exception as error:
                    event = _call_event(
                        result=result, run_id=run_id, role="enrich", review_ids=review_ids,
                        model=config.model, effort=config.effort, phase=phase, outcome="failed",
                        label_config=config.label_config,
                    )
                    event["error"] = f"{type(error).__name__}: {error}"[:2000]
                    store.log_call(event)
                    stats["failed_batches"] += 1
                    continue
                store.log_call(_call_event(
                    result=result, run_id=run_id, role="enrich", review_ids=review_ids,
                    model=config.model, effort=config.effort, phase=phase, outcome="succeeded",
                    label_config=config.label_config,
                ))
                by_id = {value["review_id"]: value for value in values}
                for source in batch:
                    store.save_completed(source, by_id[source["review_id"]], config.label_config, 1)
                store.expand_new_duplicate_cache_hits(
                    config.label_config, [row["text_sha256"] for row in batch]
                )
                stats["succeeded_batches"] += 1
                done_batches = stats["succeeded_batches"] + stats["failed_batches"]
                if done_batches % 10 == 0 or done_batches == len(batches):
                    _progress(
                        f"classify_luna: {stats['succeeded_batches']} saved / "
                        f"{stats['failed_batches']} failed of {len(batches)} batches "
                        f"({time.perf_counter() - started:.0f} s)"
                    )
                if interrupt_after and stats["succeeded_batches"] >= interrupt_after:
                    stop = True
                    stats["interrupted"] = True
            while not stop and len(futures) < workers and submit():
                pass
    stats["wall_seconds"] = round(time.perf_counter() - started, 3)
    if provider_error is not None:
        raise provider_error
    if stats["interrupted"]:
        raise ControlledInterrupt(
            f"Controlled interruption after {stats['succeeded_batches']} saved Luna batches; "
            "rerun the same command to resume"
        )
    return stats


def _stratified_sample(
    *, state: Path, target: int, seed: str, luna_label_config: str, luna_share: float
) -> dict[str, Any]:
    """Predeclared verification sample of distinct texts, stratified by final label source.

    The gpt-6-luna stratum gets round(target x luna_share) texts (capped by its size);
    the local stratum gets the rest. Within a stratum texts are taken in
    SHA-256(seed:review_id) order. Each verified text carries the Horvitz-Thompson
    row weight group_size x stratum_texts / stratum_sample.
    """
    with Store(state) as store:
        rows = [
            dict(row) for row in store.connection.execute(
                "SELECT t.representative_review_id AS review_id,t.group_size,r.label_config "
                "FROM texts t JOIN records r ON r.review_id=t.representative_review_id "
                "WHERE r.status='completed'"
            )
        ]
    strata: dict[str, list[dict[str, Any]]] = {"gpt-6-luna": [], "local": []}
    for row in rows:
        strata["gpt-6-luna" if row["label_config"] == luna_label_config else "local"].append(row)
    target = min(target, len(rows))
    luna_n = min(len(strata["gpt-6-luna"]), round(target * luna_share))
    sizes = {"gpt-6-luna": luna_n, "local": min(len(strata["local"]), target - luna_n)}
    by_id: dict[str, dict[str, Any]] = {}
    for name, members in strata.items():
        ordered = sorted(
            members, key=lambda row: hashlib.sha256(f"{seed}:{row['review_id']}".encode()).hexdigest()
        )[: sizes[name]]
        for row in ordered:
            by_id[str(row["review_id"])] = {
                "final_source": "luna" if name == "gpt-6-luna" else "local",
                "weight": int(row["group_size"]) * len(members) / max(1, sizes[name]),
            }
    return {
        "design": "distinct texts stratified by final label source; SHA-256(seed:review_id) order within stratum",
        "seed": seed,
        "target": target,
        "strata": {
            name: {"population_texts": len(members),
                   "population_rows": sum(int(row["group_size"]) for row in members),
                   "sampled_texts": sizes[name]}
            for name, members in strata.items()
        },
        "weights": "group_size x stratum population texts / stratum sample (row-weighted estimates)",
        "ids": list(by_id),
        "by_id": by_id,
    }


def _verification_by_source(state: Path, verifier_prefix: str) -> dict[str, Any]:
    """Agreement per final label source, unweighted and row-weighted."""
    with Store(state) as store:
        rows = store.connection.execute(
            """
            SELECT v.final_source, COALESCE(v.sample_weight,1.0) AS weight, r.topic, r.intent,
                   r.severity, v.topic AS v_topic, v.intent AS v_intent, v.severity AS v_severity
            FROM verifier_records v JOIN records r USING(review_id)
            WHERE v.label_config LIKE ? AND r.status='completed'
            """,
            (verifier_prefix + "%",),
        ).fetchall()
    result: dict[str, Any] = {}
    for row in rows:
        source = "gpt-6-luna" if row["final_source"] == "luna" else "local_classifier"
        bucket = result.setdefault(source, {"texts": 0, "w": 0.0, "n": {}, "wsum": {}})
        bucket["texts"] += 1
        bucket["w"] += float(row["weight"])
        checks = {
            "topic": row["topic"] == row["v_topic"],
            "intent": row["intent"] == row["v_intent"],
            "severity_exact": int(row["severity"]) == int(row["v_severity"]),
        }
        for key, ok in checks.items():
            bucket["n"][key] = bucket["n"].get(key, 0) + int(ok)
            bucket["wsum"][key] = bucket["wsum"].get(key, 0.0) + float(row["weight"]) * int(ok)
        bucket["n"]["abs"] = bucket["n"].get("abs", 0) + abs(int(row["severity"]) - int(row["v_severity"]))
    output: dict[str, Any] = {}
    for source, bucket in result.items():
        n, w = bucket["texts"], bucket["w"]
        output[source] = {
            "verified_texts": n,
            "estimated_rows_represented": round(w, 1),
            **{f"{key}_agreement": round(bucket["n"][key] / n, 6) for key in ("topic", "intent", "severity_exact")},
            **{f"row_weighted_{key}_agreement": round(bucket["wsum"][key] / w, 6) for key in ("topic", "intent", "severity_exact")},
            "severity_mae": round(bucket["n"]["abs"] / n, 6),
            "interpretation": (
                "same-model test-retest, not independent accuracy" if source == "gpt-6-luna"
                else "independent verifier agreement estimate"
            ),
        }
    return output


def _write_disagreements(state: Path, path: Path) -> None:
    with Store(state) as store:
        rows = store.connection.execute(
            """
            SELECT v.review_id, s.review_text, r.label_config,
                   r.topic, v.topic AS verifier_topic, r.intent, v.intent AS verifier_intent,
                   r.severity, v.severity AS verifier_severity
            FROM verifier_records v JOIN records r USING(review_id)
            JOIN source_rows s USING(review_id)
            WHERE r.status='completed' AND (r.topic<>v.topic OR r.intent<>v.intent OR r.severity<>v.severity)
            ORDER BY v.review_id
            """
        ).fetchall()
    write_csv(
        path,
        ["review_id", "first_pass_source", "topic", "verifier_topic", "intent",
         "verifier_intent", "severity", "verifier_severity", "review_text"],
        [
            {
                "review_id": row["review_id"],
                "first_pass_source": "gpt-6-luna" if str(row["label_config"]).startswith("gpt-6-luna") else "local_classifier",
                "topic": row["topic"], "verifier_topic": row["verifier_topic"],
                "intent": row["intent"], "verifier_intent": row["verifier_intent"],
                "severity": row["severity"], "verifier_severity": row["verifier_severity"],
                "review_text": row["review_text"],
            }
            for row in rows
        ],
    )


def _write_summary(
    output: Path, summary: dict[str, Any], started: float, state: Path, run_id: str,
    guard_holder: dict[str, GuardedProvider],
) -> None:
    summary["wall_seconds"] = round(time.perf_counter() - started, 6)
    if state.exists():
        with Store(state) as store:
            calls = store.connection.execute(
                "SELECT role,outcome,model,input_tokens,cached_input_tokens,output_tokens "
                "FROM calls WHERE run_id=?", (run_id,),
            ).fetchall()
            summary["counts"] = {
                "source_rows": store.source_count(),
                "completed": store.completed_count(),
                "quarantined": int(store.connection.execute(
                    "SELECT COUNT(*) FROM records WHERE status='quarantined'").fetchone()[0]),
                "exact_text_reuse_rows": int(store.connection.execute(
                    "SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL").fetchone()[0]),
            }
        luna_calls = [row for row in calls if row["model"] != DISTILLED_MODEL_VERSION]
        summary["new_model_calls_this_run"] = {
            "luna_attempts": len(luna_calls),
            "luna_failed_attempts": sum(row["outcome"] == "failed" for row in luna_calls),
            "luna_by_role": {
                role: sum(row["role"] == role for row in luna_calls) for role in ROLES
            },
            "local_classifier_batches": len(calls) - len(luna_calls),
            "luna_usage_tokens": {
                key: sum(int(row[key] or 0) for row in luna_calls)
                for key in ("input_tokens", "cached_input_tokens", "output_tokens")
            },
        }
    summary["all_stages_skipped_unchanged"] = bool(summary["stages"]) and all(
        value.get("skipped_unchanged") is True for value in summary["stages"].values()
    )
    if "guard" in guard_holder:
        summary["guard"] = guard_holder["guard"].runtime_metadata()["guard"]
    else:
        summary["guard"] = {"luna_provider_created": False}
    write_json(output / "run_summary.json", summary)


def _write_manifest(
    *, output: Path, root: Path, run_id: str, input_csv: Path, source_sha: str,
    config_file: Path, prompt_sha: dict[str, str], artifact_sha: str,
    labels: dict[str, str], model: str, effort: str, expected_cli: str,
) -> None:
    commit = None
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=10, check=False,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        commit = None
    dirty = None
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no", "--", "src", "prompts", "config"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        dirty = None
    package = Path(__file__).resolve().parent
    source_files = {
        path.name: file_sha(path) for path in sorted(package.glob("*.py"))
    }
    write_json(output / "run_manifest.json", {
        "run_id": run_id,
        "pipeline_version": PIPELINE_VERSION,
        "input_file": input_csv.name,
        "input_sha256": source_sha,
        "config_file": str(config_file.relative_to(root)) if config_file.is_relative_to(root) else config_file.name,
        "config_sha256": file_sha(config_file),
        "code_commit": commit,
        "code_uncommitted_changes": dirty,
        "code_files_sha256": source_files,
        "python": platform.python_version(),
        "prompts_sha256": prompt_sha,
        "local_classifier_artifact_sha256": artifact_sha,
        "label_configs": labels,
        "luna": {"model": model, "effort": effort, "codex_cli": expected_cli,
                 "access": "Codex CLI, ChatGPT subscription authentication; no API key"},
        "outputs": sorted(
            str(path.relative_to(output)) for path in output.rglob("*")
            if path.is_file() and "luna_traces" not in path.parts and not path.name.startswith("state.db")
        ),
    })
