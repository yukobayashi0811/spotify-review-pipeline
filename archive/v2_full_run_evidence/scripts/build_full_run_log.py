#!/usr/bin/env python3
"""Consolidated run log and summary for the completed full-corpus run (no model calls).

The full run was executed in stages over several invocations (teacher labels,
active learning, finalization with an interruption/resume, grouping, memo).
This script reads the saved SQLite state and writes:

* artifacts/full_run_v2/run_log.jsonl  - one line per recorded invocation
  (run_id) and per stage_runs entry, in time order, with attempts, outcomes,
  token usage and summed request time;
* artifacts/full_run_v2/run_summary.json - totals by role/provider, final
  record statuses, spending controls and pointers to the receipts.

Standard library only:  python3 scripts/build_full_run_log.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "artifacts/full_run_v2"
RATES = {"input": 0.10 / 1e6, "cached": 0.01 / 1e6, "output": 0.50 / 1e6}


def api_equivalent(input_tokens: int, cached: int, output: int) -> float:
    cached = min(cached, input_tokens)
    return (input_tokens - cached) * RATES["input"] + cached * RATES["cached"] + output * RATES["output"]


def main() -> int:
    connection = sqlite3.connect(f"file:{FULL / 'state.db'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    events = []
    runs: dict[str, dict] = {}
    for row in connection.execute("SELECT * FROM calls ORDER BY created_at, rowid"):
        run = runs.setdefault(row["run_id"], {
            "kind": "model_or_classifier_calls", "run_id": row["run_id"], "roles": set(),
            "providers": set(), "models": set(), "first_call_utc": row["created_at"],
            "last_call_utc": row["created_at"], "attempts": 0, "succeeded": 0, "failed": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "summed_request_seconds": 0.0, "phases": set(),
        })
        run["roles"].add(row["role"]); run["providers"].add(row["provider"]); run["models"].add(row["model"])
        run["phases"].add(row["phase"])
        run["last_call_utc"] = row["created_at"]
        run["attempts"] += 1
        run["succeeded"] += row["outcome"] == "succeeded"
        run["failed"] += row["outcome"] == "failed"
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            run[key] += int(row[key] or 0)
        run["summed_request_seconds"] += float(row["duration_seconds"] or 0)
    for run in runs.values():
        for key in ("roles", "providers", "models", "phases"):
            run[key] = sorted(run[key])
        run["summed_request_seconds"] = round(run["summed_request_seconds"], 3)
        run["at_utc"] = run["first_call_utc"]
        if any(provider.startswith("codex") for provider in run["providers"]):
            run["api_equivalent_usd"] = round(api_equivalent(
                run["input_tokens"], run["cached_input_tokens"], run["output_tokens"]), 8)
        events.append(run)
    for row in connection.execute("SELECT * FROM stage_runs ORDER BY stage_run_id"):
        details = json.loads(row["details_json"] or "{}")
        events.append({
            "kind": "stage_run", "at_utc": row["started_at"], "run_id": row["run_id"],
            "stage": row["stage"], "status": row["status"], "finished_utc": row["finished_at"],
            "wall_seconds": details.get("wall_seconds"),
            "note": "frozen-mode stage (validation/export only)" if str(row["run_id"]).startswith("frozen") else None,
        })
    for row in connection.execute(
        "SELECT bucket,status,COUNT(*) AS texts,MIN(reserved_at) AS first,MAX(settled_at) AS last "
        "FROM luna_ledger GROUP BY bucket,status ORDER BY first"
    ):
        events.append({
            "kind": "luna_text_ledger", "at_utc": row["first"], "bucket": row["bucket"],
            "status": row["status"], "texts": row["texts"], "last_settled_utc": row["last"],
        })
    events.sort(key=lambda value: (str(value.get("at_utc") or ""), value["kind"]))
    with (FULL / "run_log.jsonl").open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, sort_keys=True) + "\n")

    totals: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"attempts": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
    for row in connection.execute("SELECT provider,role,outcome,COUNT(*) n,SUM(COALESCE(input_tokens,0)) i,SUM(COALESCE(cached_input_tokens,0)) c,SUM(COALESCE(output_tokens,0)) o FROM calls GROUP BY provider,role,outcome"):
        value = totals[(row["provider"], row["role"], row["outcome"])]
        value.update({"attempts": row["n"], "input_tokens": row["i"], "cached_input_tokens": row["c"], "output_tokens": row["o"]})
    statuses = {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM records GROUP BY status")}
    reasons = {row[0]: row[1] for row in connection.execute("SELECT reason,COUNT(*) FROM records WHERE status='quarantined' GROUP BY reason")}
    summary = {
        "run": "full corpus, data/spotify_reviews_18months.csv (sha256 1fc85de6…)",
        "record_statuses": statuses,
        "quarantine_reasons": reasons,
        "source_rows": connection.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0],
        "calls_by_provider_role_outcome": [
            {"provider": key[0], "role": key[1], "outcome": key[2], **value} for key, value in sorted(totals.items())
        ],
        "invocations_logged": sum(event["kind"] == "model_or_classifier_calls" for event in events),
        "spending_controls": {
            "luna_text_budgets": "config/full.json luna_budgets (H 60, R 500, AL1 1500, AL2 1500, V 400); per-bucket text ledger in state luna_ledger",
            "mandatory_gate": "stopped after 1,000 new Luna texts until approval (artifacts/full_run_v2/APPROVAL_GATE_REPORT.md)",
            "grouping_evidence_cap": 600, "memo_request_cap": 2,
        },
        "receipts": {
            "status_and_metrics": "artifacts/full_run_v2/run_report.json",
            "stage_times_and_luna_usage": "cost/full_run_actuals.md",
            "per_request_luna_calls": "cost/reference_full_run_calls.jsonl",
            "interruption_and_resume": "evals/recovery/interrupt_resume_check.json",
            "grading_export": "grading/",
        },
        "note": "Summed request seconds are not wall time; stages ran as separate invocations, so no single end-to-end wall time exists for the full run.",
        "model_calls_made_by_this_script": 0,
    }
    (FULL / "run_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"events": len(events), "statuses": statuses}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
