from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from spotify_pipeline.io_utils import file_sha, write_json


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, strict=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Independently validate a Codex experiment")
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    experiment = args.experiment
    plan = json.loads((experiment / "experiment_plan.json").read_text(encoding="utf-8"))
    benchmark = json.loads((experiment / "benchmark_report.json").read_text(encoding="utf-8"))
    merge = json.loads((experiment / "consolidated/merge_report.json").read_text(encoding="utf-8"))
    warm = json.loads((experiment / "warm_replay.json").read_text(encoding="utf-8"))
    downstream = json.loads(
        (experiment / "downstream/end_to_end_report.json").read_text(encoding="utf-8")
    )
    artifacts = experiment / "downstream/artifacts"
    verification = json.loads((artifacts / "verification_report.json").read_text(encoding="utf-8"))
    memo = json.loads((artifacts / "memo_payload.json").read_text(encoding="utf-8"))
    source_rows = read_csv(experiment / "sample.csv")
    source_by_id = {row["review_id"]: row for row in source_rows}
    connection = sqlite3.connect(experiment / "consolidated/state.db")
    connection.row_factory = sqlite3.Row
    records = [dict(row) for row in connection.execute("SELECT * FROM records ORDER BY review_id")]
    completed = [row for row in records if row["status"] == "completed"]
    calls = [dict(row) for row in connection.execute("SELECT * FROM calls ORDER BY rowid")]

    checks: dict[str, bool] = {}
    checks["source_hash_matches_plan"] = file_sha(experiment / "sample.csv") == merge["input_sha256"]
    checks["source_ids_unique"] = len(source_rows) == len(source_by_id)
    checks["all_records_completed"] = len(completed) == len(source_rows) == int(plan["sample_size"])
    checks["all_quotes_are_exact_substrings"] = all(
        row["evidence_quote"] in source_by_id[row["review_id"]]["review_text"]
        for row in completed
    )
    checks["benchmark_merge_valid"] = benchmark["merge_valid"] is True and merge["valid"] is True
    checks["warm_zero_model_calls"] = (
        warm["new_model_calls"] == 0
        and warm["completed_before"] == warm["completed_after"] == len(source_rows)
    )
    call_counts = Counter((row["role"], row["outcome"]) for row in calls)
    checks["call_log_has_all_roles"] = all(
        call_counts[(role, "succeeded")] > 0 for role in ("enrich", "verify", "group", "memo")
    )
    checks["all_call_providers_are_codex"] = all(
        row["provider"] == "codex-exec-subscription" for row in calls
    )
    checks["succeeded_calls_are_metered"] = all(
        row["input_tokens"] is not None
        and row["output_tokens"] is not None
        and row["duration_seconds"] > 0
        for row in calls
        if row["outcome"] == "succeeded"
    )
    checks["failed_calls_preserve_errors"] = all(
        bool(row["error"])
        for row in calls
        if row["outcome"] == "failed"
    )

    memberships = [
        tuple(row) for row in connection.execute("SELECT issue_id,review_id FROM membership")
    ]
    records_by_id = {row["review_id"]: row for row in records}
    expected_members = sorted(
        row["review_id"] for row in completed if row["intent"] in {"complaint", "cancellation"}
    )
    actual_members = sorted(review_id for _, review_id in memberships)
    checks["membership_complete_and_unique"] = (
        actual_members == expected_members and len(actual_members) == len(set(actual_members))
    )
    by_issue: defaultdict[str, list[int]] = defaultdict(list)
    for issue_id, review_id in memberships:
        by_issue[issue_id].append(int(records_by_id[review_id]["severity"]))
    recomputed = []
    for issue_id, severities in by_issue.items():
        total = sum(severities)
        mean = (Decimal(total) / Decimal(len(severities))).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        recomputed.append(
            {
                "issue_id": issue_id,
                "complaint_count": len(severities),
                "severity_sum": total,
                "mean_severity": f"{mean:.6f}",
                "priority_score": total,
            }
        )
    recomputed.sort(key=lambda row: (-row["priority_score"], row["issue_id"]))
    for index, row in enumerate(recomputed, start=1):
        row["rank"] = index
    saved_ranking = [
        {
            "issue_id": row["issue_id"],
            "complaint_count": int(row["complaint_count"]),
            "severity_sum": int(row["severity_sum"]),
            "mean_severity": row["mean_severity"],
            "priority_score": int(row["priority_score"]),
            "rank": int(row["rank"]),
        }
        for row in read_csv(artifacts / "ranking.csv")
    ]
    checks["ranking_recomputed_exactly"] = recomputed == saved_ranking
    ranking_by_issue = {row["issue_id"]: row for row in recomputed}
    claims = read_csv(artifacts / "claims.csv")
    checks["claims_match_ranking"] = all(
        claim["issue_id"] in ranking_by_issue
        and claim["metric"] in ranking_by_issue[claim["issue_id"]]
        and str(ranking_by_issue[claim["issue_id"]][claim["metric"]]) == claim["value"]
        for claim in claims
    )
    expected_verification = min(len(source_rows), max(10, round(len(source_rows) * 0.1)))
    checks["verification_complete"] = (
        verification["selected"] == verification["verified"] == expected_verification
        and not verification["failures"]
        and verification["planted_error_test"]["detected"] is True
    )
    checks["memo_priority_present"] = bool(memo["priority"].strip()) and len(claims) >= 4

    trace_paths = sorted(experiment.glob("worker-*/traces/*/metadata.json")) + sorted(
        (experiment / "downstream/traces").glob("*/metadata.json")
    )
    trace_metadata = [json.loads(path.read_text(encoding="utf-8")) for path in trace_paths]
    checks["all_traces_accounted_and_tool_free"] = (
        len(trace_metadata) == len(calls)
        and all(
            (
                item["event_summary"]["turn_completed"] is True
                or item["timed_out"] is True
                or item["exit_code"] not in (None, 0)
            )
            and not item["event_summary"]["forbidden_tool_types"]
            for item in trace_metadata
        )
    )
    checks["no_api_credentials_in_children"] = all(
        item["api_credentials_present_in_child"] is False for item in trace_metadata
    )
    checks["development_boundary_preserved"] = (
        plan["development_only"] is True
        and plan["grading_eligible"] is False
        and downstream["grading_eligible"] is False
        and benchmark["monetary_cost_usd"] == "unknown"
        and downstream["monetary_cost_usd"] == "unknown"
    )
    connection.close()
    result = {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "details": {
            "input_rows": len(source_rows),
            "completed_records": len(completed),
            "calls": len(calls),
            "call_counts": {
                f"{role}:{outcome}": count
                for (role, outcome), count in sorted(call_counts.items())
            },
            "input_tokens": sum(row["input_tokens"] or 0 for row in calls),
            "output_tokens": sum(row["output_tokens"] or 0 for row in calls),
            "trace_files": len(trace_metadata),
            "membership_rows": len(memberships),
            "ranking_issues": len(recomputed),
            "top_issue": recomputed[0] if recomputed else None,
            "verification_agreement": verification["agreement"],
            "classification_wall_seconds": benchmark["wall_seconds"],
            "downstream_wall_seconds": downstream["downstream_wall_seconds"],
            "warm_wall_seconds": warm["wall_seconds"],
        },
    }
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
