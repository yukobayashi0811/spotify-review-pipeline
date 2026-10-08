from __future__ import annotations

import argparse
import json
from pathlib import Path

from spotify_pipeline.io_utils import atomic_write_text, canonical, write_json
from spotify_pipeline.storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Export measured checkpoint calls and runtime metadata")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--run-summary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--spend-limit-usd", type=float, default=25.0)
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    parser.add_argument("--fallback-fraction", type=float, default=0.0)
    args = parser.parse_args()

    summary = json.loads(args.run_summary.read_text(encoding="utf-8"))
    if summary.get("status") != "completed":
        raise ValueError("Checkpoint run summary is not completed")
    run_id = summary["run_id"]
    args.output.mkdir(parents=True, exist_ok=True)

    with Store(args.state) as store:
        calls = list(store.call_rows())
        run_calls = [call for call in calls if call["run_id"] == run_id]
        if not run_calls:
            raise ValueError(f"No saved calls for run {run_id}")
        call_lines = [canonical(call) for call in run_calls]
        atomic_write_text(args.output / "calls.jsonl", "\n".join(call_lines) + "\n")

        ingestion = store.get_metadata("ingestion_report")
        source_rows = store.source_count()
        completed = store.completed_count()
        quarantined = store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE status='quarantined'"
        ).fetchone()[0]
        unique_nonempty = store.connection.execute(
            "SELECT COUNT(DISTINCT review_text) FROM source_rows WHERE TRIM(review_text)<>''"
        ).fetchone()[0]
        cache_hits = store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL"
        ).fetchone()[0]
        ids = [row[0] for row in store.connection.execute("SELECT review_id FROM source_rows ORDER BY rowid")]

    successful_enrich = [
        call for call in run_calls if call["role"] == "enrich" and call["outcome"] == "succeeded"
    ]
    if not successful_enrich:
        raise ValueError("Checkpoint has no successful enrichment calls")
    first = successful_enrich[0]
    batch_size = max(len(call["review_ids"]) for call in successful_enrich)
    metadata = {
        "evidence_type": "measured_checkpoint",
        "projection_run_id": run_id,
        "pilot_rows": source_rows,
        "input_sha256": ingestion["file_sha256"],
        "pilot_ids": ids,
        "completed_records": completed,
        "quarantined_or_failed_records": quarantined,
        "unique_nonempty_texts": unique_nonempty,
        "direct_exact_text_cache_hits": cache_hits,
        "warm_saved_result_reuses": None,
        "warm_new_enrichment_calls": None,
        "provider": first["provider"],
        "model": first["model"],
        "effort": first["effort"],
        "batch_size": batch_size,
        "worker_limit": 1,
        "cold_wall_seconds": summary["wall_seconds"],
        "cold_stage_wall_seconds": summary["stage_wall_seconds"],
        "cold_verification_selected": summary.get("stages", {}).get("verify", {}).get("selected", 0),
        "controls": {
            "spend_limit_usd": args.spend_limit_usd,
            "max_output_tokens": args.max_output_tokens,
            "max_workers": 1,
            "fallback_fraction": args.fallback_fraction,
        },
        "provider_runtime": {
            "provider": first["provider"],
            "model": first["model"],
            "api_charges_usd": 0.0 if first["provider"] == "local-mlx" else "calculated_from_rates",
            "hardware_cost_usd": "unknown",
            "electricity_cost_usd": "unknown"
        },
    }
    write_json(args.output / "run.json", metadata)
    print(json.dumps({"calls": len(run_calls), "metadata": metadata}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
