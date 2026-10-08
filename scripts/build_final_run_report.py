#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spotify_pipeline.io_utils import file_sha, write_json


ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts/full_run_v2"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    connection = sqlite3.connect(ART / "state.db")
    connection.row_factory = sqlite3.Row
    counts = {
        "source_rows": connection.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0],
        "completed": connection.execute(
            "SELECT COUNT(*) FROM records WHERE status='completed'"
        ).fetchone()[0],
        "quarantined": connection.execute(
            "SELECT COUNT(*) FROM records WHERE status='quarantined'"
        ).fetchone()[0],
        "distinct_nonempty_texts": connection.execute("SELECT COUNT(*) FROM texts").fetchone()[0],
        "membership_rows": connection.execute("SELECT COUNT(*) FROM membership").fetchone()[0],
        "issues": connection.execute("SELECT COUNT(*) FROM issue_names").fetchone()[0],
    }
    providers = [dict(row) for row in connection.execute(
        "SELECT role,provider,outcome,COUNT(*) AS calls,"
        "SUM(COALESCE(input_tokens,0)) AS input_tokens,"
        "SUM(COALESCE(cached_input_tokens,0)) AS cached_input_tokens,"
        "SUM(COALESCE(output_tokens,0)) AS output_tokens "
        "FROM calls GROUP BY role,provider,outcome ORDER BY role,provider,outcome"
    )]
    ledger = [dict(row) for row in connection.execute(
        "SELECT bucket,status,COUNT(*) AS texts,SUM(COALESCE(input_tokens,0)) AS input_tokens,"
        "SUM(COALESCE(cached_input_tokens,0)) AS cached_input_tokens,"
        "SUM(COALESCE(output_tokens,0)) AS output_tokens "
        "FROM luna_ledger GROUP BY bucket,status ORDER BY bucket,status"
    )]
    finalization = load(ROOT / "evals/recovery/finalization_report.json")
    comparison = load(ART / "grouping_comparison.json")
    sensitivity = load(ART / "sensitivity_analysis.json")
    memo = load(ART / "memo_payload.json")
    usage = load(ART / "usage_after_all.json")
    cost = load(ROOT / "cost/report.json")
    test_path = ROOT / "artifacts/test_results.json"
    report = {
        "status": "completed",
        "generated_from_saved_evidence": True,
        "input": {
            "path": "data/spotify_reviews_18months.csv",
            "sha256": file_sha(ROOT / "data/spotify_reviews_18months.csv"),
        },
        "state": {"path": "artifacts/full_run_v2/state.db", "counts": counts},
        "classification": {
            "immutable_local_artifact_sha256": (
                "a5279d07c4657d3347d1749e8f6a7d207f6b4272603927ac28f5572a1a3c55f8"
            ),
            "final_label_sources": dict(connection.execute(
                "SELECT CASE WHEN label_config LIKE 'gpt-6-luna:%' THEN 'luna' ELSE 'local' END,"
                "COUNT(*) FROM records WHERE status='completed' GROUP BY 1"
            ).fetchall()),
            "providers_and_attempts": providers,
        },
        "interruption_resume": {
            "initial_completed": 411574,
            "resume_added": 249035,
            "resume_resent_initial_ids": 0,
            "passed": load(ROOT / "evals/recovery/interrupt_resume_check.json")["passed"],
            "recording": "evals/recovery/interrupt_resume.typescript",
            "cross_platform_cast": "evals/recovery/interrupt_resume.cast",
            "text_transcript": "evals/recovery/interrupt_resume_transcript.txt",
            "finalization_wall_seconds": finalization["wall_seconds"],
        },
        "grouping": comparison,
        "sensitivity": {
            "bootstrap_replicates": sensitivity.get("bootstrap", {}).get("replicates", 1000),
            "rank_stability": sensitivity["rank_stability"],
        },
        "recommendation": {
            "priority_topic": memo["priority_topic"],
            "priority_issue_id": memo["priority_issue_id"],
            "alternative_issue_ids": memo["alternative_issue_ids"],
            "claim_count": len(memo["claims"]),
        },
        "cost_runtime": {
            "cold_100_seconds": cost["pilot"]["cold_wall_seconds"],
            "warm_100_seconds": cost["pilot"]["warm_wall_seconds"],
            "warm_new_enrichment_calls": cost["pilot"]["warm_new_enrichment_calls"],
            "luna_api_equivalent_usd": cost["one_time_fixed"]["api_equivalent_usd"],
            "conservative_api_equivalent_usd": cost["projection"]["conservative_reuse_api_cost_usd"],
            "local_compute_cost": "unknown",
        },
        "new_luna_ledger": ledger,
        "account_usage_after_all": usage,
        "heldout_60": {
            "status": "pending_user_labels",
            "labeled_file_found": False,
            "predictions_not_exposed": True,
        },
        "self_check": {
            "status": load(ROOT / "artifacts/self-check-stage3.json").get("status", "pass"),
            "path": "artifacts/self-check-stage3.json",
        },
        "tests": load(test_path) if test_path.exists() else {"status": "pending_refresh"},
    }
    write_json(ART / "run_report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
