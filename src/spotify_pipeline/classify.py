from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .constants import PROMPT_VERSION, SCHEMA_VERSION
from .providers import Provider, ProviderResult, ProviderUnavailableError
from .schemas import (
    compact_review_batch,
    enrichment_schema_for_count,
    repair_and_validate_enrichment_payload,
    restore_source_review_ids,
    validate_enrichment_payload_partial,
)
from .storage import Store


class ControlledInterrupt(RuntimeError):
    pass


RECOVERY_BATCH_SIZE = 10


@dataclass
class ModelConfig:
    model: str = "gpt-6-luna"
    effort: str = "none"
    fallback_model: str | None = None
    fallback_effort: str = "low"
    batch_size: int = 10
    max_workers: int = 1
    max_attempts: int = 2
    max_output_tokens: int = 6000
    timeout_seconds: float = 120.0
    spend_limit_usd: float = 25.0
    fallback_fraction: float = 0.02
    input_rate_per_million: float = 0.10
    cached_input_rate_per_million: float = 0.01
    output_rate_per_million: float = 0.50
    execution_fingerprint: str = ""
    prompt_version: str = PROMPT_VERSION
    schema_version: str = SCHEMA_VERSION

    @property
    def label_config(self) -> str:
        base = (
            f"{self.model}:{self.effort}:classify-{self.prompt_version}:"
            f"{self.schema_version}"
        )
        return f"{base}:{self.execution_fingerprint}" if self.execution_fingerprint else base

    def validate(self) -> None:
        if not 1 <= self.batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if not 1 <= self.max_workers <= 32:
            raise ValueError("max_workers must be between 1 and 32")
        if not 1 <= self.max_attempts <= 2:
            raise ValueError("max_attempts must be 1 or 2")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.spend_limit_usd <= 0:
            raise ValueError("spend_limit_usd must be positive")
        if not 0 <= self.fallback_fraction <= 1:
            raise ValueError("fallback_fraction must be between 0 and 1")


class SpendLedger:
    def __init__(self, limit_usd: float):
        self.limit = limit_usd
        self.spent = 0.0
        self.reserved = 0.0

    def reserve(self, amount: float) -> None:
        if self.spent + self.reserved + amount > self.limit:
            raise RuntimeError(
                f"Spend cap would be exceeded: spent={self.spent:.6f}, "
                f"reserved={self.reserved:.6f}, next={amount:.6f}, limit={self.limit:.6f}"
            )
        self.reserved += amount

    def settle(self, reserved: float, actual: float | None) -> None:
        self.reserved = max(0.0, self.reserved - reserved)
        if actual is not None:
            self.spent += actual


def estimate_reservation(
    rows: list[dict[str, str]], config: ModelConfig, *, include_fallback: bool = True
) -> float:
    estimated_input_tokens = math.ceil(
        (sum(len(row["review_text"]) for row in rows) + 8000) / 4
    )
    single_attempt = (
        estimated_input_tokens * config.input_rate_per_million
        + config.max_output_tokens * config.output_rate_per_million
    ) / 1_000_000
    maximum_attempts = config.max_attempts + int(
        include_fallback and config.fallback_model is not None
    )
    return single_attempt * maximum_attempts


def actual_cost(result: ProviderResult, config: ModelConfig) -> float | None:
    if result.billing_mode == "local":
        return 0.0
    if result.billing_mode != "api_key":
        return None
    if result.input_tokens is None or result.output_tokens is None:
        return None
    cached = min(result.cached_input_tokens or 0, result.input_tokens)
    uncached = result.input_tokens - cached
    return (
        uncached * config.input_rate_per_million
        + cached * config.cached_input_rate_per_million
        + result.output_tokens * config.output_rate_per_million
    ) / 1_000_000


def chunks(values: Iterable[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    batch: list[dict[str, str]] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _attempt_recovery_batch(
    *,
    rows: list[dict[str, str]],
    prior_attempts: int,
    store: Store,
    ledger: SpendLedger,
    provider: Provider,
    prompt: str,
    config: ModelConfig,
    run_id: str,
    phase: str,
) -> dict[str, Any]:
    """Attempt one bounded recovery unit and persist it only after validation."""
    reservation = estimate_reservation(rows, config, include_fallback=False)
    ledger.reserve(reservation)
    validated: list[dict[str, Any]] | None = None
    attempts_used = 0
    last_error = "unknown recovery error"
    actual_cost_total = 0.0
    cost_unknown = False
    repaired_count = 0
    for attempt in range(1, 2):
        attempts_used = attempt
        result: ProviderResult | None = None
        try:
            model_rows, source_by_alias, alias_to_source_id = compact_review_batch(rows)
            result = provider.call(
                role="enrich",
                model=config.model,
                effort=config.effort,
                developer_prompt=prompt,
                user_payload={"reviews": model_rows},
                schema=enrichment_schema_for_count(len(rows)),
                max_output_tokens=config.max_output_tokens,
                timeout_seconds=config.timeout_seconds,
            )
            validated_aliases, repaired_ids = repair_and_validate_enrichment_payload(
                result.payload, source_by_alias
            )
            validated = restore_source_review_ids(validated_aliases, alias_to_source_id)
            repaired_count += len(repaired_ids)
            charge = actual_cost(result, config)
            if charge is None:
                cost_unknown = True
            else:
                actual_cost_total += charge
            store.log_call(
                _call_event(
                    result=result,
                    run_id=run_id,
                    role="enrich",
                    review_ids=[row["review_id"] for row in rows],
                    model=config.model,
                    effort=config.effort,
                    phase=phase,
                    outcome="succeeded",
                    label_config=config.label_config,
                )
            )
            break
        except ProviderUnavailableError as error:
            event = _failed_event(
                run_id=run_id,
                role="enrich",
                review_ids=[row["review_id"] for row in rows],
                model=config.model,
                effort=config.effort,
                phase=phase,
                label_config=config.label_config,
                error=f"{type(error).__name__}: {error}",
                provider=provider.name,
            )
            event["request_id"] = error.request_id
            event["duration_seconds"] = error.duration_seconds
            store.log_call(event)
            ledger.settle(reservation, reservation)
            raise
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
            if result is not None:
                charge = actual_cost(result, config)
                if charge is None:
                    cost_unknown = True
                else:
                    actual_cost_total += charge
                event = _failed_result_event(
                    result=result,
                    run_id=run_id,
                    role="enrich",
                    review_ids=[row["review_id"] for row in rows],
                    model=config.model,
                    effort=config.effort,
                    phase=phase,
                    label_config=config.label_config,
                    error=last_error,
                )
            else:
                cost_unknown = True
                event = _failed_event(
                    run_id=run_id,
                    role="enrich",
                    review_ids=[row["review_id"] for row in rows],
                    model=config.model,
                    effort=config.effort,
                    phase=phase,
                    label_config=config.label_config,
                    error=last_error,
                    provider=provider.name,
                )
            store.log_call(event)
    ledger.settle(reservation, reservation if cost_unknown else actual_cost_total)
    if validated is not None:
        by_id = {record["review_id"]: record for record in validated}
        for source in rows:
            store.save_completed(
                source,
                by_id[source["review_id"]],
                config.label_config,
                prior_attempts + attempts_used,
            )
        store.expand_new_duplicate_cache_hits(
            config.label_config, [row["text_sha256"] for row in rows]
        )
    return {
        "validated": validated is not None,
        "attempts_used": attempts_used,
        "last_error": last_error,
        "unknown_charge_attempts": int(cost_unknown),
        "evidence_quote_repairs": repaired_count,
    }


def _recover_failed_batch(
    *,
    rows: list[dict[str, str]],
    prior_attempts: int,
    store: Store,
    ledger: SpendLedger,
    provider: Provider,
    prompt: str,
    config: ModelConfig,
    run_id: str,
    phase: str,
) -> dict[str, int]:
    """Recover a failed batch as <=10-row units, then split only bad units."""
    stats = {
        "failed_records": 0,
        "unknown_charge_attempts": 0,
        "evidence_quote_repairs": 0,
    }
    recovery_groups = list(chunks(rows, RECOVERY_BATCH_SIZE))
    for recovery_group in recovery_groups:
        group_result = _attempt_recovery_batch(
            rows=recovery_group,
            prior_attempts=prior_attempts,
            store=store,
            ledger=ledger,
            provider=provider,
            prompt=prompt,
            config=config,
            run_id=run_id,
            phase=phase,
        )
        stats["unknown_charge_attempts"] += group_result["unknown_charge_attempts"]
        stats["evidence_quote_repairs"] += group_result["evidence_quote_repairs"]
        if group_result["validated"]:
            continue
        if not group_result["validated"]:
            for source in recovery_group:
                stats["failed_records"] += 1
                store.quarantine(
                    source["review_id"], source["source_sha256"],
                    "enrichment_failed_after_one_retry: "
                    + str(group_result["last_error"])[:500],
                    prior_attempts + int(group_result["attempts_used"]),
                )
            continue
        if len(recovery_group) == 1:
            source = recovery_group[0]
            stats["failed_records"] += 1
            store.quarantine(
                source["review_id"],
                source["source_sha256"],
                "enrichment_failed_after_batch_split: "
                + str(group_result["last_error"])[:500],
                prior_attempts + int(group_result["attempts_used"]),
            )
            continue
    return stats


def run_enrichment(
    *,
    state_path: str | Path,
    provider: Provider,
    prompt_path: str | Path,
    config: ModelConfig,
    run_id: str,
    phase: str,
    interrupt_after_batches: int | None = None,
    restrict_text_sha256: set[str] | None = None,
) -> dict[str, Any]:
    """Classify pending distinct texts in validated batches of at most 50.

    ``restrict_text_sha256`` limits this pass to the named exact texts (used to
    send a declared, capped routed subset to a model after a local pass).
    """
    config.validate()
    if phase not in {"initial", "resume"}:
        raise ValueError("phase must be initial or resume")
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    ledger = SpendLedger(config.spend_limit_usd)
    processed_unique = 0
    fallback_unique = 0
    failed_records = 0
    batch_count = 0
    unknown_charge_attempts = 0
    evidence_quote_repairs = 0
    with Store(state_path) as store:
        pending = list(
            store.iter_pending_unique(config.label_config, restrict_text_sha256)
        )
        for batch in chunks(pending, config.batch_size):
            original_batch = list(batch)
            partial_saved_ids: set[str] = set()
            reservation = estimate_reservation(batch, config)
            ledger.reserve(reservation)
            result: ProviderResult | None = None
            validated: list[dict[str, Any]] | None = None
            attempts_used = 0
            last_error = "unknown error"
            batch_actual_cost = 0.0
            batch_cost_unknown = False
            for attempt in range(1, 2):
                attempts_used = attempt
                result = None
                try:
                    model_rows, source_by_alias, alias_to_source_id = compact_review_batch(batch)
                    result = provider.call(
                        role="enrich",
                        model=config.model,
                        effort=config.effort,
                        developer_prompt=prompt,
                        user_payload={"reviews": model_rows},
                        schema=enrichment_schema_for_count(len(batch)),
                        max_output_tokens=config.max_output_tokens,
                        timeout_seconds=config.timeout_seconds,
                    )
                    validated_aliases, repaired_ids = repair_and_validate_enrichment_payload(
                        result.payload, source_by_alias
                    )
                    validated = restore_source_review_ids(validated_aliases, alias_to_source_id)
                    evidence_quote_repairs += len(repaired_ids)
                    charge = actual_cost(result, config)
                    if charge is None:
                        batch_cost_unknown = True
                    else:
                        batch_actual_cost += charge
                    store.log_call(
                        _call_event(
                            result=result,
                            run_id=run_id,
                            role="enrich",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.model,
                            effort=config.effort,
                            phase=phase,
                            outcome="succeeded",
                            label_config=config.label_config,
                        )
                    )
                    break
                except ProviderUnavailableError as error:
                    event = _failed_event(
                        run_id=run_id,
                        role="enrich",
                        review_ids=[row["review_id"] for row in batch],
                        model=config.model,
                        effort=config.effort,
                        phase=phase,
                        label_config=config.label_config,
                        error=f"{type(error).__name__}: {error}",
                        provider=provider.name,
                    )
                    event["request_id"] = error.request_id
                    event["duration_seconds"] = error.duration_seconds
                    store.log_call(event)
                    ledger.settle(reservation, reservation)
                    raise
                except Exception as error:
                    last_error = f"{type(error).__name__}: {error}"
                    if result is not None:
                        charge = actual_cost(result, config)
                        if charge is None:
                            batch_cost_unknown = True
                        else:
                            batch_actual_cost += charge
                        event = _call_event(
                            result=result,
                            run_id=run_id,
                            role="enrich",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.model,
                            effort=config.effort,
                            phase=phase,
                            outcome="failed",
                            label_config=config.label_config,
                        )
                        event["error"] = last_error
                    else:
                        batch_cost_unknown = True
                        event = _failed_event(
                            run_id=run_id,
                            role="enrich",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.model,
                            effort=config.effort,
                            phase=phase,
                            label_config=config.label_config,
                            error=last_error,
                            provider=provider.name,
                        )
                    store.log_call(event)
                    if result is not None:
                        valid_aliases, _invalid, repaired_ids = validate_enrichment_payload_partial(
                            result.payload, source_by_alias
                        )
                        if valid_aliases:
                            valid_values = restore_source_review_ids(
                                valid_aliases, alias_to_source_id
                            )
                            by_source = {row["review_id"]: row for row in batch}
                            for value in valid_values:
                                source = by_source[value["review_id"]]
                                store.save_completed(
                                    source, value, config.label_config, attempts_used
                                )
                                partial_saved_ids.add(value["review_id"])
                            with store.transaction() as connection:
                                connection.execute(
                                    "UPDATE calls SET outcome='succeeded',error=? "
                                    "WHERE request_id=?",
                                    (
                                        "partial_response: valid records persisted; "
                                        "only invalid IDs retried",
                                        result.request_id,
                                    ),
                                )
                            store.expand_new_duplicate_cache_hits(
                                config.label_config,
                                [row["text_sha256"] for row in batch],
                            )
                            evidence_quote_repairs += len(repaired_ids)
            if partial_saved_ids:
                batch = [row for row in batch if row["review_id"] not in partial_saved_ids]
                if not batch:
                    processed_unique += len(original_batch)
                    batch_count += 1
                    continue
            use_fallback = (
                validated is None
                and config.fallback_model is not None
                and (fallback_unique + len(batch))
                <= math.floor(max(1, len(pending)) * config.fallback_fraction)
            )
            if use_fallback:
                fallback_unique += len(batch)
                try:
                    result = None
                    model_rows, source_by_alias, alias_to_source_id = compact_review_batch(batch)
                    result = provider.call(
                        role="enrich",
                        model=config.fallback_model or "",
                        effort=config.fallback_effort,
                        developer_prompt=prompt,
                        user_payload={"reviews": model_rows},
                        schema=enrichment_schema_for_count(len(batch)),
                        max_output_tokens=config.max_output_tokens,
                        timeout_seconds=config.timeout_seconds,
                    )
                    validated_aliases, repaired_ids = repair_and_validate_enrichment_payload(
                        result.payload, source_by_alias
                    )
                    validated = restore_source_review_ids(validated_aliases, alias_to_source_id)
                    evidence_quote_repairs += len(repaired_ids)
                    charge = actual_cost(result, config)
                    if charge is None:
                        batch_cost_unknown = True
                    else:
                        batch_actual_cost += charge
                    fallback_config = (
                        f"{config.fallback_model}:{config.fallback_effort}:"
                        f"classify-{config.prompt_version}:{config.schema_version}"
                    )
                    store.log_call(
                        _call_event(
                            result=result,
                            run_id=run_id,
                            role="enrich",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.fallback_model or "",
                            effort=config.fallback_effort,
                            phase=phase,
                            outcome="succeeded",
                            label_config=fallback_config,
                            fallback=True,
                        )
                    )
                except ProviderUnavailableError as error:
                    event = _failed_event(
                        run_id=run_id,
                        role="enrich",
                        review_ids=[row["review_id"] for row in batch],
                        model=config.fallback_model or "",
                        effort=config.fallback_effort,
                        phase=phase,
                        label_config=config.label_config,
                        error=f"{type(error).__name__}: {error}",
                        fallback=True,
                        provider=provider.name,
                    )
                    event["request_id"] = error.request_id
                    event["duration_seconds"] = error.duration_seconds
                    store.log_call(event)
                    ledger.settle(reservation, reservation)
                    raise
                except Exception as error:
                    last_error = f"fallback {type(error).__name__}: {error}"
                    if result is not None:
                        charge = actual_cost(result, config)
                        if charge is None:
                            batch_cost_unknown = True
                        else:
                            batch_actual_cost += charge
                        event = _call_event(
                            result=result,
                            run_id=run_id,
                            role="enrich",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.fallback_model or "",
                            effort=config.fallback_effort,
                            phase=phase,
                            outcome="failed",
                            label_config=config.label_config,
                            fallback=True,
                        )
                        event["error"] = last_error
                    else:
                        batch_cost_unknown = True
                        event = _failed_event(
                            run_id=run_id,
                            role="enrich",
                            review_ids=[row["review_id"] for row in batch],
                            model=config.fallback_model or "",
                            effort=config.fallback_effort,
                            phase=phase,
                            label_config=config.label_config,
                            error=last_error,
                            fallback=True,
                            provider=provider.name,
                        )
                    store.log_call(event)
            if validated is None and batch and not use_fallback:
                if batch_cost_unknown:
                    unknown_charge_attempts += 1
                ledger.settle(
                    reservation,
                    reservation if batch_cost_unknown else batch_actual_cost,
                )
                recovery = _recover_failed_batch(
                    rows=batch,
                    prior_attempts=attempts_used + int(use_fallback),
                    store=store,
                    ledger=ledger,
                    provider=provider,
                    prompt=prompt,
                    config=config,
                    run_id=run_id,
                    phase=phase,
                )
                failed_records += recovery["failed_records"]
                unknown_charge_attempts += recovery["unknown_charge_attempts"]
                evidence_quote_repairs += recovery["evidence_quote_repairs"]
                processed_unique += len(original_batch)
                batch_count += 1
                if interrupt_after_batches is not None and batch_count >= interrupt_after_batches:
                    raise ControlledInterrupt(
                        f"Intentional interruption after {batch_count} saved batches"
                    )
                continue
            if batch_cost_unknown:
                unknown_charge_attempts += 1
            ledger.settle(
                reservation,
                reservation if batch_cost_unknown else batch_actual_cost,
            )
            if validated is None:
                failed_records += len(batch)
                for source in batch:
                    store.quarantine(
                        source["review_id"],
                        source["source_sha256"],
                        "enrichment_failed: " + last_error[:500],
                        attempts_used,
                    )
            else:
                by_id = {record["review_id"]: record for record in validated}
                for source in batch:
                    label_config = config.label_config
                    if use_fallback:
                        label_config = (
                            f"{config.fallback_model}:{config.fallback_effort}:"
                            f"classify-{config.prompt_version}:{config.schema_version}"
                        )
                    store.save_completed(
                        source,
                        by_id[source["review_id"]],
                        label_config,
                        attempts_used + int(use_fallback),
                    )
                store.expand_new_duplicate_cache_hits(
                    label_config, [row["text_sha256"] for row in batch]
                )
            processed_unique += len(original_batch)
            batch_count += 1
            if interrupt_after_batches is not None and batch_count >= interrupt_after_batches:
                raise ControlledInterrupt(
                    f"Intentional interruption after {batch_count} saved batches"
                )
        cache_hits = store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE status='completed' AND cache_source_id IS NOT NULL"
        ).fetchone()[0]
        return {
            "run_id": run_id,
            "phase": phase,
            "pending_unique_at_start": len(pending),
            "processed_unique": processed_unique,
            "failed_records": failed_records,
            "fallback_unique": fallback_unique,
            "cache_hits_total": cache_hits,
            "completed_total": store.completed_count(),
            "spend_ledger_usd_including_unknown_reservations": ledger.spent,
            "unsettled_reservation_usd": ledger.reserved,
            "unknown_charge_batches_reserved_at_cap": unknown_charge_attempts,
            "evidence_quote_repairs": evidence_quote_repairs,
            "max_workers_configured": config.max_workers,
            "note": "Dispatch is intentionally serial in v0.1; max_workers is a hard upper control.",
        }


def _call_event(
    *,
    result: ProviderResult,
    run_id: str,
    role: str,
    review_ids: list[str],
    model: str,
    effort: str,
    phase: str,
    outcome: str,
    label_config: str,
    fallback: bool = False,
) -> dict[str, Any]:
    prompt_version, schema_version = _versions_from_label_config(label_config)
    return {
        "request_id": result.request_id,
        "provider": result.provider,
        "billing_mode": result.billing_mode,
        "usage_status": result.usage_status,
        "provider_metadata": result.provider_metadata,
        "run_id": run_id,
        "role": role,
        "review_ids": review_ids,
        "model": model,
        "effort": effort,
        "phase": phase,
        "outcome": outcome,
        "label_config": label_config,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "input_tokens": result.input_tokens,
        "cached_input_tokens": result.cached_input_tokens,
        "output_tokens": result.output_tokens,
        "duration_seconds": result.duration_seconds,
        "fallback": fallback,
    }


def _failed_event(
    *,
    run_id: str,
    role: str,
    review_ids: list[str],
    model: str,
    effort: str,
    phase: str,
    label_config: str,
    error: str,
    fallback: bool = False,
    provider: str = "unknown",
) -> dict[str, Any]:
    prompt_version, schema_version = _versions_from_label_config(label_config)
    return {
        "request_id": f"local-failure-{uuid.uuid4()}",
        "provider": provider,
        "run_id": run_id,
        "role": role,
        "review_ids": review_ids,
        "model": model,
        "effort": effort,
        "phase": phase,
        "outcome": "failed",
        "label_config": label_config,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "duration_seconds": 0.0,
        "error": error,
        "fallback": fallback,
    }


def _versions_from_label_config(label_config: str) -> tuple[str, str]:
    """Recover the logical prompt/schema versions from any role's label config.

    Label configs look like ``model:effort:<role>-<prompt_version>:<schema>[:...]``
    for the classify, verify, group and memo roles.
    """

    for role in ("classify", "verify", "group", "memo"):
        marker = f":{role}-"
        if marker not in label_config:
            continue
        suffix = label_config.split(marker, 1)[1]
        prompt_version, separator, rest = suffix.partition(":")
        if not separator:
            break
        schema_version = rest.split(":", 1)[0]
        return prompt_version, schema_version
    return PROMPT_VERSION, SCHEMA_VERSION


def _failed_result_event(
    *,
    result: ProviderResult,
    run_id: str,
    role: str,
    review_ids: list[str],
    model: str,
    effort: str,
    phase: str,
    label_config: str,
    error: str,
    fallback: bool = False,
) -> dict[str, Any]:
    event = _call_event(
        result=result,
        run_id=run_id,
        role=role,
        review_ids=review_ids,
        model=model,
        effort=effort,
        phase=phase,
        outcome="failed",
        label_config=label_config,
        fallback=fallback,
    )
    event["error"] = error
    return event
