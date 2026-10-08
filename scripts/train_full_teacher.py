from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.distilled import (  # noqa: E402
    DistilledTextProvider,
    DistilledTrainingConfig,
    build_training_rows,
    read_csv_rows,
    train_distilled_classifier,
)
from spotify_pipeline.io_utils import file_sha, write_csv, write_json  # noqa: E402
from train_distilled_classifier import compare_predictions, predict, write_prediction_csv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, default=PROJECT_ROOT / "data/checkpoint_500.csv")
    parser.add_argument(
        "--holdout-labels",
        type=Path,
        default=PROJECT_ROOT / "experiments/codex_luna/checkpoint-500-normal-usage-v2/predictions.csv",
    )
    parser.add_argument(
        "--golden-input",
        type=Path,
        default=PROJECT_ROOT / "artifacts/golden_prediction_input.csv",
    )
    parser.add_argument(
        "--baseline-summary",
        type=Path,
        default=PROJECT_ROOT / "experiments/distilled/pilot-v2/summary.json",
    )
    parser.add_argument("--minimum-improvement", type=float, default=0.005)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source_path = args.output / "teacher_source.csv"
    labels_path = args.output / "teacher_labels.csv"

    with sqlite3.connect(args.state) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT tl.text_sha256,t.representative_review_id,t.review_text,tl.result_json
            FROM teacher_labels tl JOIN texts t USING(text_sha256)
            ORDER BY t.first_source_rowid
            """
        ).fetchall()
    source_rows = []
    label_rows = []
    hash_by_id: dict[str, str] = {}
    for row in rows:
        review_id = str(row["representative_review_id"])
        result = json.loads(row["result_json"])
        hash_by_id[review_id] = str(row["text_sha256"])
        source_rows.append(
            {
                "review_id": review_id,
                "review_text": row["review_text"],
                "review_rating": "",
                "review_likes": "",
                "app_version": "",
                "review_timestamp": "",
            }
        )
        label_rows.append(
            {
                "review_id": review_id,
                "topic": result["topic"],
                "intent": result["intent"],
                "sentiment": result["sentiment"],
                "severity": result["severity"],
                "needs_review": str(bool(result["needs_review"])).lower(),
            }
        )
    write_csv(
        source_path,
        ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp"],
        source_rows,
    )
    write_csv(
        labels_path,
        ["review_id", "topic", "intent", "sentiment", "severity", "needs_review"],
        label_rows,
    )

    started = time.perf_counter()
    training_rows, training_report = build_training_rows(
        source_path=source_path,
        labels_path=labels_path,
        excluded_source_paths=(args.holdout, args.golden_input),
    )
    artifact_path = args.output / "distilled_model.joblib"
    manifest_path = args.output / "model_manifest.json"
    manifest = train_distilled_classifier(
        rows=training_rows,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        training_report=training_report,
        config=DistilledTrainingConfig(),
    )
    provider = DistilledTextProvider(
        artifact_path=artifact_path, expected_sha256=manifest["artifact_sha256"]
    )
    holdout_rows = read_csv_rows(args.holdout)
    predictions, prediction_seconds = predict(provider, holdout_rows)
    prediction_path = args.output / "holdout_500_predictions.csv"
    write_prediction_csv(prediction_path, predictions)
    comparison = compare_predictions(
        holdout_rows, read_csv_rows(args.holdout_labels), predictions
    )
    agreement = comparison["agreement"]
    current_mean = (
        float(agreement["topic"])
        + float(agreement["intent"])
        + float(agreement["severity_exact"])
    ) / 3
    baseline = json.loads(args.baseline_summary.read_text(encoding="utf-8"))
    baseline_agreement = baseline["holdout_500"]["agreement"]
    baseline_mean = (
        float(baseline_agreement["topic"])
        + float(baseline_agreement["intent"])
        + float(baseline_agreement["severity_exact"])
    ) / 3
    improvement = current_mean - baseline_mean
    decision = {
        "baseline_mean_agreement": round(baseline_mean, 6),
        "al1_mean_agreement": round(current_mean, 6),
        "improvement": round(improvement, 6),
        "minimum_improvement_for_al2": args.minimum_improvement,
        "run_al2": improvement >= args.minimum_improvement,
        "component_agreement": agreement,
        "quality_claim": "agreement with held-out saved Luna pseudo-labels; not human accuracy",
    }
    write_json(args.output / "al2_decision.json", decision)
    with sqlite3.connect(args.state) as connection:
        connection.executemany(
            "UPDATE teacher_labels SET used_for_training=1 WHERE text_sha256=?",
            ((hash_by_id[row["review_id"]],) for row in training_rows),
        )
        connection.commit()
    summary = {
        "status": "completed",
        "teacher_texts_available": len(rows),
        "training_rows_after_leakage_controls": len(training_rows),
        "artifact_sha256": manifest["artifact_sha256"],
        "manifest_sha256": file_sha(manifest_path),
        "training_and_evaluation_seconds": round(time.perf_counter() - started, 6),
        "holdout_prediction_seconds": round(prediction_seconds, 6),
        "golden_labels_opened": False,
        "decision": decision,
    }
    write_json(args.output / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

