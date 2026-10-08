#!/usr/bin/env python3
"""Regenerate the baseline ranking from the committed grading export (no database, no model calls).

Reads grading/records.jsonl (or records.jsonl.gz) and grading/membership.csv, recomputes
complaint_count, severity_sum, mean_severity (six decimals, half-up) and priority_score
(= severity_sum) per issue, orders by descending score then ascending issue ID, writes the
result, and compares it field by field with grading/ranking.csv.

Usage:
    python3 scripts/rerank_from_grading.py [--grading grading] [--output artifacts/rerank_from_grading.csv]
Exit code 0 means the regenerated ranking is identical to grading/ranking.csv.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

FIELDS = ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"]
RANKED_INTENTS = {"complaint", "cancellation"}


def open_records(folder: Path):
    plain, compressed = folder / "records.jsonl", folder / "records.jsonl.gz"
    if plain.exists():
        return plain.open(encoding="utf-8")
    if compressed.exists():
        return gzip.open(compressed, "rt", encoding="utf-8")
    raise FileNotFoundError(f"No records.jsonl or records.jsonl.gz in {folder}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--grading", type=Path, default=Path("grading"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/rerank_from_grading.csv"))
    args = parser.parse_args()

    severity: dict[str, int] = {}
    with open_records(args.grading) as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "completed" and row.get("intent") in RANKED_INTENTS:
                severity[row["review_id"]] = int(row["severity"])

    members: dict[str, list[int]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    with (args.grading / "membership.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["issue_id"], row["review_id"])
            if key in seen:
                raise SystemExit(f"Duplicate membership pair: {key}")
            seen.add(key)
            if row["review_id"] not in severity:
                raise SystemExit(f"Membership review is not a completed complaint/cancellation: {row['review_id']}")
            members[row["issue_id"]].append(severity[row["review_id"]])

    missing = set(severity) - {review_id for _, review_id in seen}
    if missing:
        raise SystemExit(f"{len(missing)} complaint/cancellation records have no issue membership")

    ranking = []
    for issue_id, values in members.items():
        total = sum(values)
        mean = (Decimal(total) / Decimal(len(values))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        ranking.append({"issue_id": issue_id, "complaint_count": str(len(values)),
                        "severity_sum": str(total), "mean_severity": str(mean), "priority_score": str(total)})
    ranking.sort(key=lambda row: (-int(row["priority_score"]), row["issue_id"]))
    for rank, row in enumerate(ranking, 1):
        row["rank"] = str(rank)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(ranking)

    with (args.grading / "ranking.csv").open(encoding="utf-8", newline="") as stream:
        saved = [{key: row[key] for key in FIELDS} for row in csv.DictReader(stream)]
    regenerated = [{key: row[key] for key in FIELDS} for row in ranking]
    identical = saved == regenerated
    print(json.dumps({"issues": len(ranking), "memberships": len(seen),
                      "identical_to_saved_ranking": identical, "output": str(args.output)}, indent=2))
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
