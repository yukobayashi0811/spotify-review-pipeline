#!/usr/bin/env python3
"""Offline 100-review cost and runtime calculator (standard library only).

Default mode is offline replay: it reads saved measurements and editable rates
and never contacts a provider or needs an API key. Importing this module makes
no calls. The paid pilot is a separate, explicit command:

    spotify-pipeline run --input data/cost_100.csv --config config/pipeline.json \
        --output cost/pilot_100          # cold, then run again unchanged (warm)

Offline replay (from the repository root):

    python3 cost/calculator.py                      # writes cost/report.md, report.json, usage.csv
    python3 cost/calculator.py --rate-multiplier 2  # what-if: every API rate doubled
    python3 cost/calculator.py --budget-usd 0.5 --max-workers 1
    python3 cost/calculator.py --self-check         # arithmetic invariants -> cost/replay_check.json

Formulas
    item_cost   = billed_units x price_per_unit   (per mutually exclusive item)
    input       = input_tokens - cached_input_tokens   (uncached part only)
    request_usd = input x rate_in + cached x rate_cached + output x rate_out
    Reasoning tokens are already inside output_tokens and are not added again.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ITEMS = ("input_tokens", "cached_input_tokens", "output_tokens")


# --- loading ---------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_rates(path: Path, multiplier: float = 1.0) -> dict[tuple[str, str, str], dict[str, Any]]:
    rates: dict[tuple[str, str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["provider"], row["model"], row["billing_item"])
            if key in rates:
                raise ValueError(f"duplicate rate {key}")
            price = float(row["price_per_unit"])
            row["price_per_unit"] = price * multiplier if price else 0.0
            rates[key] = row
    return rates


def billed_items(call: dict[str, Any]) -> list[tuple[str, int]] | None:
    total, cached, output = call.get("input_tokens"), call.get("cached_input_tokens"), call.get("output_tokens")
    if type(total) is not int or type(output) is not int:
        return None
    cached = min(int(cached or 0), total)
    return [("input_tokens", total - cached), ("cached_input_tokens", cached), ("output_tokens", output)]


def price_call(call: dict[str, Any], rates: dict) -> tuple[float | None, list[dict[str, Any]]]:
    items = billed_items(call)
    if items is None:
        return None, []
    rows, total = [], 0.0
    for item, units in items:
        rate = rates.get((call["provider"], call["model"], item))
        if rate is None:
            raise ValueError(f"no rate for {(call['provider'], call['model'], item)}")
        cost = units * float(rate["price_per_unit"])
        total += cost
        rows.append({
            "run_phase": call.get("run_phase", ""), "request_id": call["request_id"],
            "role": call["role"], "stage": call.get("stage", call["role"]),
            "provider": call["provider"], "model": call["model"], "billing_item": item,
            "billed_units": units, "unit": rate["unit"],
            "price_per_unit": f"{float(rate['price_per_unit']):.12f}", "currency": rate["currency"],
            "cost": f"{cost:.12f}", "effective_date": rate["effective_date"],
            "source_url": rate["source_url"],
        })
    return total, rows


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct / 100
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


# --- measured pilot ----------------------------------------------------------------


def measured(calls: list[dict[str, Any]], run: dict[str, Any], rates: dict) -> tuple[dict, list]:
    usage_rows: list[dict[str, Any]] = []
    stages: dict[str, dict[str, Any]] = {}
    phases = {"cold": {"api_usd": 0.0, "requests": 0, "failed": 0, "missing_usage": 0},
              "warm": {"api_usd": 0.0, "requests": 0, "failed": 0, "missing_usage": 0}}
    for call in calls:
        phase = call.get("run_phase", "other")
        cost, rows = price_call(call, rates)
        usage_rows.extend(rows)
        bucket = phases.setdefault(phase, {"api_usd": 0.0, "requests": 0, "failed": 0, "missing_usage": 0})
        bucket["requests"] += 1
        bucket["failed"] += call["outcome"] == "failed"
        if cost is None:
            bucket["missing_usage"] += 1
        else:
            bucket["api_usd"] += cost
        if phase != "cold":
            continue
        stage = stages.setdefault(call["stage"], {
            "provider": call["provider"], "model": call["model"], "effort": call["effort"],
            "prompt_version": call["prompt_version"], "schema_version": call["schema_version"],
            "batch_sizes": [], "worker_limit": call.get("worker_limit", 1),
            "attempts": 0, "succeeded": 0, "failed": 0, "transient_retries": 0, "fallbacks": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "api_usd": 0.0, "missing_usage": 0, "summed_call_seconds": 0.0,
        })
        stage["batch_sizes"].append(call["batch_size"])
        stage["attempts"] += 1
        stage["succeeded"] += call["outcome"] == "succeeded"
        stage["failed"] += call["outcome"] == "failed"
        stage["transient_retries"] += bool(call.get("transient_retry"))
        stage["fallbacks"] += bool(call.get("fallback"))
        for item in ITEMS:
            stage[item] += int(call.get(item) or 0)
        stage["summed_call_seconds"] += float(call.get("duration_seconds") or 0)
        if cost is None:
            stage["missing_usage"] += 1
        else:
            stage["api_usd"] += cost
    for name, stage in stages.items():
        sizes = stage.pop("batch_sizes")
        stage["batch_size_max"] = max(sizes) if sizes else 0
        stage["requests_by_batch_size"] = {str(size): sizes.count(size) for size in sorted(set(sizes))}
        stage["wall_seconds"] = run["cold"]["stage_wall_seconds"].get(name)
    rows = run["pilot_rows"]
    completed = run["completed_records"]
    cold = phases["cold"]
    cold_wall = run["cold"]["wall_seconds"]
    result = {
        "input_file": run["input_file"], "input_sha256": run["input_sha256"],
        "checksum_matches_manifest": run["checksum_matches_manifest"],
        "pilot_rows": rows, "record_ids_match_input": run["record_ids_match_input"],
        "completed_records": completed,
        "failed_or_quarantined_records": run["failed_or_quarantined_records"],
        "distinct_nonempty_texts": run["distinct_nonempty_texts"],
        "exact_text_cache_hit_rows": run["exact_text_cache_hits_rows"],
        "result_cache_at_start": run["result_cache_at_start"],
        "routing": run["routing"], "verification": run["verification"],
        "stages": stages,
        "cold": {
            "wall_seconds": cold_wall, "api_equivalent_usd": cold["api_usd"],
            "requests": cold["requests"], "failed_attempts": cold["failed"],
            "requests_missing_usage": cold["missing_usage"],
            "usd_per_1000_input_rows": cold["api_usd"] / rows * 1000 if rows else None,
            "usd_per_completed_record": cold["api_usd"] / completed if completed else None,
            "rows_per_second": rows / cold_wall if cold_wall else None,
        },
        "warm": {
            "wall_seconds": run["warm"]["wall_seconds"],
            "api_equivalent_usd_incremental": phases["warm"]["api_usd"],
            "new_requests": phases["warm"]["requests"],
            "new_enrichment_requests": run["warm"]["new_model_calls"]["luna_by_role"]["enrich"]
            + run["warm"]["new_model_calls"]["local_classifier_batches"],
            "all_stages_skipped_unchanged": run["warm"]["all_stages_skipped_unchanged"],
        },
        "local_compute_cost": "unknown: hardware, electricity and memory were not metered (local classifier, embeddings, SQLite)",
        "billing_note": (
            "Luna requests ran through the Codex CLI with ChatGPT subscription authentication. "
            "USD figures are usage x the editable API-equivalent rates, not an invoice."
        ),
    }
    return result, usage_rows


# --- projection --------------------------------------------------------------------


def reference_stats(reference: list[dict[str, Any]], role: str, pct: float, rates: dict) -> dict[str, float]:
    rows = [row for row in reference if row["role"] == role and row["outcome"] == "succeeded"]
    if not rows:
        return {"requests": 0, "usd": 0.0, "seconds": 0.0, "reviews": 0}
    costs = [price_call(row, rates)[0] or 0.0 for row in rows]
    return {
        "requests": len(rows),
        "usd": percentile(costs, pct),
        "seconds": percentile([float(row.get("duration_seconds") or 0) for row in rows], pct),
        "reviews": statistics.median(len(row.get("review_ids") or []) for row in rows),
    }


def project(
    *, name: str, settings: dict[str, Any], measured_values: dict[str, Any], reference: list[dict[str, Any]],
    rates: dict, assumptions: dict[str, Any], controls: dict[str, Any], distinct: int, nonempty: int,
    verification_config: dict[str, Any],
) -> dict[str, Any]:
    pct = float(settings["token_percentile"])
    duration_pct = float(settings["duration_percentile"])
    routing = measured_values["routing"]
    # Demand measured on the pilot: texts eligible under the risk rule, before the cap.
    measured_fraction = routing["eligible_texts"] / routing["distinct_texts"] if routing["distinct_texts"] else 0.0
    cap = float(controls["max_fallback_fraction"])
    route_fraction = min(cap, measured_fraction) if settings["route_fraction"] == "measured" else cap
    failed_rate = (
        sum(row["outcome"] == "failed" for row in reference) / len(reference)
        if settings["failed_attempt_rate"] == "reference" and reference
        else float(settings["failed_attempt_rate"])
    )
    routed_texts = math.floor(distinct * route_fraction)
    verify_rows = min(
        int(verification_config["maximum"]),
        max(int(verification_config["minimum"]), round(nonempty * float(verification_config["sample_fraction"]))),
    )
    work = {
        "classify_luna": math.ceil(routed_texts / 50),
        "verify": math.ceil(verify_rows / 50),
        "group": int(settings["group_requests"]),
        "recommend": int(settings["memo_requests"]),
    }
    role_of = {"classify_luna": "enrich", "verify": "verify", "group": "group", "recommend": "memo"}
    stages = {}
    total_usd = 0.0
    luna_seconds = 0.0
    for stage, requests in work.items():
        stats = reference_stats(reference, role_of[stage], pct, rates)
        duration = reference_stats(reference, role_of[stage], duration_pct, rates)["seconds"]
        attempts = requests * (1 + failed_rate)
        usd = attempts * stats["usd"]
        seconds = attempts * duration
        stages[stage] = {
            "requests": requests, "attempts_incl_failures": round(attempts, 3),
            "usd_per_request": round(stats["usd"], 8), "usd": round(usd, 8),
            "seconds_per_request": round(duration, 3), "summed_request_seconds": round(seconds, 1),
            "reference_requests": stats["requests"],
        }
        total_usd += usd
        luna_seconds += seconds
    local_seconds = float(settings["fixed_local_seconds"]) + distinct / 1000 * float(
        settings["local_seconds_per_1000_distinct_texts"]
    )
    workers = max(1, int(controls["max_workers"]))
    wall = local_seconds + luna_seconds / workers
    over = total_usd > float(controls["budget_usd"])
    return {
        "scenario": name,
        "distinct_texts_processed": distinct,
        "route_fraction": round(route_fraction, 6),
        "route_fraction_source": settings["route_fraction"],
        "routed_texts": routed_texts,
        "verification_rows": verify_rows,
        "failed_attempt_rate": round(failed_rate, 6),
        "token_percentile": pct,
        "stages": stages,
        "api_equivalent_usd": round(total_usd, 6),
        "local_compute_seconds": round(local_seconds, 1),
        "luna_summed_request_seconds": round(luna_seconds, 1),
        "workers": workers,
        "estimated_wall_seconds": round(wall, 1),
        "throughput_rows_per_second": round(assumptions["full_run"]["source_rows"] / wall, 1) if wall else None,
        "usd_per_1000_input_rows": round(total_usd / assumptions["full_run"]["source_rows"] * 1000, 8),
        "budget_usd": float(controls["budget_usd"]),
        "exceeds_budget": over,
        "local_compute_cost": "unknown",
    }


def calculate(args: argparse.Namespace) -> dict[str, Any]:
    cost_dir = args.cost_dir
    assumptions = json.loads((cost_dir / "assumptions.json").read_text(encoding="utf-8"))
    controls = dict(assumptions["controls"])
    for key in ("budget_usd", "max_workers", "max_fallback_fraction", "output_token_cap_per_request"):
        value = getattr(args, key)
        if value is not None:
            controls[key] = value
    if args.distinct_texts is not None:
        assumptions["full_run"]["distinct_nonempty_texts"] = args.distinct_texts
    rates = load_rates(args.rates, args.rate_multiplier)
    calls = load_jsonl(cost_dir / "pilot_calls.jsonl")
    run = json.loads((cost_dir / "pilot_run.json").read_text(encoding="utf-8"))
    reference = load_jsonl(cost_dir / assumptions["reference_calls"]["path"])
    pipeline_config = json.loads((cost_dir.parent / "config" / "pipeline.json").read_text(encoding="utf-8"))
    measured_values, usage_rows = measured(calls, run, rates)
    full = assumptions["full_run"]
    projections = {}
    for name, settings in assumptions["scenarios"].items():
        for reuse, distinct in (("with_exact_text_reuse", full["distinct_nonempty_texts"]),
                                ("no_reuse", full["nonempty_rows"])):
            projections[f"{name}/{reuse}"] = project(
                name=name, settings=settings, measured_values=measured_values, reference=reference,
                rates=rates, assumptions=assumptions, controls=controls, distinct=distinct,
                nonempty=full["nonempty_rows"], verification_config=pipeline_config["verification"],
            )
    return {
        "rates_file": str(args.rates.relative_to(cost_dir.parent)) if args.rates.is_relative_to(cost_dir.parent) else str(args.rates),
        "rate_multiplier": args.rate_multiplier,
        "controls": controls,
        "measured_pilot": measured_values,
        "full_run_accounting": {
            "source_rows": full["source_rows"], "nonempty_rows": full["nonempty_rows"],
            "empty_text_quarantines": full["empty_text_quarantines"],
            "distinct_nonempty_texts": full["distinct_nonempty_texts"],
            "exact_text_reuse_rows": full["nonempty_rows"] - full["distinct_nonempty_texts"],
        },
        "projections": projections,
        "fixed_overhead_note": "Local fixed overhead (ingest, export, embeddings/clustering) is added once per run; memo and grouping requests are fixed counts, not per review.",
        "usage_rows": len(usage_rows),
    }, usage_rows


# --- output ------------------------------------------------------------------------


def fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render(report: dict[str, Any]) -> str:
    m = report["measured_pilot"]
    lines = [
        "# 100-review cost and runtime calculator",
        "",
        "Generated offline by `python3 cost/calculator.py` from `pilot_calls.jsonl`, `pilot_run.json`, "
        f"`{report['rates_file']}` (rate multiplier {report['rate_multiplier']}) and `assumptions.json`. "
        "No provider was contacted.",
        "",
        m["billing_note"],
        "",
        "## Measured pilot (cold run)",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Input | `{m['input_file']}`, sha256 `{m['input_sha256']}` (matches manifest: {m['checksum_matches_manifest']}) |",
        f"| Pilot IDs | {m['pilot_rows']} (all present in pilot_records.jsonl: {m['record_ids_match_input']}) |",
        f"| Completed / failed or quarantined | {m['completed_records']} / {m['failed_or_quarantined_records']} |",
        f"| Distinct nonempty texts / exact-text cache-hit rows | {m['distinct_nonempty_texts']} / {m['exact_text_cache_hit_rows']} |",
        f"| Result cache at start | {m['result_cache_at_start']} |",
        f"| Routed to Luna | {m['routing']['routed_texts']} of {m['routing']['distinct_texts']} distinct texts (limit {m['routing']['limit']}; {m['routing']['eligible_texts']} eligible) |",
        f"| Verification sample | {m['verification']['verified_rows']} of {m['verification']['sampled_rows']} sampled rows verified |",
        f"| End-to-end wall time | {fmt(m['cold']['wall_seconds'], 3)} s |",
        f"| Requests / failed attempts / missing usage | {m['cold']['requests']} / {m['cold']['failed_attempts']} / {m['cold']['requests_missing_usage']} |",
        f"| API-equivalent cost | ${fmt(m['cold']['api_equivalent_usd'], 8)} |",
        f"| Per 1,000 input rows / per completed record | ${fmt(m['cold']['usd_per_1000_input_rows'], 6)} / ${fmt(m['cold']['usd_per_completed_record'], 8)} |",
        f"| Throughput | {fmt(m['cold']['rows_per_second'], 3)} rows/s |",
        f"| Local compute cost | {m['local_compute_cost']} |",
        "",
        "### By stage",
        "",
        "| Stage | Provider / model / effort | Prompt / schema | Batch max (requests by size) | Workers | Attempts (ok/failed, retries, fallbacks) | Tokens in / cached / out | API-equiv. USD | Stage wall s | Summed call s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, stage in m["stages"].items():
        lines.append(
            f"| {name} | {stage['provider']} / `{stage['model']}` / {stage['effort']} | "
            f"{stage['prompt_version']} / {stage['schema_version']} | {stage['batch_size_max']} "
            f"({stage['requests_by_batch_size']}) | {stage['worker_limit']} | {stage['attempts']} "
            f"({stage['succeeded']}/{stage['failed']}, {stage['transient_retries']}, {stage['fallbacks']}) | "
            f"{stage['input_tokens']} / {stage['cached_input_tokens']} / {stage['output_tokens']} | "
            f"{fmt(stage['api_usd'], 8)} | {fmt(stage['wall_seconds'], 3)} | {fmt(stage['summed_call_seconds'], 3)} |"
        )
    warm = m["warm"]
    lines += [
        "",
        "## Measured pilot (warm run, same command)",
        "",
        f"- Wall time {fmt(warm['wall_seconds'], 6)} s; every stage skipped unchanged: {warm['all_stages_skipped_unchanged']}.",
        f"- New requests {warm['new_requests']} (new enrichment requests {warm['new_enrichment_requests']}); incremental API-equivalent cost ${fmt(warm['api_equivalent_usd_incremental'], 8)}.",
        "",
        "## Full-run projection (estimate)",
        "",
        f"All {report['full_run_accounting']['source_rows']:,} rows accounted for: {report['full_run_accounting']['nonempty_rows']:,} nonempty outputs and {report['full_run_accounting']['empty_text_quarantines']} empty-text quarantines. "
        f"Exact-text reuse reduces first-pass work to {report['full_run_accounting']['distinct_nonempty_texts']:,} distinct texts ({report['full_run_accounting']['exact_text_reuse_rows']:,} reused rows); the no-reuse rows show the comparison.",
        "",
        "Per-request tokens and durations come from the saved full-run GPT-6 Luna requests at the production batch size of 50 (`reference_full_run_calls.jsonl`), because the pilot's batches are smaller. "
        + report["fixed_overhead_note"],
        "",
        f"Controls: budget ${report['controls']['budget_usd']}, output-token cap {report['controls']['output_token_cap_per_request']} per request, max workers {report['controls']['max_workers']}, max fallback (routed) fraction {report['controls']['max_fallback_fraction']}.",
        "",
        "| Scenario | Distinct texts | Routed fraction | Luna requests (enrich/verify/group/memo) | Failed-attempt rate | API-equiv. USD | Local s | Luna summed s | Est. wall s | Budget |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for key, value in report["projections"].items():
        stages = value["stages"]
        lines.append(
            f"| {key} | {value['distinct_texts_processed']:,} | {value['route_fraction']} ({value['route_fraction_source']}) | "
            f"{stages['classify_luna']['requests']}/{stages['verify']['requests']}/{stages['group']['requests']}/{stages['recommend']['requests']} | "
            f"{value['failed_attempt_rate']} | {value['api_equivalent_usd']:.6f} | {value['local_compute_seconds']} | "
            f"{value['luna_summed_request_seconds']} | {value['estimated_wall_seconds']} | "
            f"{'**EXCEEDS BUDGET**' if value['exceeds_budget'] else 'within'} |"
        )
    lines += [
        "",
        "Wall-time estimate = local compute + summed Luna request seconds / workers. Summed request seconds are not wall time when requests overlap. Local compute cost (hardware, electricity) is unknown and not included in USD.",
        "",
    ]
    return "\n".join(lines)


def write_usage(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["run_phase", "request_id", "role", "stage", "provider", "model", "billing_item", "billed_units",
              "unit", "price_per_unit", "currency", "cost", "effective_date", "source_url"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def self_check(args: argparse.Namespace) -> dict[str, Any]:
    base, _ = calculate(args)
    doubled_args = argparse.Namespace(**{**vars(args), "rate_multiplier": args.rate_multiplier * 2})
    doubled, _ = calculate(doubled_args)
    bigger_args = argparse.Namespace(**{**vars(args), "distinct_texts": 600_000})
    bigger, _ = calculate(bigger_args)
    b, d = base["measured_pilot"], doubled["measured_pilot"]
    checks = {
        "doubling_rates_doubles_measured_api_usd": math.isclose(
            d["cold"]["api_equivalent_usd"], 2 * b["cold"]["api_equivalent_usd"], rel_tol=1e-12),
        "doubling_rates_doubles_projected_api_usd": all(
            math.isclose(doubled["projections"][key]["api_equivalent_usd"],
                         2 * base["projections"][key]["api_equivalent_usd"], rel_tol=1e-6, abs_tol=1e-6)
            for key in base["projections"]),
        "doubling_rates_leaves_measured_time_unchanged": d["cold"]["wall_seconds"] == b["cold"]["wall_seconds"],
        "doubling_rates_leaves_local_compute_unchanged": all(
            doubled["projections"][key]["local_compute_seconds"] == base["projections"][key]["local_compute_seconds"]
            for key in base["projections"]),
        "changing_projected_volume_leaves_measured_pilot_unchanged": bigger["measured_pilot"] == base["measured_pilot"],
        "pilot_ids_match_cost_100": b["record_ids_match_input"] and b["pilot_rows"] == 100,
        "input_checksum_matches_manifest": b["checksum_matches_manifest"],
    }
    receipt = {
        "checks": checks, "passed": all(checks.values()),
        "measured_api_usd": b["cold"]["api_equivalent_usd"],
        "measured_api_usd_with_2x_rates": d["cold"]["api_equivalent_usd"],
        "model_calls": 0,
    }
    (args.cost_dir / "replay_check.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline cost/runtime calculator; never calls a model.")
    parser.add_argument("--cost-dir", type=Path, default=HERE)
    parser.add_argument("--rates", type=Path, default=HERE / "rates.csv")
    parser.add_argument("--rate-multiplier", type=float, default=1.0)
    parser.add_argument("--budget-usd", dest="budget_usd", type=float)
    parser.add_argument("--max-workers", dest="max_workers", type=int)
    parser.add_argument("--max-fallback-fraction", dest="max_fallback_fraction", type=float)
    parser.add_argument("--output-token-cap", dest="output_token_cap_per_request", type=int)
    parser.add_argument("--distinct-texts", type=int, help="Override projected distinct texts")
    parser.add_argument("--output-dir", type=Path, help="Where to write report files (default: cost dir)")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    if args.self_check:
        receipt = self_check(args)
        print(json.dumps(receipt, indent=2))
        return 0 if receipt["passed"] else 1
    report, usage_rows = calculate(args)
    out = args.output_dir or args.cost_dir
    out.mkdir(parents=True, exist_ok=True)
    write_usage(out / "usage.csv", usage_rows)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render(report), encoding="utf-8")
    m = report["measured_pilot"]["cold"]
    print(json.dumps({
        "measured_cold_api_equivalent_usd": round(m["api_equivalent_usd"], 8),
        "measured_cold_wall_seconds": m["wall_seconds"],
        "projections_usd": {key: value["api_equivalent_usd"] for key, value in report["projections"].items()},
        "any_projection_exceeds_budget": any(value["exceeds_budget"] for value in report["projections"].values()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
