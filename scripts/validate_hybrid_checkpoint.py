from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a saved local-plus-Luna checkpoint.")
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-rows", required=True, type=int)
    args = parser.parse_args()

    root = args.checkpoint
    cold = json.loads((root / "hybrid_cold_report.json").read_text(encoding="utf-8"))
    warm_path = root / "hybrid_warm_report.json"
    warm = json.loads(warm_path.read_text(encoding="utf-8")) if warm_path.exists() else None
    with (root / "routing_decisions.csv").open(encoding="utf-8", newline="") as stream:
        decisions = list(csv.DictReader(stream))
    decision_by_id = {row["representative_review_id"]: row for row in decisions}
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    final_db = root / "final" / "state.db"
    connection = sqlite3.connect(final_db)
    connection.row_factory = sqlite3.Row
    records = connection.execute(
        "SELECT r.*,s.review_text FROM records r JOIN source_rows s USING(review_id) "
        "ORDER BY s.rowid"
    ).fetchall()
    calls = connection.execute("SELECT * FROM calls ORDER BY rowid").fetchall()
    connection.close()
    completed = [row for row in records if row["status"] == "completed"]
    check("expected_rows", len(records) == args.expected_rows, len(records))
    check("all_completed", len(completed) == args.expected_rows, len(completed))
    bad_quotes = [
        row["review_id"]
        for row in completed
        if not row["evidence_quote"] or row["evidence_quote"] not in row["review_text"]
    ]
    check("all_quotes_are_exact_source_substrings", not bad_quotes, bad_quotes[:10])
    labels = {row["label_config"] for row in completed}
    check(
        "single_hybrid_label_namespace",
        labels == {cold["hybrid_label_config"]},
        sorted(labels),
    )
    check(
        "routing_count_matches",
        len(decisions) == cold["distinct_nonempty_texts"],
        len(decisions),
    )
    routed = [row for row in decisions if row["route_to_luna"] == "True"]
    check(
        "routed_count_matches",
        len(routed) == cold["routed_unique_texts"],
        len(routed),
    )
    check(
        "routing_hard_cap_respected",
        cold["routed_rows"] <= cold["routing_cap_rows"],
        {"routed_rows": cold["routed_rows"], "cap_rows": cold["routing_cap_rows"]},
    )
    if cold.get("routing_policy", {}).get("routing_mode") == "uncertainty":
        metric = cold["routing_policy"]["uncertainty_metric"]
        missing_diagnostics = [
            row["representative_review_id"]
            for row in decisions
            if row.get(metric, "") == ""
        ]
        check(
            "uncertainty_diagnostics_complete",
            not missing_diagnostics,
            {"metric": metric, "missing": missing_diagnostics[:10]},
        )
    wrong_sources = [
        row["representative_review_id"]
        for row in decisions
        if (row["route_to_luna"] == "True") != (row["final_source"] == "luna")
    ]
    check("route_controls_final_source", not wrong_sources, wrong_sources[:10])
    check("golden_labels_not_loaded", cold["golden_labels_loaded"] is False, cold["golden_labels_loaded"])
    check("cold_run_recorded", cold["cache_mode"] == "cold_or_partial_resume", cold["cache_mode"])
    providers = sorted({row["provider"] for row in calls})
    local_providers = {"local-mlx", "local-distilled-sklearn"}
    check(
        "both_provider_provenances_saved",
        "codex-exec-subscription" in providers
        and len(set(providers) & local_providers) == 1
        and set(providers) <= local_providers | {"codex-exec-subscription"},
        providers,
    )
    successful_calls = [row for row in calls if row["outcome"] == "succeeded"]
    check("all_saved_calls_succeeded", len(successful_calls) == len(calls), len(calls))
    trace_metadata = list((root / "escalation" / "traces").rglob("metadata.json"))
    trace_violations: list[str] = []
    for path in trace_metadata:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("api_credentials_present_in_child") is not False:
            trace_violations.append(f"{path}:credentials")
        if value.get("event_summary", {}).get("forbidden_tool_types"):
            trace_violations.append(f"{path}:forbidden_tools")
    check("codex_traces_are_credential_and_tool_free", not trace_violations, trace_violations)
    if warm is not None:
        check("warm_run_recorded", warm["cache_mode"] == "warm", warm["cache_mode"])
        check(
            "warm_replay_zero_model_records",
            warm["local_run"]["processed_unique"] == 0
            and warm["escalation_run"]["processed_unique"] == 0,
            {
                "local": warm["local_run"]["processed_unique"],
                "luna": warm["escalation_run"]["processed_unique"],
            },
        )
    else:
        check("warm_run_recorded", False, "hybrid_warm_report.json missing")

    report = {
        "checkpoint": str(root.resolve()),
        "expected_rows": args.expected_rows,
        "checks_passed": sum(bool(item["passed"]) for item in checks),
        "checks_total": len(checks),
        "passed": all(bool(item["passed"]) for item in checks),
        "checks": checks,
        "decision_ids_unique": len(decision_by_id) == len(decisions),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
