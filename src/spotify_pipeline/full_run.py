from __future__ import annotations

import json
import math
import re
import resource
import sys
import sqlite3
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

from .classify import _call_event, _failed_event
from .distilled import DISTILLED_MODEL_VERSION, DistilledTextProvider, normalize_text, text_sha256
from .io_utils import file_sha, write_json
from .legacy_luna import (
    LEGACY_BATCH_SIZE,
    LEGACY_CLI_VERSION,
    LEGACY_LABEL_CONFIG,
    check_legacy_compatibility,
    legacy_luna_config,
)
from .prepare import ingest
from .providers import CodexExecProvider, ProviderResult, ProviderUnavailableError
from .schemas import compact_review_batch, enrichment_schema_for_count, repair_and_validate_enrichment_payload, restore_source_review_ids
from .storage import Store


_HIGH_RISK = re.compile(
    r"\b(?:charge[ds]?|charged|refund|billing|payment|privacy|private|hacked|"
    r"stolen|fraud|unauthorized|support|customer\s+service)\b",
    re.IGNORECASE,
)


def run_full(input_path: str | Path, config_path: str | Path) -> dict[str, Any]:
    """Execute or resume the full-corpus pipeline through its approval gate."""

    config_file = Path(config_path).resolve()
    config = json.loads(config_file.read_text(encoding="utf-8"))
    root = config_file.parent.parent
    output = _resolve(root, config["output_dir"])
    state_path = _resolve(root, config["state_path"])
    output.mkdir(parents=True, exist_ok=True)
    stage_times: dict[str, float] = {}
    run_started = time.perf_counter()

    started = time.perf_counter()
    if not state_path.exists() or not _state_has_source(state_path):
        ingest(Path(input_path), state_path, output / "ingestion.json")
    with Store(state_path) as store:
        distinct_texts = int(store.connection.execute("SELECT COUNT(*) FROM texts").fetchone()[0])
        if not distinct_texts:
            distinct_texts = store.rebuild_texts()
        if store.get_metadata("normalized_text_hash_version") != "NFKC-casefold-whitespace-v1":
            store.refresh_normalized_text_hashes()
        _write_checkpoint(store, output / "checkpoint_before.json", "before_luna_gate")
    stage_times["ingest_and_text_index"] = time.perf_counter() - started

    started = time.perf_counter()
    legacy_state = _resolve(root, config["legacy_state_path"])
    prompt_path = _resolve(root, config["classification_prompt_path"])
    cli_version = _cli_version(_resolve(root, config["codex_executable"]))
    compatibility = check_legacy_compatibility(
        legacy_state_path=legacy_state,
        prompt_path=prompt_path,
        cli_version=cli_version,
    )
    write_json(output / "legacy_compatibility.json", compatibility.to_dict())
    if not compatibility.compatible:
        return _finish_report(
            output=output,
            state_path=state_path,
            status="blocked_legacy_compatibility",
            stage_times=stage_times,
            run_started=run_started,
            extra={"compatibility": compatibility.to_dict()},
        )
    with Store(state_path) as store:
        legacy_import = _import_legacy_teacher(store, legacy_state)
    stage_times["legacy_teacher_import"] = time.perf_counter() - started

    artifact_path = _resolve(root, config["local_artifact_path"])
    manifest_path = _resolve(root, config["local_manifest_path"])
    if not artifact_path.is_file() or not manifest_path.is_file():
        return _finish_report(
            output=output,
            state_path=state_path,
            status="blocked_missing_local_v2_artifact",
            stage_times=stage_times,
            run_started=run_started,
            extra={"artifact_path": str(artifact_path), "manifest_path": str(manifest_path)},
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_version") != DISTILLED_MODEL_VERSION:
        raise RuntimeError("The configured local artifact is not the frozen v2 model")
    if manifest.get("artifact_sha256") != file_sha(artifact_path):
        raise RuntimeError("The configured local artifact does not match its manifest")

    started = time.perf_counter()
    with Store(state_path) as store:
        scored = _score_unlabeled_texts(
            store=store,
            artifact_path=artifact_path,
            artifact_sha=manifest["artifact_sha256"],
            policy_fingerprint=str(config["routing_policy_fingerprint"]),
        )
    stage_times["router_scoring"] = time.perf_counter() - started

    golden_input = _resolve(root, config["golden_input_path"])
    holdout = _resolve(root, config["checkpoint_500_path"])
    started = time.perf_counter()
    with Store(state_path) as store:
        selection = _select_pre_gate_routes(
            store=store,
            golden_input=golden_input,
            holdout=holdout,
            budgets=config["luna_budgets"],
        )
    stage_times["route_selection"] = time.perf_counter() - started

    approval_file = _resolve(root, config["approval_file"])
    stop_after = int(config.get("mandatory_stop_after_new_luna_texts", 1000))
    with Store(state_path) as store:
        sent_before = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM luna_ledger WHERE status IN ('reserved','succeeded','failed')"
            ).fetchone()[0]
        )
    if sent_before >= stop_after and not approval_file.is_file():
        return _finish_report(
            output=output,
            state_path=state_path,
            status="approval_required_after_1000",
            stage_times=stage_times,
            run_started=run_started,
            extra={
                "legacy_import": legacy_import,
                "router_scored_new": scored,
                "selection": selection,
                "mandatory_gate": _gate_summary(state_path, stop_after),
            },
        )

    if not bool(config.get("execute_luna", False)):
        return _finish_report(
            output=output,
            state_path=state_path,
            status="ready_for_luna_gate",
            stage_times=stage_times,
            run_started=run_started,
            extra={"legacy_import": legacy_import, "router_scored_new": scored, "selection": selection},
        )

    started = time.perf_counter()
    gate = _execute_luna_gate(
        state_path=state_path,
        output=output,
        prompt_path=prompt_path,
        executable=_resolve(root, config["codex_executable"]),
        workers=int(config.get("luna_workers", 3)),
        global_limit=int(config["new_luna_distinct_limit"]),
        stop_after_new=None if approval_file.is_file() else stop_after,
        bucket_limits={key: int(value) for key, value in config["luna_budgets"].items()},
    )
    stage_times["luna_gate"] = time.perf_counter() - started
    with Store(state_path) as store:
        _write_checkpoint(store, output / "checkpoint_after.json", "after_luna_gate")
    status = "approval_required_after_1000" if not approval_file.is_file() else "post_gate_incomplete"
    return _finish_report(
        output=output,
        state_path=state_path,
        status=status,
        stage_times=stage_times,
        run_started=run_started,
        extra={
            "legacy_import": legacy_import,
            "router_scored_new": scored,
            "selection": selection,
            "mandatory_gate": gate,
            "next_stage": "AL1 retraining and checkpoint_500 evaluation",
        },
    )


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _state_has_source(path: Path) -> bool:
    try:
        with sqlite3.connect(path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0]) > 0
    except sqlite3.Error:
        return False


def _cli_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Unable to read Codex CLI version: {result.stderr[:300]}")
    return result.stdout.strip()


def _import_legacy_teacher(store: Store, legacy_state: Path) -> dict[str, int]:
    existing = int(store.connection.execute("SELECT COUNT(*) FROM teacher_labels").fetchone()[0])
    if existing:
        return {"already_present": existing, "imported": 0}
    with sqlite3.connect(legacy_state) as legacy:
        legacy.row_factory = sqlite3.Row
        rows = legacy.execute(
            """
            SELECT s.review_text,r.review_id,r.topic,r.intent,r.sentiment,r.severity,
                   r.entities_json,r.evidence_quote,r.needs_review,r.label_config
            FROM records r JOIN source_rows s USING(review_id)
            WHERE r.status='completed'
            ORDER BY s.rowid
            """
        )
        by_text: dict[str, sqlite3.Row] = {}
        for row in rows:
            by_text.setdefault(str(row["review_text"]), row)
        call_by_id: dict[str, str] = {}
        for call in legacy.execute(
            "SELECT request_id,review_ids_json FROM calls WHERE role='enrich' AND outcome='succeeded'"
        ):
            for review_id in json.loads(call["review_ids_json"]):
                call_by_id[str(review_id)] = str(call["request_id"])
    batch: list[tuple[str, str, str, str, dict[str, Any], str | None]] = []
    for text, row in by_text.items():
        digest = text_sha256(text)
        target = store.connection.execute(
            "SELECT representative_review_id FROM texts WHERE text_sha256=? AND review_text=?",
            (digest, text),
        ).fetchone()
        if target is None:
            continue
        batch.append(
            (
                digest,
                str(target["representative_review_id"]),
                "legacy_luna_10000",
                str(row["label_config"]),
                {
                    "topic": row["topic"],
                    "intent": row["intent"],
                    "sentiment": row["sentiment"],
                    "severity": row["severity"],
                    "entities": json.loads(row["entities_json"]),
                    "evidence_quote": row["evidence_quote"],
                    "needs_review": bool(row["needs_review"]),
                },
                call_by_id.get(str(row["review_id"])),
            )
        )
    store.save_teacher_batch(rows=batch)
    return {"already_present": 0, "imported": len(batch)}


def _score_unlabeled_texts(
    *, store: Store, artifact_path: Path, artifact_sha: str, policy_fingerprint: str
) -> int:
    provider = DistilledTextProvider(artifact_path=artifact_path, expected_sha256=artifact_sha)
    cursor = store.connection.execute(
        """
        SELECT t.text_sha256,t.representative_review_id,t.review_text,t.group_size
        FROM texts t
        LEFT JOIN router_scores rs USING(text_sha256)
        WHERE rs.text_sha256 IS NULL
        ORDER BY t.first_source_rowid
        """
    )
    scored = 0
    while True:
        source = cursor.fetchmany(50)
        if not source:
            break
        model_rows = [
            {"review_id": str(row["representative_review_id"]), "review_text": str(row["review_text"])}
            for row in source
        ]
        result = provider.call(
            role="enrich",
            model=DISTILLED_MODEL_VERSION,
            effort="none",
            developer_prompt="",
            user_payload={"reviews": model_rows},
            schema=enrichment_schema_for_count(len(model_rows)),
            max_output_tokens=1,
            timeout_seconds=60.0,
        )
        records = {str(row["review_id"]): row for row in result.payload["records"]}
        diagnostics = (result.provider_metadata or {}).get("review_diagnostics") or {}
        values = []
        for row in source:
            review_id = str(row["representative_review_id"])
            prediction = records[review_id]
            diagnostic = diagnostics[review_id]
            rule_hit = bool(_HIGH_RISK.search(str(row["review_text"])))
            risk_weight = (
                3
                if rule_hit or int(prediction["severity"]) >= 4
                else 2
                if prediction["intent"] in {"complaint", "cancellation"}
                else 1
            )
            p_err = float(diagnostic["p_err"])
            priority = p_err * int(row["group_size"]) * risk_weight
            values.append(
                (
                    row["text_sha256"], artifact_sha, policy_fingerprint,
                    prediction["topic"], prediction["intent"], prediction["sentiment"],
                    prediction["severity"], int(prediction["needs_review"]),
                    diagnostic["mean_margin"], p_err, int(rule_hit), risk_weight,
                    priority, json.dumps(diagnostic, sort_keys=True, separators=(",", ":")),
                )
            )
        with store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO router_scores(
                    text_sha256,model_sha256,policy_fingerprint,topic,intent,sentiment,
                    severity,needs_review,mean_margin,p_err,rule_hit,risk_weight,
                    priority,diagnostics_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
        scored += len(values)
    return scored


def _exclusion_hashes(paths: list[Path]) -> tuple[set[str], set[str], set[str]]:
    import csv

    ids: set[str] = set()
    exact: set[str] = set()
    normalized: set[str] = set()
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ids.add(row["review_id"])
                exact.add(text_sha256(row["review_text"]))
                normalized.add(text_sha256(normalize_text(row["review_text"])))
    return ids, exact, normalized


def _select_pre_gate_routes(
    *, store: Store, golden_input: Path, holdout: Path, budgets: dict[str, Any]
) -> dict[str, int]:
    _, exact_excluded, normalized_excluded = _exclusion_hashes([golden_input, holdout])
    existing_routes = int(store.connection.execute("SELECT COUNT(*) FROM routing").fetchone()[0])
    if existing_routes:
        return {
            row[0]: int(row[1])
            for row in store.connection.execute(
                "SELECT route_bucket,COUNT(*) FROM routing GROUP BY route_bucket"
            )
        }

    excluded = exact_excluded | {
        row[0]
        for row in store.connection.execute("SELECT text_sha256 FROM texts WHERE normalized_text_sha256 IN (%s)" % ",".join("?" * len(normalized_excluded)), tuple(normalized_excluded))
    }
    def eligible(row: sqlite3.Row) -> bool:
        return str(row["text_sha256"]) not in excluded

    selected: dict[str, list[sqlite3.Row]] = {}
    frequent = store.connection.execute(
        """
        SELECT t.text_sha256,t.group_size,rs.* FROM texts t
        JOIN router_scores rs USING(text_sha256)
        LEFT JOIN teacher_labels tl USING(text_sha256)
        WHERE tl.text_sha256 IS NULL
        ORDER BY t.group_size DESC,t.text_sha256 ASC LIMIT 2000
        """
    ).fetchall()
    selected["H"] = [row for row in frequent if eligible(row) and row["mean_margin"] < 0.5][
        : int(budgets["H"])
    ]
    chosen = {str(row["text_sha256"]) for row in selected["H"]}
    risky = store.connection.execute(
        """
        SELECT t.text_sha256,t.group_size,rs.* FROM texts t
        JOIN router_scores rs USING(text_sha256)
        LEFT JOIN teacher_labels tl USING(text_sha256)
        WHERE tl.text_sha256 IS NULL
          AND (rs.topic='support' OR rs.rule_hit=1 OR rs.severity>=4)
        ORDER BY rs.priority DESC,t.text_sha256 ASC
        """
    )
    selected["R"] = []
    for row in risky:
        digest = str(row["text_sha256"])
        if digest not in chosen and eligible(row):
            selected["R"].append(row)
            chosen.add(digest)
            if len(selected["R"]) == int(budgets["R"]):
                break
    selected["AL1"] = _select_active_learning(
        store=store,
        excluded=excluded | chosen,
        limit=int(budgets["AL1"]),
    )
    order = 0
    with store.transaction() as connection:
        for bucket in ("H", "R", "AL1"):
            for row in selected[bucket]:
                order += 1
                connection.execute(
                    "INSERT INTO routing(text_sha256,route_bucket,selected_order,reasons_json) "
                    "VALUES(?,?,?,?)",
                    (
                        row["text_sha256"], bucket, order,
                        json.dumps(
                            {"p_err": row["p_err"], "priority": row["priority"], "policy": bucket},
                            sort_keys=True,
                        ),
                    ),
                )
    return {bucket: len(rows) for bucket, rows in selected.items()}


def _select_active_learning(*, store: Store, excluded: set[str], limit: int) -> list[sqlite3.Row]:
    """Calibrated uncertainty pool refined by deterministic TF-IDF/SVD k-center."""

    per_topic_cap = math.floor(limit * 0.35)
    cursor = store.connection.execute(
        """
        SELECT t.text_sha256,t.group_size,t.review_text,rs.* FROM texts t
        JOIN router_scores rs USING(text_sha256)
        LEFT JOIN teacher_labels tl USING(text_sha256)
        WHERE tl.text_sha256 IS NULL
        ORDER BY rs.p_err DESC,rs.mean_margin ASC,t.text_sha256 ASC
        """
    )
    pool: list[sqlite3.Row] = []
    pool_limit = max(limit * 4, limit)
    for row in cursor:
        if str(row["text_sha256"]) in excluded:
            continue
        pool.append(row)
        if len(pool) == pool_limit:
            break
    if len(pool) <= limit:
        return pool
    import numpy as np
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize

    vectorizer = TfidfVectorizer(
        lowercase=True, strip_accents="unicode", ngram_range=(1, 2),
        min_df=2, max_features=40_000, sublinear_tf=True,
    )
    matrix = vectorizer.fit_transform([str(row["review_text"]) for row in pool])
    components = min(128, matrix.shape[0] - 1, matrix.shape[1] - 1)
    if components < 2:
        return pool[:limit]
    vectors = normalize(
        TruncatedSVD(n_components=components, random_state=20261002).fit_transform(matrix)
    )
    selected_indices: list[int] = []
    selected_mask = np.zeros(len(pool), dtype=bool)
    min_distance = np.full(len(pool), np.inf, dtype=float)
    counts: dict[str, int] = {}
    for _ in range(limit):
        allowed = np.array(
            [
                (not selected_mask[index])
                and counts.get(str(row["topic"]), 0) < per_topic_cap
                for index, row in enumerate(pool)
            ],
            dtype=bool,
        )
        if not allowed.any():
            break
        index = int(np.flatnonzero(allowed)[0]) if not selected_indices else int(
            np.argmax(np.where(allowed, min_distance, -1.0))
        )
        selected_indices.append(index)
        selected_mask[index] = True
        topic = str(pool[index]["topic"])
        counts[topic] = counts.get(topic, 0) + 1
        distance = np.sum((vectors - vectors[index]) ** 2, axis=1)
        min_distance = np.minimum(min_distance, distance)
    return [pool[index] for index in selected_indices]


def _execute_luna_gate(
    *,
    state_path: Path,
    output: Path,
    prompt_path: Path,
    executable: Path,
    workers: int,
    global_limit: int,
    stop_after_new: int | None,
    bucket_limits: dict[str, int],
) -> dict[str, Any]:
    prompt = prompt_path.read_text(encoding="utf-8")
    config = legacy_luna_config()
    with Store(state_path) as store:
        pending: list[tuple[str, str, str, str]] = []
        for row in store.connection.execute(
            """
            SELECT r.route_bucket,r.text_sha256,t.representative_review_id,t.review_text
            FROM routing r JOIN texts t USING(text_sha256)
            LEFT JOIN teacher_labels tl USING(text_sha256)
            LEFT JOIN luna_ledger ll ON ll.bucket=r.route_bucket AND ll.text_sha256=r.text_sha256
            WHERE tl.text_sha256 IS NULL AND ll.ledger_id IS NULL
            ORDER BY r.selected_order,r.text_sha256
            """
        ):
            pending.append(tuple(str(value) for value in row))
        reserved: list[tuple[str, str, str, str]] = []
        by_bucket: dict[str, list[tuple[str, str, str, str]]] = {}
        for item in pending:
            by_bucket.setdefault(item[0], []).append(item)
        for bucket in ("H", "R", "AL1", "AL2", "V"):
            items = by_bucket.get(bucket, [])
            if not items:
                continue
            hashes = [item[1] for item in items]
            admitted = set(
                store.reserve_luna(
                    bucket=bucket,
                    text_hashes=hashes,
                    bucket_limit=bucket_limits[bucket],
                    global_limit=global_limit,
                    stop_after_new=stop_after_new,
                )
            )
            reserved.extend(item for item in items if item[1] in admitted)
    batches = [reserved[offset : offset + LEGACY_BATCH_SIZE] for offset in range(0, len(reserved), LEGACY_BATCH_SIZE)]
    shards = [batches[index::workers] for index in range(workers)]

    def worker(worker_index: int, worker_batches: list[list[tuple[str, str, str, str]]]) -> list[dict[str, Any]]:
        provider = CodexExecProvider(
            trace_root=output / "luna_traces" / f"worker-{worker_index:02d}",
            executable=str(executable),
            expected_version=LEGACY_CLI_VERSION,
            ephemeral=True,
        )
        outcomes: list[dict[str, Any]] = []
        for batch in worker_batches:
            source_rows = [
                {"review_id": review_id, "review_text": text}
                for _, _, review_id, text in batch
            ]
            result: ProviderResult | None = None
            error_text: str | None = None
            attempts = 0
            for attempts in range(1, config.max_attempts + 1):
                try:
                    model_rows, source_by_alias, alias_to_source = compact_review_batch(source_rows)
                    result = provider.call(
                        role="enrich", model=config.model, effort=config.effort,
                        developer_prompt=prompt, user_payload={"reviews": model_rows},
                        schema=enrichment_schema_for_count(len(batch)),
                        max_output_tokens=config.max_output_tokens,
                        timeout_seconds=config.timeout_seconds,
                    )
                    aliases, _ = repair_and_validate_enrichment_payload(result.payload, source_by_alias)
                    records = restore_source_review_ids(aliases, alias_to_source)
                    outcomes.append({"batch": batch, "result": result, "records": records, "attempts": attempts})
                    break
                except Exception as error:  # bounded and recorded below
                    error_text = f"{type(error).__name__}: {error}"
                    if attempts == config.max_attempts:
                        outcomes.append({"batch": batch, "result": result, "records": None, "attempts": attempts, "error": error_text})
            if error_text and outcomes[-1].get("records") is None:
                continue
        return outcomes

    outcomes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, index, shard) for index, shard in enumerate(shards) if shard]
        for future in as_completed(futures):
            outcomes.extend(future.result())
    outcomes.sort(key=lambda item: min(batch_item[1] for batch_item in item["batch"]))
    run_id = f"full-luna-gate-{uuid.uuid4()}"
    with Store(state_path) as store:
        for outcome in outcomes:
            batch = outcome["batch"]
            bucket = str(batch[0][0])
            hashes = [str(item[1]) for item in batch]
            ledger_items = [(str(item[0]), str(item[1])) for item in batch]
            review_ids = [str(item[2]) for item in batch]
            result = outcome.get("result")
            if outcome.get("records") is not None and result is not None:
                by_id = {str(record["review_id"]): record for record in outcome["records"]}
                teacher_rows = []
                for item_bucket, digest, review_id, _ in batch:
                    teacher_rows.append((digest, review_id, f"new_luna_{item_bucket}", LEGACY_LABEL_CONFIG, by_id[review_id], result.request_id))
                store.save_teacher_batch(rows=teacher_rows)
                store.settle_luna_batch(
                    items=ledger_items, request_id=result.request_id,
                    succeeded=True, input_tokens=result.input_tokens,
                    cached_input_tokens=result.cached_input_tokens,
                    output_tokens=result.output_tokens,duration_seconds=result.duration_seconds,
                )
                store.log_call(
                    _call_event(
                        result=result, run_id=run_id, role="enrich", review_ids=review_ids,
                        model=config.model, effort=config.effort, phase="initial",
                        outcome="succeeded", label_config=LEGACY_LABEL_CONFIG,
                    )
                )
            else:
                request_id = result.request_id if result is not None else f"failed-{uuid.uuid4()}"
                store.settle_luna_batch(
                    items=ledger_items, request_id=request_id,
                    succeeded=False, error=outcome.get("error"),
                    duration_seconds=result.duration_seconds if result is not None else 0.0,
                )
                event = _failed_event(
                    run_id=run_id, role="enrich", review_ids=review_ids,
                    model=config.model, effort=config.effort, phase="initial",
                    label_config=LEGACY_LABEL_CONFIG,error=str(outcome.get("error")),
                    provider="codex-exec-subscription",
                )
                event["request_id"] = request_id
                store.log_call(event)
    return _gate_summary(state_path, stop_after_new)


def _gate_summary(state_path: Path, stop_after: int | None) -> dict[str, Any]:
    with Store(state_path) as store:
        row = store.connection.execute(
            """
            SELECT COUNT(*) AS sent,
                   SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(duration_seconds),0) AS model_call_seconds
            FROM luna_ledger WHERE status IN ('reserved','succeeded','failed')
            """
        ).fetchone()
        buckets = {
            row[0]: int(row[1])
            for row in store.connection.execute(
                "SELECT bucket,COUNT(*) FROM luna_ledger GROUP BY bucket"
            )
        }
    before_path = state_path.parent / "usage_before_1000.json"
    after_path = state_path.parent / "usage_after_1000.json"
    before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.is_file() else None
    after = json.loads(after_path.read_text(encoding="utf-8")) if after_path.is_file() else None
    return {
        **dict(row),
        "bucket_counts": buckets,
        "mandatory_stop_after_new_luna_texts": stop_after,
        "account_usage_before": before,
        "account_usage_after": after,
        "account_usage_delta_percentage_points": (
            int(after["used_percent"]) - int(before["used_percent"])
            if before is not None and after is not None
            else None
        ),
    }


def _write_checkpoint(store: Store, path: Path, name: str) -> None:
    write_json(
        path,
        {
            "name": name,
            "source_rows": store.source_count(),
            "completed_rows": store.completed_count(),
            "teacher_texts": int(store.connection.execute("SELECT COUNT(*) FROM teacher_labels").fetchone()[0]),
            "router_scored_texts": int(store.connection.execute("SELECT COUNT(*) FROM router_scores").fetchone()[0]),
            "luna_ledger_rows": int(store.connection.execute("SELECT COUNT(*) FROM luna_ledger").fetchone()[0]),
        },
    )


def _finish_report(
    *, output: Path, state_path: Path, status: str, stage_times: dict[str, float],
    run_started: float, extra: dict[str, Any],
) -> dict[str, Any]:
    report = {
        "status": status,
        "state_path": str(state_path),
        "stage_wall_seconds": {key: round(value, 6) for key, value in stage_times.items()},
        "wall_seconds": round(time.perf_counter() - run_started, 6),
        "peak_memory_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            / (1024 * 1024 if sys.platform == "darwin" else 1024),
            3,
        ),
        **extra,
    }
    write_json(output / "run_report.json", report)
    return report
