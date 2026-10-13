from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.classify import _call_event  # noqa: E402
from spotify_pipeline.full_run import _score_unlabeled_texts  # noqa: E402
from spotify_pipeline.io_utils import file_sha, write_json  # noqa: E402
from spotify_pipeline.legacy_luna import (  # noqa: E402
    LEGACY_CLI_VERSION,
    LEGACY_EXECUTION_FINGERPRINT,
)
from spotify_pipeline.providers import CodexExecProvider  # noqa: E402
from spotify_pipeline.schemas import (  # noqa: E402
    compact_review_batch,
    repair_and_validate_verification_payload,
    restore_source_review_ids,
    verification_schema_for_count,
)
from spotify_pipeline.storage import Store  # noqa: E402


VERIFY_PROMPT_VERSION = "2026-10-03-v2"
VERIFY_SCHEMA_VERSION = "verification-v2"
VERIFY_LABEL_CONFIG = (
    f"gpt-6-luna:low:verify-{VERIFY_PROMPT_VERSION}:{VERIFY_SCHEMA_VERSION}:"
    f"{LEGACY_EXECUTION_FINGERPRINT}"
)
WORD_BUCKET_SQL = (
    "CASE WHEN t.word_count<=5 THEN '00-05' WHEN t.word_count<=15 THEN '06-15' "
    "WHEN t.word_count<=40 THEN '16-40' ELSE '41+' END"
)


def _allocation(populations: dict[str, int], target: int) -> dict[str, int]:
    nonempty = sorted(key for key, value in populations.items() if value > 0)
    if target < len(nonempty):
        raise ValueError("Target is too small to sample every non-empty stratum")
    total = sum(populations.values())
    allocation = {key: 1 for key in nonempty}
    remaining = target - len(nonempty)
    quotas = {key: remaining * populations[key] / total for key in nonempty}
    for key in nonempty:
        allocation[key] += math.floor(quotas[key])
    unassigned = target - sum(allocation.values())
    for key in sorted(nonempty, key=lambda item: (-(quotas[item] % 1), item))[:unassigned]:
        allocation[key] += 1
    return allocation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, default=PROJECT_ROOT / "prompts/verify.md")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    artifact_sha = file_sha(args.artifact)
    if manifest.get("artifact_sha256") != artifact_sha:
        raise RuntimeError("Final artifact does not match its manifest")
    with Store(args.state) as store:
        if not store.connection.execute("SELECT 1 FROM verifier_records LIMIT 1").fetchone():
            with store.transaction() as connection:
                connection.execute("DELETE FROM router_scores")
            _score_unlabeled_texts(
                store=store,
                artifact_path=args.artifact,
                artifact_sha=artifact_sha,
                policy_fingerprint="calibrated-error-group-risk-v3-final",
            )
        selected = _select(store, local_target=300, luna_target=100)
        pending = [
            row
            for row in selected
            if not store.connection.execute(
                "SELECT 1 FROM verifier_records WHERE review_id=? AND label_config=?",
                (row["review_id"], VERIFY_LABEL_CONFIG),
            ).fetchone()
        ]
        admitted = set(
            store.reserve_luna(
                bucket="V",
                text_hashes=[row["text_sha256"] for row in pending],
                bucket_limit=400,
                global_limit=3960,
            )
        )
        pending = [row for row in pending if row["text_sha256"] in admitted]
    batches = [pending[offset : offset + 50] for offset in range(0, len(pending), 50)]
    shards = [batches[index :: args.workers] for index in range(args.workers)]
    prompt = args.prompt.read_text(encoding="utf-8")

    def worker(index: int, worker_batches: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        provider = CodexExecProvider(
            trace_root=args.output / "traces" / f"worker-{index:02d}",
            executable=args.codex,
            expected_version=LEGACY_CLI_VERSION,
            ephemeral=True,
        )
        outcomes = []
        for batch in worker_batches:
            source_rows = [
                {"review_id": row["review_id"], "review_text": row["review_text"]}
                for row in batch
            ]
            last_error = None
            for attempt in range(1, 3):
                result = None
                try:
                    model_rows, source_by_alias, alias_to_source = compact_review_batch(source_rows)
                    result = provider.call(
                        role="verify",
                        model="gpt-6-luna",
                        effort="low",
                        developer_prompt=prompt,
                        user_payload={
                            "reviews": model_rows,
                            "instruction": "Classify independently; no first-pass labels are supplied.",
                        },
                        schema=verification_schema_for_count(len(batch)),
                        max_output_tokens=4_000,
                        timeout_seconds=300,
                    )
                    aliases, repaired = repair_and_validate_verification_payload(
                        result.payload, source_by_alias
                    )
                    values = restore_source_review_ids(aliases, alias_to_source)
                    outcomes.append(
                        {"batch": batch, "result": result, "records": values, "repaired": repaired}
                    )
                    break
                except Exception as error:
                    last_error = f"{type(error).__name__}: {error}"
                    if attempt == 2:
                        outcomes.append(
                            {"batch": batch, "result": result, "records": None, "error": last_error}
                        )
        return outcomes

    outcomes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, index, shard) for index, shard in enumerate(shards) if shard]
        for future in as_completed(futures):
            outcomes.extend(future.result())
    run_id = f"full-verify-{uuid.uuid4()}"
    with Store(args.state) as store:
        selected_by_id = {row["review_id"]: row for row in selected}
        for outcome in outcomes:
            batch = outcome["batch"]
            items = [("V", row["text_sha256"]) for row in batch]
            result = outcome.get("result")
            if outcome.get("records") is None or result is None:
                request_id = result.request_id if result else f"verify-failed-{uuid.uuid4()}"
                store.settle_luna_batch(
                    items=items,
                    request_id=request_id,
                    succeeded=False,
                    duration_seconds=result.duration_seconds if result else 0.0,
                    error=outcome.get("error"),
                )
                continue
            store.settle_luna_batch(
                items=items,
                request_id=result.request_id,
                succeeded=True,
                input_tokens=result.input_tokens,
                cached_input_tokens=result.cached_input_tokens,
                output_tokens=result.output_tokens,
                duration_seconds=result.duration_seconds,
            )
            event = _call_event(
                result=result,
                run_id=run_id,
                role="verify",
                review_ids=[row["review_id"] for row in batch],
                model="gpt-6-luna",
                effort="low",
                phase="initial",
                outcome="succeeded",
                label_config=VERIFY_LABEL_CONFIG,
            )
            event["prompt_version"] = VERIFY_PROMPT_VERSION
            event["schema_version"] = VERIFY_SCHEMA_VERSION
            store.log_call(event)
            with store.transaction() as connection:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO verifier_records(
                        review_id,topic,intent,sentiment,severity,entities_json,
                        evidence_quote,needs_review,label_config,quote_supported,
                        final_source,stratum,sample_weight,request_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        (
                            value["review_id"], value["topic"], value["intent"],
                            value["sentiment"], value["severity"],
                            json.dumps(value["entities"], ensure_ascii=False, separators=(",", ":")),
                            value["evidence_quote"], int(value["needs_review"]),
                            VERIFY_LABEL_CONFIG, int(value["quote_supported"]),
                            selected_by_id[value["review_id"]]["final_source"],
                            selected_by_id[value["review_id"]]["stratum"],
                            selected_by_id[value["review_id"]]["sample_weight"],
                            result.request_id,
                        )
                        for value in outcome["records"]
                    ),
                )
        report = _report(store, selected, artifact_sha, time.perf_counter() - started)
    write_json(args.output / "verification_report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _select(store: Store, *, local_target: int, luna_target: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for final_source, target in (("local", local_target), ("luna", luna_target)):
        if final_source == "local":
            source_join = "JOIN router_scores rs USING(text_sha256) LEFT JOIN teacher_labels tl USING(text_sha256)"
            condition = "tl.text_sha256 IS NULL"
            topic_expr = "rs.topic"
        else:
            source_join = "JOIN teacher_labels tl USING(text_sha256)"
            condition = "1=1"
            topic_expr = "json_extract(tl.result_json,'$.topic')"
        strata_rows = store.connection.execute(
            f"""
            SELECT {topic_expr} AS topic,{WORD_BUCKET_SQL} AS word_bucket,
                   COUNT(*) AS distinct_count,SUM(t.group_size) AS row_count
            FROM texts t {source_join}
            WHERE {condition}
            GROUP BY topic,word_bucket ORDER BY topic,word_bucket
            """
        ).fetchall()
        populations = {
            f"{row['topic']}|{row['word_bucket']}": int(row["distinct_count"])
            for row in strata_rows
        }
        allocation = _allocation(populations, target)
        stats = {
            f"{row['topic']}|{row['word_bucket']}": row for row in strata_rows
        }
        for stratum, count in allocation.items():
            topic, bucket = stratum.split("|", 1)
            query = f"""
                SELECT t.text_sha256,t.representative_review_id AS review_id,t.review_text,
                       t.group_size,{topic_expr} AS topic
                FROM texts t {source_join}
                WHERE {condition} AND {topic_expr}=? AND {WORD_BUCKET_SQL}=?
                ORDER BY t.text_sha256 LIMIT ?
            """
            rows = store.connection.execute(query, (topic, bucket, count)).fetchall()
            distinct_count = int(stats[stratum]["distinct_count"])
            for row in rows:
                selected.append(
                    {
                        **dict(row),
                        "final_source": final_source,
                        "stratum": stratum,
                        "sample_weight": float(row["group_size"]) * distinct_count / count,
                    }
                )
    return selected


def _report(store: Store, selected: list[dict[str, Any]], artifact_sha: str, seconds: float) -> dict[str, Any]:
    expected: dict[str, tuple[str, str, int]] = {}
    for row in selected:
        if row["final_source"] == "local":
            value = store.connection.execute(
                "SELECT topic,intent,severity FROM router_scores WHERE text_sha256=?",
                (row["text_sha256"],),
            ).fetchone()
            expected[row["review_id"]] = (value[0], value[1], int(value[2]))
        else:
            value = json.loads(
                store.connection.execute(
                    "SELECT result_json FROM teacher_labels WHERE text_sha256=?",
                    (row["text_sha256"],),
                ).fetchone()[0]
            )
            expected[row["review_id"]] = (value["topic"], value["intent"], int(value["severity"]))
    metrics: dict[str, Any] = {}
    for source in ("local", "luna"):
        rows = store.connection.execute(
            "SELECT * FROM verifier_records WHERE label_config=? AND final_source=?",
            (VERIFY_LABEL_CONFIG, source),
        ).fetchall()
        total_weight = sum(float(row["sample_weight"]) for row in rows)
        def weighted(index: int) -> float | None:
            if not total_weight:
                return None
            return sum(
                float(row["sample_weight"])
                for row in rows
                if (row["topic"], row["intent"], int(row["severity"]))[index]
                == expected[str(row["review_id"])][index]
            ) / total_weight
        metrics[source] = {
            "verified_distinct_texts": len(rows),
            "row_weighted_topic_agreement": weighted(0),
            "row_weighted_intent_agreement": weighted(1),
            "row_weighted_severity_exact": weighted(2),
            "quote_supported_rate": (
                sum(int(row["quote_supported"]) for row in rows) / len(rows) if rows else None
            ),
            "interpretation": (
                "independent verifier agreement estimate"
                if source == "local"
                else "same-model Luna test-retest, not independent accuracy"
            ),
        }
    ledger = store.connection.execute(
        "SELECT status,COUNT(*) FROM luna_ledger WHERE bucket='V' GROUP BY status"
    ).fetchall()
    return {
        "status": "completed",
        "selection": "topic × word-count bucket stratified distinct texts",
        "local_target": 300,
        "luna_test_retest_target": 100,
        "metrics": metrics,
        "ledger": {row[0]: int(row[1]) for row in ledger},
        "final_local_artifact_sha256": artifact_sha,
        "golden_labels_opened": False,
        "wall_seconds": round(seconds, 6),
    }


if __name__ == "__main__":
    raise SystemExit(main())

