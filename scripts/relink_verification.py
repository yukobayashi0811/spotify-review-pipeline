from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.state) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(
            """
            SELECT v.*,r.topic AS final_topic,r.intent AS final_intent,
                   r.severity AS final_severity,t.word_count,t.group_size
            FROM verifier_records v JOIN records r USING(review_id)
            JOIN source_rows s USING(review_id) JOIN texts t USING(text_sha256)
            ORDER BY v.final_source,v.review_id
            """
        )]
    if len(rows) != 400:
        raise RuntimeError(f"Expected the saved 400-review verifier sample, found {len(rows)}")
    for row in rows:
        row["word_bucket"] = (
            "00-05" if row["word_count"] <= 5 else
            "06-15" if row["word_count"] <= 15 else
            "16-40" if row["word_count"] <= 40 else "41+"
        )
        row["topic_agree"] = row["final_topic"] == row["topic"]
        row["intent_agree"] = row["final_intent"] == row["intent"]
        row["severity_agree"] = row["final_severity"] == row["severity"]

    def metrics(values: list[dict[str, object]]) -> dict[str, object]:
        weight = sum(float(row["sample_weight"]) for row in values)
        weighted = lambda field: sum(
            float(row["sample_weight"]) for row in values if row[field]
        ) / weight
        return {
            "distinct_texts": len(values),
            "estimated_rows_weight": weight,
            "row_weighted_topic_agreement": weighted("topic_agree"),
            "row_weighted_intent_agreement": weighted("intent_agree"),
            "row_weighted_severity_exact": weighted("severity_agree"),
            "quote_supported_rate": sum(int(row["quote_supported"]) for row in values) / len(values),
        }

    by_source = {
        source: metrics([row for row in rows if row["final_source"] == source])
        for source in ("local", "luna")
    }
    by_source["local"]["interpretation"] = "independent verifier agreement estimate"
    by_source["luna"]["interpretation"] = "same-model Luna test-retest, not independent accuracy"
    buckets = {
        bucket: metrics([row for row in rows if row["word_bucket"] == bucket])
        for bucket in ("00-05", "06-15", "16-40", "41+")
    }
    disagreements = [row for row in rows if not (
        row["topic_agree"] and row["intent_agree"] and row["severity_agree"]
    )]
    patterns = Counter()
    for row in disagreements:
        if not row["topic_agree"]:
            patterns[f"topic:{row['final_topic']}->{row['topic']}"] += 1
        if not row["intent_agree"]:
            patterns[f"intent:{row['final_intent']}->{row['intent']}"] += 1
        if not row["severity_agree"]:
            patterns[f"severity:{row['final_severity']}->{row['severity']}"] += 1
    report = {
        "status": "completed_relinked_without_new_model_calls",
        "selection": {
            "design": "topic × word-count-bucket stratified deterministic sample of distinct texts",
            "seed": 0,
            "ordering": "text_sha256 ascending within each stratum",
            "local_distinct_texts": 300,
            "luna_distinct_texts": 100,
            "weight": "group_size × stratum_population / stratum_sample_size",
        },
        "metrics_by_final_source": by_source,
        "metrics_by_word_count_bucket": buckets,
        "disagreement_count": len(disagreements),
        "main_error_patterns": [
            {"pattern": key, "count": count} for key, count in patterns.most_common(20)
        ],
        "golden_labels_opened": False,
        "new_luna_calls": 0,
        "historical_hybrid_10000_note": (
            "The earlier hybrid-10000 result is an in-training-data evaluation and is not used as quality evidence."
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "verification_relinked_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fields = [
        "review_id", "final_source", "stratum", "sample_weight", "word_bucket",
        "final_topic", "topic", "topic_agree", "final_intent", "intent", "intent_agree",
        "final_severity", "severity", "severity_agree", "quote_supported",
    ]
    with (args.output / "verification_disagreements.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{field: row[field] for field in fields} for row in disagreements])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
