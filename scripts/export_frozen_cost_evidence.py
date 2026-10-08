#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from spotify_pipeline.io_utils import atomic_write_text, canonical, file_sha, write_json
from spotify_pipeline.storage import Store


ROOT = Path(__file__).resolve().parents[1]
COST = ROOT / "cost"
PILOT_STATE = COST / "frozen_100" / "state.db"
LEGACY_STATE = ROOT / "experiments/codex_luna/checkpoint-10000-normal-usage-v2/consolidated/state.db"
FULL_STATE = ROOT / "artifacts/full_run_v2/state.db"


def main() -> None:
    cold = json.loads((COST / "frozen_100/cold_run_summary.json").read_text())
    warm = json.loads((COST / "frozen_100/warm_run_summary.json").read_text())
    with Store(PILOT_STATE) as store:
        records = []
        for row in store.connection.execute("SELECT * FROM records ORDER BY review_id"):
            value = dict(row)
            item = {
                "review_id": value["review_id"], "source_sha256": value["source_sha256"],
                "status": value["status"],
            }
            if value["status"] == "completed":
                item.update({
                    "topic": value["topic"], "intent": value["intent"],
                    "sentiment": value["sentiment"], "severity": value["severity"],
                    "entities": json.loads(value["entities_json"]),
                    "evidence_quote": value["evidence_quote"],
                    "needs_review": bool(value["needs_review"]),
                    "label_config": value["label_config"],
                })
            else:
                item["reason"] = value["reason"]
            records.append(item)
        pilot_calls = [{**row, "cost_scope": "pilot"} for row in store.call_rows()]
        ids = [row[0] for row in store.connection.execute(
            "SELECT review_id FROM source_rows ORDER BY rowid"
        )]
        completed = store.completed_count()
        direct_hits = int(store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL"
        ).fetchone()[0])

    fixed_calls = _fixed_calls()
    _write_jsonl(COST / "pilot_records.jsonl", records)
    _write_jsonl(COST / "pilot_calls.jsonl", pilot_calls)
    _write_jsonl(COST / "teacher_fixed_calls.jsonl", fixed_calls)
    _write_jsonl(COST / "all_measured_calls.jsonl", [*pilot_calls, *fixed_calls])

    fixed_evidence = _fixed_evidence(fixed_calls)
    metadata = {
        "projection_run_id": cold["run_id"],
        "pilot_rows": 100,
        "input_sha256": file_sha(ROOT / "data/cost_100.csv"),
        "pilot_ids": ids,
        "completed_records": completed,
        "quarantined_or_failed_records": 100 - completed,
        "unique_nonempty_texts": 100,
        "direct_exact_text_cache_hits": direct_hits,
        "warm_saved_result_reuses": 100,
        "provider": "local-distilled plus local-mlx; Luna shown only as API-rate equivalent",
        "model": "distilled-tfidf-linear-v2 + pinned local Gemma",
        "batch_size": 50,
        "worker_limit": 1,
        "cold_wall_seconds": cold["wall_seconds"],
        "warm_wall_seconds": warm["wall_seconds"],
        "cold_stage_wall_seconds": {
            key: value["wall_seconds"] for key, value in cold["stages"].items()
        },
        "cold_verification_selected": cold["stages"]["verify"]["selected"],
        # Full-run verification is a fixed stratified sample of 400 distinct texts
        # (artifacts/full_run_v2/verification_report.json), not the pilot's 10% rate.
        "projection_verification_texts": 400,
        "cold_new_enrichment_calls": cold["new_model_calls_by_role"]["enrich"],
        "warm_new_enrichment_calls": warm["new_model_calls_by_role"]["enrich"],
        "cold_new_model_calls_by_role": cold["new_model_calls_by_role"],
        "warm_new_model_calls_by_role": warm["new_model_calls_by_role"],
        "one_time_fixed_evidence": fixed_evidence,
        "provider_runtime": cold["runtime"],
        "controls": {
            "spend_limit_usd": 1,
            "max_output_tokens": 6000,
            "max_workers": 1,
            "fallback_fraction": 0,
            "luna_escalation_limit": 0,
            "memo_attempt_limit": 2,
        },
    }
    write_json(COST / "pilot_run.json", metadata)


def _fixed_calls() -> list[dict]:
    calls: list[dict] = []
    legacy = sqlite3.connect(LEGACY_STATE)
    legacy.row_factory = sqlite3.Row
    for row in legacy.execute("SELECT * FROM calls WHERE role='enrich' ORDER BY rowid"):
        item = _call_row(row)
        item["cost_scope"] = (
            "teacher_existing_8448" if item["outcome"] == "succeeded"
            else "teacher_existing_failed_attempts"
        )
        calls.append(item)

    full = sqlite3.connect(FULL_STATE)
    full.row_factory = sqlite3.Row
    scopes = {
        "H": "teacher_new_3560", "R": "teacher_new_3560",
        "AL1": "teacher_new_3560", "AL2": "teacher_new_3560",
        "V": "verification_new_400",
        "G_EMBED_V3": "selected_grouping_565",
        "M_EMBED_V3": "selected_memo_two_attempts",
    }
    for bucket, scope in scopes.items():
        request_ids = [row[0] for row in full.execute(
            "SELECT DISTINCT request_id FROM luna_ledger WHERE bucket=? AND request_id IS NOT NULL",
            (bucket,),
        )]
        if bucket == "G_EMBED_V3" and request_ids:
            run_id = full.execute(
                "SELECT run_id FROM calls WHERE request_id=?", (request_ids[0],)
            ).fetchone()[0]
            # The ledger's final successful reservation owns the text cap, while
            # the calls table additionally retains the failed validation attempt.
            request_ids = [row[0] for row in full.execute(
                "SELECT request_id FROM calls WHERE run_id=? ORDER BY rowid", (run_id,)
            )]
        for request_id in request_ids:
            row = full.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"Missing measured call row for {bucket}/{request_id}")
            item = _call_row(row)
            item["cost_scope"] = scope
            item["ledger_bucket"] = bucket
            calls.append(item)
    return calls


def _call_row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["review_ids"] = json.loads(value.pop("review_ids_json"))
    metadata = value.pop("provider_metadata_json", None)
    value["provider_metadata"] = json.loads(metadata) if metadata else None
    value["fallback"] = bool(value["fallback"])
    return value


def _fixed_evidence(calls: list[dict]) -> dict:
    scopes: dict[str, dict] = {}
    for call in calls:
        scope = call["cost_scope"]
        value = scopes.setdefault(scope, {
            "calls": 0, "successful_review_ids": set(), "attempted_review_ids": set(),
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
        })
        value["calls"] += 1
        value["attempted_review_ids"].update(call["review_ids"])
        if call["outcome"] == "succeeded":
            value["successful_review_ids"].update(call["review_ids"])
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value[field] += int(call.get(field) or 0)
    result = {}
    for scope, value in scopes.items():
        result[scope] = {
            **{key: val for key, val in value.items() if not isinstance(val, set)},
            "successful_distinct_review_ids": len(value["successful_review_ids"]),
            "attempted_distinct_review_ids": len(value["attempted_review_ids"]),
        }
    result["declared_teacher_and_verifier_texts"] = {
        "existing_teacher": 8448, "new_teacher": 3560, "new_verifier": 400,
        "new_total": 3960,
    }
    result["token_source"] = "saved provider call rows and Luna ledger request IDs"
    return result


def _write_jsonl(path: Path, values: list[dict]) -> None:
    atomic_write_text(path, "\n".join(canonical(value) for value in values) + "\n")


if __name__ == "__main__":
    main()
