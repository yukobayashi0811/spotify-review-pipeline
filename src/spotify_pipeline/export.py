from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .constants import AUDIT_VERSION
from .io_utils import atomic_write_text, canonical, write_csv, write_json
from .storage import Store


def save_checkpoint(state_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    with Store(state_path) as store:
        value = {"completed_ids": store.completed_ids()}
    write_json(output_path, value)
    return value


def export_grading(
    *,
    state_path: str | Path,
    output_dir: str | Path,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with Store(state_path) as store:
        source_count = store.source_count()
        record_count = int(store.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        nonempty = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM source_rows WHERE TRIM(review_text)<>''"
            ).fetchone()[0]
        )
        completed = store.completed_count()
        if not allow_incomplete and (record_count != source_count or completed != nonempty):
            raise RuntimeError(
                "Refusing to export a final grading folder from an incomplete run: "
                f"source={source_count}, records={record_count}, completed_nonempty={completed}/{nonempty}. "
                "Use --allow-incomplete only for an explicitly marked diagnostic export."
            )
        ingestion = store.get_metadata("ingestion_report")
        input_csv = store.get_metadata("input_csv")
        run = {
            "version": AUDIT_VERSION,
            "analysis_count": source_count,
            "analysis_sha256": ingestion["file_sha256"],
            "classification_input_fields": ["review_text"],
            "allow_multi_issue": False,
        }
        grading_ingestion = {
            key: ingestion[key]
            for key in (
                "file_sha256",
                "parsed_rows_sha256",
                "counts",
                "reviews_by_month",
                "reviews_by_rating",
                "first_review",
                "last_review",
            )
        }
        write_json(output / "run.json", run)
        write_json(output / "ingestion.json", grading_ingestion)
        record_lines = []
        for row in store.connection.execute("SELECT * FROM records ORDER BY review_id"):
            value = dict(row)
            if value["status"] == "quarantined":
                record_lines.append(
                    canonical(
                        {
                            "review_id": value["review_id"],
                            "source_sha256": value["source_sha256"],
                            "status": "quarantined",
                            "reason": value["reason"],
                        }
                    )
                )
            else:
                item = {
                    "review_id": value["review_id"],
                    "source_sha256": value["source_sha256"],
                    "status": "completed",
                    "topic": value["topic"],
                    "intent": value["intent"],
                    "sentiment": value["sentiment"],
                    "severity": value["severity"],
                    "entities": json.loads(value["entities_json"]),
                    "evidence_quote": value["evidence_quote"],
                    "needs_review": bool(value["needs_review"]),
                    "label_config": value["label_config"],
                }
                if value["cache_source_id"]:
                    item["cache_source_id"] = value["cache_source_id"]
                record_lines.append(canonical(item))
        atomic_write_text(output / "records.jsonl", "\n".join(record_lines) + ("\n" if record_lines else ""))
        membership = [
            dict(row)
            for row in store.connection.execute(
                "SELECT issue_id,review_id FROM membership ORDER BY issue_id,review_id"
            )
        ]
        write_csv(output / "membership.csv", ["issue_id", "review_id"], membership)
        ranking = store.get_metadata("ranking", [])
        write_csv(
            output / "ranking.csv",
            [
                "rank", "issue_id", "complaint_count", "severity_sum",
                "mean_severity", "priority_score",
            ],
            ranking,
        )
        memo = store.get_metadata("memo_payload", {"claims": []})
        write_csv(
            output / "claims.csv",
            ["claim_id", "issue_id", "metric", "value"],
            [
                {key: claim[key] for key in ("claim_id", "issue_id", "metric", "value")}
                for claim in memo.get("claims", [])
            ],
        )
        call_lines = []
        for event in store.call_rows():
            item = {
                "request_id": event["request_id"],
                "role": event["role"],
                "provider": event["provider"],
                "review_ids": event["review_ids"],
                "model": event["model"],
                "phase": event["phase"],
                "outcome": event["outcome"],
                "label_config": event["label_config"],
                "input_tokens": event["input_tokens"],
                "output_tokens": event["output_tokens"],
                "effort": event["effort"],
                "prompt_version": event["prompt_version"],
                "schema_version": event["schema_version"],
                "cached_input_tokens": event["cached_input_tokens"],
                "duration_seconds": event["duration_seconds"],
            }
            if event["error"]:
                item["error"] = event["error"]
            call_lines.append(canonical(item))
        atomic_write_text(output / "calls.jsonl", "\n".join(call_lines) + ("\n" if call_lines else ""))
        for name in ("checkpoint_before", "checkpoint_after"):
            value = store.get_metadata(name, {"completed_ids": []})
            write_json(output / f"{name}.json", value)
        status = {
            "final_export": record_count == source_count and completed == nonempty,
            "source_records": source_count,
            "exported_records": record_count,
            "completed_nonempty": completed,
            "expected_nonempty": nonempty,
            "input_csv": Path(input_csv).name if input_csv else None,
        }
        write_json(output / "export_status.json", status)
        return status
