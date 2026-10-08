from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import re
import tempfile
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .constants import INTENTS, TOPICS
from .io_utils import canonical, file_sha, write_json
from .providers import ProviderResult


DISTILLED_MODEL_VERSION = "distilled-tfidf-linear-v2"

_ENTITY_PATTERN = re.compile(
    r"\b(?:Spotify|Premium|CarPlay|Android\s+Auto|Alexa|Google\s+Home|"
    r"podcasts?|playlists?|lyrics|ads?|downloads?)\b",
    re.IGNORECASE,
)
_SENTENCE_PATTERN = re.compile(r"[^.!?\n]+(?:[.!?]+|\n|$)")


@dataclass(frozen=True)
class DistilledTrainingConfig:
    random_state: int = 20261002
    validation_fraction: float = 0.15
    confidence_quantile: float = 0.20
    word_max_features: int = 60_000
    char_max_features: int = 80_000

    def validate(self) -> None:
        if not 0.05 <= self.validation_fraction <= 0.40:
            raise ValueError("validation_fraction must be between 0.05 and 0.40")
        if not 0.0 <= self.confidence_quantile <= 0.50:
            raise ValueError("confidence_quantile must be between 0 and 0.50")
        if self.word_max_features <= 0 or self.char_max_features <= 0:
            raise ValueError("feature limits must be positive")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def build_training_rows(
    *,
    source_path: str | Path,
    labels_path: str | Path,
    excluded_source_paths: Iterable[str | Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join saved Luna labels to source text and exclude held-out exact texts.

    Exclusion is by both source ID and exact-text hash. The exact-text rule
    prevents a duplicate review under a different ID from leaking into an
    evaluation split.
    """

    source_rows = read_csv_rows(source_path)
    label_rows = read_csv_rows(labels_path)
    labels_by_id = {row["review_id"]: row for row in label_rows}
    if len(labels_by_id) != len(label_rows):
        raise ValueError("Duplicate review_id in saved Luna labels")

    excluded_ids: set[str] = set()
    excluded_text_hashes: set[str] = set()
    excluded_normalized_hashes: set[str] = set()
    exclusion_files: list[dict[str, Any]] = []
    for raw_path in excluded_source_paths:
        path = Path(raw_path)
        rows = read_csv_rows(path)
        excluded_ids.update(row["review_id"] for row in rows)
        excluded_text_hashes.update(text_sha256(row["review_text"]) for row in rows)
        excluded_normalized_hashes.update(
            text_sha256(normalize_text(row["review_text"])) for row in rows
        )
        exclusion_files.append(
            {"path": str(path.resolve()), "sha256": file_sha(path), "rows": len(rows)}
        )

    candidates: list[dict[str, Any]] = []
    excluded_by_id = 0
    excluded_by_text = 0
    excluded_by_normalized_text = 0
    for source in source_rows:
        review_id = source["review_id"]
        label = labels_by_id.get(review_id)
        if label is None:
            raise ValueError(f"Missing saved Luna label for review_id={review_id}")
        text = source["review_text"]
        digest = text_sha256(text)
        normalized_digest = text_sha256(normalize_text(text))
        if review_id in excluded_ids:
            excluded_by_id += 1
            continue
        if digest in excluded_text_hashes:
            excluded_by_text += 1
            continue
        if normalized_digest in excluded_normalized_hashes:
            excluded_by_normalized_text += 1
            continue
        candidates.append(
            {
                "review_id": review_id,
                "review_text": text,
                "text_sha256": digest,
                "normalized_text_sha256": normalized_digest,
                "topic": label["topic"],
                "intent": label["intent"],
                "severity": int(label["severity"]),
                "sentiment": float(label["sentiment"]),
                "needs_review": str(label["needs_review"]).strip().lower() == "true",
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        grouped.setdefault(row["normalized_text_sha256"], []).append(row)
    joined: list[dict[str, Any]] = []
    conflicting_groups = 0
    conflicting_rows = 0
    duplicate_training_texts = 0
    for digest, group in sorted(grouped.items()):
        label_tuples = {
            (row["topic"], row["intent"], row["severity"], row["needs_review"])
            for row in group
        }
        if len(label_tuples) != 1:
            conflicting_groups += 1
            conflicting_rows += len(group)
            continue
        joined.append(min(group, key=lambda row: row["review_id"]))
        duplicate_training_texts += len(group) - 1

    if not joined:
        raise ValueError("No training rows remain after exclusions")
    if any(row["topic"] not in TOPICS for row in joined):
        raise ValueError("Training data contains an invalid topic")
    if any(row["intent"] not in INTENTS for row in joined):
        raise ValueError("Training data contains an invalid intent")

    report = {
        "source_path": str(Path(source_path).resolve()),
        "source_sha256": file_sha(source_path),
        "labels_path": str(Path(labels_path).resolve()),
        "labels_sha256": file_sha(labels_path),
        "source_rows": len(source_rows),
        "label_rows": len(label_rows),
        "training_rows": len(joined),
        "excluded_by_review_id": excluded_by_id,
        "excluded_by_exact_text_after_id_exclusion": excluded_by_text,
        "excluded_by_normalized_text_after_exact_exclusion": excluded_by_normalized_text,
        "duplicate_training_normalized_texts_removed": duplicate_training_texts,
        "conflicting_normalized_label_groups_removed": conflicting_groups,
        "conflicting_normalized_label_rows_removed": conflicting_rows,
        "excluded_files": exclusion_files,
        "topic_distribution": dict(sorted(Counter(row["topic"] for row in joined).items())),
        "intent_distribution": dict(sorted(Counter(row["intent"] for row in joined).items())),
        "severity_distribution": {
            str(key): value
            for key, value in sorted(Counter(row["severity"] for row in joined).items())
        },
        "teacher": "saved Codex Luna classifications; pseudo-labels, not human ground truth",
    }
    return joined, report


def train_distilled_classifier(
    *,
    rows: list[dict[str, Any]],
    artifact_path: str | Path,
    manifest_path: str | Path,
    training_report: dict[str, Any],
    config: DistilledTrainingConfig | None = None,
) -> dict[str, Any]:
    config = config or DistilledTrainingConfig()
    config.validate()
    try:
        import joblib
        import sklearn
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.isotonic import IsotonicRegression
        from sklearn.metrics import accuracy_score, mean_absolute_error
        from sklearn.model_selection import StratifiedKFold, train_test_split
        from sklearn.pipeline import FeatureUnion
    except ImportError as error:
        raise RuntimeError(
            "Distilled classification requires the optional ML dependencies. "
            "Install with: python -m pip install -e '.[distilled]'"
        ) from error

    texts = [row["review_text"] for row in rows]
    indices = list(range(len(rows)))
    train_indices, validation_indices = train_test_split(
        indices,
        test_size=config.validation_fraction,
        random_state=config.random_state,
        stratify=[row["topic"] for row in rows],
    )

    def make_vectorizer() -> Any:
        return FeatureUnion(
            [
                (
                    "word",
                    TfidfVectorizer(
                        lowercase=True,
                        strip_accents="unicode",
                        ngram_range=(1, 2),
                        min_df=2,
                        max_df=0.995,
                        max_features=config.word_max_features,
                        sublinear_tf=True,
                    ),
                ),
                (
                    "char",
                    TfidfVectorizer(
                        analyzer="char_wb",
                        lowercase=True,
                        ngram_range=(3, 5),
                        min_df=2,
                        max_features=config.char_max_features,
                        sublinear_tf=True,
                    ),
                ),
            ]
        )

    def make_classifier() -> Any:
        return LogisticRegression(
            C=4.0,
            class_weight="balanced",
            max_iter=800,
            random_state=config.random_state,
            solver="lbfgs",
        )

    # Error probability is calibrated only from out-of-fold teacher predictions.
    # External checkpoint_500 and Golden labels are not inputs to this function.
    calibration_started = time.perf_counter()
    fold = StratifiedKFold(n_splits=5, shuffle=True, random_state=config.random_state)
    oof_rows: list[tuple[float, str, int]] = []
    topics = [str(row["topic"]) for row in rows]
    for train_fold, test_fold in fold.split(indices, topics):
        fold_vectorizer = make_vectorizer()
        x_fold_train = fold_vectorizer.fit_transform([texts[index] for index in train_fold])
        x_fold_test = fold_vectorizer.transform([texts[index] for index in test_fold])
        fold_models: dict[str, Any] = {}
        fold_probabilities: dict[str, Any] = {}
        fold_margins: dict[str, Any] = {}
        for target in ("topic", "intent", "severity"):
            model = make_classifier()
            model.fit(x_fold_train, [str(rows[index][target]) for index in train_fold])
            probabilities = model.predict_proba(x_fold_test)
            import numpy as np

            ordered = np.sort(probabilities, axis=1)
            fold_models[target] = model
            fold_probabilities[target] = probabilities
            fold_margins[target] = ordered[:, -1] - ordered[:, -2]
        for local_index, source_index in enumerate(test_fold):
            predicted = joint_decode(
                topic_classes=[str(value) for value in fold_models["topic"].classes_],
                topic_probabilities=fold_probabilities["topic"][local_index],
                intent_classes=[str(value) for value in fold_models["intent"].classes_],
                intent_probabilities=fold_probabilities["intent"][local_index],
                severity_classes=[int(str(value)) for value in fold_models["severity"].classes_],
                severity_probabilities=fold_probabilities["severity"][local_index],
            )
            truth = (
                str(rows[source_index]["topic"]),
                str(rows[source_index]["intent"]),
                int(rows[source_index]["severity"]),
            )
            mean_margin = sum(
                float(fold_margins[target][local_index])
                for target in ("topic", "intent", "severity")
            ) / 3.0
            oof_rows.append(
                (mean_margin, _word_bucket(texts[source_index]), int(predicted != truth))
            )
    global_calibrator = IsotonicRegression(increasing=False, out_of_bounds="clip")
    global_calibrator.fit(
        [row[0] for row in oof_rows], [row[2] for row in oof_rows]
    )
    bucket_calibrators: dict[str, Any] = {}
    for bucket in sorted({row[1] for row in oof_rows}):
        bucket_rows = [row for row in oof_rows if row[1] == bucket]
        if len(bucket_rows) >= 30 and len({row[2] for row in bucket_rows}) == 2:
            calibrator = IsotonicRegression(increasing=False, out_of_bounds="clip")
            calibrator.fit(
                [row[0] for row in bucket_rows], [row[2] for row in bucket_rows]
            )
            bucket_calibrators[bucket] = calibrator
    calibration_report = {
        "method": "5-fold out-of-fold isotonic",
        "features": ["mean_margin", "word_count_bucket"],
        "rows": len(oof_rows),
        "folds": 5,
        "word_buckets": sorted(bucket_calibrators),
        "observed_any_label_error_rate": round(
            sum(row[2] for row in oof_rows) / len(oof_rows), 6
        ),
        "seconds": round(time.perf_counter() - calibration_started, 6),
        "external_checkpoint_used": False,
        "golden_labels_used": False,
    }

    validation_vectorizer = make_vectorizer()
    validation_train_texts = [texts[index] for index in train_indices]
    validation_texts = [texts[index] for index in validation_indices]
    x_train = validation_vectorizer.fit_transform(validation_train_texts)
    x_validation = validation_vectorizer.transform(validation_texts)
    validation_metrics: dict[str, Any] = {}
    validation_confidences: list[list[float]] = []
    for target in ("topic", "intent", "severity"):
        classifier = make_classifier()
        y_train = [str(rows[index][target]) for index in train_indices]
        y_validation = [str(rows[index][target]) for index in validation_indices]
        classifier.fit(x_train, y_train)
        predicted = classifier.predict(x_validation)
        probabilities = classifier.predict_proba(x_validation)
        validation_confidences.append(probabilities.max(axis=1).tolist())
        validation_metrics[f"{target}_accuracy"] = round(
            float(accuracy_score(y_validation, predicted)), 6
        )
    sentiment_model = Ridge(alpha=4.0)
    sentiment_model.fit(x_train, [rows[index]["sentiment"] for index in train_indices])
    sentiment_prediction = sentiment_model.predict(x_validation)
    validation_metrics["sentiment_mae"] = round(
        float(
            mean_absolute_error(
                [rows[index]["sentiment"] for index in validation_indices],
                sentiment_prediction,
            )
        ),
        6,
    )
    minimum_confidences = [
        min(values) for values in zip(*validation_confidences, strict=True)
    ]
    confidence_threshold = _quantile(minimum_confidences, config.confidence_quantile)
    validation_metrics["confidence_threshold"] = round(confidence_threshold, 8)
    validation_metrics["validation_rows"] = len(validation_indices)
    validation_metrics["threshold_quantile"] = config.confidence_quantile

    vectorizer = make_vectorizer()
    x_all = vectorizer.fit_transform(texts)
    classifiers: dict[str, Any] = {}
    for target in ("topic", "intent", "severity", "needs_review"):
        classifier = make_classifier()
        classifier.fit(x_all, [str(row[target]) for row in rows])
        classifiers[target] = classifier
    sentiment_model = Ridge(alpha=4.0)
    sentiment_model.fit(x_all, [row["sentiment"] for row in rows])

    artifact = {
        "model_version": DISTILLED_MODEL_VERSION,
        "vectorizer": vectorizer,
        "classifiers": classifiers,
        "sentiment_model": sentiment_model,
        "confidence_threshold": confidence_threshold,
        "error_calibrator_global": global_calibrator,
        "error_calibrators_by_word_bucket": bucket_calibrators,
        "training_text_hashes": sorted(row["text_sha256"] for row in rows),
        "training_review_ids": sorted(row["review_id"] for row in rows),
    }
    artifact_path = Path(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=artifact_path.name + ".", dir=artifact_path.parent, delete=False
    ) as handle:
        temporary_path = Path(handle.name)
    try:
        joblib.dump(artifact, temporary_path, compress=3)
        os.replace(temporary_path, artifact_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    manifest = {
        "model_version": DISTILLED_MODEL_VERSION,
        "artifact_path": str(artifact_path.resolve()),
        "artifact_sha256": file_sha(artifact_path),
        "created_at_utc": _utc_now(),
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "training_config": config.__dict__,
        "training_report": training_report,
        "validation_metrics_against_held-out_luna_pseudo_labels": validation_metrics,
        "oof_error_calibration": calibration_report,
        "leakage_controls": {
            "excluded_by_review_id": True,
            "excluded_by_exact_text_sha256": True,
            "excluded_by_normalized_text_sha256": True,
            "exclusion_files": training_report["excluded_files"],
            "golden_labels_used_for_training_or_thresholds": False,
        },
    }
    write_json(manifest_path, manifest)
    return manifest


class DistilledTextProvider:
    """Fast local text-only classifier compatible with the Provider protocol."""

    name = "local-distilled-sklearn"

    def __init__(self, *, artifact_path: str | Path, expected_sha256: str | None = None) -> None:
        self.artifact_path = Path(artifact_path)
        if not self.artifact_path.is_file():
            raise RuntimeError(f"Distilled model artifact was not found: {self.artifact_path}")
        self.artifact_sha256 = file_sha(self.artifact_path)
        if expected_sha256 and self.artifact_sha256 != expected_sha256:
            raise RuntimeError("Distilled model artifact SHA-256 does not match the manifest")
        try:
            import joblib
        except ImportError as error:
            raise RuntimeError(
                "Distilled classification requires the optional ML dependencies"
            ) from error
        self._artifact = joblib.load(self.artifact_path)
        if self._artifact.get("model_version") != DISTILLED_MODEL_VERSION:
            raise RuntimeError("Unsupported distilled model artifact version")
        self._call_count = 0
        self._record_count = 0
        self._duration_seconds = 0.0

    def call(
        self,
        *,
        role: str,
        model: str,
        effort: str,
        developer_prompt: str,
        user_payload: dict[str, Any],
        schema: dict[str, Any],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult:
        del developer_prompt, schema, max_output_tokens
        if role not in {"enrich", "verify"}:
            raise ValueError("DistilledTextProvider supports only enrich and verify roles")
        if model != DISTILLED_MODEL_VERSION:
            raise ValueError(
                f"model must equal {DISTILLED_MODEL_VERSION!r} for this provider"
            )
        reviews = user_payload.get("reviews")
        if not isinstance(reviews, list) or not 1 <= len(reviews) <= 50:
            raise ValueError("Distilled enrichment requires between 1 and 50 reviews")
        for item in reviews:
            if not isinstance(item, dict) or set(item) != {"review_id", "review_text"}:
                raise ValueError("Each review must contain only review_id and review_text")
            if not all(isinstance(item[key], str) for key in ("review_id", "review_text")):
                raise ValueError("review_id and review_text must be strings")

        started = time.perf_counter()
        texts = [item["review_text"] for item in reviews]
        x = self._artifact["vectorizer"].transform(texts)
        classifiers = self._artifact["classifiers"]
        predictions: dict[str, Any] = {}
        probability_rows: dict[str, Any] = {}
        confidences: dict[str, list[float]] = {}
        margins: dict[str, list[float]] = {}
        for target in ("topic", "intent", "severity", "needs_review"):
            classifier = classifiers[target]
            predictions[target] = classifier.predict(x)
            probabilities = classifier.predict_proba(x)
            probability_rows[target] = probabilities
            try:
                import numpy as np

                ordered = np.sort(probabilities, axis=1)
            except ImportError as error:  # pragma: no cover - sklearn already requires numpy
                raise RuntimeError("numpy is required for distilled diagnostics") from error
            confidences[target] = ordered[:, -1].tolist()
            margins[target] = (ordered[:, -1] - ordered[:, -2]).tolist()
        sentiments = self._artifact["sentiment_model"].predict(x)
        threshold = float(self._artifact["confidence_threshold"])
        records: list[dict[str, Any]] = []
        minimum_confidences: list[float] = []
        mean_margins: list[float] = []
        review_diagnostics: dict[str, dict[str, float]] = {}
        for index, item in enumerate(reviews):
            text = item["review_text"]
            minimum_confidence = min(
                confidences[target][index] for target in ("topic", "intent", "severity")
            )
            minimum_confidences.append(minimum_confidence)
            mean_margin = sum(
                margins[target][index] for target in ("topic", "intent", "severity")
            ) / 3.0
            mean_margins.append(mean_margin)
            topic, intent, severity = joint_decode(
                topic_classes=[str(value) for value in classifiers["topic"].classes_],
                topic_probabilities=probability_rows["topic"][index],
                intent_classes=[str(value) for value in classifiers["intent"].classes_],
                intent_probabilities=probability_rows["intent"][index],
                severity_classes=[int(str(value)) for value in classifiers["severity"].classes_],
                severity_probabilities=probability_rows["severity"][index],
            )
            diagnostics = {
                "topic_confidence": float(confidences["topic"][index]),
                "topic_margin": float(margins["topic"][index]),
                "intent_confidence": float(confidences["intent"][index]),
                "intent_margin": float(margins["intent"][index]),
                "severity_confidence": float(confidences["severity"][index]),
                "severity_margin": float(margins["severity"][index]),
                "minimum_confidence": float(minimum_confidence),
                "minimum_margin": float(
                    min(margins[target][index] for target in ("topic", "intent", "severity"))
                ),
                "mean_margin": float(mean_margin),
                "p_err": _predict_error_probability(
                    self._artifact, mean_margin, _word_bucket(text)
                ),
            }
            review_diagnostics[item["review_id"]] = diagnostics
            predicted_needs_review = str(predictions["needs_review"][index]) == "True"
            record = {
                    "review_id": item["review_id"],
                    "topic": topic,
                    "intent": intent,
                    "sentiment": round(max(-1.0, min(1.0, float(sentiments[index]))), 4),
                    "severity": severity,
                    "entities": _extract_entities(text),
                    "evidence_quote": _select_evidence_quote(
                        text,
                        topic,
                        self._artifact["vectorizer"],
                        classifiers["topic"],
                    ),
                    "needs_review": bool(
                        predicted_needs_review or minimum_confidence < threshold
                    ),
                }
            if role == "verify":
                record["quote_supported"] = True
            records.append(record)
        duration = time.perf_counter() - started
        if duration > timeout_seconds:
            raise TimeoutError(
                f"Distilled classification exceeded the configured {timeout_seconds:.3f}s timeout"
            )
        request_id = f"local-distilled-{uuid.uuid4()}"
        call_metadata = {
            "request_id": request_id,
            "records": len(records),
            "duration_seconds": round(duration, 6),
            "minimum_confidence_min": round(min(minimum_confidences), 6),
            "minimum_confidence_mean": round(
                sum(minimum_confidences) / len(minimum_confidences), 6
            ),
            "mean_margin_min": round(min(mean_margins), 6),
            "mean_margin_mean": round(sum(mean_margins) / len(mean_margins), 6),
            "confidence_threshold": round(threshold, 8),
            "review_diagnostics": review_diagnostics,
        }
        self._call_count += 1
        self._record_count += len(records)
        self._duration_seconds += duration
        return ProviderResult(
            request_id=request_id,
            payload={"records": records},
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            duration_seconds=duration,
            provider=self.name,
            billing_mode="local",
            usage_status="not_applicable",
            provider_metadata=call_metadata,
        )

    def runtime_metadata(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model_version": DISTILLED_MODEL_VERSION,
            "artifact_path": str(self.artifact_path.resolve()),
            "artifact_sha256": self.artifact_sha256,
            "network_required": False,
            "api_credentials_required": False,
            "api_charges_usd": 0.0,
            "call_count": self._call_count,
            "record_count": self._record_count,
            "duration_seconds": round(self._duration_seconds, 6),
        }


def _extract_entities(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in _ENTITY_PATTERN.finditer(text):
        value = match.group(0)
        key = value.casefold()
        if key not in seen:
            values.append(value)
            seen.add(key)
        if len(values) == 8:
            break
    return values


def joint_decode(
    *,
    topic_classes: list[str],
    topic_probabilities: Any,
    intent_classes: list[str],
    intent_probabilities: Any,
    severity_classes: list[int],
    severity_probabilities: Any,
    complaint_severity_one_penalty: float = 2.5,
    cancellation_severity_one_penalty: float = 1.0,
) -> tuple[str, str, int]:
    """Decode the most likely valid topic × intent × severity combination."""

    epsilon = 1e-12
    best: tuple[float, str, str, int] | None = None
    for topic_index, topic in enumerate(topic_classes):
        for intent_index, intent in enumerate(intent_classes):
            for severity_index, severity in enumerate(severity_classes):
                if intent in {"praise", "request", "unclear"} and severity != 1:
                    continue
                score = (
                    math.log(max(epsilon, float(topic_probabilities[topic_index])))
                    + math.log(max(epsilon, float(intent_probabilities[intent_index])))
                    + math.log(max(epsilon, float(severity_probabilities[severity_index])))
                )
                if intent == "complaint" and severity == 1:
                    score -= complaint_severity_one_penalty
                if intent == "cancellation" and severity == 1:
                    score -= cancellation_severity_one_penalty
                candidate = (score, topic, intent, severity)
                if best is None or candidate > best:
                    best = candidate
    if best is None:
        raise ValueError("No valid joint label combination")
    return best[1], best[2], int(best[3])


def _select_evidence_quote(text: str, topic: str, vectorizer: Any, topic_model: Any) -> str:
    """Return a source substring; score sentences only for long reviews."""

    if len(text.split()) <= 25 and len(text) <= 200:
        return text
    candidates = [match.group(0).strip() for match in _SENTENCE_PATTERN.finditer(text)]
    candidates = [candidate for candidate in candidates if candidate and candidate in text]
    if not candidates:
        return text
    matrix = vectorizer.transform(candidates)
    scores = topic_model.decision_function(matrix)
    classes = [str(value) for value in topic_model.classes_]
    if topic not in classes:
        return candidates[0]
    class_index = classes.index(topic)
    if getattr(scores, "ndim", 1) == 1:
        selected = max(range(len(candidates)), key=lambda index: float(scores[index]))
    else:
        selected = max(
            range(len(candidates)), key=lambda index: float(scores[index, class_index])
        )
    return candidates[selected]


def _word_bucket(text: str) -> str:
    words = len(text.split())
    if words <= 5:
        return "00-05"
    if words <= 15:
        return "06-15"
    if words <= 40:
        return "16-40"
    return "41+"


def _predict_error_probability(artifact: dict[str, Any], mean_margin: float, bucket: str) -> float:
    calibrators = artifact.get("error_calibrators_by_word_bucket") or {}
    calibrator = calibrators.get(bucket) or artifact.get("error_calibrator_global")
    if calibrator is None:
        return float(max(0.0, min(1.0, 1.0 - mean_margin)))
    value = float(calibrator.predict([float(mean_margin)])[0])
    return max(0.0, min(1.0, value))


def _quantile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a quantile of an empty list")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
