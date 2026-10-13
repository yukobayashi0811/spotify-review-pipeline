#!/usr/bin/env python3
"""Check the interruption/resume evidence of a pipeline run (no model calls).

Reads <grading>/checkpoint_before.json, checkpoint_after.json, calls.jsonl and
records.jsonl(.gz) and applies the grading contract's resume rules:

* the checkpoint before is nonempty and a strict subset of the one after;
* every ID saved before the interruption was completed by a successful
  ``initial`` enrichment call (or is an exact-text cache reuse);
* no ``resume`` enrichment call re-sent an ID that was already saved;
* the resume completed new IDs through successful ``resume`` calls.

Usage: python3 scripts/check_interrupt_resume.py --grading grading --out evals/recovery_v4/interrupt_resume_check.json
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grading", type=Path, default=Path("grading"))
    parser.add_argument("--out", type=Path, default=Path("evals/recovery_v4/interrupt_resume_check.json"))
    args = parser.parse_args()
    folder = args.grading
    before = set(json.loads((folder / "checkpoint_before.json").read_text())["completed_ids"])
    after = set(json.loads((folder / "checkpoint_after.json").read_text())["completed_ids"])
    plain, packed = folder / "records.jsonl", folder / "records.jsonl.gz"
    stream = plain.open(encoding="utf-8") if plain.exists() else gzip.open(packed, "rt", encoding="utf-8")
    with stream:
        records = {row["review_id"]: row for row in map(json.loads, stream)}
    cached = {rid for rid, row in records.items() if row.get("cache_source_id")}
    initial, resume, resent = set(), set(), set()
    calls = {"initial": 0, "resume": 0}
    run_ids = {"initial": set(), "resume": set()}
    for event in map(json.loads, (folder / "calls.jsonl").read_text(encoding="utf-8").splitlines()):
        if event.get("role") != "enrich" or event.get("phase") not in calls:
            continue
        ids = set(event.get("review_ids") or [])
        if event["phase"] == "resume":
            resent |= ids & before
        if event.get("outcome") != "succeeded":
            continue
        calls[event["phase"]] += 1
        (initial if event["phase"] == "initial" else resume).update(ids)
    checks = {
        "before_nonempty_and_strict_subset_of_after": bool(before) and before < after,
        "before_ids_trace_to_successful_initial_calls_or_cache": not (before - cached - initial),
        "resume_sent_zero_before_ids": not resent,
        "resume_completed_new_ids": bool((after - before - cached) & resume),
    }
    report = {
        "before_completed_ids": len(before),
        "after_completed_ids": len(after),
        "after_minus_before": len(after - before),
        "resume_sent_before_ids": len(resent),
        "successful_initial_enrich_calls": calls["initial"],
        "successful_resume_enrich_calls": calls["resume"],
        "checks": checks,
        "passed": all(checks.values()),
        "model_calls": 0,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
