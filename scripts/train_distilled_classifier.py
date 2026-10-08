from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.distilled import (  # noqa: E402
    DISTILLED_MODEL_VERSION,
    DistilledTextProvider,
    DistilledTrainingConfig,
    build_training_rows,
    normalize_text,
    read_csv_rows,
    text_sha256,
    train_distilled_classifier,
)
from spotify_pipeline.evaluate import score_golden  # noqa: E402
from spotify_pipeline.io_utils import file_sha, write_csv, write_json  # noqa: E402
from spotify_pipeline.constants import SOURCE_FIELDS  # noqa: E402
from spotify_pipeline.schemas import (  # noqa: E402
    enrichment_schema_for_count,
    validate_enrichment_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train and evaluate a fast text-only classifier from saved Codex Luna "
            "pseudo-labels without opening Golden labels until predictions are saved."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT / "data" / "analysis_10000.csv",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=(
            PROJECT_ROOT
            / "experiments"
            / "codex_luna"
            / "checkpoint-10000-normal-usage-v2"
            / "predictions.csv"
        ),
    )
    parser.add_argument(
        "--holdout",
        type=Path,
        default=PROJECT_ROOT / "data" / "checkpoint_500.csv",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=PROJECT_ROOT / "evals" / "golden_50.csv",
    )
    parser.add_argument(
        "--golden-input",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "golden_prediction_input.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "experiments" / "distilled" / "pilot-v2",
    )
    parser.add_argument("--confidence-quantile", type=float, default=0.20)
    parser.add_argument(
        "--score-golden",
        action="store_true",
        help="One-time final evaluation only. Never use this while selecting the model.",
    )
    args = parser.parse_args()

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    validate_golden_input(args.golden_input)
    artifact_path = output / "distilled_model.joblib"
    manifest_path = output / "model_manifest.json"
    started = time.perf_counter()
    training_rows, training_report = build_training_rows(
        source_path=args.source,
        labels_path=args.labels,
        excluded_source_paths=(args.holdout, args.golden_input),
    )
    manifest = train_distilled_classifier(
        rows=training_rows,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        training_report=training_report,
        config=DistilledTrainingConfig(confidence_quantile=args.confidence_quantile),
    )
    training_seconds = time.perf_counter() - started

    provider = DistilledTextProvider(
        artifact_path=artifact_path,
        expected_sha256=manifest["artifact_sha256"],
    )
    holdout_rows = read_csv_rows(args.holdout)
    holdout_predictions, holdout_seconds = predict(provider, holdout_rows)
    holdout_predictions_path = output / "holdout_500_predictions.csv"
    write_prediction_csv(holdout_predictions_path, holdout_predictions)
    holdout_teacher = read_csv_rows(
        PROJECT_ROOT
        / "experiments"
        / "codex_luna"
        / "checkpoint-500-normal-usage-v2"
        / "predictions.csv"
    )
    holdout_report = compare_predictions(holdout_rows, holdout_teacher, holdout_predictions)
    write_json(output / "holdout_500_comparison.json", holdout_report)

    golden_input_rows = read_csv_rows(args.golden_input)
    golden_predictions, golden_seconds = predict(provider, golden_input_rows)
    golden_predictions_path = output / "golden_50_predictions.csv"
    write_prediction_csv(golden_predictions_path, golden_predictions)
    golden_report: dict[str, Any] | None = None
    if args.score_golden:
        golden_report = score_golden(
            args.golden,
            golden_predictions_path,
            output / "golden_50_evaluation.json",
            output / "golden_50_cases.csv",
        )

    leakage_audit = audit_leakage(
        training_rows=training_rows,
        holdout_rows=holdout_rows,
        golden_rows=golden_input_rows,
    )
    write_json(output / "leakage_audit.json", leakage_audit)
    summary = {
        "status": "completed",
        "model_version": DISTILLED_MODEL_VERSION,
        "training_seconds": round(training_seconds, 6),
        "holdout_500_prediction_seconds": round(holdout_seconds, 6),
        "golden_50_prediction_seconds": round(golden_seconds, 6),
        "model_manifest_sha256": file_sha(manifest_path),
        "artifact_sha256": manifest["artifact_sha256"],
        "training_rows": len(training_rows),
        "holdout_500": holdout_report,
        "golden_50": golden_report,
        "golden_labels_opened": bool(args.score_golden),
        "leakage_audit": leakage_audit,
        "provider_runtime": provider.runtime_metadata(),
        "interpretation": (
            "Holdout-500 measures agreement with saved Codex Luna pseudo-labels. "
            "Golden labels are opened only when --score-golden is explicitly supplied."
        ),
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def predict(
    provider: DistilledTextProvider, rows: list[dict[str, str]], batch_size: int = 50
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    predictions: list[dict[str, Any]] = []
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset : offset + batch_size]
        model_rows = [
            {"review_id": row["review_id"], "review_text": row["review_text"]}
            for row in batch
        ]
        result = provider.call(
            role="enrich",
            model=DISTILLED_MODEL_VERSION,
            effort="none",
            developer_prompt="",
            user_payload={"reviews": model_rows},
            schema=enrichment_schema_for_count(len(batch)),
            max_output_tokens=1,
            timeout_seconds=60.0,
        )
        validated = validate_enrichment_payload(
            result.payload, {row["review_id"]: row for row in batch}
        )
        predictions.extend(validated)
    return predictions, time.perf_counter() - started


def validate_golden_input(path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if tuple(reader.fieldnames or ()) != SOURCE_FIELDS:
        raise ValueError("Golden exclusion input must contain only the six source fields")
    if len(rows) != 50:
        raise ValueError("Golden exclusion input must contain exactly 50 rows")
    manifest_path = path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("rows") != 50 or manifest.get("human_label_fields_copied") != []:
        raise ValueError("Golden exclusion manifest does not prove a label-free 50-row input")


def write_prediction_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    encoded = []
    for row in rows:
        encoded.append(
            {
                **row,
                "entities": json.dumps(row["entities"], ensure_ascii=False),
                "needs_review": str(row["needs_review"]).lower(),
            }
        )
    write_csv(
        path,
        [
            "review_id",
            "topic",
            "intent",
            "sentiment",
            "severity",
            "entities",
            "evidence_quote",
            "needs_review",
        ],
        encoded,
    )


def compare_predictions(
    source_rows: list[dict[str, str]],
    teacher_rows: list[dict[str, str]],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    source_ids = [row["review_id"] for row in source_rows]
    teacher = {row["review_id"]: row for row in teacher_rows}
    predicted = {row["review_id"]: row for row in predictions}
    if set(source_ids) != set(teacher) or set(source_ids) != set(predicted):
        raise ValueError("Holdout source, teacher, and prediction ID sets differ")
    counts = Counter()
    for review_id in source_ids:
        expected = teacher[review_id]
        actual = predicted[review_id]
        counts["topic"] += actual["topic"] == expected["topic"]
        counts["intent"] += actual["intent"] == expected["intent"]
        counts["severity"] += actual["severity"] == int(expected["severity"])
        counts["severity_error"] += abs(actual["severity"] - int(expected["severity"]))
        counts["needs_review"] += bool(actual["needs_review"])
    total = len(source_ids)
    return {
        "cases": total,
        "teacher": "saved Codex Luna pseudo-labels",
        "agreement": {
            "topic": round(counts["topic"] / total, 6),
            "intent": round(counts["intent"] / total, 6),
            "severity_exact": round(counts["severity"] / total, 6),
            "severity_mae": round(counts["severity_error"] / total, 6),
        },
        "routed_by_needs_review_fraction": round(counts["needs_review"] / total, 6),
    }


def audit_leakage(
    *,
    training_rows: list[dict[str, Any]],
    holdout_rows: list[dict[str, str]],
    golden_rows: list[dict[str, str]],
) -> dict[str, Any]:
    train_ids = {row["review_id"] for row in training_rows}
    train_texts = {row["text_sha256"] for row in training_rows}
    train_normalized_texts = {row["normalized_text_sha256"] for row in training_rows}
    holdout_ids = {row["review_id"] for row in holdout_rows}
    golden_ids = {row["review_id"] for row in golden_rows}
    holdout_texts = {text_sha256(row["review_text"]) for row in holdout_rows}
    golden_texts = {text_sha256(row["review_text"]) for row in golden_rows}
    holdout_normalized_texts = {
        text_sha256(normalize_text(row["review_text"])) for row in holdout_rows
    }
    golden_normalized_texts = {
        text_sha256(normalize_text(row["review_text"])) for row in golden_rows
    }
    result = {
        "training_review_id_overlap_with_holdout_500": len(train_ids & holdout_ids),
        "training_exact_text_overlap_with_holdout_500": len(train_texts & holdout_texts),
        "training_normalized_text_overlap_with_holdout_500": len(
            train_normalized_texts & holdout_normalized_texts
        ),
        "training_review_id_overlap_with_golden_50": len(train_ids & golden_ids),
        "training_exact_text_overlap_with_golden_50": len(train_texts & golden_texts),
        "training_normalized_text_overlap_with_golden_50": len(
            train_normalized_texts & golden_normalized_texts
        ),
    }
    result["passed"] = all(value == 0 for value in result.values())
    if not result["passed"]:
        raise RuntimeError(f"Leakage audit failed: {result}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
