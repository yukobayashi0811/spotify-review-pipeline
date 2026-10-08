from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .classify import ModelConfig, _call_event, _failed_event, _failed_result_event, chunks
from .constants import PROMPT_VERSION, SCHEMA_VERSION, TOPICS
from .io_utils import write_json
from .providers import Provider, ProviderUnavailableError
from .schemas import (
    compact_review_batch,
    enrichment_schema_for_count,
    repair_and_validate_enrichment_payload,
    restore_source_review_ids,
)
from .storage import Store


def run_verification(
    *,
    state_path: str | Path,
    provider: Provider,
    prompt_path: str | Path,
    config: ModelConfig,
    run_id: str,
    output_path: str | Path,
    sample_fraction: float = 0.1,
    minimum_sample: int = 10,
    maximum_sample: int | None = None,
    seed: str = "spotify-verifier-v1",
) -> dict[str, Any]:
    if not 0 < sample_fraction <= 1:
        raise ValueError("sample_fraction must be in (0,1]")
    if maximum_sample is not None and maximum_sample < minimum_sample:
        raise ValueError("maximum_sample must be at least minimum_sample")
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    verifier_config = f"{config.model}:{config.effort}:verify-{PROMPT_VERSION}:{SCHEMA_VERSION}"
    if config.execution_fingerprint:
        verifier_config += f":{config.execution_fingerprint}"
    with Store(state_path) as store:
        completed = list(store.completed_records())
        target = min(len(completed), max(minimum_sample, round(len(completed) * sample_fraction)))
        if maximum_sample is not None:
            target = min(target, maximum_sample)
        selected = sorted(
            completed,
            key=lambda row: hashlib.sha256(
                f"{seed}:{row['review_id']}".encode("utf-8")
            ).hexdigest(),
        )[:target]
        selected_ids = {row["review_id"] for row in selected}
        existing_ids = {
            row[0]
            for row in store.connection.execute(
                "SELECT review_id FROM verifier_records WHERE label_config=?",
                (verifier_config,),
            )
            if row[0] in selected_ids
        }
        pending = [row for row in selected if row["review_id"] not in existing_ids]
        call_phase = "resume" if existing_ids else "initial"
        evidence_quote_repairs = 0

        def attempt_batch(batch: list[dict[str, Any]]) -> bool:
            nonlocal evidence_quote_repairs
            for attempt in range(1, 2):
                result = None
                try:
                    model_rows, source_by_alias, alias_to_source_id = compact_review_batch(batch)
                    result = provider.call(
                        role="verify",
                        model=config.model,
                        effort=config.effort,
                        developer_prompt=prompt,
                        user_payload={
                            "reviews": model_rows,
                            "instruction": "Classify independently. No first-pass predictions are supplied.",
                        },
                        schema=enrichment_schema_for_count(len(batch)),
                        max_output_tokens=config.max_output_tokens,
                        timeout_seconds=config.timeout_seconds,
                    )
                    alias_values, repaired_ids = repair_and_validate_enrichment_payload(
                        result.payload, source_by_alias
                    )
                    values = restore_source_review_ids(alias_values, alias_to_source_id)
                    evidence_quote_repairs += len(repaired_ids)
                    store.log_call(
                        _call_event(
                            result=result,
                            run_id=run_id,
                            role="verify",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.model,
                            effort=config.effort,
                            phase=call_phase,
                            outcome="succeeded",
                            label_config=verifier_config,
                        )
                    )
                    with store.transaction() as connection:
                        connection.executemany(
                            """
                            INSERT OR REPLACE INTO verifier_records(
                                review_id,topic,intent,sentiment,severity,entities_json,
                                evidence_quote,needs_review,label_config
                            ) VALUES(?,?,?,?,?,?,?,?,?)
                            """,
                            [
                                (
                                    value["review_id"], value["topic"], value["intent"],
                                    value["sentiment"], value["severity"],
                                    json.dumps(value["entities"], ensure_ascii=False, separators=(",", ":")),
                                    value["evidence_quote"], int(value["needs_review"]), verifier_config,
                                )
                                for value in values
                            ],
                        )
                    return True
                except ProviderUnavailableError as error:
                    event = _failed_event(
                        run_id=run_id,
                        role="verify",
                        review_ids=[row["review_id"] for row in batch],
                        model=config.model,
                        effort=config.effort,
                        phase=call_phase,
                        label_config=verifier_config,
                        error=f"{type(error).__name__}: {error}",
                        provider=provider.name,
                    )
                    event["request_id"] = error.request_id
                    event["duration_seconds"] = error.duration_seconds
                    store.log_call(event)
                    raise
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"
                    if result is not None:
                        event = _failed_result_event(
                            result=result,
                            run_id=run_id,
                            role="verify",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.model,
                            effort=config.effort,
                            phase=call_phase,
                            label_config=verifier_config,
                            error=message,
                        )
                    else:
                        event = _failed_event(
                            run_id=run_id,
                            role="verify",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.model,
                            effort=config.effort,
                            phase=call_phase,
                            label_config=verifier_config,
                            error=message,
                            provider=provider.name,
                        )
                    store.log_call(event)
            return False

        for batch in chunks(pending, min(config.batch_size, 50)):
            if attempt_batch(batch):
                continue
            recovery_groups = list(chunks(batch, 10))
            for recovery_group in recovery_groups:
                attempt_batch(recovery_group)

        verified_ids = {
            row[0]
            for row in store.connection.execute(
                "SELECT review_id FROM verifier_records WHERE label_config=?",
                (verifier_config,),
            )
            if row[0] in selected_ids
        }
        failures = [
            {"review_id": row["review_id"], "reason": "verification_failed"}
            for row in selected
            if row["review_id"] not in verified_ids
        ]
        comparisons = []
        for row in store.connection.execute(
            """
            SELECT r.review_id,r.topic AS first_topic,r.intent AS first_intent,
                   r.severity AS first_severity,v.topic AS verify_topic,
                   v.intent AS verify_intent,v.severity AS verify_severity
            FROM records r JOIN verifier_records v USING(review_id)
            WHERE v.label_config=?
            ORDER BY r.review_id
            """,
            (verifier_config,),
        ):
            item = dict(row)
            if item["review_id"] not in selected_ids:
                continue
            item["topic_agree"] = item["first_topic"] == item["verify_topic"]
            item["intent_agree"] = item["first_intent"] == item["verify_intent"]
            item["severity_distance"] = abs(item["first_severity"] - item["verify_severity"])
            comparisons.append(item)

        planted = None
        if comparisons:
            base = comparisons[0]
            wrong_topic = next(topic for topic in TOPICS if topic != base["verify_topic"])
            planted = {
                "synthetic_test": True,
                "review_id": base["review_id"],
                "planted_topic": wrong_topic,
                "verifier_topic": base["verify_topic"],
                "detected": wrong_topic != base["verify_topic"],
                "note": "The planted label exists only in this test copy and is excluded from business outputs.",
            }
        result_report = {
            "sample_seed": seed,
            "sample_fraction": sample_fraction,
            "minimum_sample": minimum_sample,
            "maximum_sample": maximum_sample,
            "sample_policy": (
                "deterministic SHA-256 ordering of review_id under the declared seed; "
                "target=min(completed,max(minimum,round(completed*fraction)),maximum when set)"
            ),
            "selected": target,
            "verified": len(verified_ids),
            "already_verified_at_start": len(existing_ids),
            "pending_at_start": len(pending),
            "evidence_quote_repairs": evidence_quote_repairs,
            "failures": failures,
            "agreement": {
                "topic": _fraction(sum(item["topic_agree"] for item in comparisons), len(comparisons)),
                "intent": _fraction(sum(item["intent_agree"] for item in comparisons), len(comparisons)),
                "severity_exact": _fraction(
                    sum(item["severity_distance"] == 0 for item in comparisons), len(comparisons)
                ),
                "severity_mae": _fraction(
                    sum(item["severity_distance"] for item in comparisons), len(comparisons)
                ),
            },
            "disagreements": [
                item
                for item in comparisons
                if not item["topic_agree"]
                or not item["intent_agree"]
                or item["severity_distance"] != 0
            ],
            "planted_error_test": planted,
        }
    write_json(output_path, result_report)
    return result_report


def _fraction(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None
