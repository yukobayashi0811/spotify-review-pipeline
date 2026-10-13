#!/usr/bin/env python3
"""Export two inspectable tables from a saved run state (no model calls).

* ``verification/verifier_predictions.csv``: every sampled text the blind verifier
  labeled, next to the first-pass label it never saw, with per-field agreement,
  stratum and sample weight (the agreement figures in ``verification_report.json``
  are computed from these rows); sampled IDs whose verification failed are listed
  with ``verifier_status=failed``.
* ``quarantine.jsonl``: every quarantined record with its reason and attempt count.

Usage (from the repository root; the state database is not committed):

    python3 scripts/export_run_tables.py --run-dir artifacts/full_run_v4
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

FIELDS = ("topic", "intent", "severity", "sentiment")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("artifacts/full_run_v4"))
    parser.add_argument("--state", type=Path, default=None, help="default: <run-dir>/state.db")
    args = parser.parse_args()
    state = args.state or args.run_dir / "state.db"
    db = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    report = json.loads((args.run_dir / "verification" / "verification_report.json").read_text(encoding="utf-8"))
    failed = [item["review_id"] for item in report.get("failures", [])]
    first = {}
    verified = list(db.execute("SELECT * FROM verifier_records ORDER BY stratum, review_id"))
    ids = [row["review_id"] for row in verified] + failed
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in db.execute(f"SELECT * FROM records WHERE review_id IN ({marks})", chunk):
            first[row["review_id"]] = row

    out = args.run_dir / "verification" / "verifier_predictions.csv"
    columns = [
        "review_id", "verifier_status", "stratum", "final_label_source", "sample_weight",
        *[f"first_{field}" for field in FIELDS], "first_label_config",
        *[f"verifier_{field}" for field in FIELDS], "verifier_evidence_quote", "verifier_label_config",
        "topic_match", "intent_match", "severity_match",
    ]
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in verified:
            base = first[row["review_id"]]
            writer.writerow({
                "review_id": row["review_id"], "verifier_status": "verified", "stratum": row["stratum"],
                "final_label_source": row["final_source"], "sample_weight": row["sample_weight"],
                **{f"first_{field}": base[field] for field in FIELDS}, "first_label_config": base["label_config"],
                **{f"verifier_{field}": row[field] for field in FIELDS},
                "verifier_evidence_quote": row["evidence_quote"], "verifier_label_config": row["label_config"],
                **{f"{field}_match": str(base[field] == row[field]).lower() for field in ("topic", "intent", "severity")},
            })
        for review_id in failed:
            base = first.get(review_id)
            writer.writerow({
                "review_id": review_id, "verifier_status": "failed",
                **({f"first_{field}": base[field] for field in FIELDS} if base else {}),
                "first_label_config": base["label_config"] if base else "",
            })

    quarantine = args.run_dir / "quarantine.jsonl"
    rows = list(db.execute(
        "SELECT review_id, source_sha256, status, reason, attempt_count FROM records "
        "WHERE status = 'quarantined' ORDER BY review_id"
    ))
    with quarantine.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({
        "verifier_predictions": str(out), "verified": len(verified), "failed": len(failed),
        "quarantine": str(quarantine), "quarantined": len(rows), "model_calls": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
