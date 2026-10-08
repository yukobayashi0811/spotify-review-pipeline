from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description="Independently validate saved 100-review pilot evidence")
    parser.add_argument("--source", type=Path, default=Path("data/cost_100.csv"))
    parser.add_argument("--records", type=Path, default=Path("cost/pilot_records.jsonl"))
    parser.add_argument("--calls", type=Path, default=Path("cost/pilot_calls.jsonl"))
    parser.add_argument("--metadata", type=Path, default=Path("cost/pilot_run.json"))
    parser.add_argument("--membership", type=Path, default=Path("cost/frozen_100/membership.csv"))
    parser.add_argument("--ranking", type=Path, default=Path("cost/frozen_100/ranking.csv"))
    parser.add_argument("--report", type=Path, default=Path("cost/report.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/pilot_validation.json"))
    args = parser.parse_args()

    with args.source.open(encoding="utf-8-sig", newline="") as stream:
        source_rows = list(csv.DictReader(stream, strict=True))
    source_by_id = {row["review_id"]: row for row in source_rows}
    records = _jsonl(args.records)
    record_by_id = {row["review_id"]: row for row in records}
    calls = _jsonl(args.calls)
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    cost_report = json.loads(args.report.read_text(encoding="utf-8"))

    checks: dict[str, Any] = {}
    checks["source_rows"] = len(source_rows) == 100
    checks["record_rows"] = len(records) == 100
    checks["unique_record_ids"] = len(record_by_id) == len(records)
    checks["exact_id_set"] = set(source_by_id) == set(record_by_id)
    checks["all_completed"] = all(row.get("status") == "completed" for row in records)
    checks["quotes_are_source_substrings"] = all(
        isinstance(row.get("evidence_quote"), str)
        and row["evidence_quote"] in source_by_id[row["review_id"]]["review_text"]
        for row in records
    )
    checks["all_calls_local"] = bool(calls) and all(
        row.get("provider") in {"local-mlx", "local-distilled-sklearn"} for row in calls
    )
    checks["all_attempts_have_timing"] = all(
        isinstance(row.get("duration_seconds"), (int, float))
        and row["duration_seconds"] >= 0
        for row in calls
    )
    successful = [row for row in calls if row.get("outcome") == "succeeded"]
    checks["successful_calls_have_usage"] = all(
        isinstance(row.get("input_tokens"), int)
        and isinstance(row.get("output_tokens"), int)
        for row in successful
    )
    checks["all_roles_present"] = {row["role"] for row in successful} == {
        "enrich", "verify", "group", "memo"
    }
    checks["warm_has_no_enrichment_calls"] = not any(
        row.get("run_id") != metadata.get("projection_run_id") and row.get("role") == "enrich"
        for row in calls
    )
    checks["warm_metadata_zero_new_enrichments"] = metadata.get("warm_new_enrichment_calls") == 0
    checks["cold_completed_100"] = metadata.get("completed_records") == 100
    checks["api_cost_zero"] = cost_report.get("pilot", {}).get("api_cost_usd") == 0
    checks["local_total_cost_unknown"] = (
        cost_report.get("projection", {}).get("total_cost_including_local_compute") == "unknown"
    )

    with args.membership.open(encoding="utf-8-sig", newline="") as stream:
        membership = list(csv.DictReader(stream, strict=True))
    aggregates: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "severity": 0})
    for item in membership:
        record = record_by_id[item["review_id"]]
        if record["intent"] not in {"complaint", "cancellation"}:
            raise ValueError(f"Non-eligible membership: {item}")
        aggregates[item["issue_id"]]["count"] += 1
        aggregates[item["issue_id"]]["severity"] += int(record["severity"])
    with args.ranking.open(encoding="utf-8-sig", newline="") as stream:
        ranking = list(csv.DictReader(stream, strict=True))
    ranking_matches = True
    for item in ranking:
        aggregate = aggregates[item["issue_id"]]
        expected_mean = f"{aggregate['severity'] / aggregate['count']:.6f}"
        ranking_matches &= int(item["complaint_count"]) == aggregate["count"]
        ranking_matches &= int(item["severity_sum"]) == aggregate["severity"]
        ranking_matches &= int(item["priority_score"]) == aggregate["severity"]
        ranking_matches &= item["mean_severity"] == expected_mean
    expected_order = sorted(aggregates, key=lambda issue: (-aggregates[issue]["severity"], issue))
    ranking_matches &= [row["issue_id"] for row in ranking] == expected_order
    checks["ranking_independently_recomputed"] = ranking_matches

    failed_attempts = [row for row in calls if row.get("outcome") == "failed"]
    receipt = {
        "status": "passed" if all(checks.values()) else "failed",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "observed": {
            "calls": len(calls),
            "successful_calls": len(successful),
            "failed_attempts": len(failed_attempts),
            "warm_new_enrichment_calls": metadata.get("warm_new_enrichment_calls"),
            "cold_wall_seconds": metadata.get("cold_wall_seconds"),
            "warm_wall_seconds": metadata.get("warm_wall_seconds"),
            "api_cost_usd": cost_report.get("pilot", {}).get("api_cost_usd"),
            "ranking_issues": len(ranking),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
