from __future__ import annotations

import csv
import json
import math
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .constants import INTENTS, SOURCE_FIELDS, TOPICS
from .io_utils import read_csv, write_csv, write_json
from .storage import Store


GOLD_FIELDS = (
    "topic",
    "intent",
    "sentiment",
    "severity",
    "entities",
    "evidence_quote",
)


def sanitize_golden_source(golden_csv: str | Path, output_csv: str | Path) -> dict[str, Any]:
    rows = []
    for row in read_csv(golden_csv):
        rows.append({field: row[field] for field in SOURCE_FIELDS})
    write_csv(output_csv, list(SOURCE_FIELDS), rows)
    manifest = {
        "rows": len(rows),
        "classification_input_fields": ["review_text"],
        "source_fields_copied": list(SOURCE_FIELDS),
        "human_label_fields_copied": [],
        "note": "Prediction must run from this sanitized file. Scoring reads labels only after predictions are saved.",
    }
    write_json(Path(output_csv).with_suffix(".manifest.json"), manifest)
    return manifest


def export_predictions(state_path: str | Path, output_csv: str | Path) -> None:
    with Store(state_path) as store:
        rows = []
        for record in store.completed_records():
            rows.append(
                {
                    "review_id": record["review_id"],
                    "topic": record["topic"],
                    "intent": record["intent"],
                    "sentiment": record["sentiment"],
                    "severity": record["severity"],
                    "entities": json.dumps(record["entities"], ensure_ascii=False),
                    "evidence_quote": record["evidence_quote"],
                    "needs_review": str(record["needs_review"]).lower(),
                }
            )
    write_csv(output_csv, ["review_id", *GOLD_FIELDS, "needs_review"], rows)


def score_golden(
    golden_csv: str | Path,
    predictions_csv: str | Path,
    output_json: str | Path,
    cases_csv: str | Path,
) -> dict[str, Any]:
    gold = _read_gold(golden_csv)
    predictions = _read_predictions(predictions_csv)
    if len(gold) != 50:
        raise ValueError(f"Golden set must contain exactly 50 rows, found {len(gold)}")
    extra_prediction_ids = sorted(set(predictions) - set(gold))
    if extra_prediction_ids:
        raise ValueError(
            f"Predictions contain IDs outside the Golden set: {extra_prediction_ids[:5]}"
        )
    totals = Counter()
    confusions: dict[str, Counter[tuple[str, str]]] = {
        "topic": Counter(),
        "intent": Counter(),
    }
    cases = []
    normalized_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review_id, expected in gold.items():
        predicted = predictions.get(review_id)
        row: dict[str, Any] = {"review_id": review_id, "prediction_present": bool(predicted)}
        if not predicted:
            row.update(
                topic_match=False,
                intent_match=False,
                severity_match=False,
                severity_error="",
                quote_is_substring=False,
            )
            totals["missing"] += 1
        else:
            row["topic_match"] = predicted["topic"] == expected["topic"]
            row["intent_match"] = predicted["intent"] == expected["intent"]
            row["severity_match"] = predicted["severity"] == expected["severity"]
            row["severity_error"] = abs(predicted["severity"] - expected["severity"])
            row["sentiment_error"] = abs(predicted["sentiment"] - expected["sentiment"])
            row["entities_exact_match"] = predicted["entities"] == expected["entities"]
            row["quote_is_substring"] = predicted["evidence_quote"] in expected["review_text"]
            confusions["topic"][(expected["topic"], predicted["topic"])] += 1
            confusions["intent"][(expected["intent"], predicted["intent"])] += 1
            totals["topic_correct"] += row["topic_match"]
            totals["intent_correct"] += row["intent_match"]
            totals["severity_correct"] += row["severity_match"]
            totals["severity_error_sum"] += row["severity_error"]
            totals["sentiment_error_sum"] += row["sentiment_error"]
            totals["entities_correct"] += row["entities_exact_match"]
            totals["valid_predictions"] += 1
            totals["quote_membership"] += row["quote_is_substring"]
            row.update(
                expected_topic=expected["topic"],
                predicted_topic=predicted["topic"],
                expected_intent=expected["intent"],
                predicted_intent=predicted["intent"],
                expected_severity=expected["severity"],
                predicted_severity=predicted["severity"],
            )
        cases.append(row)
        normalized_groups[_normalize_text(expected["review_text"])].append(row)
    n = len(gold)
    report = {
        "status": "completed",
        "cases": n,
        "missing_predictions": totals["missing"],
        "agreement": {
            "topic": round(totals["topic_correct"] / n, 6),
            "intent": round(totals["intent_correct"] / n, 6),
            "severity_exact": round(totals["severity_correct"] / n, 6),
            "severity_mae_on_valid_predictions": round(
                totals["severity_error_sum"] / totals["valid_predictions"], 6
            )
            if totals["valid_predictions"]
            else None,
            "sentiment_mae_on_valid_predictions": round(
                totals["sentiment_error_sum"] / totals["valid_predictions"], 6
            )
            if totals["valid_predictions"]
            else None,
            "entities_exact": round(totals["entities_correct"] / n, 6),
            "evidence_quote_substring": round(totals["quote_membership"] / n, 6),
        },
        "macro_f1": {
            "topic": _macro_f1(gold, predictions, "topic"),
            "intent": _macro_f1(gold, predictions, "intent"),
            "severity": _macro_f1(gold, predictions, "severity"),
        },
        "normalized_text_group_weighted_agreement": {
            field: round(
                sum(
                    sum(bool(row.get(field)) for row in group) / len(group)
                    for group in normalized_groups.values()
                )
                / len(normalized_groups),
                6,
            )
            for field in ("topic_match", "intent_match", "severity_match")
        },
        "normalized_text_groups": len(normalized_groups),
        "confusion": {
            field: [
                {"expected": expected, "predicted": predicted, "count": count}
                for (expected, predicted), count in sorted(values.items())
            ]
            for field, values in confusions.items()
        },
        "note": "Quote membership is mechanical; a human must inspect whether the quote supports the label and whether entities are grounded.",
    }
    write_json(output_json, report)
    fields = sorted({key for row in cases for key in row})
    write_csv(cases_csv, fields, cases)
    return report


def _read_gold(path: str | Path) -> dict[str, dict[str, Any]]:
    result = {}
    rows = list(read_csv(path))
    if len(rows) != 50:
        raise ValueError(f"Golden set must contain exactly 50 rows, found {len(rows)}")
    for row in rows:
        if row["review_id"] in result:
            raise ValueError(f"Duplicate Golden review_id: {row['review_id']}")
        missing = [field for field in GOLD_FIELDS if not row.get(field, "").strip()]
        if missing:
            raise ValueError(
                f"Golden row {row['review_id']} is not human-labeled; missing {missing}. "
                "Do not substitute AI-generated labels."
            )
        topic, intent = row["topic"], row["intent"]
        if topic not in TOPICS or intent not in INTENTS:
            raise ValueError(f"Invalid human label for {row['review_id']}")
        try:
            severity = int(row["severity"])
            sentiment = float(row["sentiment"])
            entities = json.loads(row["entities"])
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid human label encoding for {row['review_id']}: {error}") from error
        if not 1 <= severity <= 5 or not -1 <= sentiment <= 1 or not isinstance(entities, list):
            raise ValueError(f"Invalid human label range for {row['review_id']}")
        if row["evidence_quote"] not in row["review_text"]:
            raise ValueError(f"Human evidence quote is not a substring for {row['review_id']}")
        result[row["review_id"]] = {
            **row,
            "severity": severity,
            "sentiment": sentiment,
            "entities": entities,
        }
    return result


def _read_predictions(path: str | Path) -> dict[str, dict[str, Any]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream, strict=True)
        result = {}
        for row in rows:
            review_id = row["review_id"]
            if review_id in result:
                raise ValueError(f"Duplicate prediction review_id: {review_id}")
            if row["topic"] not in TOPICS or row["intent"] not in INTENTS:
                raise ValueError(f"Invalid prediction label for {review_id}")
            severity = int(row["severity"])
            sentiment = float(row["sentiment"])
            entities = json.loads(row["entities"])
            if (
                not 1 <= severity <= 5
                or not math.isfinite(sentiment)
                or not -1 <= sentiment <= 1
                or not isinstance(entities, list)
                or any(not isinstance(item, str) for item in entities)
            ):
                raise ValueError(f"Invalid prediction encoding for {review_id}")
            result[row["review_id"]] = {
                **row,
                "severity": severity,
                "sentiment": sentiment,
                "entities": entities,
            }
        return result


def _macro_f1(
    expected: dict[str, dict[str, Any]],
    predicted: dict[str, dict[str, Any]],
    field: str,
) -> float:
    labels = sorted({row[field] for row in expected.values()}, key=str)
    scores = []
    for label in labels:
        true_positive = false_positive = false_negative = 0
        for review_id, gold_row in expected.items():
            predicted_value = predicted.get(review_id, {}).get(field)
            if gold_row[field] == label and predicted_value == label:
                true_positive += 1
            elif gold_row[field] != label and predicted_value == label:
                false_positive += 1
            elif gold_row[field] == label and predicted_value != label:
                false_negative += 1
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(2 * true_positive / denominator if denominator else 0.0)
    return round(sum(scores) / len(scores), 6) if scores else 0.0


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
