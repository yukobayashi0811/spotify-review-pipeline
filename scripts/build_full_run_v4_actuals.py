#!/usr/bin/env python3
"""Measured cost and runtime of the final full-corpus run, compared with the pre-run projection.

Reads only saved files (no model calls):

* ``grading/calls.jsonl`` - every request of the final run, both invocations
  (the deliberately interrupted one and the resume);
* ``evals/recovery_v4/run_summary_interrupted.json`` and
  ``evals/recovery_v4/run_summary_completed.json`` - wall time of each invocation;
* ``cost/rates.csv`` - the same API-equivalent rates the calculator uses;
* ``cost/report.json`` - the projection made from the 100-review pilot.

Writes ``cost/full_run_v4_actuals.json`` and ``cost/full_run_v4_actuals.md``.
USD figures are token usage x API-equivalent rates (the requests ran under a
ChatGPT subscription through the Codex CLI); they are not an invoice.

Usage: python3 scripts/build_full_run_v4_actuals.py
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "grading/calls.jsonl"
INTERRUPTED = ROOT / "evals/recovery_v4/run_summary_interrupted.json"
COMPLETED = ROOT / "evals/recovery_v4/run_summary_completed.json"
RATES = ROOT / "cost/rates.csv"
PROJECTION = ROOT / "cost/report.json"
OUT_JSON = ROOT / "cost/full_run_v4_actuals.json"
OUT_MD = ROOT / "cost/full_run_v4_actuals.md"


def rates() -> dict[tuple[str, str], dict[str, Decimal]]:
    table: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(dict)
    with RATES.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            table[(row["provider"], row["model"])][row["billing_item"]] = Decimal(row["price_per_unit"])
    return table


def main() -> int:
    rate_table = rates()
    roles: dict[str, dict] = {}
    local_batches = 0
    for line in CALLS.read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        if call["model"] != "gpt-6-luna":
            local_batches += 1
            continue
        bucket = roles.setdefault(call["role"], {
            "attempts": 0, "succeeded": 0, "failed": 0, "by_phase": defaultdict(int),
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "missing_usage": 0, "summed_request_seconds": 0.0, "usd": Decimal(0),
        })
        bucket["attempts"] += 1
        bucket["succeeded" if call["outcome"] == "succeeded" else "failed"] += 1
        bucket["by_phase"][call["phase"]] += 1
        bucket["summed_request_seconds"] += float(call.get("duration_seconds") or 0)
        total, cached, output = call.get("input_tokens"), call.get("cached_input_tokens"), call.get("output_tokens")
        if total is None or output is None:
            bucket["missing_usage"] += 1
            continue
        cached = min(int(cached or 0), int(total))
        rate = rate_table[(call["provider"], call["model"])]
        bucket["input_tokens"] += int(total)
        bucket["cached_input_tokens"] += cached
        bucket["output_tokens"] += int(output)
        bucket["usd"] += (
            (int(total) - cached) * rate["input_tokens"]
            + cached * rate["cached_input_tokens"]
            + int(output) * rate["output_tokens"]
        )
    total_usd = sum((bucket["usd"] for bucket in roles.values()), Decimal(0))
    interrupted = json.loads(INTERRUPTED.read_text(encoding="utf-8"))
    completed = json.loads(COMPLETED.read_text(encoding="utf-8"))
    wall = {
        "interrupted_invocation_seconds": round(interrupted["wall_seconds"], 1),
        "resume_invocation_seconds": round(completed["wall_seconds"], 1),
    }
    wall["total_seconds"] = round(wall["interrupted_invocation_seconds"] + wall["resume_invocation_seconds"], 1)
    guard_usd = Decimal(str(interrupted["guard"]["spent_usd_api_equivalent"])) + Decimal(
        str(completed["guard"]["spent_usd_api_equivalent"])
    )
    projections = json.loads(PROJECTION.read_text(encoding="utf-8"))["projections"]
    report = {
        "run_ids": [interrupted["run_id"], completed["run_id"]],
        "source": "grading/calls.jsonl (both invocations) and evals/recovery_v4 run summaries",
        "rows": completed["counts"],
        "luna_requests_by_role": {
            role: {
                **{key: value for key, value in bucket.items() if key not in ("usd", "by_phase")},
                "by_phase": dict(bucket["by_phase"]),
                "summed_request_seconds": round(bucket["summed_request_seconds"], 1),
                "api_equivalent_usd": float(round(bucket["usd"], 8)),
            }
            for role, bucket in sorted(roles.items())
        },
        "luna_requests_total": sum(bucket["attempts"] for bucket in roles.values()),
        "luna_failed_attempts_total": sum(bucket["failed"] for bucket in roles.values()),
        "local_classifier_batches": local_batches,
        "api_equivalent_usd": float(round(total_usd, 8)),
        "api_equivalent_usd_from_run_guard_ledger": float(round(guard_usd, 8)),
        "wall_seconds": wall,
        "projection_comparison": {
            name: {
                "projected_usd": value["api_equivalent_usd"],
                "projected_wall_seconds": value["estimated_wall_seconds"],
            }
            for name, value in projections.items()
        },
        "budget_usd": 5.0,
        "within_budget": total_usd <= Decimal("5.0"),
        "note": "API-equivalent USD = usage x cost/rates.csv; requests ran under a ChatGPT subscription through the Codex CLI, so this is not an invoice. Local compute cost was not metered.",
        "model_calls": 0,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Final full run: measured cost and runtime",
        "",
        "Generated by `scripts/build_full_run_v4_actuals.py` (no model calls) from `grading/calls.jsonl` "
        "and the two run summaries in `evals/recovery_v4/`. USD = token usage x `cost/rates.csv` "
        "(API-equivalent; the requests ran under a ChatGPT subscription through the Codex CLI, not an invoice).",
        "",
        "| Role | Requests (ok / failed) | Tokens in / cached / out | API-equiv. USD | Summed request s |",
        "|---|---:|---:|---:|---:|",
    ]
    for role, bucket in report["luna_requests_by_role"].items():
        lines.append(
            f"| {role} | {bucket['attempts']} ({bucket['succeeded']} / {bucket['failed']}) | "
            f"{bucket['input_tokens']:,} / {bucket['cached_input_tokens']:,} / {bucket['output_tokens']:,} | "
            f"{bucket['api_equivalent_usd']:.6f} | {bucket['summed_request_seconds']:,.1f} |"
        )
    lines += [
        f"| **total** | {report['luna_requests_total']} ({report['luna_requests_total'] - report['luna_failed_attempts_total']} / "
        f"{report['luna_failed_attempts_total']}) | | **{report['api_equivalent_usd']:.6f}** | |",
        "",
        f"- Local classifier batches (no API cost): {local_batches:,}.",
        f"- Run guard ledger total (both invocations): ${report['api_equivalent_usd_from_run_guard_ledger']:.6f}.",
        f"- Wall time: interrupted invocation {wall['interrupted_invocation_seconds']:,} s + resume "
        f"{wall['resume_invocation_seconds']:,} s = {wall['total_seconds']:,} s.",
        "",
        "## Against the pilot-based projection (`cost/report.md`)",
        "",
        "| Scenario | Projected USD | Projected wall s |",
        "|---|---:|---:|",
    ]
    for name, value in report["projection_comparison"].items():
        lines.append(f"| {name} | {value['projected_usd']:.6f} | {value['projected_wall_seconds']:,} |")
    lines += [
        f"| **measured (this run)** | **{report['api_equivalent_usd']:.6f}** | **{wall['total_seconds']:,}** |",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("luna_requests_total", "luna_failed_attempts_total", "api_equivalent_usd", "api_equivalent_usd_from_run_guard_ledger", "wall_seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
