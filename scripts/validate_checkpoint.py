from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from spotify_pipeline.io_utils import write_json


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, strict=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Independently validate a saved development checkpoint")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--cost-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source_rows = read_csv(args.input)
    source_by_id = {row["review_id"]: row for row in source_rows}
    run_summary = json.loads((args.artifacts / "run_summary.json").read_text(encoding="utf-8"))
    verification = json.loads((args.artifacts / "verification_report.json").read_text(encoding="utf-8"))
    cost = json.loads(args.cost_report.read_text(encoding="utf-8"))

    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    checks["input_sha256_matches"] = (
        hashlib.sha256(args.input.read_bytes()).hexdigest()
        == run_summary["stages"]["prepare"]["file_sha256"]
    )
    checks["input_ids_unique"] = len(source_rows) == len(source_by_id)

    connection = sqlite3.connect(args.state)
    connection.row_factory = sqlite3.Row
    db_ids = [row[0] for row in connection.execute("SELECT review_id FROM source_rows ORDER BY rowid")]
    records = [dict(row) for row in connection.execute("SELECT * FROM records ORDER BY review_id")]
    completed = [row for row in records if row["status"] == "completed"]
    quarantined = [row for row in records if row["status"] == "quarantined"]
    checks["source_ids_match"] = db_ids == [row["review_id"] for row in source_rows]
    checks["all_records_completed"] = len(completed) == len(source_rows) and not quarantined
    checks["all_quotes_are_exact_substrings"] = all(
        row["evidence_quote"] in source_by_id[row["review_id"]]["review_text"] for row in completed
    )

    records_by_id = {row["review_id"]: row for row in records}
    cache_checks = []
    for row in completed:
        cache_source_id = row["cache_source_id"]
        if not cache_source_id:
            continue
        source = records_by_id.get(cache_source_id)
        same_text = (
            source_by_id[row["review_id"]]["review_text"]
            == source_by_id[cache_source_id]["review_text"]
        )
        same_fields = source is not None and all(
            row[field] == source[field]
            for field in (
                "topic", "intent", "sentiment", "severity", "entities_json",
                "evidence_quote", "needs_review", "label_config",
            )
        )
        cache_checks.append(bool(source and source["status"] == "completed" and same_text and same_fields))
    checks["direct_cache_provenance_valid"] = all(cache_checks)

    call_counts = Counter()
    tokens = Counter()
    run_id = run_summary["run_id"]
    for row in connection.execute("SELECT * FROM calls WHERE run_id=?", (run_id,)):
        call_counts[(row["role"], row["outcome"])] += 1
        tokens["input"] += row["input_tokens"] or 0
        tokens["output"] += row["output_tokens"] or 0
    checks["call_log_has_all_roles"] = all(
        any(role == required and outcome == "succeeded" for role, outcome in call_counts)
        for required in ("enrich", "verify", "group", "memo")
    )
    checks["failed_attempts_retained"] = call_counts[("enrich", "failed")] == 2

    membership = [tuple(row) for row in connection.execute("SELECT issue_id,review_id FROM membership")]
    by_issue: defaultdict[str, list[int]] = defaultdict(list)
    member_ids = []
    for issue_id, review_id in membership:
        record = records_by_id[review_id]
        if record["intent"] not in {"complaint", "cancellation"}:
            raise ValueError(f"Non-complaint record in membership: {review_id}")
        by_issue[issue_id].append(int(record["severity"]))
        member_ids.append(review_id)
    expected_member_ids = sorted(
        row["review_id"] for row in completed if row["intent"] in {"complaint", "cancellation"}
    )
    checks["membership_complete_and_unique"] = (
        sorted(member_ids) == expected_member_ids and len(member_ids) == len(set(member_ids))
    )

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
    for index, row in enumerate(recomputed, 1):
        row["rank"] = index
    saved_ranking = read_csv(args.artifacts / "ranking.csv")
    normalized_saved = [
        {
            "issue_id": row["issue_id"],
            "complaint_count": int(row["complaint_count"]),
            "severity_sum": int(row["severity_sum"]),
            "mean_severity": row["mean_severity"],
            "priority_score": int(row["priority_score"]),
            "rank": int(row["rank"]),
        }
        for row in saved_ranking
    ]
    checks["ranking_recomputed_exactly"] = recomputed == normalized_saved

    ranking_by_issue = {row["issue_id"]: row for row in recomputed}
    claims = read_csv(args.artifacts / "claims.csv")
    checks["claims_match_ranking"] = all(
        claim["issue_id"] in ranking_by_issue
        and str(ranking_by_issue[claim["issue_id"]][claim["metric"]]) == claim["value"]
        for claim in claims
    )
    checks["verification_complete"] = (
        verification["selected"] == 50
        and verification["verified"] == 50
        and not verification["failures"]
        and verification["planted_error_test"]["detected"] is True
    )
    checks["cost_report_matches_run"] = (
        cost["status"] == "measured"
        and cost["pilot"]["rows"] == len(source_rows)
        and cost["pilot"]["completed_records"] == len(completed)
        and cost["pilot"]["quarantined_or_failed_records"] == len(quarantined)
        and cost["pilot"]["input_tokens"] == tokens["input"]
        and cost["pilot"]["output_tokens"] == tokens["output"]
        and cost["pilot"]["cold_wall_seconds"] == run_summary["wall_seconds"]
    )

    details.update(
        input_rows=len(source_rows),
        completed_records=len(completed),
        quarantined_records=len(quarantined),
        direct_cache_reuses=len(cache_checks),
        membership_rows=len(membership),
        ranking_issues=len(recomputed),
        top_issue=recomputed[0] if recomputed else None,
        call_counts={f"{role}:{outcome}": count for (role, outcome), count in sorted(call_counts.items())},
        input_tokens=tokens["input"],
        output_tokens=tokens["output"],
        wall_seconds=run_summary["wall_seconds"],
        verification_agreement=verification["agreement"],
        projected_reuse_days=cost["projection"]["base_reuse_elapsed_days"],
        projected_conservative_days=cost["projection"]["conservative_reuse_elapsed_days"],
    )
    connection.close()

    result = {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "details": details,
    }
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
