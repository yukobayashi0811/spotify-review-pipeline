from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.classify import ModelConfig, _call_event  # noqa: E402
from spotify_pipeline.distilled import DISTILLED_MODEL_VERSION, DistilledTextProvider  # noqa: E402
from spotify_pipeline.io_utils import file_sha, write_json  # noqa: E402
from spotify_pipeline.schemas import enrichment_schema_for_count  # noqa: E402
from spotify_pipeline.storage import Store  # noqa: E402


class StopState:
    requested = False
    signal_name: str | None = None


def _install_signal_handlers(stop: StopState) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def request_stop(signum: int, _frame: object) -> None:
        stop.requested = True
        stop.signal_name = signal.Signals(signum).name

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _create_fixed_pending_queue(store: Store) -> dict[str, int]:
    """Freeze work before writes; later reads use bounded SELECTs, not a live cursor."""
    connection = store.connection
    connection.execute("DROP TABLE IF EXISTS temp.pending_finalization")
    connection.execute(
        """
        CREATE TEMP TABLE pending_finalization(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          work_kind TEXT NOT NULL,
          text_sha256 TEXT NOT NULL UNIQUE,
          representative_review_id TEXT NOT NULL,
          review_text TEXT NOT NULL,
          label_config TEXT,
          result_json TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO pending_finalization(
          work_kind,text_sha256,representative_review_id,review_text,label_config,result_json
        )
        SELECT 'teacher',t.text_sha256,tl.representative_review_id,t.review_text,
               tl.label_config,tl.result_json
        FROM teacher_labels tl JOIN texts t USING(text_sha256)
        LEFT JOIN records r ON r.review_id=tl.representative_review_id
        WHERE r.review_id IS NULL OR r.status<>'completed'
        ORDER BY t.first_source_rowid
        """
    )
    teacher = int(connection.execute("SELECT changes()").fetchone()[0])
    connection.execute(
        """
        INSERT INTO pending_finalization(
          work_kind,text_sha256,representative_review_id,review_text
        )
        SELECT 'local',t.text_sha256,t.representative_review_id,t.review_text
        FROM texts t LEFT JOIN teacher_labels tl USING(text_sha256)
        LEFT JOIN records r ON r.review_id=t.representative_review_id
        WHERE tl.text_sha256 IS NULL AND (r.review_id IS NULL OR r.status<>'completed')
        ORDER BY t.first_source_rowid
        """
    )
    local = int(connection.execute("SELECT changes()").fetchone()[0])
    connection.commit()
    return {"teacher_texts": teacher, "local_texts": local, "total_texts": teacher + local}


def _repair_teacher_representatives(store: Store) -> dict[str, int]:
    """Rewire imported teacher groups to a row that was actually sent to Luna."""
    connection = store.connection
    connection.execute("DROP TABLE IF EXISTS temp.successful_enrich_evidence")
    connection.execute(
        """
        CREATE TEMP TABLE successful_enrich_evidence AS
        SELECT c.rowid AS call_rowid,CAST(je.value AS TEXT) AS review_id,c.label_config
        FROM calls c,json_each(c.review_ids_json) je
        WHERE c.role='enrich' AND c.outcome='succeeded'
        """
    )
    connection.execute(
        "CREATE INDEX temp.successful_evidence_idx "
        "ON successful_enrich_evidence(review_id,label_config,call_rowid)"
    )
    affected = connection.execute(
        """
        SELECT tl.text_sha256,tl.label_config,tl.result_json,
               (
                 SELECT e.review_id
                 FROM successful_enrich_evidence e
                 JOIN source_rows sent ON sent.review_id=e.review_id
                 WHERE e.label_config=tl.label_config
                   AND sent.text_sha256=t.text_sha256
                   AND sent.review_text=t.review_text
                 ORDER BY e.call_rowid,sent.rowid LIMIT 1
               ) AS evidenced_review_id
        FROM teacher_labels tl JOIN texts t USING(text_sha256)
        JOIN records r ON r.review_id=t.representative_review_id
        LEFT JOIN successful_enrich_evidence current
          ON current.review_id=t.representative_review_id
         AND current.label_config=tl.label_config
        WHERE r.status='completed' AND r.cache_source_id IS NULL
          AND current.review_id IS NULL
        ORDER BY t.first_source_rowid
        """
    ).fetchall()
    missing = [row["text_sha256"] for row in affected if row["evidenced_review_id"] is None]
    if missing:
        raise RuntimeError(f"Teacher provenance cannot be repaired for {len(missing)} exact texts")
    rewired_rows = 0
    for start in range(0, len(affected), 100):
        batch = affected[start : start + 100]
        store.finalize_text_batch(
            rows=[
                (
                    row["text_sha256"], json.loads(row["result_json"]),
                    row["label_config"], row["evidenced_review_id"],
                )
                for row in batch
            ]
        )
        with store.transaction() as tx:
            tx.executemany(
                "UPDATE texts SET representative_review_id=? WHERE text_sha256=?",
                ((row["evidenced_review_id"], row["text_sha256"]) for row in batch),
            )
            tx.executemany(
                "UPDATE teacher_labels SET representative_review_id=? WHERE text_sha256=?",
                ((row["evidenced_review_id"], row["text_sha256"]) for row in batch),
            )
        rewired_rows += sum(
            int(connection.execute(
                "SELECT group_size FROM texts WHERE text_sha256=?", (row["text_sha256"],)
            ).fetchone()[0])
            for row in batch
        )
    return {"teacher_texts_repaired": len(affected), "record_rows_rewired": rewired_rows}


def _counts(store: Store) -> dict[str, Any]:
    return {
        "record_status_counts": store.status_counts(),
        "direct_exact_text_cache_rows": int(store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL"
        ).fetchone()[0]),
        "cache_chain_rows": int(store.connection.execute(
            """
            SELECT COUNT(*) FROM records child JOIN records parent
              ON child.cache_source_id=parent.review_id
            WHERE child.cache_source_id IS NOT NULL AND parent.cache_source_id IS NOT NULL
            """
        ).fetchone()[0]),
        "component_label_config_counts": {
            row[0]: int(row[1]) for row in store.connection.execute(
                "SELECT label_config,COUNT(*) FROM records "
                "WHERE status='completed' GROUP BY label_config"
            )
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--legacy-state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("initial", "resume"), required=True)
    parser.add_argument("--stop-after-batches", type=int)
    args = parser.parse_args()
    if args.stop_after_batches is not None and args.stop_after_batches < 1:
        parser.error("--stop-after-batches must be at least 1")
    args.output.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    report_path = args.output / "finalization_report.json"
    report: dict[str, Any] = {
        "status": "failed", "phase": args.phase,
        "state_path": str(args.state.resolve()), "started_at_unix": time.time(),
        "teacher_rows_finalized": 0, "local_rows_finalized": 0,
        "local_enrich_calls": 0, "completed_batches": 0,
    }
    stop = StopState()
    previous_handlers = _install_signal_handlers(stop)
    exit_code = 1
    provider: DistilledTextProvider | None = None
    store: Store | None = None
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        artifact_sha = file_sha(args.artifact)
        if manifest.get("artifact_sha256") != artifact_sha:
            raise RuntimeError("Final local artifact does not match its manifest")
        local_config = ModelConfig(
            model=DISTILLED_MODEL_VERSION, effort="none", batch_size=50, max_workers=1,
            max_attempts=1, max_output_tokens=1, timeout_seconds=60, spend_limit_usd=1,
            fallback_fraction=0, input_rate_per_million=0,
            cached_input_rate_per_million=0, output_rate_per_million=0,
            execution_fingerprint=(
                f"artifact-{artifact_sha}:sklearn-{manifest['scikit_learn_version']}:final-local-v2"
            ),
        )
        provider = DistilledTextProvider(artifact_path=args.artifact, expected_sha256=artifact_sha)
        run_id = f"full-local-final-{uuid.uuid4()}"
        report.update({"run_id": run_id, "artifact_sha256": artifact_sha,
                       "local_label_config": local_config.label_config})
        store = Store(args.state)
        _import_legacy_calls(store, args.legacy_state)
        report["teacher_provenance_repair"] = _repair_teacher_representatives(store)
        report["fixed_pending_queue"] = _create_fixed_pending_queue(store)

        last_seq = 0
        while not stop.requested:
            rows = store.connection.execute(
                "SELECT * FROM temp.pending_finalization WHERE seq>? ORDER BY seq LIMIT 50",
                (last_seq,),
            ).fetchall()
            if not rows:
                break
            kind = str(rows[0]["work_kind"])
            rows = [row for row in rows if row["work_kind"] == kind]
            last_seq = int(rows[-1]["seq"])
            if kind == "teacher":
                report["teacher_rows_finalized"] += store.finalize_text_batch(rows=[
                    (row["text_sha256"], json.loads(row["result_json"]),
                     row["label_config"], row["representative_review_id"])
                    for row in rows
                ])
                with store.transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO routing(text_sha256,route_bucket,final_source,
                          final_label_config,reasons_json,completed_at)
                        VALUES(?,'TEACHER','luna',?,'{}',CURRENT_TIMESTAMP)
                        ON CONFLICT(text_sha256) DO UPDATE SET final_source='luna',
                          final_label_config=excluded.final_label_config,
                          completed_at=CURRENT_TIMESTAMP
                        """,
                        ((row["text_sha256"], row["label_config"]) for row in rows),
                    )
            else:
                reviews = [{"review_id": row["representative_review_id"],
                            "review_text": row["review_text"]} for row in rows]
                result = provider.call(
                    role="enrich", model=DISTILLED_MODEL_VERSION, effort="none",
                    developer_prompt="", user_payload={"reviews": reviews},
                    schema=enrichment_schema_for_count(len(reviews)), max_output_tokens=1,
                    timeout_seconds=60,
                )
                by_id = {str(value["review_id"]): value for value in result.payload["records"]}
                store.log_call(_call_event(
                    result=result, run_id=run_id, role="enrich",
                    review_ids=[str(row["representative_review_id"]) for row in rows],
                    model=DISTILLED_MODEL_VERSION, effort="none", phase=args.phase,
                    outcome="succeeded", label_config=local_config.label_config,
                ))
                report["local_rows_finalized"] += store.finalize_text_batch(rows=[
                    (row["text_sha256"], by_id[str(row["representative_review_id"])],
                     local_config.label_config, row["representative_review_id"])
                    for row in rows
                ])
                with store.transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO routing(text_sha256,route_bucket,final_source,
                          final_label_config,reasons_json,completed_at)
                        VALUES(?,'LOCAL','local',?,'{}',CURRENT_TIMESTAMP)
                        ON CONFLICT(text_sha256) DO UPDATE SET final_source='local',
                          final_label_config=excluded.final_label_config,
                          completed_at=CURRENT_TIMESTAMP
                        """,
                        ((row["text_sha256"], local_config.label_config) for row in rows),
                    )
                report["local_enrich_calls"] += 1
            report["completed_batches"] += 1
            if args.stop_after_batches is not None and report["completed_batches"] >= args.stop_after_batches:
                stop.requested = True
                stop.signal_name = "CONTROLLED_STOP"

        if not stop.requested:
            empty_rows = store.connection.execute(
                """SELECT s.review_id,s.source_sha256 FROM source_rows s
                   LEFT JOIN records r USING(review_id)
                   WHERE TRIM(s.review_text)='' AND r.review_id IS NULL"""
            ).fetchall()
            for row in empty_rows:
                store.quarantine(row["review_id"], row["source_sha256"], "empty_review_text")
        report["status"] = "interrupted" if stop.requested else "completed"
        report["stop_reason"] = stop.signal_name
        report.update(_counts(store))
        exit_code = 75 if stop.requested else 0
    except BaseException as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
        if store is not None:
            report.update(_counts(store))
        exit_code = 75 if report["status"] == "interrupted" else 1
    finally:
        if provider is not None:
            report["provider_runtime"] = provider.runtime_metadata()
        report["wall_seconds"] = round(time.perf_counter() - started, 6)
        report["finished_at_unix"] = time.time()
        if store is not None:
            store.close()
        write_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        _restore_signal_handlers(previous_handlers)
    return exit_code


def _import_legacy_calls(store: Store, legacy_state: Path) -> None:
    with sqlite3.connect(legacy_state) as connection:
        connection.row_factory = sqlite3.Row
        calls = connection.execute("SELECT * FROM calls WHERE role='enrich' ORDER BY rowid").fetchall()
    for call in calls:
        if store.connection.execute("SELECT 1 FROM calls WHERE request_id=?", (call["request_id"],)).fetchone():
            continue
        store.log_call({
            "request_id": call["request_id"], "run_id": call["run_id"],
            "role": call["role"], "provider": call["provider"],
            "review_ids": json.loads(call["review_ids_json"]), "model": call["model"],
            "effort": call["effort"], "phase": "initial", "outcome": call["outcome"],
            "label_config": call["label_config"], "prompt_version": call["prompt_version"],
            "schema_version": call["schema_version"], "input_tokens": call["input_tokens"],
            "cached_input_tokens": call["cached_input_tokens"],
            "output_tokens": call["output_tokens"], "duration_seconds": call["duration_seconds"],
            "billing_mode": "chatgpt_subscription", "usage_status": "measured",
            "error": call["error"], "fallback": bool(call["fallback"]),
        })


if __name__ == "__main__":
    raise SystemExit(main())
