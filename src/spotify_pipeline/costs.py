from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .io_utils import write_csv, write_json, atomic_write_text


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    result = []
    with target.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"Malformed JSONL at {target}:{number}: {error}") from error
    return result


def load_rates(path: str | Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream, strict=True)
        rates = {}
        for row in rows:
            key = (row["provider"], row["model"], row["billing_item"])
            if key in rates:
                raise ValueError(f"Duplicate rate {key}")
            rates[key] = {**row, "price_per_unit": float(row["price_per_unit"])}
        return rates


def replay_cost(
    *,
    calls_path: str | Path,
    rates_path: str | Path,
    usage_path: str | Path,
    report_path: str | Path,
    run_metadata_path: str | Path | None = None,
    projected_rows: int = 660_622,
    projected_nonempty: int = 660_609,
    projected_distinct: int = 484_189,
    budget_usd: float = 25.0,
    conservative_multiplier: float = 1.25,
) -> dict[str, Any]:
    calls = load_jsonl(calls_path)
    rates = load_rates(rates_path)
    metadata = {}
    if run_metadata_path and Path(run_metadata_path).exists():
        metadata = json.loads(Path(run_metadata_path).read_text(encoding="utf-8"))
    projection_run_id = metadata.get("projection_run_id")
    usage_rows: list[dict[str, Any]] = []
    totals = Counter()
    stage_costs: defaultdict[str, float] = defaultdict(float)
    cold_stage_costs: defaultdict[str, float] = defaultdict(float)
    run_costs: defaultdict[str, float] = defaultdict(float)
    one_time_fixed_api_cost = 0.0
    fixed_scope_costs: defaultdict[str, float] = defaultdict(float)
    missing_usage = 0
    for call in calls:
        provider = call.get("provider", "openai")
        model = call.get("model", "")
        input_tokens = call.get("input_tokens")
        output_tokens = call.get("output_tokens")
        cached_tokens = call.get("cached_input_tokens", 0) or 0
        if type(input_tokens) is not int or type(output_tokens) is not int:
            missing_usage += 1
            continue
        cached_tokens = min(cached_tokens, input_tokens)
        billing = [
            ("input_tokens", input_tokens - cached_tokens),
            ("cached_input_tokens", cached_tokens),
            ("output_tokens", output_tokens),
        ]
        for item, units in billing:
            rate = rates.get((provider, model, item))
            if rate is None:
                raise ValueError(f"Missing rate for {(provider, model, item)}")
            cost = units * rate["price_per_unit"]
            usage_rows.append(
                {
                    "run_id": call.get("run_id", "unknown"),
                    "request_id": call.get("request_id", ""),
                    "role": call.get("role", ""),
                    "provider": provider,
                    "model": model,
                    "billing_item": item,
                    "billed_units": units,
                    "price_per_unit": f"{rate['price_per_unit']:.12f}",
                    "currency": rate["currency"],
                    "cost": f"{cost:.12f}",
                    "source_url": rate["source_url"],
                    "effective_date": rate["effective_date"],
                }
            )
            totals["api_cost"] += cost
            stage_costs[call.get("role", "unknown")] += cost
            run_costs[call.get("run_id", "unknown")] += cost
            scope = str(call.get("cost_scope", "pilot"))
            if scope != "pilot":
                one_time_fixed_api_cost += cost
                fixed_scope_costs[scope] += cost
            call_run_id = str(call.get("run_id", ""))
            if (
                call_run_id == projection_run_id
                if projection_run_id
                else call_run_id.startswith("pilot-cold")
            ):
                cold_stage_costs[call.get("role", "unknown")] += cost
        totals["input_tokens"] += input_tokens
        totals["cached_input_tokens"] += cached_tokens
        totals["output_tokens"] += output_tokens
        totals["succeeded_calls"] += call.get("outcome") == "succeeded"
    totals["failed_attempts"] = sum(call.get("outcome") == "failed" for call in calls)
    def is_projection_run(call: dict[str, Any]) -> bool:
        run_id = str(call.get("run_id", ""))
        return run_id == projection_run_id if projection_run_id else run_id.startswith("pilot-cold")

    cold_calls = [call for call in calls if is_projection_run(call)]
    pilot_attempts_by_stage = Counter(call.get("role", "unknown") for call in cold_calls)
    pilot_succeeded_by_stage = Counter(
        call.get("role", "unknown") for call in cold_calls if call.get("outcome") == "succeeded"
    )
    pilot_failed_by_stage = Counter(
        call.get("role", "unknown") for call in cold_calls if call.get("outcome") == "failed"
    )
    pilot_input_tokens = sum(int(call.get("input_tokens") or 0) for call in cold_calls)
    pilot_cached_tokens = sum(int(call.get("cached_input_tokens") or 0) for call in cold_calls)
    pilot_output_tokens = sum(int(call.get("output_tokens") or 0) for call in cold_calls)
    cold_enrich_ids = {
        review_id
        for call in cold_calls
        if call.get("role") == "enrich" and call.get("outcome") == "succeeded"
        for review_id in call.get("review_ids", [])
    }
    pilot_rows = metadata.get("pilot_rows", 100)
    completed_records = metadata.get("completed_records", 0)
    attempts_by_stage = Counter(call.get("role", "unknown") for call in calls)
    succeeded_by_stage = Counter(
        call.get("role", "unknown") for call in calls if call.get("outcome") == "succeeded"
    )
    failed_by_stage = Counter(
        call.get("role", "unknown") for call in calls if call.get("outcome") == "failed"
    )
    stage_settings: dict[str, list[dict[str, Any]]] = {}
    for role in sorted(attempts_by_stage):
        combinations = {
            (
                call.get("provider", "openai"),
                call.get("model"),
                call.get("effort"),
                call.get("prompt_version"),
                call.get("schema_version"),
            )
            for call in calls
            if call.get("role") == role
        }
        stage_settings[role] = [
            {
                "provider": provider,
                "model": model,
                "effort": effort,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
            }
            for provider, model, effort, prompt_version, schema_version in sorted(
                combinations, key=lambda value: tuple(str(item) for item in value)
            )
        ]
    variable_cost_per_unique = (
        sum(cold_stage_costs[role] for role in ("enrich", "verify"))
        / max(1, len(cold_enrich_ids))
    )
    fixed_cost = cold_stage_costs["group"] + cold_stage_costs["memo"]
    base_reuse_cost = (
        variable_cost_per_unique * projected_distinct + fixed_cost + one_time_fixed_api_cost
    )
    base_no_reuse_cost = (
        variable_cost_per_unique * projected_nonempty + fixed_cost + one_time_fixed_api_cost
    )
    conservative_cost = base_reuse_cost * conservative_multiplier
    cold_stage_seconds = metadata.get("cold_stage_wall_seconds", {}) or {}
    cold_wall_seconds = metadata.get("cold_wall_seconds")
    verification_selected = metadata.get("cold_verification_selected") or 0
    unique_pilot = metadata.get("unique_nonempty_texts") or len(cold_enrich_ids) or 1
    prepare_seconds = (
        (cold_stage_seconds.get("prepare", 0) / max(1, pilot_rows)) * projected_rows
    )
    classify_seconds_reuse = (
        (cold_stage_seconds.get("classify", 0) / max(1, unique_pilot)) * projected_distinct
    )
    classify_seconds_no_reuse = (
        (cold_stage_seconds.get("classify", 0) / max(1, unique_pilot)) * projected_nonempty
    )
    verification_rate = verification_selected / max(1, completed_records or pilot_rows)
    # A declared fixed verification sample (e.g. 400 distinct texts in the full run) replaces
    # proportional scaling when supplied; proportional scaling remains the legacy default.
    declared_verification_texts = metadata.get("projection_verification_texts")
    projected_verification_texts = (
        int(declared_verification_texts)
        if declared_verification_texts is not None
        else round(projected_nonempty * verification_rate)
    )
    verify_seconds = (
        (cold_stage_seconds.get("verify", 0) / max(1, verification_selected))
        * projected_verification_texts
        if verification_selected
        else 0
    )
    if declared_verification_texts is not None:
        enrich_cost_per_unique = cold_stage_costs["enrich"] / max(1, len(cold_enrich_ids))
        verify_cost_per_text = (
            cold_stage_costs["verify"] / verification_selected if verification_selected else 0.0
        )
        verification_cost = verify_cost_per_text * projected_verification_texts
        base_reuse_cost = (
            enrich_cost_per_unique * projected_distinct
            + verification_cost + fixed_cost + one_time_fixed_api_cost
        )
        base_no_reuse_cost = (
            enrich_cost_per_unique * projected_nonempty
            + verification_cost + fixed_cost + one_time_fixed_api_cost
        )
        conservative_cost = base_reuse_cost * conservative_multiplier
    fixed_seconds = sum(
        cold_stage_seconds.get(role, 0) for role in ("group", "rank", "recommend")
    )
    base_reuse_seconds = prepare_seconds + classify_seconds_reuse + verify_seconds + fixed_seconds
    base_no_reuse_seconds = prepare_seconds + classify_seconds_no_reuse + verify_seconds + fixed_seconds
    cold_api_cost = (
        run_costs.get(projection_run_id, 0.0)
        if projection_run_id
        else sum(value for key, value in run_costs.items() if key.startswith("pilot-cold"))
    )
    warm_api_cost = sum(
        value for key, value in run_costs.items() if key.startswith("pilot-warm")
    )
    report = {
        "status": "measured" if calls else "not_measured",
        "pilot": {
            "provider": metadata.get("provider"),
            "rows": pilot_rows,
            "input_sha256": metadata.get("input_sha256"),
            "ids": metadata.get("pilot_ids", []),
            "completed_records": completed_records,
            "quarantined_or_failed_records": metadata.get("quarantined_or_failed_records"),
            "unique_nonempty_texts": metadata.get("unique_nonempty_texts"),
            "direct_exact_text_cache_hits": metadata.get("direct_exact_text_cache_hits"),
            "warm_saved_result_reuses": metadata.get("warm_saved_result_reuses"),
            "succeeded_calls": sum(pilot_succeeded_by_stage.values()),
            "failed_attempts": sum(pilot_failed_by_stage.values()),
            "attempts_by_stage": dict(sorted(pilot_attempts_by_stage.items())),
            "succeeded_by_stage": dict(sorted(pilot_succeeded_by_stage.items())),
            "failed_by_stage": dict(sorted(pilot_failed_by_stage.items())),
            "stage_settings": stage_settings,
            "batch_size": metadata.get("batch_size"),
            "worker_limit": metadata.get("worker_limit"),
            "input_tokens": pilot_input_tokens,
            "cached_input_tokens": pilot_cached_tokens,
            "output_tokens": pilot_output_tokens,
            "api_cost_usd": round(cold_api_cost, 8),
            "all_measured_api_equivalent_usd": round(totals["api_cost"], 8),
            "one_time_fixed_api_equivalent_usd": round(one_time_fixed_api_cost, 8),
            "cold_api_cost_usd": round(cold_api_cost, 8),
            "warm_incremental_api_cost_usd": round(warm_api_cost, 8),
            "cold_cost_per_1000_input_rows_usd": round(
                cold_api_cost / max(1, pilot_rows) * 1000, 8
            ) if calls else None,
            "cold_cost_per_completed_record_usd": round(
                cold_api_cost / max(1, completed_records), 10
            ) if calls else None,
            "missing_usage_calls": missing_usage,
            "cold_wall_seconds": cold_wall_seconds,
            "warm_wall_seconds": metadata.get("warm_wall_seconds"),
            "warm_new_enrichment_calls": metadata.get("warm_new_enrichment_calls"),
            "cold_throughput_rows_per_second": round(
                pilot_rows / cold_wall_seconds, 6
            ) if cold_wall_seconds else None,
            "stage_wall_seconds": cold_stage_seconds,
            "local_compute_cost": {
                "status": "unknown",
                "api_charges_usd": (metadata.get("provider_runtime") or {}).get("api_charges_usd"),
                "hardware_cost_usd": (metadata.get("provider_runtime") or {}).get("hardware_cost_usd", "unknown"),
                "electricity_cost_usd": (metadata.get("provider_runtime") or {}).get("electricity_cost_usd", "unknown"),
                "measured_wall_time_seconds": cold_wall_seconds,
                "runtime": metadata.get("provider_runtime"),
            },
        },
        "by_stage_usd": {key: round(value, 8) for key, value in sorted(stage_costs.items())},
        "by_run_usd": {key: round(value, 8) for key, value in sorted(run_costs.items())},
        "one_time_fixed": {
            "api_equivalent_usd": round(one_time_fixed_api_cost, 8),
            "by_scope_usd": {
                key: round(value, 8) for key, value in sorted(fixed_scope_costs.items())
            },
            "measured_token_evidence": metadata.get("one_time_fixed_evidence", {}),
            "pricing_basis": (
                "Estimated at editable GPT-6 Luna standard API token rates; actual calls used "
                "ChatGPT subscription authentication and did not create measured API invoices."
            ),
        },
        "projection": {
            "rows": projected_rows,
            "nonempty": projected_nonempty,
            "empty_quarantines": projected_rows - projected_nonempty,
            "distinct_nonempty_with_exact_text_reuse": projected_distinct,
            "base_reuse_api_cost_usd": round(base_reuse_cost, 6) if calls else None,
            "base_no_reuse_api_cost_usd": round(base_no_reuse_cost, 6) if calls else None,
            "conservative_reuse_api_cost_usd": round(conservative_cost, 6) if calls else None,
            "base_reuse_elapsed_seconds": round(base_reuse_seconds, 3) if calls else None,
            "base_no_reuse_elapsed_seconds": round(base_no_reuse_seconds, 3) if calls else None,
            "conservative_reuse_elapsed_seconds": round(
                base_reuse_seconds * conservative_multiplier, 3
            ) if calls else None,
            "base_reuse_elapsed_days": round(base_reuse_seconds / 86_400, 3) if calls else None,
            "base_no_reuse_elapsed_days": round(base_no_reuse_seconds / 86_400, 3) if calls else None,
            "conservative_reuse_elapsed_days": round(
                base_reuse_seconds * conservative_multiplier / 86_400, 3
            ) if calls else None,
            "base_reuse_throughput_rows_per_second": round(
                projected_rows / base_reuse_seconds, 6
            ) if calls and base_reuse_seconds else None,
            "verification_rate": round(verification_rate, 6) if calls else None,
            "verification_rule": (
                "declared fixed sample" if declared_verification_texts is not None
                else "pilot sampling rate x projected nonempty rows"
            ),
            "projected_verification_texts": projected_verification_texts if calls else None,
            "conservative_multiplier": conservative_multiplier,
            "budget_usd": budget_usd,
            "base_exceeds_budget": base_reuse_cost > budget_usd if calls else None,
            "conservative_exceeds_budget": conservative_cost > budget_usd if calls else None,
            "total_cost_including_local_compute": "unknown"
            if "local" in str(metadata.get("provider", "")).casefold()
            else round(conservative_cost, 6) if calls else None,
            "note": (
                f"The projection was refreshed from the measured {pilot_rows}-review checkpoint. "
                "Refresh again after 10,000 reviews."
                if pilot_rows >= 500
                else "The projection is an initial extrapolation. Refresh after 500 and 10,000 reviews."
            ),
        },
        "controls": metadata.get(
            "controls",
            {
                "spend_limit_usd": budget_usd,
                "max_output_tokens": 6000,
                "max_workers": 1,
                "fallback_fraction": 0.02,
            },
        ),
    }
    write_csv(
        usage_path,
        [
            "run_id", "request_id", "role", "provider", "model", "billing_item",
            "billed_units", "price_per_unit", "currency", "cost", "source_url",
            "effective_date",
        ],
        usage_rows,
    )
    write_json(Path(report_path).with_suffix(".json"), report)
    atomic_write_text(report_path, _render_cost_report(report))
    return report


def _render_cost_report(report: dict[str, Any]) -> str:
    pilot = report["pilot"]
    projection = report["projection"]
    if report["status"] != "measured":
        return """# 100-review cost and runtime report

**Status: not measured.** No real pilot call log is present. The calculator is runnable,
but this file must not be presented as measured evidence until both cold and warm runs
have completed with real model usage and wall-clock timing.

Run the explicit local or paid pilot command in `cost/README.md`, then run the default offline
replay command. Local-compute cost remains `unknown` unless separately measured.
"""
    evidence_name = "checkpoint" if pilot["rows"] >= 500 else "pilot"
    warm_wall = (
        f"{pilot['warm_wall_seconds']} seconds"
        if pilot["warm_wall_seconds"] is not None
        else f"not run for this {evidence_name}"
    )
    warm_calls = (
        str(pilot["warm_new_enrichment_calls"])
        if pilot["warm_new_enrichment_calls"] is not None
        else f"not run for this {evidence_name}"
    )
    warm_reuses = (
        str(pilot["warm_saved_result_reuses"])
        if pilot["warm_saved_result_reuses"] is not None
        else f"not run for this {evidence_name}"
    )
    lines = [
        f"# {pilot['rows']}-review cost and runtime report",
        "",
        f"## Measured {evidence_name}",
        "",
        f"- Provider: {pilot['provider']}",
        f"- Rows: {pilot['rows']}",
        f"- Input SHA-256: {pilot['input_sha256']}",
        f"- Completed / failed or quarantined: {pilot['completed_records']} / {pilot['quarantined_or_failed_records']}",
        f"- Unique nonempty texts: {pilot['unique_nonempty_texts']}",
        f"- Direct exact-text cache hits: {pilot['direct_exact_text_cache_hits']}",
        f"- Batch size / worker limit: {pilot['batch_size']} / {pilot['worker_limit']}",
        f"- Successful calls: {pilot['succeeded_calls']}",
        f"- Failed attempts: {pilot['failed_attempts']}",
        f"- Input tokens: {pilot['input_tokens']}",
        f"- Cached input tokens: {pilot['cached_input_tokens']}",
        f"- Output tokens: {pilot['output_tokens']}",
        f"- Pilot API spend: ${pilot['api_cost_usd']:.8f}",
        f"- One-time Luna API-equivalent estimate: ${pilot['one_time_fixed_api_equivalent_usd']:.8f}",
        f"- Cold API spend: ${pilot['cold_api_cost_usd']:.8f}",
        f"- Warm incremental API spend: ${pilot['warm_incremental_api_cost_usd']:.8f}",
        f"- Cold wall time: {pilot['cold_wall_seconds']} seconds",
        f"- Warm wall time: {warm_wall}",
        f"- Warm new enrichment calls: {warm_calls}",
        f"- Warm saved-result reuses: {warm_reuses}",
        f"- Cold throughput: {pilot['cold_throughput_rows_per_second']} rows/second",
        f"- Cold cost per 1,000 input rows: ${pilot['cold_cost_per_1000_input_rows_usd']:.8f}",
        f"- Cold cost per completed record: ${pilot['cold_cost_per_completed_record_usd']:.10f}",
        "- Hardware and electricity cost: unknown (API charge is tracked separately)",
        "",
        "## One-time measured Luna work, API-rate equivalent",
        "",
        f"- Estimated equivalent: ${report['one_time_fixed']['api_equivalent_usd']:.8f}",
        "- Actual billing mode: ChatGPT subscription; this is not an API invoice.",
        "- Pricing basis: editable GPT-6 Luna standard token rates in `rates.csv`.",
        f"- Evidence: {json.dumps(report['one_time_fixed']['measured_token_evidence'], ensure_ascii=False, sort_keys=True)}",
        "",
        "## Full-run projection",
        "",
        f"- Rows / nonempty / empty: {projection['rows']} / {projection['nonempty']} / {projection['empty_quarantines']}",
        f"- Distinct nonempty texts with exact reuse: {projection['distinct_nonempty_with_exact_text_reuse']}",
        f"- Base reuse API cost: ${projection['base_reuse_api_cost_usd']:.6f}",
        f"- Base no-reuse API cost: ${projection['base_no_reuse_api_cost_usd']:.6f}",
        f"- Conservative reuse API cost: ${projection['conservative_reuse_api_cost_usd']:.6f}",
        f"- Base reuse elapsed time: {projection['base_reuse_elapsed_seconds']} seconds",
        f"- Base no-reuse elapsed time: {projection['base_no_reuse_elapsed_seconds']} seconds",
        f"- Conservative reuse elapsed time: {projection['conservative_reuse_elapsed_seconds']} seconds",
        f"- Base reuse elapsed time: {projection['base_reuse_elapsed_days']} days",
        f"- Base no-reuse elapsed time: {projection['base_no_reuse_elapsed_days']} days",
        f"- Conservative reuse elapsed time: {projection['conservative_reuse_elapsed_days']} days",
        f"- Base reuse throughput: {projection['base_reuse_throughput_rows_per_second']} rows/second",
        f"- Verification rule: {projection.get('verification_rule')}",
        f"- Projected verification texts: {projection.get('projected_verification_texts')}",
        f"- Pilot verification rate: {projection['verification_rate']}",
        f"- Budget: ${projection['budget_usd']:.2f}",
        f"- Base exceeds budget: {projection['base_exceeds_budget']}",
        f"- Conservative exceeds budget: {projection['conservative_exceeds_budget']}",
        f"- Total cost including local compute: {projection['total_cost_including_local_compute']}",
        "",
        projection["note"],
    ]
    return "\n".join(lines) + "\n"
