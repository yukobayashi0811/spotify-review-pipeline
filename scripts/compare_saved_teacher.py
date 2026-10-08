from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error


TARGETS = ("topic", "intent", "severity")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare saved hybrid records with saved Luna pseudo-labels."
    )
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--teacher", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line
    ]
    with args.teacher.open(encoding="utf-8-sig", newline="") as handle:
        teacher_rows = list(csv.DictReader(handle, strict=True))
    actual = {row["review_id"]: row for row in records}
    teacher = {row["review_id"]: row for row in teacher_rows}
    if len(actual) != len(records) or len(teacher) != len(teacher_rows):
        raise ValueError("Duplicate review IDs")
    if set(actual) != set(teacher):
        raise ValueError("Record and teacher ID sets differ")

    ordered_ids = [row["review_id"] for row in teacher_rows]
    metrics: dict[str, dict[str, float]] = {}
    for target in TARGETS:
        expected = [str(teacher[review_id][target]) for review_id in ordered_ids]
        predicted = [str(actual[review_id][target]) for review_id in ordered_ids]
        metrics[target] = {
            "accuracy": round(float(accuracy_score(expected, predicted)), 6),
            "macro_f1": round(
                float(f1_score(expected, predicted, average="macro", zero_division=0)),
                6,
            ),
        }
        if target == "severity":
            metrics[target]["mae"] = round(
                float(
                    mean_absolute_error(
                        [int(value) for value in expected],
                        [int(value) for value in predicted],
                    )
                ),
                6,
            )
    report = {
        "status": "completed",
        "golden_labels_loaded": False,
        "rows": len(ordered_ids),
        "teacher": "saved Codex Luna pseudo-labels, not human ground truth",
        "records_path": str(args.records.resolve()),
        "teacher_path": str(args.teacher.resolve()),
        "metrics": metrics,
        "mean_accuracy": round(
            sum(metrics[target]["accuracy"] for target in TARGETS) / len(TARGETS), 6
        ),
        "mean_macro_f1": round(
            sum(metrics[target]["macro_f1"] for target in TARGETS) / len(TARGETS), 6
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
