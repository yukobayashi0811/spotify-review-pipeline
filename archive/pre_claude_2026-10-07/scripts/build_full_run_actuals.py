#!/usr/bin/env python3
"""Build cost/full_run_actuals.{json,md} from saved full-run evidence only.

Zero model calls. Opens state.db read-only/immutable. Every value is copied or
summed from a named receipt; values with no receipt are reported as "unknown".
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FR = ROOT / "artifacts" / "full_run_v2"
COST = ROOT / "cost"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def luna_rates() -> dict[str, float]:
    with (COST / "rates.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = [r for r in csv.DictReader(stream)
                if r["provider"] == "codex-exec-subscription" and r["model"] == "gpt-6-luna"]
    return {r["billing_item"]: float(r["price_per_unit"]) for r in rows}


def equivalent(rates: dict[str, float], inp: int, cached: int, out: int) -> float:
    # Same convention as cost replay: cached tokens are billed at the cached rate,
    # uncached input at the input rate.
    return round((inp - cached) * rates["input_tokens"] + cached * rates["cached_input_tokens"]
                 + out * rates["output_tokens"], 8)


def span_seconds(start: str, end: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S"
    return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()


BUCKETS = {
    "H": ("Teacher labels: hard/uncertain texts", "adopted (training data)"),
    "R": ("Teacher labels: random texts", "adopted (training data)"),
    "AL1": ("Teacher labels: active-learning round 1", "adopted (training data)"),
    "AL2": ("Teacher labels: active-learning round 2", "adopted (training data)"),
    "V": ("Independent verification sample (300 local / 100 Luna test-retest)", "adopted"),
    "G": ("Issue naming for TF-IDF grouping v1", "superseded (grouping v1 not adopted)"),
    "M": ("Memo for grouping v1", "superseded (memo v1 not adopted)"),
    "G_EMBED_V3": ("Issue naming/review for adopted embedding grouping", "adopted"),
    "M_EMBED_V3": ("Recommendation memo for adopted grouping", "adopted"),
}


def main() -> None:
    rates = luna_rates()
    db = sqlite3.connect(f"file:{FR / 'state.db'}?mode=ro&immutable=1", uri=True)
    luna_rows = []
    for bucket, status, texts, calls, dur, start, end, inp, cached, out in db.execute(
        "SELECT bucket,status,COUNT(*),COUNT(DISTINCT request_id),SUM(duration_seconds),"
        "MIN(reserved_at),MAX(settled_at),SUM(input_tokens),SUM(COALESCE(cached_input_tokens,0)),"
        "SUM(output_tokens) FROM luna_ledger GROUP BY bucket,status ORDER BY MIN(reserved_at),bucket"
    ):
        purpose, adoption = BUCKETS.get(bucket, ("unknown", "unknown"))
        luna_rows.append({
            "bucket": bucket, "status": status, "purpose": purpose, "adoption": adoption,
            "texts": texts, "calls": calls,
            "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
            "luna_api_equivalent_usd": equivalent(rates, inp, cached, out),
            "ledger_first_reserved_utc": start, "ledger_last_settled_utc": end,
            "ledger_span_seconds": span_seconds(start, end),
            "summed_call_seconds": round(dur, 3),
        })
    legacy = db.execute(
        "SELECT outcome,COUNT(*),SUM(input_tokens),SUM(COALESCE(cached_input_tokens,0)),"
        "SUM(output_tokens),SUM(duration_seconds) FROM calls "
        "WHERE run_id LIKE 'codex-dev-%' GROUP BY outcome ORDER BY outcome"
    ).fetchall()
    legacy_teacher = db.execute(
        "SELECT COUNT(*) FROM teacher_labels WHERE source_kind='legacy_luna_10000'"
    ).fetchone()[0]
    local_runs = [
        {"run_id": run_id, "batches": n, "first_call_utc": a, "last_call_utc": b,
         "summed_batch_seconds": round(d, 3)}
        for run_id, n, a, b, d in db.execute(
            "SELECT run_id,COUNT(*),MIN(created_at),MAX(created_at),SUM(duration_seconds) "
            "FROM calls WHERE provider='local-distilled-sklearn' GROUP BY run_id ORDER BY MIN(created_at)"
        )
    ]
    unledgered_failed = [
        {"run_id": run_id, "role": role, "calls": n, "input_tokens": inp, "cached_input_tokens": cached,
         "output_tokens": out, "summed_call_seconds": round(dur, 3)}
        for run_id, role, n, inp, cached, out, dur in db.execute(
            "SELECT run_id,role,COUNT(*),SUM(input_tokens),SUM(COALESCE(cached_input_tokens,0)),"
            "SUM(output_tokens),SUM(duration_seconds) FROM calls WHERE outcome='failed' "
            "AND provider='codex-exec-subscription' AND run_id LIKE 'embedding-group-v3-%' GROUP BY run_id,role"
        )
    ]
    router = db.execute("SELECT COUNT(*),MIN(scored_at),MAX(scored_at) FROM router_scores").fetchone()
    final_sources = dict(db.execute(
        "SELECT CASE WHEN label_config LIKE 'gpt-6-luna%' THEN 'luna' "
        "WHEN label_config LIKE 'distilled-%' THEN 'local' ELSE 'quarantined' END, COUNT(*) "
        "FROM records GROUP BY 1"
    ).fetchall())
    stage_runs = [
        {"stage": s, "started_utc": a, "finished_utc": b}
        for s, a, b in db.execute("SELECT stage,started_at,finished_at FROM stage_runs ORDER BY started_at")
    ]
    db.close()

    legacy_rows = []
    for outcome, n, inp, cached, out, dur in legacy:
        legacy_rows.append({
            "outcome": outcome, "calls": n, "input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "luna_api_equivalent_usd": equivalent(rates, inp, cached, out),
            "summed_call_seconds": round(dur, 3),
        })
    bench10k = load(ROOT / "experiments/codex_3agent/checkpoint-10000-parallel/benchmark_report.json")
    gate_report = (FR / "APPROVAL_GATE_REPORT.md").read_text(encoding="utf-8")
    al1 = load(ROOT / "experiments/distilled/al1-v2/summary.json")
    al2 = load(ROOT / "experiments/distilled/al2-final-v2/summary.json")
    al2_sel = load(FR / "al2_selection.json")
    verify = load(FR / "verification/verification_report.json")
    group_v3 = load(FR / "grouping_v3_run.json")
    frozen = load(FR / "frozen_run_summary.json")
    initial = load(ROOT / "evals/recovery/initial_finalization_report.json")
    resume = load(ROOT / "evals/recovery/resume_finalization_report.json")
    usage = {name: load(FR / f"usage_{name}.json") for name in ("before_1000", "after_1000", "after_all")}

    def gate_value(label: str) -> float | None:
        for line in gate_report.splitlines():
            if line.startswith(f"- {label}:"):
                return float(line.split(":", 1)[1].split()[0].replace(",", ""))
        return None

    preflight = None
    for line in gate_report.splitlines():
        if "first-run wall time was" in line:
            preflight = float(line.split("first-run wall time was", 1)[1].split()[0])

    adopted_luna = [r for r in luna_rows if not r["adoption"].startswith("superseded")]
    superseded_luna = [r for r in luna_rows if r["adoption"].startswith("superseded")]
    tot = lambda rows, k: round(sum(r[k] for r in rows), 8)

    local_stages = [
        {"stage": "Local router preflight: score all distinct texts (first run)",
         "rows_or_texts": 484189, "wall_seconds": preflight,
         "source": rel(FR / "APPROVAL_GATE_REPORT.md")},
        {"stage": "Local router scoring persisted to state (scored_at span)",
         "rows_or_texts": router[0], "wall_seconds": span_seconds(router[1], router[2]),
         "source": "state.db router_scores.scored_at (min/max)"},
        {"stage": "AL1 local model training + evaluation", "rows_or_texts": None,
         "wall_seconds": al1["training_and_evaluation_seconds"],
         "source": "experiments/distilled/al1-v2/summary.json"},
        {"stage": "AL2 selection over router scores", "rows_or_texts": al2_sel["router_scored_texts"],
         "wall_seconds": al2_sel["seconds"], "source": rel(FR / "al2_selection.json")},
        {"stage": "Final local model training + evaluation (artifact a5279d07…)", "rows_or_texts": None,
         "wall_seconds": al2["training_and_evaluation_seconds"],
         "source": "experiments/distilled/al2-final-v2/summary.json"},
        {"stage": "Verification script (400 texts incl. Luna calls)", "rows_or_texts": 400,
         "wall_seconds": verify["wall_seconds"], "source": rel(FR / "verification/verification_report.json")},
        {"stage": "Local finalization, first pass (calls span)", "rows_or_texts": None,
         "wall_seconds": span_seconds(local_runs[0]["first_call_utc"], local_runs[0]["last_call_utc"]),
         "source": f"state.db calls run_id {local_runs[0]['run_id']} (created_at span; row count not separately receipted)"},
        {"stage": "Local finalization, interrupted demo phase (SIGINT)", "rows_or_texts": initial["local_rows_finalized"],
         "wall_seconds": initial["wall_seconds"], "source": "evals/recovery/initial_finalization_report.json"},
        {"stage": "Local finalization, resume to completion", "rows_or_texts": resume["local_rows_finalized"],
         "wall_seconds": resume["wall_seconds"], "source": "evals/recovery/resume_finalization_report.json"},
        {"stage": "Frozen run: prepare (ingest 660,622 rows)", "rows_or_texts": 660622,
         "wall_seconds": frozen["stages"]["prepare"]["wall_seconds"], "source": rel(FR / "frozen_run_summary.json")},
        {"stage": "Adopted embedding grouping + Luna naming (v3 rebuild)", "rows_or_texts": 288524,
         "wall_seconds": group_v3["wall_seconds"], "source": rel(FR / "grouping_v3_run.json")},
        {"stage": "Frozen run: rank", "rows_or_texts": None,
         "wall_seconds": frozen["stages"]["rank"]["wall_seconds"], "source": rel(FR / "frozen_run_summary.json")},
        {"stage": "Frozen run: export grading files", "rows_or_texts": 660622,
         "wall_seconds": frozen["stages"]["export"]["wall_seconds"], "source": rel(FR / "frozen_run_summary.json")},
    ]
    memo_adopted = [r for r in luna_rows if r["bucket"] == "M_EMBED_V3"]
    memo_span = span_seconds(min(r["ledger_first_reserved_utc"] for r in memo_adopted),
                             max(r["ledger_last_settled_utc"] for r in memo_adopted))
    local_stages.append({"stage": "Adopted memo (2 Luna attempts; ledger span)", "rows_or_texts": None,
                         "wall_seconds": memo_span, "source": "state.db luna_ledger bucket M_EMBED_V3"})

    report = {
        "generated_from_saved_evidence": True,
        "model_calls_made_by_this_script": 0,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rates_source": "cost/rates.csv (codex-exec-subscription gpt-6-luna rows)",
        "billing_note": "All Luna calls used ChatGPT/Codex subscription authentication. USD figures are API-rate equivalents, not invoices.",
        "final_label_sources": final_sources,
        "legacy_teacher_10000": {
            "teacher_labels_kept": legacy_teacher,
            "calls": legacy_rows,
            "wall_seconds_final_resume_invocation": bench10k.get("wall_seconds"),
            "wall_seconds_scope": bench10k.get("wall_seconds_scope"),
            "total_wall_seconds": "unknown",
            "total_wall_seconds_reason": bench10k.get("total_classification_wall_seconds_status"),
            "source": "state.db calls (run_id codex-dev-*), experiments/codex_3agent/checkpoint-10000-parallel/benchmark_report.json",
        },
        "new_luna_calls": luna_rows,
        "failed_attempts_not_in_text_ledger": [
            dict(r, luna_api_equivalent_usd=equivalent(rates, r["input_tokens"], r["cached_input_tokens"], r["output_tokens"]),
                 note="Failed validation attempt of the adopted grouping run; its texts were re-sent and settled in G_EMBED_V3, so the per-text ledger does not carry these tokens.")
            for r in unledgered_failed
        ],
        "new_luna_totals": {
            "adopted": {k: tot(adopted_luna, k) for k in ("texts", "input_tokens", "cached_input_tokens", "output_tokens", "luna_api_equivalent_usd", "summed_call_seconds")},
            "superseded": {k: tot(superseded_luna, k) for k in ("texts", "input_tokens", "cached_input_tokens", "output_tokens", "luna_api_equivalent_usd", "summed_call_seconds")},
        },
        "first_1000_gate": {
            "wall_seconds_three_workers": gate_value("Gate wall-clock time with three workers"),
            "summed_call_seconds": gate_value("Sum of model-call durations"),
            "source": rel(FR / "APPROVAL_GATE_REPORT.md"),
        },
        "local_and_downstream_stages": local_stages,
        "measured_stage_wall_seconds_sum": round(sum(s["wall_seconds"] for s in local_stages if s["wall_seconds"]), 3),
        "measured_stage_wall_seconds_sum_note": "Sum of the separately timed stages listed above (excludes Luna teacher-label runs and the legacy 10,000 run). Stages were run as separate invocations, so no single end-to-end wall time exists.",
        "frozen_v1_stages_not_adopted": {
            "group_v1_wall_seconds": frozen["stages"]["group"]["wall_seconds"],
            "recommend_v1_wall_seconds": frozen["stages"]["recommend"]["wall_seconds"],
            "note": "Grouping v1 (TF-IDF, 51 issues) and memo v1 ran inside the frozen run but were superseded by the embedding grouping and memo v2.",
        },
        "stage_runs_utc": stage_runs,
        "account_usage": {
            "before_first_1000_used_percent": usage["before_1000"]["used_percent"],
            "after_first_1000_used_percent": usage["after_1000"]["used_percent"],
            "after_all_used_percent": usage["after_all"]["used_percent"],
            "limitation": usage["after_all"].get("attribution_limitation"),
        },
        "local_compute_cost": "unknown (hardware/electricity not metered)",
        "calendar_span_new_luna_utc": [luna_rows[0]["ledger_first_reserved_utc"], max(r["ledger_last_settled_utc"] for r in luna_rows)],
        "calendar_span_note": "Calendar span includes development and review pauses; it is not a pipeline runtime.",
    }
    (COST / "full_run_actuals.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    f = lambda v, d=3: "unknown" if v is None else (f"{v:,.{d}f}" if isinstance(v, float) else f"{v:,}")
    md = ["# Full-run actuals (from saved evidence)", "",
          "Generated by `scripts/build_full_run_actuals.py` with zero model calls. Every number below is read from the receipt named in its row; anything without a receipt is marked unknown.", "",
          "All Luna calls used ChatGPT/Codex subscription authentication, so the USD column is an API-rate equivalent from `cost/rates.csv`, not an invoice. Local hardware/electricity cost is unknown.", "",
          "## Final label sources", "",
          f"- Local distilled classifier: {final_sources.get('local', 0):,} rows",
          f"- Luna (teacher labels reused as final labels): {final_sources.get('luna', 0):,} rows",
          f"- Quarantined (empty review text): {final_sources.get('quarantined', 0):,} rows", "",
          "## Luna usage by bucket (new calls on 2026-10-03)", "",
          "| Bucket | Purpose | Status | Adoption | Texts | Calls | Input tok | Cached tok | Output tok | USD equiv. | Ledger span (s) | Summed call s |",
          "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in luna_rows:
        md.append(f"| {r['bucket']} | {r['purpose']} | {r['status']} | {r['adoption']} | {r['texts']:,} | {r['calls']} | {r['input_tokens']:,} | {r['cached_input_tokens']:,} | {r['output_tokens']:,} | ${r['luna_api_equivalent_usd']:.6f} | {r['ledger_span_seconds']:.0f} | {r['summed_call_seconds']:.1f} |")
    a, s = report["new_luna_totals"]["adopted"], report["new_luna_totals"]["superseded"]
    md += ["", f"Adopted new Luna total: {a['texts']:,} texts, ${a['luna_api_equivalent_usd']:.6f} equivalent. Superseded (grouping/memo v1): {s['texts']:,} texts, ${s['luna_api_equivalent_usd']:.6f} equivalent.", "",
           "Ledger span is first reservation to last settlement per bucket. H, R and the first part of AL1 ran together in the 1,000-text gate, so their spans overlap. Summed call seconds count parallel workers separately, so they can exceed the span.", "",
           *[f"Also billed but not in the per-text ledger: {r['calls']} failed validation call in the adopted grouping run ({r['input_tokens']:,} input / {r['cached_input_tokens']:,} cached / {r['output_tokens']:,} output tokens, ${equivalent(rates, r['input_tokens'], r['cached_input_tokens'], r['output_tokens']):.6f} equivalent). Its texts were re-sent and settled under G_EMBED_V3." for r in unledgered_failed], "",
           f"First 1,000-text gate: {f(report['first_1000_gate']['wall_seconds_three_workers'])} s wall with three workers ({f(report['first_1000_gate']['summed_call_seconds'])} s summed call time).", "",
           "## Legacy 10,000-text teacher run (reused)", ""]
    for r in legacy_rows:
        md.append(f"- {r['outcome']}: {r['calls']} calls, {r['input_tokens']:,} input / {r['cached_input_tokens']:,} cached / {r['output_tokens']:,} output tokens, ${r['luna_api_equivalent_usd']:.6f} equivalent, {r['summed_call_seconds']:,.1f} s summed call time")
    md += [f"- Teacher labels kept: {legacy_teacher:,}",
           f"- Wall time: {f(bench10k.get('wall_seconds'))} s for the final resume invocation only; total wall time unknown ({bench10k.get('total_classification_wall_seconds_status')}).", "",
           "## Local and downstream stages", "",
           "| Stage | Rows/texts | Wall s | Source |", "|---|---:|---:|---|"]
    for st in local_stages:
        md.append(f"| {st['stage']} | {f(st['rows_or_texts'])} | {f(st['wall_seconds'])} | `{st['source']}` |")
    md += ["", f"Sum of separately timed stages: {report['measured_stage_wall_seconds_sum']:,.3f} s (about {report['measured_stage_wall_seconds_sum']/60:.1f} min). Stages were separate invocations, so this is not one end-to-end timing, and it excludes the Luna teacher-label runs.", "",
           f"Not adopted but executed: grouping v1 {f(frozen['stages']['group']['wall_seconds'])} s, memo v1 {f(frozen['stages']['recommend']['wall_seconds'])} s (inside the frozen run).", "",
           "## Account usage (Codex subscription, account-wide)", "",
           f"- Before the first 1,000 new Luna texts: {usage['before_1000']['used_percent']}% used",
           f"- After the first 1,000: {usage['after_1000']['used_percent']}% used",
           f"- After all new Luna work: {usage['after_all']['used_percent']}% used",
           f"- {usage['after_all'].get('attribution_limitation')}", "",
           f"Calendar span of new Luna work: {report['calendar_span_new_luna_utc'][0]} to {report['calendar_span_new_luna_utc'][1]} UTC. This includes development and review pauses and is not a runtime.", ""]
    (COST / "full_run_actuals.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"new_luna_totals": report["new_luna_totals"], "stage_sum": report["measured_stage_wall_seconds_sum"]}, indent=2))


if __name__ == "__main__":
    main()
