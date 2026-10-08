from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .constants import INTENTS, TOPICS


ENRICHMENT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "review_id": {"type": "string"},
        "topic": {"type": "string", "enum": list(TOPICS)},
        "intent": {"type": "string", "enum": list(INTENTS)},
        "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
        "entities": {"type": "array", "items": {"type": "string"}},
        "evidence_quote": {"type": "string"},
        "needs_review": {"type": "boolean"},
    },
    "required": [
        "review_id",
        "topic",
        "intent",
        "sentiment",
        "severity",
        "entities",
        "evidence_quote",
        "needs_review",
    ],
}

ENRICHMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"records": {"type": "array", "items": ENRICHMENT_ITEM_SCHEMA}},
    "required": ["records"],
}

VERIFICATION_ITEM_SCHEMA: dict[str, Any] = deepcopy(ENRICHMENT_ITEM_SCHEMA)
VERIFICATION_ITEM_SCHEMA["properties"]["quote_supported"] = {"type": "boolean"}
VERIFICATION_ITEM_SCHEMA["required"] = [
    *ENRICHMENT_ITEM_SCHEMA["required"],
    "quote_supported",
]


def verification_schema_for_count(expected_count: int) -> dict[str, Any]:
    if not 1 <= expected_count <= 50:
        raise ValueError("expected_count must be between 1 and 50")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "records": {
                "type": "array",
                "items": deepcopy(VERIFICATION_ITEM_SCHEMA),
                "minItems": expected_count,
                "maxItems": expected_count,
            }
        },
        "required": ["records"],
    }


def repair_and_validate_verification_payload(
    payload: dict[str, Any], source_by_id: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], list[str]]:
    copied = deepcopy(payload)
    records = copied.get("records") if isinstance(copied, dict) else None
    if not isinstance(records, list):
        raise ValueError("Verification response must contain records")
    flags: dict[str, bool] = {}
    enrichment_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != set(VERIFICATION_ITEM_SCHEMA["required"]):
            raise ValueError("Each verification record must match the exact schema")
        if type(record["quote_supported"]) is not bool:
            raise ValueError("quote_supported must be boolean")
        flags[str(record["review_id"])] = bool(record.pop("quote_supported"))
        enrichment_records.append(record)
    values, repaired = repair_and_validate_enrichment_payload(
        {"records": enrichment_records}, source_by_id
    )
    for value in values:
        value["quote_supported"] = flags[str(value["review_id"])]
    return values, repaired


def enrichment_schema_for_count(expected_count: int) -> dict[str, Any]:
    """Return the enrichment schema with an exact batch cardinality.

    The generic schema remains available for fixtures and static inspection.
    Real provider calls use this bounded form so structured generation cannot
    silently omit an otherwise valid review from a large batch.
    """
    if not 1 <= expected_count <= 50:
        raise ValueError("expected_count must be between 1 and 50")
    schema = deepcopy(ENRICHMENT_SCHEMA)
    records = schema["properties"]["records"]
    records["minItems"] = expected_count
    records["maxItems"] = expected_count
    return schema


def compact_review_batch(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]], dict[str, str]]:
    """Use deterministic, short batch-local keys for model I/O."""
    if not 1 <= len(rows) <= 50:
        raise ValueError("review batch must contain between 1 and 50 rows")
    model_rows: list[dict[str, str]] = []
    source_by_alias: dict[str, dict[str, Any]] = {}
    alias_to_source_id: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        alias = f"R{index:03d}"
        source_id = str(row["review_id"])
        model_rows.append({"review_id": alias, "review_text": str(row["review_text"])})
        source_by_alias[alias] = {**row, "review_id": alias}
        alias_to_source_id[alias] = source_id
    return model_rows, source_by_alias, alias_to_source_id


def restore_source_review_ids(
    records: list[dict[str, Any]], alias_to_source_id: dict[str, str]
) -> list[dict[str, Any]]:
    """Restore authoritative source IDs after validating model output."""
    restored = deepcopy(records)
    for record in restored:
        alias = record["review_id"]
        if alias not in alias_to_source_id:
            raise ValueError(f"Unknown batch-local review_id: {alias}")
        record["review_id"] = alias_to_source_id[alias]
    return restored

GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "issue_id": {"type": "string"},
                    "issue_name": {"type": "string"},
                    "description": {"type": "string"},
                    "coherent": {"type": "string", "enum": ["yes", "no"]},
                    "actionable": {"type": "string", "enum": ["yes", "no"]},
                    "heterogeneous_review_ids": {
                        "type": "array", "items": {"type": "string"}
                    },
                },
                "required": [
                    "issue_id", "issue_name", "description", "coherent", "actionable",
                    "heterogeneous_review_ids"
                ],
            },
        }
    },
    "required": ["issues"],
}

MEMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "priority_topic": {
            "type": "string",
            "enum": ["access", "usability", "playback", "billing", "support"],
        },
        "priority_issue_id": {"type": "string"},
        "priority": {"type": "string"},
        "rationale": {"type": "string"},
        "alternative_issue_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
        },
        "alternatives": {
            "type": "array", "items": {"type": "string"}, "minItems": 2
        },
        "baseline_top_handling": {"type": "string"},
        "sensitivity_interpretation": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "claim_id": {"type": "string"},
                    "issue_id": {"type": "string"},
                    "metric": {
                        "type": "string",
                        "enum": [
                            "complaint_count",
                            "severity_sum",
                            "mean_severity",
                            "priority_score",
                        ],
                    },
                    "value": {"type": "string"},
                    "sentence": {"type": "string"},
                    "review_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "claim_id",
                    "issue_id",
                    "metric",
                    "value",
                    "sentence",
                    "review_ids",
                ],
            },
        },
    },
    "required": [
        "priority_topic", "priority_issue_id", "priority", "rationale",
        "alternative_issue_ids", "alternatives", "baseline_top_handling",
        "sensitivity_interpretation", "limitations", "claims",
    ],
}


def validate_enrichment_payload(
    payload: dict[str, Any],
    source_by_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {"records"}:
        raise ValueError("Response must be an object containing only records")
    records = payload["records"]
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    expected_ids = set(source_by_id)
    received_ids: list[str] = []
    validated: list[dict[str, Any]] = []
    required = set(ENRICHMENT_ITEM_SCHEMA["required"])
    for record in records:
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("Each enrichment record must match the exact schema")
        review_id = record["review_id"]
        received_ids.append(review_id)
        if review_id not in source_by_id:
            raise ValueError(f"Unknown returned review_id: {review_id}")
        if record["topic"] not in TOPICS or record["intent"] not in INTENTS:
            raise ValueError(f"Invalid label for {review_id}")
        if type(record["severity"]) is not int or not 1 <= record["severity"] <= 5:
            raise ValueError(f"Invalid severity for {review_id}")
        sentiment = record["sentiment"]
        if type(sentiment) not in (int, float) or not math.isfinite(sentiment) or not -1 <= sentiment <= 1:
            raise ValueError(f"Invalid sentiment for {review_id}")
        if type(record["needs_review"]) is not bool:
            raise ValueError(f"Invalid needs_review for {review_id}")
        if not isinstance(record["entities"], list) or any(
            not isinstance(entity, str) or not entity.strip() for entity in record["entities"]
        ):
            raise ValueError(f"Invalid entities for {review_id}")
        quote = record["evidence_quote"]
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError(f"Empty evidence quote for {review_id}")
        if quote not in source_by_id[review_id]["review_text"]:
            raise ValueError(f"Evidence quote is not an exact source substring for {review_id}")
        validated.append(record)
    if len(received_ids) != len(set(received_ids)):
        raise ValueError("Duplicate review_id returned")
    if set(received_ids) != expected_ids:
        missing = sorted(expected_ids - set(received_ids))
        extra = sorted(set(received_ids) - expected_ids)
        raise ValueError(f"Returned IDs differ; missing={missing[:5]} extra={extra[:5]}")
    return validated


def repair_and_validate_enrichment_payload(
    payload: dict[str, Any],
    source_by_id: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Repair only ungrounded evidence quotes, then run the strict validator.

    A model-normalized or translated quote is not accepted as evidence. The
    deterministic fallback is the exact original review text, and the record is
    marked for human review. A quote that belongs to a *different* review is
    never repaired: that indicates ID/record misalignment and the caller must
    retry or split the batch. All IDs, labels, ranges, and schema fields remain
    subject to the ordinary strict validation path.
    """
    repaired = deepcopy(payload)
    repaired_ids: list[str] = []
    records = repaired.get("records") if isinstance(repaired, dict) else None
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            review_id = record.get("review_id")
            if review_id not in source_by_id:
                continue
            source_text = source_by_id[review_id]["review_text"]
            quote = record.get("evidence_quote")
            if not isinstance(quote, str) or not quote.strip() or quote not in source_text:
                if isinstance(quote, str) and quote.strip():
                    other_owners = [
                        candidate_id
                        for candidate_id, source in source_by_id.items()
                        if candidate_id != review_id and quote in source["review_text"]
                    ]
                    if other_owners:
                        raise ValueError(
                            "Evidence quote belongs to a different review; "
                            f"returned_id={review_id} candidate_ids={other_owners[:5]}"
                        )
                record["evidence_quote"] = source_text
                record["needs_review"] = True
                repaired_ids.append(review_id)
    return validate_enrichment_payload(repaired, source_by_id), repaired_ids


def validate_enrichment_payload_partial(
    payload: dict[str, Any], source_by_id: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Admit independently valid records and identify only IDs that need one retry."""
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return [], {review_id: "missing records array" for review_id in source_by_id}, []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("review_id"), str):
            by_id.setdefault(record["review_id"], []).append(record)
    valid: list[dict[str, Any]] = []
    invalid: dict[str, str] = {}
    repaired: list[str] = []
    for review_id, source in source_by_id.items():
        candidates = by_id.get(review_id, [])
        if len(candidates) != 1:
            invalid[review_id] = "missing or duplicate returned review_id"
            continue
        try:
            values, repaired_ids = repair_and_validate_enrichment_payload(
                {"records": candidates}, {review_id: source}
            )
            valid.extend(values)
            repaired.extend(repaired_ids)
        except Exception as error:
            invalid[review_id] = f"{type(error).__name__}: {error}"
    return valid, invalid, repaired
