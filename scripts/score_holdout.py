#!/usr/bin/env python3
"""Score the final labels on the fresh held-out 30 (human labels, labeled blind; no model calls).

The 30 reviews are the first 30 (by review_id) of a seeded random sample of 60
that excludes the Golden 50 and checkpoint_500 (evals/holdout_60_sampling_manifest.json).
They were labeled by a person from the review text only; no model prediction was
shown. This script:

1. converts the labeled workbook/CSV into evals/holdout_30.csv (same columns as
   evals/golden_50.csv; comma-separated entities become a JSON list);
2. extracts the final full-run labels for those IDs from the grading export
   into evals/holdout_30_predictions.csv;
3. scores them with the same code as the Golden 50 and writes
   evals/holdout_30_evaluation.json and evals/holdout_30_evaluation_cases.csv.

Usage: python3 scripts/score_holdout.py --labels <downloaded .xlsx or .csv>
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spotify_pipeline.evaluate import score_golden  # noqa: E402

SOURCE_FIELDS = ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp"]
LABEL_FIELDS = ["topic", "intent", "sentiment", "severity", "entities", "evidence_quote"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_labels(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    import openpyxl

    sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    rows = [[cell for cell in row] for row in sheet.iter_rows(values_only=True)]
    header_index = next(i for i, row in enumerate(rows) if row and row[0] == "review_id")
    header = [str(value) for value in rows[header_index] if value is not None]
    result = []
    for row in rows[header_index + 1:]:
        if not row or not row[0]:
            continue
        result.append({name: "" if row[i] is None else str(row[i]) for i, name in enumerate(header)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    sources = {}
    with (ROOT / "data/spotify_reviews_18months.csv").open(encoding="utf-8", newline="") if (
        ROOT / "data/spotify_reviews_18months.csv"
    ).exists() else (ROOT / "evals/holdout_60_to_label.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            sources[row["review_id"]] = row
    template_ids = [row["review_id"] for row in csv.DictReader((ROOT / "evals/holdout_30_to_label.csv").open(encoding="utf-8"))]
    labeled = {row["review_id"]: row for row in read_labels(args.labels)}
    if set(labeled) != set(template_ids):
        raise SystemExit(f"Labeled IDs differ from the 30 template IDs: missing={sorted(set(template_ids) - set(labeled))}")
    problems, gold_rows = [], []
    for review_id in template_ids:
        label, source = labeled[review_id], sources[review_id]
        text = source["review_text"]
        entities = [value.strip() for value in (label.get("entities") or "").split(",") if value.strip()]
        quote = (label.get("evidence_quote") or "").strip()
        if quote and quote not in text:
            collapsed = " ".join(quote.split())
            match = next((text[i:i + len(collapsed)] for i in range(len(text)) if " ".join(text[i:i + len(collapsed)].split()) == collapsed), None)
            quote = match or quote
        if not quote or quote not in text:
            problems.append(f"{review_id}: evidence_quote is not an exact substring of the review")
        for field in ("topic", "intent", "sentiment", "severity"):
            if not str(label.get(field) or "").strip():
                problems.append(f"{review_id}: {field} is blank")
        severity = str(label.get("severity") or "").split(".")[0]
        gold_rows.append({
            **{field: source[field] for field in SOURCE_FIELDS},
            "topic": (label.get("topic") or "").strip(), "intent": (label.get("intent") or "").strip(),
            "sentiment": str(label.get("sentiment") or "").strip(), "severity": severity,
            "entities": json.dumps(entities, ensure_ascii=False), "evidence_quote": quote,
            "notes": label.get("notes", ""),
        })
    if problems:
        print("\n".join(problems))
        raise SystemExit("Fix the rows above in the labeling sheet, then rerun.")
    labels_csv = ROOT / "evals/holdout_30.csv"
    with labels_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*SOURCE_FIELDS, *LABEL_FIELDS, "notes"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(gold_rows)

    wanted, found = set(template_ids), {}
    grading = ROOT / "grading"
    opener = (grading / "records.jsonl").open(encoding="utf-8") if (grading / "records.jsonl").exists() else gzip.open(grading / "records.jsonl.gz", "rt", encoding="utf-8")
    with opener as stream:
        for line in stream:
            record = json.loads(line)
            if record["review_id"] in wanted:
                found[record["review_id"]] = record
    predictions_csv = ROOT / "evals/holdout_30_predictions.csv"
    with predictions_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["review_id", *LABEL_FIELDS, "needs_review", "label_config"], lineterminator="\n")
        writer.writeheader()
        for review_id in template_ids:
            record = found[review_id]
            writer.writerow({
                "review_id": review_id, "topic": record["topic"], "intent": record["intent"],
                "sentiment": record["sentiment"], "severity": record["severity"],
                "entities": json.dumps(record["entities"], ensure_ascii=False),
                "evidence_quote": record["evidence_quote"],
                "needs_review": str(record["needs_review"]).lower(), "label_config": record["label_config"],
            })
    output = ROOT / "evals/holdout_30_evaluation.json"
    report = score_golden(labels_csv, predictions_csv, output, ROOT / "evals/holdout_30_evaluation_cases.csv", expected_rows=30)
    with (ROOT / "evals/holdout_30_evaluation_cases.csv").open(encoding="utf-8") as stream:
        cases = list(csv.DictReader(stream))
    source_of = {review_id: ("gpt-6-luna" if found[review_id]["label_config"].startswith("gpt-6-luna") else "local_classifier") for review_id in template_ids}
    by_source = {}
    for case in cases:
        bucket = by_source.setdefault(source_of[case["review_id"]], {"rows": 0, "topic": 0, "intent": 0, "severity_exact": 0, "severity_within_1": 0})
        bucket["rows"] += 1
        bucket["topic"] += case["topic_match"] == "True"
        bucket["intent"] += case["intent_match"] == "True"
        bucket["severity_exact"] += case["severity_match"] == "True"
        bucket["severity_within_1"] += case["severity_error"] not in ("", None) and int(case["severity_error"]) <= 1
    for bucket in by_source.values():
        for key in ("topic", "intent", "severity_exact", "severity_within_1"):
            bucket[key] = round(bucket[key] / bucket["rows"], 6)
    report.update({
        "set": "held-out 30 (fresh; labeled blind by a person from review text only, no predictions shown; never a model input and never used for tuning)",
        "sampling": "first 30 by review_id of a seeded random 60 excluding Golden 50 and checkpoint_500 (evals/holdout_60_sampling_manifest.json)",
        "labels_file": "evals/holdout_30.csv", "labels_sha256": sha(labels_csv),
        "labeled_workbook_sha256": sha(args.labels),
        "predictions_file": "evals/holdout_30_predictions.csv", "predictions_sha256": sha(predictions_csv),
        "severity_within_1": round(sum(case["severity_error"] not in ("", None) and int(case["severity_error"]) <= 1 for case in cases) / len(cases), 6),
        "by_label_source": by_source,
        "ambiguous_cases_marked_unclear_by_labeler": sum(row["intent"] == "unclear" for row in gold_rows),
        "model_calls": 0,
    })
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("agreement", "macro_f1", "severity_within_1", "by_label_source")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
