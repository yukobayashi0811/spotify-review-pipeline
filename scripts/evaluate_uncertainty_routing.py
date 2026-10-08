from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error  # noqa: E402

from spotify_pipeline.distilled import (  # noqa: E402
    DISTILLED_MODEL_VERSION,
    DistilledTextProvider,
    read_csv_rows,
)
from spotify_pipeline.hybrid import (  # noqa: E402
    RoutingPolicy,
    _routing_priority,
    escalation_reasons,
    repair_local_consistency,
)
from spotify_pipeline.io_utils import file_sha, write_csv, write_json  # noqa: E402
from spotify_pipeline.schemas import enrichment_schema_for_count  # noqa: E402


SPLIT_SEED = "routing-calibration-v1"
TARGETS = ("topic", "intent", "severity")
STRATEGIES = ("minimum_confidence", "minimum_margin", "mean_margin")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Select a label-free uncertainty routing rule on a fixed calibration split "
            "and compare it with legacy risk routing. Golden labels are never opened."
        )
    )
    parser.add_argument(
        "--source", type=Path, default=PROJECT_ROOT / "data" / "checkpoint_500.csv"
    )
    parser.add_argument(
        "--teacher",
        type=Path,
        default=(
            PROJECT_ROOT
            / "experiments"
            / "codex_luna"
            / "checkpoint-500-normal-usage-v2"
            / "predictions.csv"
        ),
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=(
            PROJECT_ROOT
            / "experiments"
            / "distilled"
            / "pilot-v1"
            / "distilled_model.joblib"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "experiments" / "distilled" / "routing-v2",
    )
    parser.add_argument("--calibration-fraction", type=float, default=0.50)
    parser.add_argument("--escalation-fraction", type=float, default=0.20)
    args = parser.parse_args()
    if not 0.1 <= args.calibration_fraction <= 0.9:
        raise SystemExit("--calibration-fraction must be between 0.1 and 0.9")
    if not 0.0 <= args.escalation_fraction <= 1.0:
        raise SystemExit("--escalation-fraction must be between 0 and 1")

    source = read_csv_rows(args.source)
    teacher_rows = read_csv_rows(args.teacher)
    if len(source) != 500:
        raise ValueError("The routing development source must contain exactly 500 rows")
    teacher = {row["review_id"]: row for row in teacher_rows}
    source_ids = [row["review_id"] for row in source]
    if len(teacher) != len(teacher_rows) or set(teacher) != set(source_ids):
        raise ValueError("Source and saved Luna teacher ID sets differ")

    provider = DistilledTextProvider(artifact_path=args.artifact)
    predictions, diagnostics = _predict(provider, source)
    prediction_by_id = {row["review_id"]: row for row in predictions}
    order = sorted(
        range(len(source)),
        key=lambda index: hashlib.sha256(
            f"{SPLIT_SEED}:{source[index]['review_id']}".encode("utf-8")
        ).hexdigest(),
    )
    calibration_count = int(len(order) * args.calibration_fraction)
    calibration = order[:calibration_count]
    audit = order[calibration_count:]

    selectors: dict[str, Callable[[int], tuple[Any, ...]]] = {
        strategy: (
            lambda index, metric=strategy: (
                diagnostics[source[index]["review_id"]][metric],
                source[index]["review_id"],
            )
        )
        for strategy in STRATEGIES
    }
    risk_policy = RoutingPolicy(
        routing_mode="risk", max_escalation_fraction=args.escalation_fraction
    )
    risk_decisions = _risk_decisions(source, prediction_by_id, risk_policy)
    selectors["legacy_risk"] = lambda index: _routing_priority(
        risk_decisions[index], risk_policy
    )

    calibration_results = {
        name: _evaluate_strategy(
            source=source,
            teacher=teacher,
            prediction_by_id=prediction_by_id,
            pool=calibration,
            key=key,
            escalation_fraction=args.escalation_fraction,
            eligible=(
                None
                if name != "legacy_risk"
                else lambda index: bool(risk_decisions[index]["route_reasons"])
            ),
        )
        for name, key in selectors.items()
    }
    selected_strategy = max(
        STRATEGIES,
        key=lambda name: (
            calibration_results[name]["selection_score_mean_macro_f1"],
            calibration_results[name]["selection_score_mean_accuracy"],
            name,
        ),
    )
    audit_results = {
        "legacy_risk": _evaluate_strategy(
            source=source,
            teacher=teacher,
            prediction_by_id=prediction_by_id,
            pool=audit,
            key=selectors["legacy_risk"],
            escalation_fraction=args.escalation_fraction,
            eligible=lambda index: bool(risk_decisions[index]["route_reasons"]),
        ),
        selected_strategy: _evaluate_strategy(
            source=source,
            teacher=teacher,
            prediction_by_id=prediction_by_id,
            pool=audit,
            key=selectors[selected_strategy],
            escalation_fraction=args.escalation_fraction,
        ),
    }

    selected_calibration = set(calibration_results[selected_strategy]["selected_indices"])
    selected_audit = set(audit_results[selected_strategy]["selected_indices"])
    case_rows: list[dict[str, Any]] = []
    calibration_set = set(calibration)
    for index, row in enumerate(source):
        review_id = row["review_id"]
        local = prediction_by_id[review_id]
        expected = teacher[review_id]
        case_rows.append(
            {
                "review_id": review_id,
                "split": "calibration" if index in calibration_set else "audit",
                **diagnostics[review_id],
                "local_topic": local["topic"],
                "teacher_topic": expected["topic"],
                "local_intent": local["intent"],
                "teacher_intent": expected["intent"],
                "local_severity": local["severity"],
                "teacher_severity": expected["severity"],
                "local_error_count": sum(
                    str(local[target]) != str(expected[target]) for target in TARGETS
                ),
                "selected_by_policy": index in selected_calibration or index in selected_audit,
            }
        )

    args.output.mkdir(parents=True, exist_ok=True)
    cases_path = args.output / "routing_cases.csv"
    write_csv(cases_path, list(case_rows[0]), case_rows)
    report = {
        "status": "completed",
        "development_only": True,
        "golden_labels_loaded": False,
        "source": {
            "path": str(args.source.resolve()),
            "sha256": file_sha(args.source),
            "rows": len(source),
        },
        "teacher": {
            "path": str(args.teacher.resolve()),
            "sha256": file_sha(args.teacher),
            "description": "saved Codex Luna pseudo-labels, not human ground truth",
        },
        "local_model": {
            "artifact_path": str(args.artifact.resolve()),
            "artifact_sha256": file_sha(args.artifact),
            "model_version": DISTILLED_MODEL_VERSION,
        },
        "split": {
            "seed": SPLIT_SEED,
            "method": "lowest SHA-256(seed + ':' + review_id) values",
            "calibration_rows": len(calibration),
            "audit_rows": len(audit),
        },
        "policy_selection": {
            "candidate_strategies": list(STRATEGIES),
            "selected_strategy": selected_strategy,
            "selection_population": "calibration split only",
            "selection_objective": (
                "maximum mean macro-F1 across topic, intent, and severity after "
                "bounded teacher replacement; mean accuracy then strategy name break ties"
            ),
            "escalation_fraction": args.escalation_fraction,
        },
        "calibration_results": _strip_indices(calibration_results),
        "audit_results": _strip_indices(audit_results),
        "artifacts": {"routing_cases": str(cases_path.resolve())},
    }
    report_path = args.output / "report.json"
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _predict(
    provider: DistilledTextProvider, source: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    predictions: list[dict[str, Any]] = []
    diagnostics: dict[str, dict[str, float]] = {}
    for offset in range(0, len(source), 50):
        batch = source[offset : offset + 50]
        response = provider.call(
            role="enrich",
            model=DISTILLED_MODEL_VERSION,
            effort="none",
            developer_prompt="",
            user_payload={
                "reviews": [
                    {"review_id": row["review_id"], "review_text": row["review_text"]}
                    for row in batch
                ]
            },
            schema=enrichment_schema_for_count(len(batch)),
            max_output_tokens=1,
            timeout_seconds=60.0,
        )
        predictions.extend(response.payload["records"])
        diagnostics.update(response.provider_metadata["review_diagnostics"])
    return predictions, diagnostics


def _risk_decisions(
    source: list[dict[str, str]],
    prediction_by_id: dict[str, dict[str, Any]],
    policy: RoutingPolicy,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for row in source:
        local, _ = repair_local_consistency(prediction_by_id[row["review_id"]])
        decisions.append(
            {
                "route_reasons": escalation_reasons(row, local, policy),
                "local_severity": local["severity"],
                "text_sha256": hashlib.sha256(
                    row["review_text"].encode("utf-8")
                ).hexdigest(),
            }
        )
    return decisions


def _evaluate_strategy(
    *,
    source: list[dict[str, str]],
    teacher: dict[str, dict[str, str]],
    prediction_by_id: dict[str, dict[str, Any]],
    pool: list[int],
    key: Callable[[int], tuple[Any, ...]],
    escalation_fraction: float,
    eligible: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    candidates = [index for index in pool if eligible is None or eligible(index)]
    capacity = int(len(pool) * escalation_fraction)
    selected = sorted(candidates, key=key)[:capacity]
    selected_set = set(selected)
    metrics: dict[str, Any] = {}
    accuracies: list[float] = []
    macro_scores: list[float] = []
    for target in TARGETS:
        expected = [str(teacher[source[index]["review_id"]][target]) for index in pool]
        actual = [
            expected[position]
            if index in selected_set
            else str(prediction_by_id[source[index]["review_id"]][target])
            for position, index in enumerate(pool)
        ]
        accuracy = float(accuracy_score(expected, actual))
        macro = float(f1_score(expected, actual, average="macro", zero_division=0))
        metrics[target] = {
            "accuracy": round(accuracy, 6),
            "macro_f1": round(macro, 6),
        }
        accuracies.append(accuracy)
        macro_scores.append(macro)
        if target == "severity":
            metrics[target]["mae"] = round(
                float(
                    mean_absolute_error(
                        [int(value) for value in expected],
                        [int(value) for value in actual],
                    )
                ),
                6,
            )
    return {
        "rows": len(pool),
        "candidate_rows": len(candidates),
        "routing_capacity_rows": capacity,
        "selected_rows": len(selected),
        "selection_score_mean_macro_f1": round(sum(macro_scores) / len(macro_scores), 6),
        "selection_score_mean_accuracy": round(sum(accuracies) / len(accuracies), 6),
        "metrics": metrics,
        "selected_indices": selected,
    }


def _strip_indices(results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: {key: value for key, value in result.items() if key != "selected_indices"}
        for name, result in results.items()
    }


if __name__ == "__main__":
    raise SystemExit(main())
