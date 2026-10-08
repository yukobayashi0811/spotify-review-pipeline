from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _mean(total: int, count: int) -> str:
    return str(
        (Decimal(total) / Decimal(count)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently validate saved distilled downstream artifacts."
    )
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.checkpoint
    downstream = root / "downstream"
    state_path = root / "final" / "state.db"
    cold = json.loads((downstream / "cold_report.json").read_text(encoding="utf-8"))
    warm = json.loads((downstream / "warm_report.json").read_text(encoding="utf-8"))
    saved_ranking = _read_csv(downstream / "ranking.csv")
    membership_csv = _read_csv(downstream / "membership.csv")
    issues_csv = _read_csv(downstream / "issues.csv")
    claims_csv = _read_csv(downstream / "claims.csv")
    memo_payload = json.loads(
        (downstream / "memo_payload.json").read_text(encoding="utf-8")
    )
    evidence_pack = json.loads(
        (downstream / "memo_evidence_pack.json").read_text(encoding="utf-8")
    )

    connection = sqlite3.connect(state_path)
    connection.row_factory = sqlite3.Row
    complaint_rows = list(
        connection.execute(
            "SELECT review_id,topic,severity FROM records "
            "WHERE status='completed' AND intent IN ('complaint','cancellation')"
        )
    )
    db_memberships = [
        dict(row)
        for row in connection.execute(
            "SELECT issue_id,review_id FROM membership ORDER BY issue_id,review_id"
        )
    ]
    verifier_count = int(
        connection.execute("SELECT COUNT(*) FROM verifier_records").fetchone()[0]
    )
    downstream_calls = [
        dict(row)
        for row in connection.execute(
            "SELECT role,outcome,provider FROM calls "
            "WHERE role IN ('verify','group','memo') ORDER BY created_at,request_id"
        )
    ]
    connection.close()

    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    complaint_ids = {row["review_id"] for row in complaint_rows}
    membership_ids = [row["review_id"] for row in db_memberships]
    check(
        "one_membership_per_complaint_or_cancellation",
        len(membership_ids) == len(set(membership_ids)) == len(complaint_ids)
        and set(membership_ids) == complaint_ids,
        {"eligible": len(complaint_ids), "memberships": len(membership_ids)},
    )
    check(
        "membership_csv_matches_database",
        membership_csv == db_memberships,
        {"csv": len(membership_csv), "database": len(db_memberships)},
    )

    aggregates: dict[str, dict[str, int]] = {}
    severity_by_id = {row["review_id"]: int(row["severity"]) for row in complaint_rows}
    for row in db_memberships:
        item = aggregates.setdefault(row["issue_id"], {"count": 0, "severity_sum": 0})
        item["count"] += 1
        item["severity_sum"] += severity_by_id[row["review_id"]]
    recomputed = []
    for issue_id, values in aggregates.items():
        recomputed.append(
            {
                "issue_id": issue_id,
                "complaint_count": values["count"],
                "severity_sum": values["severity_sum"],
                "mean_severity": _mean(values["severity_sum"], values["count"]),
                "priority_score": values["severity_sum"],
            }
        )
    recomputed.sort(key=lambda row: (-row["priority_score"], row["issue_id"]))
    for rank, row in enumerate(recomputed, 1):
        row["rank"] = rank
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
    check(
        "ranking_recomputed_exactly",
        normalized_saved == recomputed,
        {"saved": normalized_saved, "recomputed": recomputed},
    )
    check(
        "issue_definitions_cover_ranking",
        {row["issue_id"] for row in issues_csv}
        == {row["issue_id"] for row in saved_ranking},
        len(issues_csv),
    )

    check(
        "verification_sample_complete",
        verifier_count == cold["verification"]["selected"]
        == cold["verification"]["verified"],
        {"selected": cold["verification"]["selected"], "saved": verifier_count},
    )
    agreement = cold["verification"]["agreement"]
    check(
        "verification_metrics_in_bounds",
        all(0.0 <= float(value) <= 1.0 for value in agreement.values()),
        agreement,
    )
    check(
        "planted_error_detected",
        cold["verification"]["planted_error_test"]["detected"] is True,
        cold["verification"]["planted_error_test"],
    )

    ranking_by_issue = {row["issue_id"]: row for row in normalized_saved}
    valid_claims = True
    invalid_claim_ids: list[str] = []
    seen_claim_ids: set[str] = set()
    evidence_ids = {
        issue_id: {item["review_id"] for item in items}
        for issue_id, items in evidence_pack["evidence"].items()
    }
    for claim in memo_payload["claims"]:
        issue = ranking_by_issue.get(claim["issue_id"])
        supported = (
            claim["claim_id"] not in seen_claim_ids
            and issue is not None
            and claim["metric"] in {
                "complaint_count",
                "severity_sum",
                "mean_severity",
                "priority_score",
            }
            and str(issue[claim["metric"]]) == claim["value"]
            and set(claim["review_ids"]) <= evidence_ids.get(claim["issue_id"], set())
        )
        seen_claim_ids.add(claim["claim_id"])
        if not supported:
            valid_claims = False
            invalid_claim_ids.append(claim["claim_id"])
    top_issue = normalized_saved[0]["issue_id"]
    required_metrics = {
        "complaint_count",
        "severity_sum",
        "mean_severity",
        "priority_score",
    }
    supplied_top_metrics = {
        claim["metric"]
        for claim in memo_payload["claims"]
        if claim["issue_id"] == top_issue
    }
    check(
        "memo_claims_supported_by_ranking_and_evidence_pack",
        valid_claims and required_metrics <= supplied_top_metrics,
        {"invalid_claim_ids": invalid_claim_ids, "top_issue": top_issue},
    )
    check(
        "claims_csv_matches_payload",
        claims_csv
        == [
            {key: str(claim[key]) for key in ("claim_id", "issue_id", "metric", "value")}
            for claim in memo_payload["claims"]
        ],
        len(claims_csv),
    )

    successful_roles = {
        row["role"] for row in downstream_calls if row["outcome"] == "succeeded"
    }
    failed_attempts = [row for row in downstream_calls if row["outcome"] == "failed"]
    check(
        "cold_downstream_roles_completed_with_bounded_failures",
        len(downstream_calls) > 0
        and successful_roles == {"verify", "group", "memo"}
        and len(failed_attempts) <= 2,
        {
            "successful_roles": sorted(successful_roles),
            "failed_attempts": failed_attempts,
        },
    )
    trace_violations: list[str] = []
    for path in (downstream / "traces").rglob("metadata.json"):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if metadata.get("api_credentials_present_in_child") is not False:
            trace_violations.append(f"{path}:credentials")
        if metadata.get("event_summary", {}).get("forbidden_tool_types"):
            trace_violations.append(f"{path}:forbidden_tools")
    check(
        "codex_traces_are_credential_and_tool_free",
        not trace_violations,
        trace_violations,
    )
    check(
        "warm_replay_zero_model_calls",
        warm["verification"]["pending_at_start"] == 0
        and warm["grouping"]["model_called"] is False
        and warm["recommendation"]["model_called"] is False
        and warm["provider_runtime"]["calls"] == [],
        {
            "verification_pending": warm["verification"]["pending_at_start"],
            "group_model_called": warm["grouping"]["model_called"],
            "memo_model_called": warm["recommendation"]["model_called"],
            "provider_calls": len(warm["provider_runtime"]["calls"]),
        },
    )

    report = {
        "checkpoint": str(root.resolve()),
        "checks_passed": sum(bool(item["passed"]) for item in checks),
        "checks_total": len(checks),
        "passed": all(bool(item["passed"]) for item in checks),
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
