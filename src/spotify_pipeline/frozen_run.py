from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .classify import ModelConfig, run_enrichment
from .distilled import DISTILLED_MODEL_VERSION, DistilledTextProvider
from .downstream_analysis import build_actionable_and_topic_outputs
from .export import export_grading
from .io_utils import canonical, file_sha, write_json
from .pilot_group import run_pilot_grouping
from .prepare import ingest
from .providers import LocalMLXProvider
from .rank import rerank
from .recommend import run_recommendation
from .storage import Store
from .verify import run_verification


def run_frozen(input_path: str | Path, config_path: str | Path) -> dict[str, Any]:
    """Run or replay the immutable 100-review submission proof."""
    started_all = time.perf_counter()
    config_file = Path(config_path).resolve()
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if not config.get("fresh_100_proof", False):
        return _replay_completed_full(input_path=input_path, config_file=config_file, config=config)
    root = config_file.parent.parent
    output = _resolve(root, config["output_dir"])
    state = _resolve(root, config["state_path"])
    grading = _resolve(root, config.get("grading_dir", "grading"))
    artifact = _resolve(root, config["local_artifact_path"])
    manifest = _resolve(root, config["local_manifest_path"])
    input_csv = Path(input_path).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = f"frozen-{uuid.uuid4()}"
    summary: dict[str, Any] = {
        "run_id": run_id, "mode": "frozen", "input": str(input_csv),
        "state": str(state), "stages": {},
    }
    run_log = output / "run_log.jsonl"

    def stage(name: str, fingerprint: dict[str, Any], action: Callable[[], Any]) -> Any:
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
                _append_log(run_log, {
                    "run_id": run_id, "stage": name, "status": "skipped_unchanged",
                    "input_sha256": input_sha, "output_sha256": cached["output_sha256"],
                })
                return value
            with store.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO stage_runs(run_id,stage,input_sha256,status) "
                    "VALUES(?,?,?,'running')", (run_id, name, input_sha),
                )
                stage_id = int(cursor.lastrowid)
        started = time.perf_counter()
        try:
            raw = action()
            details = raw if isinstance(raw, dict) else {"result": raw}
            details["wall_seconds"] = round(time.perf_counter() - started, 6)
            output_sha = hashlib.sha256(canonical(details).encode()).hexdigest()
            with Store(state) as store:
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE stage_runs SET output_sha256=?,status='completed',"
                        "details_json=?,finished_at=CURRENT_TIMESTAMP WHERE stage_run_id=?",
                        (output_sha, canonical(details), stage_id),
                    )
            summary["stages"][name] = details
            _append_log(run_log, {
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
            _append_log(run_log, {
                "run_id": run_id, "stage": name, "status": "failed",
                "input_sha256": input_sha, "error": f"{type(error).__name__}: {error}",
            })
            raise

    source_sha = file_sha(input_csv)
    expected_cost_sha = "c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e"
    if source_sha != expected_cost_sha:
        raise ValueError(
            "The fresh frozen cost proof is sealed to data/cost_100.csv; "
            f"expected {expected_cost_sha}, received {source_sha}"
        )

    def prepare() -> dict[str, Any]:
        report = ingest(input_csv, state, output / "ingestion.json")
        with Store(state) as store:
            distinct = store.rebuild_texts()
            before = {"completed_ids": store.completed_ids()}
            store.set_metadata("checkpoint_before", before)
        write_json(output / "checkpoint_before.json", before)
        return {**report, "distinct_nonempty_texts": distinct}

    stage("prepare", {"input": source_sha, "runner": "frozen-v3"}, prepare)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    artifact_sha = file_sha(artifact)
    if manifest_value.get("artifact_sha256") != artifact_sha:
        raise RuntimeError("Frozen artifact does not match manifest")
    local_config = ModelConfig(
        model=DISTILLED_MODEL_VERSION, effort="none", batch_size=50, max_workers=1,
        max_attempts=1, max_output_tokens=1, timeout_seconds=60, spend_limit_usd=1,
        execution_fingerprint=(
            f"artifact-{artifact_sha}:sklearn-{manifest_value['scikit_learn_version']}:final-local-v2"
        ),
    )

    def classify() -> dict[str, Any]:
        with Store(state) as store:
            before_calls = int(store.connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
        provider = DistilledTextProvider(artifact_path=artifact, expected_sha256=artifact_sha)
        result = run_enrichment(
            state_path=state, provider=provider, prompt_path=root / "prompts/classify.md",
            config=local_config, run_id=run_id, phase="initial",
        )
        with Store(state) as store:
            after_calls = int(store.connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
            after = {"completed_ids": store.completed_ids()}
            store.set_metadata("checkpoint_after", after)
        write_json(output / "checkpoint_after.json", after)
        return {**result, "new_model_calls": after_calls - before_calls}

    stage("classify", {
        "source": source_sha, "artifact": artifact_sha, "workers": 1, "legacy_cache": False,
    }, classify)

    downstream_model = config.get("downstream_model", "mlx-community/gemma-4-e4b-it-4bit")
    downstream_revision = config.get(
        "downstream_model_revision", "475b9088d29754a3379866cf5aeb6b41acd313c2",
    )
    local_llm = LocalMLXProvider(model_revision=downstream_revision, temperature=0.0)
    downstream_config = ModelConfig(
        model=downstream_model, effort="none", batch_size=10, max_workers=1,
        max_attempts=2, max_output_tokens=6000, timeout_seconds=300,
        spend_limit_usd=1,
        execution_fingerprint=f"revision-{downstream_revision}:offline-frozen-100-v1",
    )

    def verify() -> dict[str, Any]:
        report_path = output / "verification" / "verification_report.json"
        report = run_verification(
            state_path=state, provider=local_llm, prompt_path=root / "prompts/verify.md",
            config=downstream_config, run_id=run_id, output_path=report_path,
            sample_fraction=0.1, minimum_sample=10, maximum_sample=10,
            seed="frozen-100-verifier-v1",
        )
        report["metrics"] = report["agreement"]
        report["role_independence"] = (
            "Independent local Gemma prompt/model role; first-pass predictions were not supplied"
        )
        write_json(report_path, report)
        return report

    verification = stage("verify", {
        "classification_artifact": artifact_sha, "model": downstream_model,
        "revision": downstream_revision, "sample": 10, "blind": True,
    }, verify)
    grouping = stage("group", {
        "source": source_sha, "classification_artifact": artifact_sha,
        "model": downstream_model, "revision": downstream_revision,
        "design": "pilot-topic-groups-v1",
    }, lambda: run_pilot_grouping(
        state_path=state, provider=local_llm, prompt_path=root / "prompts/pilot_group.md",
        config=downstream_config, run_id=run_id, artifacts_dir=output,
    ))
    ranking_result = stage("rank", {
        "source": source_sha, "artifact": artifact_sha, "group_design": "pilot-topic-groups-v1",
        "formula": "severity_sum-v1",
    }, lambda: {"ranking": rerank(
        state_path=state, output_csv=output / "ranking.csv",
        summary_json=output / "ranking.json",
    )})

    def memo_evidence() -> dict[str, Any]:
        derived = build_actionable_and_topic_outputs(state_path=state, artifacts_dir=output)
        ranking = ranking_result["ranking"]
        comparison = {
            "adopted": "pilot_topic_groups_v1", "scope": "100-review execution proof only",
            "not_comparable_to_full_corpus_selection": True, "issue_count": len(ranking),
        }
        sensitivity = {
            "status": "not_estimated_for_100_review_execution_proof",
            "rank_stability": {
                "baseline_top_issue_id": ranking[0]["issue_id"] if ranking else None,
                "note": "The formal weighted 300-review verifier sensitivity is a full-run artifact.",
            },
            "issues": ranking, "topics": derived["topic_rollup"],
            "limitations": [
                "This 100-review proof is too small for a separate weighted sensitivity fit.",
                "Use artifacts/full_run_v2/sensitivity_analysis.json for the formal analysis.",
            ],
        }
        write_json(output / "grouping_comparison.json", comparison)
        write_json(output / "sensitivity_analysis.json", sensitivity)
        return {
            "actionable_issues": len(derived["actionable_ranking"]),
            "topic_count": len(derived["topic_rollup"]),
            "sensitivity_status": sensitivity["status"],
        }

    stage("memo_evidence", {
        "source": source_sha, "artifact": artifact_sha,
        "group_design": "pilot-topic-groups-v1", "sensitivity": "pilot-not-estimated-v1",
    }, memo_evidence)
    with Store(state) as store:
        ranking_hash = hashlib.sha256(canonical(store.get_metadata("ranking", [])).encode()).hexdigest()
    memo = stage("recommend", {
        "ranking": ranking_hash, "model": downstream_model,
        "revision": downstream_revision, "schema": "memo-v2",
    }, lambda: run_recommendation(
        state_path=state, provider=local_llm, prompt_path=root / "prompts/memo.md",
        config=downstream_config, run_id=run_id, artifacts_dir=output,
    ))
    stage("export", {"source": source_sha, "ranking": ranking_hash, "memo_schema": "memo-v2"},
          lambda: export_grading(state_path=state, output_dir=grading))
    summary["status"] = "completed"
    summary["wall_seconds"] = round(time.perf_counter() - started_all, 6)
    with Store(state) as store:
        current_calls = list(store.connection.execute(
            "SELECT role,outcome FROM calls WHERE run_id=? ORDER BY rowid", (run_id,),
        ))
    summary["new_model_calls"] = len(current_calls)
    summary["new_model_calls_by_role"] = {
        role: sum(row["role"] == role for row in current_calls)
        for role in ("enrich", "verify", "group", "memo")
    }
    summary["new_failed_model_attempts"] = sum(
        row["outcome"] == "failed" for row in current_calls
    )
    summary["all_stages_skipped_unchanged"] = all(
        value.get("skipped_unchanged") is True for value in summary["stages"].values()
    )
    summary["runtime"] = local_llm.runtime_metadata()
    summary["classification_artifact_sha256"] = artifact_sha
    summary["downstream_model_revision"] = downstream_revision
    write_json(output / "frozen_run_summary.json", summary)
    return summary


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _replay_completed_full(
    *, input_path: str | Path, config_file: Path, config: dict[str, Any]
) -> dict[str, Any]:
    """Fail-closed warm replay for the completed full-corpus frozen state.

    Grouping/recommendation revisions are never silently regenerated here. A
    fresh full state must use the explicitly staged training/finalization path;
    the frozen path validates the sealed completed state and refreshes only its
    deterministic grading adapter.
    """

    root = config_file.parent.parent
    state = _resolve(root, config["state_path"])
    output = _resolve(root, config["output_dir"])
    grading = _resolve(root, config.get("grading_dir", "grading"))
    artifact = _resolve(root, config["local_artifact_path"])
    manifest = _resolve(root, config["local_manifest_path"])
    input_csv = Path(input_path).resolve()
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    artifact_sha = file_sha(artifact)
    if manifest_value.get("artifact_sha256") != artifact_sha:
        raise RuntimeError("Frozen artifact does not match its manifest")
    with Store(state) as store:
        ingestion = store.get_metadata("ingestion_report")
        if not ingestion or ingestion.get("file_sha256") != file_sha(input_csv):
            raise RuntimeError("Frozen full input does not match the completed state's source hash")
        source = store.source_count()
        completed = store.completed_count()
        quarantined = int(store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE status='quarantined'"
        ).fetchone()[0])
        issues = int(store.connection.execute("SELECT COUNT(*) FROM issue_names").fetchone()[0])
        membership = int(store.connection.execute("SELECT COUNT(*) FROM membership").fetchone()[0])
        verifier_rows = int(store.connection.execute(
            "SELECT COUNT(*) FROM verifier_records"
        ).fetchone()[0])
        memo = store.get_metadata("memo_payload")
        ranking = store.get_metadata("ranking", [])
    if (source, completed, quarantined) != (660622, 660609, 13):
        raise RuntimeError(
            "Frozen full replay requires the completed 660,622-row state; "
            f"observed source={source}, completed={completed}, quarantined={quarantined}"
        )
    if issues != 41 or membership != 288524 or verifier_rows < 400 or not memo or not ranking:
        raise RuntimeError(
            "Completed downstream evidence is missing or does not match the adopted revision"
        )
    export_status = export_grading(state_path=state, output_dir=grading)
    result = {
        "status": "completed_saved_state_replay",
        "mode": "frozen",
        "new_model_calls": 0,
        "input_sha256": ingestion["file_sha256"],
        "classification_artifact_sha256": artifact_sha,
        "counts": {
            "source": source, "completed": completed, "quarantined": quarantined,
            "issues": issues, "membership": membership, "verifier_rows": verifier_rows,
        },
        "priority_issue_id": memo["priority_issue_id"],
        "ranking_issues": len(ranking),
        "export": export_status,
        "note": (
            "Warm frozen replay validates the sealed full state and regenerates only the "
            "deterministic grading adapter; it never overwrites adopted grouping or memo evidence."
        ),
    }
    write_json(output / "frozen_replay_summary.json", result)
    return result


def _append_log(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
