from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.distilled import normalize_text, text_sha256  # noqa: E402
from spotify_pipeline.io_utils import file_sha  # noqa: E402

SEED = 20261003


def exclusions(paths: list[Path]) -> tuple[set[str], set[str], set[str]]:
    ids: set[str] = set()
    exact: set[str] = set()
    normalized: set[str] = set()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                ids.add(row["review_id"])
                exact.add(text_sha256(row["review_text"]))
                normalized.add(text_sha256(normalize_text(row["review_text"])))
    return ids, exact, normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--golden-input", type=Path, required=True)
    parser.add_argument("--checkpoint-500", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    excluded_ids, excluded_exact, excluded_normalized = exclusions(
        [args.golden_input, args.checkpoint_500]
    )
    with sqlite3.connect(args.state) as connection:
        connection.row_factory = sqlite3.Row
        candidates = [
            dict(row) for row in connection.execute(
                """
                SELECT t.representative_review_id AS review_id,t.review_text,
                       t.text_sha256,t.normalized_text_sha256
                FROM texts t JOIN records r ON r.review_id=t.representative_review_id
                WHERE r.status='completed'
                ORDER BY t.text_sha256
                """
            )
            if row["review_id"] not in excluded_ids
            and row["text_sha256"] not in excluded_exact
            and row["normalized_text_sha256"] not in excluded_normalized
        ]
    sample = random.Random(SEED).sample(candidates, 60)
    sample.sort(key=lambda row: row["review_id"])
    csv_path = args.output_dir / "holdout_60_to_label.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["review_id", "review_text"])
        writer.writeheader()
        writer.writerows(
            {"review_id": row["review_id"], "review_text": row["review_text"]}
            for row in sample
        )
    workbook_source = args.output_dir / "holdout_60_workbook_source.json"
    workbook_source.write_text(
        json.dumps(
            {
                "seed": SEED,
                "rows": [
                    {"review_id": row["review_id"], "review_text": row["review_text"]}
                    for row in sample
                ],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "seed": SEED,
        "sampling_unit": "distinct exact review text among completed records",
        "sampling_method": "Python random.Random(seed).sample after deterministic hash ordering",
        "candidate_distinct_texts_after_exclusions": len(candidates),
        "selected": len(sample),
        "exclusions": {
            "sources": [str(args.golden_input), str(args.checkpoint_500)],
            "by_review_id_exact_text_and_normalized_text": True,
            "golden_human_labels_opened": False,
        },
        "prediction_columns_included": False,
        "csv_sha256": file_sha(csv_path),
    }
    (args.output_dir / "holdout_60_sampling_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
