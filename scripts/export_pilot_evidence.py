#!/usr/bin/env python3
"""Export the measured 100-review pilot into cost/ (no model calls).

Reads the pilot's state database and the cold/warm run summaries written by
``spotify-pipeline run`` and writes, under ``--cost-dir``:

* ``pilot_records.jsonl`` - one contract record (status, labels, source hash)
  per pilot review ID, copied from the pilot's own grading export;
* ``pilot_calls.jsonl``   - every attempted call (local classifier batches and
  Luna requests, including failures and transient retries) with stage, model,
  effort, prompt/schema versions, batch size, worker limit, usage and timing,
  tagged cold or warm by run ID;
* ``pilot_run.json``      - checksums, IDs, counts, cache hits, controls and the
  cold/warm stage timings used by ``cost/calculator.py``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE_BY_ROLE = {"enrich": "classify", "verify": "verify", "group": "group", "memo": "recommend"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-dir", type=Path, default=ROOT / "cost/pilot_100")
    parser.add_argument("--cost-dir", type=Path, default=ROOT / "cost")
    parser.add_argument("--input", type=Path, default=ROOT / "data/cost_100.csv")
    args = parser.parse_args()
    pilot, cost = args.pilot_dir, args.cost_dir
    cold = json.loads((pilot / "cold_run_summary.json").read_text(encoding="utf-8"))
    warm = json.loads((pilot / "warm_run_summary.json").read_text(encoding="utf-8"))
    phase_by_run = {cold["run_id"]: "cold", warm["run_id"]: "warm"}

    shutil.copyfile(pilot / "grading" / "records.jsonl", cost / "pilot_records.jsonl")
    connection = sqlite3.connect(pilot / "state.db")
    connection.row_factory = sqlite3.Row
    calls = []
    for row in connection.execute("SELECT * FROM calls ORDER BY created_at, rowid"):
        review_ids = json.loads(row["review_ids_json"])
        metadata = json.loads(row["provider_metadata_json"]) if row["provider_metadata_json"] else {}
        guard = metadata.get("guard", {})
        calls.append({
            "run_id": row["run_id"],
            "run_phase": phase_by_run.get(row["run_id"], "other"),
            "request_id": row["request_id"],
            "role": row["role"],
            "stage": "classify_local" if row["model"].startswith("distilled") else (
                "classify_luna" if row["role"] == "enrich" else STAGE_BY_ROLE.get(row["role"], row["role"])
            ),
            "provider": row["provider"],
            "billing_mode": row["billing_mode"],
            "model": row["model"],
            "effort": row["effort"],
            "prompt_version": row["prompt_version"],
            "schema_version": row["schema_version"],
            "label_config": row["label_config"],
            "batch_size": len(review_ids),
            "worker_limit": 1,
            "review_ids": review_ids,
            "phase": row["phase"],
            "outcome": row["outcome"],
            "fallback": bool(row["fallback"]),
            "transient_retry": row["phase"] == "transient_retry",
            "error": row["error"],
            "usage_status": row["usage_status"],
            "input_tokens": row["input_tokens"],
            "cached_input_tokens": row["cached_input_tokens"],
            "output_tokens": row["output_tokens"],
            "duration_seconds": row["duration_seconds"],
            "served_model_reported": guard.get("served_model_reported"),
            "created_at": row["created_at"],
        })
    with (cost / "pilot_calls.jsonl").open("w", encoding="utf-8") as stream:
        for call in calls:
            stream.write(json.dumps(call, ensure_ascii=False, sort_keys=True) + "\n")

    with args.input.open(encoding="utf-8", newline="") as stream:
        input_ids = [row["review_id"] for row in csv.DictReader(stream)]
    records = [json.loads(line) for line in (cost / "pilot_records.jsonl").read_text().splitlines() if line]
    statuses: dict[str, int] = {}
    for record in records:
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
    manifest = json.loads((ROOT / "data/manifest.json").read_text(encoding="utf-8"))
    expected = manifest["files"]["cost_100.csv"]["sha256"]
    routing = json.loads((pilot / "routing.json").read_text(encoding="utf-8"))
    verification = json.loads(
        (pilot / "verification" / "verification_report.json").read_text(encoding="utf-8")
    )
    run = {
        "input_file": args.input.name,
        "input_sha256": sha256(args.input),
        "manifest_sha256": expected,
        "checksum_matches_manifest": expected == sha256(args.input),
        "pilot_rows": len(input_ids),
        "pilot_review_ids": input_ids,
        "record_ids_match_input": sorted(record["review_id"] for record in records) == sorted(input_ids),
        "status_counts": statuses,
        "completed_records": statuses.get("completed", 0),
        "failed_or_quarantined_records": statuses.get("quarantined", 0),
        "distinct_nonempty_texts": cold["stages"]["prepare"]["distinct_nonempty_texts"],
        "exact_text_cache_hits_rows": cold["counts"]["exact_text_reuse_rows"],
        "result_cache_at_start": "empty (new output directory and state database)",
        "routing": {key: routing[key] for key in ("policy", "rule", "distinct_texts", "eligible_texts", "limit", "routed_texts", "routed_rows")},
        "verification": {
            "sampled_rows": verification.get("selected"),
            "verified_rows": verification.get("verified"),
            "agreement": verification.get("agreement"),
            "by_label_source": verification.get("by_label_source"),
            "sampling": verification.get("sampling"),
        },
        "declared_controls": cold["declared_controls"],
        "cold": {
            "run_id": cold["run_id"],
            "wall_seconds": cold["wall_seconds"],
            "stage_wall_seconds": {name: value.get("wall_seconds") for name, value in cold["stages"].items()},
            "new_model_calls": cold["new_model_calls_this_run"],
            "guard": cold.get("guard"),
        },
        "warm": {
            "run_id": warm["run_id"],
            "wall_seconds": warm["wall_seconds"],
            "stage_wall_seconds": {name: value.get("wall_seconds") for name, value in warm["stages"].items()},
            "all_stages_skipped_unchanged": warm["all_stages_skipped_unchanged"],
            "new_model_calls": warm["new_model_calls_this_run"],
        },
        "commands": {
            "cold": "spotify-pipeline run --input data/cost_100.csv --config config/pipeline.json --output cost/pilot_100",
            "warm": "the same command again, unchanged",
        },
    }
    (cost / "pilot_run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"calls": len(calls), "records": len(records), "statuses": statuses}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
