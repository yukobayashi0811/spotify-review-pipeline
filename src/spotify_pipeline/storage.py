from __future__ import annotations

import json
import hashlib
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .io_utils import canonical


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS source_rows (
    review_id TEXT PRIMARY KEY,
    review_text TEXT NOT NULL,
    review_rating TEXT NOT NULL,
    review_likes TEXT NOT NULL,
    app_version TEXT NOT NULL,
    review_timestamp TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    text_sha256 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS source_text_idx ON source_rows(text_sha256);

CREATE TABLE IF NOT EXISTS records (
    review_id TEXT PRIMARY KEY REFERENCES source_rows(review_id),
    source_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed','quarantined')),
    topic TEXT,
    intent TEXT,
    sentiment REAL,
    severity INTEGER,
    entities_json TEXT,
    evidence_quote TEXT,
    needs_review INTEGER,
    label_config TEXT,
    cache_source_id TEXT,
    reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS result_cache (
    text_sha256 TEXT NOT NULL,
    label_config TEXT NOT NULL,
    source_review_id TEXT NOT NULL REFERENCES records(review_id),
    result_json TEXT NOT NULL,
    PRIMARY KEY(text_sha256, label_config)
);

CREATE TABLE IF NOT EXISTS calls (
    request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown',
    review_ids_json TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    label_config TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    duration_seconds REAL NOT NULL,
    billing_mode TEXT NOT NULL DEFAULT 'unknown',
    usage_status TEXT NOT NULL DEFAULT 'unknown',
    provider_metadata_json TEXT,
    error TEXT,
    fallback INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS verifier_records (
    review_id TEXT PRIMARY KEY REFERENCES source_rows(review_id),
    topic TEXT NOT NULL,
    intent TEXT NOT NULL,
    sentiment REAL NOT NULL,
    severity INTEGER NOT NULL,
    entities_json TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    needs_review INTEGER NOT NULL,
    label_config TEXT NOT NULL,
    quote_supported INTEGER,
    final_source TEXT,
    stratum TEXT,
    sample_weight REAL,
    request_id TEXT
);

CREATE TABLE IF NOT EXISTS membership (
    issue_id TEXT NOT NULL,
    review_id TEXT NOT NULL REFERENCES records(review_id),
    PRIMARY KEY(issue_id, review_id)
);

CREATE TABLE IF NOT EXISTS issue_names (
    issue_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    issue_name TEXT NOT NULL,
    description TEXT NOT NULL,
    label_config TEXT NOT NULL,
    coherent TEXT NOT NULL DEFAULT 'yes',
    actionable TEXT NOT NULL DEFAULT 'no',
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS membership_one_issue_per_review_idx
ON membership(review_id);

CREATE TABLE IF NOT EXISTS stage_runs (
    stage_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    output_sha256 TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS stage_runs_lookup_idx
ON stage_runs(stage,input_sha256,status,stage_run_id);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS texts (
    text_sha256 TEXT PRIMARY KEY,
    normalized_text_sha256 TEXT NOT NULL,
    review_text TEXT NOT NULL,
    representative_review_id TEXT NOT NULL REFERENCES source_rows(review_id),
    group_size INTEGER NOT NULL CHECK(group_size > 0),
    word_count INTEGER NOT NULL,
    first_source_rowid INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS texts_frequency_idx
ON texts(group_size DESC, text_sha256 ASC);

CREATE TABLE IF NOT EXISTS router_scores (
    text_sha256 TEXT PRIMARY KEY REFERENCES texts(text_sha256),
    model_sha256 TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    topic TEXT NOT NULL,
    intent TEXT NOT NULL,
    sentiment REAL NOT NULL,
    severity INTEGER NOT NULL,
    needs_review INTEGER NOT NULL,
    mean_margin REAL NOT NULL,
    p_err REAL NOT NULL CHECK(p_err >= 0 AND p_err <= 1),
    rule_hit INTEGER NOT NULL,
    risk_weight INTEGER NOT NULL,
    priority REAL NOT NULL,
    diagnostics_json TEXT NOT NULL,
    scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS router_priority_idx
ON router_scores(priority DESC, text_sha256 ASC);

CREATE TABLE IF NOT EXISTS teacher_labels (
    text_sha256 TEXT PRIMARY KEY REFERENCES texts(text_sha256),
    representative_review_id TEXT NOT NULL REFERENCES source_rows(review_id),
    source_kind TEXT NOT NULL,
    label_config TEXT NOT NULL,
    result_json TEXT NOT NULL,
    call_request_id TEXT,
    used_for_training INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS routing (
    text_sha256 TEXT PRIMARY KEY REFERENCES texts(text_sha256),
    route_bucket TEXT NOT NULL,
    final_source TEXT,
    final_label_config TEXT,
    selected_order INTEGER,
    reasons_json TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS routing_bucket_idx
ON routing(route_bucket, selected_order, text_sha256);

CREATE TABLE IF NOT EXISTS luna_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    text_sha256 TEXT NOT NULL REFERENCES texts(text_sha256),
    status TEXT NOT NULL CHECK(status IN ('reserved','succeeded','failed','released')),
    request_id TEXT,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    duration_seconds REAL,
    error TEXT,
    reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TEXT,
    UNIQUE(bucket, text_sha256)
);

CREATE INDEX IF NOT EXISTS luna_ledger_status_idx
ON luna_ledger(status, bucket, ledger_id);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        call_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(calls)")
        }
        if "provider" not in call_columns:
            self.connection.execute(
                "ALTER TABLE calls ADD COLUMN provider TEXT NOT NULL DEFAULT 'unknown'"
            )
            self.connection.commit()
        migrations = {
            "billing_mode": "TEXT NOT NULL DEFAULT 'unknown'",
            "usage_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "provider_metadata_json": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in call_columns:
                self.connection.execute(
                    f"ALTER TABLE calls ADD COLUMN {column} {definition}"
                )
        verifier_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(verifier_records)")
        }
        verifier_migrations = {
            "quote_supported": "INTEGER",
            "final_source": "TEXT",
            "stratum": "TEXT",
            "sample_weight": "REAL",
            "request_id": "TEXT",
        }
        for column, definition in verifier_migrations.items():
            if column not in verifier_columns:
                self.connection.execute(
                    f"ALTER TABLE verifier_records ADD COLUMN {column} {definition}"
                )
        issue_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(issue_names)")
        }
        for column, definition in {
            "coherent": "TEXT NOT NULL DEFAULT 'yes'",
            "actionable": "TEXT NOT NULL DEFAULT 'no'",
            "flags_json": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if column not in issue_columns:
                self.connection.execute(
                    f"ALTER TABLE issue_names ADD COLUMN {column} {definition}"
                )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def set_metadata(self, key: str, value: Any) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata(key,value_json) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, canonical(value)),
            )

    def get_metadata(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def source_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0])

    def completed_count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM records WHERE status='completed'"
            ).fetchone()[0]
        )

    def status_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status,COUNT(*) AS n FROM records GROUP BY status"
        )
        return {row["status"]: row["n"] for row in rows}

    def iter_pending_unique(
        self, label_config: str, restrict_text_sha256: set[str] | None = None
    ) -> Iterator[dict[str, str]]:
        query = """
        WITH pending AS (
            SELECT s.*, s.rowid AS source_rowid,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.text_sha256, s.review_text ORDER BY s.rowid
                   ) AS text_rank
            FROM source_rows s
            LEFT JOIN records r ON r.review_id=s.review_id
            WHERE TRIM(s.review_text) <> ''
              AND (r.review_id IS NULL OR r.status <> 'completed'
                   OR COALESCE(r.label_config,'') <> ?)
        )
        SELECT * FROM pending WHERE text_rank=1 ORDER BY source_rowid
        """
        for row in self.connection.execute(query, (label_config,)):
            source = dict(row)
            source.pop("source_rowid", None)
            source.pop("text_rank", None)
            digest = source["text_sha256"]
            if restrict_text_sha256 is not None and digest not in restrict_text_sha256:
                continue
            cached = self.connection.execute(
                "SELECT c.source_review_id,c.result_json,r.status "
                "FROM result_cache c "
                "JOIN records r ON r.review_id=c.source_review_id "
                "JOIN source_rows cs ON cs.review_id=c.source_review_id "
                "WHERE c.text_sha256=? AND c.label_config=? AND cs.review_text=?",
                (digest, label_config, source["review_text"]),
            ).fetchone()
            if cached and cached["status"] == "completed":
                result = json.loads(cached["result_json"])
                self.save_cached_record(source, result, label_config, cached["source_review_id"])
                continue
            yield source

    def expand_new_duplicate_cache_hits(
        self, label_config: str, text_hashes: Iterable[str] | None = None
    ) -> int:
        """Copy exact-text cache hits to rows that do not yet carry ``label_config``.

        When ``text_hashes`` is given (the texts just saved by one batch), only rows
        with those hashes are examined, so each batch costs O(batch) instead of a
        scan of every pending row.
        """
        query = """
            SELECT s.*
            FROM source_rows s
            LEFT JOIN records r ON r.review_id=s.review_id
            WHERE TRIM(s.review_text) <> ''
              AND (
                r.review_id IS NULL
                OR r.status <> 'completed'
                OR COALESCE(r.label_config,'') <> ?
              )
        """
        parameters: list[Any] = [label_config]
        if text_hashes is not None:
            hashes = sorted(set(text_hashes))
            if not hashes:
                return 0
            query += " AND s.text_sha256 IN (%s)" % ",".join("?" for _ in hashes)
            parameters.extend(hashes)
        rows = self.connection.execute(query + " ORDER BY s.rowid", parameters).fetchall()
        saved = 0
        for row in rows:
            source = dict(row)
            cached = self.connection.execute(
                "SELECT c.source_review_id,c.result_json FROM result_cache c "
                "JOIN source_rows cs ON cs.review_id=c.source_review_id "
                "WHERE c.text_sha256=? AND c.label_config=? AND cs.review_text=?",
                (source["text_sha256"], label_config, source["review_text"]),
            ).fetchone()
            if cached:
                self.save_cached_record(
                    source, json.loads(cached["result_json"]), label_config, cached["source_review_id"]
                )
                saved += 1
        return saved

    def save_completed(
        self,
        source: dict[str, str],
        result: dict[str, Any],
        label_config: str,
        attempt_count: int,
    ) -> None:
        stored_result = {key: value for key, value in result.items() if key != "review_id"}
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO records(
                    review_id,source_sha256,status,topic,intent,sentiment,severity,
                    entities_json,evidence_quote,needs_review,label_config,attempt_count,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(review_id) DO UPDATE SET
                    source_sha256=excluded.source_sha256,status=excluded.status,topic=excluded.topic,
                    intent=excluded.intent,sentiment=excluded.sentiment,severity=excluded.severity,
                    entities_json=excluded.entities_json,evidence_quote=excluded.evidence_quote,
                    needs_review=excluded.needs_review,label_config=excluded.label_config,
                    cache_source_id=NULL,reason=NULL,attempt_count=excluded.attempt_count,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    source["review_id"], source["source_sha256"], "completed", result["topic"],
                    result["intent"], result["sentiment"], result["severity"],
                    canonical(result["entities"]), result["evidence_quote"],
                    int(result["needs_review"]), label_config, attempt_count,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO result_cache(text_sha256,label_config,source_review_id,result_json) "
                "VALUES(?,?,?,?)",
                (source["text_sha256"], label_config, source["review_id"], canonical(stored_result)),
            )

    def save_cached_record(
        self,
        source: dict[str, str],
        result: dict[str, Any],
        label_config: str,
        cache_source_id: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO records(
                    review_id,source_sha256,status,topic,intent,sentiment,severity,
                    entities_json,evidence_quote,needs_review,label_config,cache_source_id,
                    attempt_count,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,CURRENT_TIMESTAMP)
                """,
                (
                    source["review_id"], source["source_sha256"], "completed", result["topic"],
                    result["intent"], result["sentiment"], result["severity"],
                    canonical(result["entities"]), result["evidence_quote"],
                    int(result["needs_review"]), label_config, cache_source_id,
                ),
            )

    def quarantine(self, review_id: str, source_sha256: str, reason: str, attempts: int = 0) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO records(
                    review_id,source_sha256,status,reason,attempt_count,updated_at
                ) VALUES(?,?,'quarantined',?,?,CURRENT_TIMESTAMP)
                """,
                (review_id, source_sha256, reason, attempts),
            )

    def log_call(self, event: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO calls(
                    request_id,run_id,role,provider,review_ids_json,model,effort,phase,outcome,
                    label_config,prompt_version,schema_version,input_tokens,cached_input_tokens,
                    output_tokens,duration_seconds,billing_mode,usage_status,
                    provider_metadata_json,error,fallback
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event["request_id"], event["run_id"], event["role"],
                    event.get("provider", "unknown"), canonical(event["review_ids"]),
                    event["model"], event["effort"],
                    event["phase"], event["outcome"], event["label_config"],
                    event["prompt_version"], event["schema_version"],
                    event.get("input_tokens"), event.get("cached_input_tokens"),
                    event.get("output_tokens"), event["duration_seconds"],
                    event.get("billing_mode", "unknown"),
                    event.get("usage_status", "unknown"),
                    canonical(event["provider_metadata"])
                    if event.get("provider_metadata") is not None
                    else None,
                    event.get("error"),
                    int(event.get("fallback", False)),
                ),
            )

    def completed_ids(self) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT review_id FROM records WHERE status='completed' ORDER BY review_id"
            )
        ]

    def completed_records(self) -> Iterator[dict[str, Any]]:
        query = """
        SELECT r.*,s.review_text,s.text_sha256
        FROM records r JOIN source_rows s USING(review_id)
        WHERE r.status='completed' ORDER BY s.rowid
        """
        for row in self.connection.execute(query):
            value = dict(row)
            value["entities"] = json.loads(value.pop("entities_json"))
            value["needs_review"] = bool(value["needs_review"])
            yield value

    def record_for_id(self, review_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT r.*,s.review_text,s.text_sha256 FROM records r JOIN source_rows s USING(review_id) "
            "WHERE r.review_id=?",
            (review_id,),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        if value.get("entities_json") is not None:
            value["entities"] = json.loads(value.pop("entities_json"))
            value["needs_review"] = bool(value["needs_review"])
        return value

    def call_rows(self) -> Iterator[dict[str, Any]]:
        for row in self.connection.execute("SELECT * FROM calls ORDER BY rowid"):
            value = dict(row)
            value["review_ids"] = json.loads(value.pop("review_ids_json"))
            metadata = value.pop("provider_metadata_json", None)
            value["provider_metadata"] = json.loads(metadata) if metadata else None
            value["fallback"] = bool(value["fallback"])
            yield value

    def upsert_source_rows(self, rows: Iterable[tuple[Any, ...]]) -> None:
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO source_rows(
                    review_id,review_text,review_rating,review_likes,app_version,
                    review_timestamp,source_sha256,text_sha256
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(review_id) DO UPDATE SET
                    review_text=excluded.review_text,review_rating=excluded.review_rating,
                    review_likes=excluded.review_likes,app_version=excluded.app_version,
                    review_timestamp=excluded.review_timestamp,
                    source_sha256=excluded.source_sha256,text_sha256=excluded.text_sha256
                """,
                rows,
            )

    def rebuild_texts(self) -> int:
        """Materialize one deterministic row per non-empty exact text."""

        with self.transaction() as connection:
            connection.execute("DELETE FROM texts")
            connection.execute(
                """
                INSERT INTO texts(
                    text_sha256,normalized_text_sha256,review_text,
                    representative_review_id,group_size,word_count,first_source_rowid
                )
                SELECT text_sha256,'',review_text,review_id,group_size,word_count,source_rowid
                FROM (
                    SELECT text_sha256,review_text,review_id,rowid AS source_rowid,
                           COUNT(*) OVER (PARTITION BY text_sha256,review_text) AS group_size,
                           ROW_NUMBER() OVER (
                               PARTITION BY text_sha256,review_text ORDER BY rowid
                           ) AS text_rank,
                           CASE WHEN TRIM(review_text)='' THEN 0
                                ELSE LENGTH(TRIM(review_text))
                                     - LENGTH(REPLACE(TRIM(review_text),' ','')) + 1
                           END AS word_count
                    FROM source_rows WHERE TRIM(review_text)<>''
                ) WHERE text_rank=1
                ORDER BY source_rowid
                """
            )
            rows = connection.execute(
                "SELECT text_sha256,review_text FROM texts"
            ).fetchall()
            connection.executemany(
                "UPDATE texts SET normalized_text_sha256=? WHERE text_sha256=?",
                (
                    (
                        hashlib.sha256(
                            " ".join(
                                unicodedata.normalize("NFKC", str(row["review_text"]))
                                .casefold()
                                .split()
                            ).encode("utf-8")
                        ).hexdigest(),
                        row["text_sha256"],
                    )
                    for row in rows
                ),
            )
            return int(connection.execute("SELECT COUNT(*) FROM texts").fetchone()[0])

    def refresh_normalized_text_hashes(self) -> int:
        rows = self.connection.execute("SELECT text_sha256,review_text FROM texts")
        updated = 0
        with self.transaction() as connection:
            batch: list[tuple[str, str]] = []
            for row in rows:
                normalized = " ".join(
                    unicodedata.normalize("NFKC", str(row["review_text"]))
                    .casefold()
                    .split()
                )
                batch.append((hashlib.sha256(normalized.encode("utf-8")).hexdigest(), row["text_sha256"]))
                if len(batch) == 5000:
                    connection.executemany(
                        "UPDATE texts SET normalized_text_sha256=? WHERE text_sha256=?", batch
                    )
                    updated += len(batch)
                    batch = []
            if batch:
                connection.executemany(
                    "UPDATE texts SET normalized_text_sha256=? WHERE text_sha256=?", batch
                )
                updated += len(batch)
        self.set_metadata("normalized_text_hash_version", "NFKC-casefold-whitespace-v1")
        return updated

    def reserve_luna(
        self,
        *,
        bucket: str,
        text_hashes: list[str],
        bucket_limit: int,
        global_limit: int,
        stop_after_new: int | None = None,
    ) -> list[str]:
        """Atomically reserve distinct texts without exceeding any declared cap."""

        ordered = list(dict.fromkeys(text_hashes))
        if len(ordered) != len(text_hashes):
            raise ValueError("Luna reservations must contain distinct text hashes")
        with self.transaction() as connection:
            used_global = int(
                connection.execute(
                    "SELECT COUNT(*) FROM luna_ledger WHERE status IN ('reserved','succeeded')"
                ).fetchone()[0]
            )
            used_bucket = int(
                connection.execute(
                    "SELECT COUNT(*) FROM luna_ledger "
                    "WHERE bucket=? AND status IN ('reserved','succeeded')",
                    (bucket,),
                ).fetchone()[0]
            )
            remaining = min(bucket_limit - used_bucket, global_limit - used_global)
            if stop_after_new is not None:
                remaining = min(remaining, stop_after_new - used_global)
            if remaining <= 0:
                return []
            selected = ordered[:remaining]
            connection.executemany(
                "INSERT OR IGNORE INTO luna_ledger(bucket,text_sha256,status) "
                "VALUES(?,?,'reserved')",
                ((bucket, digest) for digest in selected),
            )
            return [
                row[0]
                for row in connection.execute(
                    "SELECT text_sha256 FROM luna_ledger "
                    "WHERE bucket=? AND status='reserved' ORDER BY ledger_id",
                    (bucket,),
                )
                if row[0] in set(selected)
            ]

    def settle_luna(
        self,
        *,
        bucket: str,
        text_hashes: list[str],
        request_id: str,
        succeeded: bool,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> None:
        status = "succeeded" if succeeded else "failed"
        with self.transaction() as connection:
            connection.executemany(
                """
                UPDATE luna_ledger
                SET status=?,request_id=?,input_tokens=?,cached_input_tokens=?,
                    output_tokens=?,duration_seconds=?,error=?,settled_at=CURRENT_TIMESTAMP
                WHERE bucket=? AND text_sha256=? AND status='reserved'
                """,
                (
                    (
                        status,
                        request_id,
                        input_tokens,
                        cached_input_tokens,
                        output_tokens,
                        duration_seconds,
                        error,
                        bucket,
                        digest,
                    )
                    for digest in text_hashes
                ),
            )

    def settle_luna_batch(
        self,
        *,
        items: list[tuple[str, str]],
        request_id: str,
        succeeded: bool,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> None:
        """Settle a possibly cross-bucket call and allocate call totals exactly once."""

        if not items:
            raise ValueError("Cannot settle an empty Luna batch")

        def allocate(total: int | None) -> list[int | None]:
            if total is None:
                return [None] * len(items)
            quotient, remainder = divmod(int(total), len(items))
            return [quotient + (1 if index < remainder else 0) for index in range(len(items))]

        allocated_input = allocate(input_tokens)
        allocated_cached = allocate(cached_input_tokens)
        allocated_output = allocate(output_tokens)
        allocated_duration = (
            [None] * len(items)
            if duration_seconds is None
            else [float(duration_seconds) / len(items)] * len(items)
        )
        status = "succeeded" if succeeded else "failed"
        with self.transaction() as connection:
            connection.executemany(
                """
                UPDATE luna_ledger
                SET status=?,request_id=?,input_tokens=?,cached_input_tokens=?,
                    output_tokens=?,duration_seconds=?,error=?,settled_at=CURRENT_TIMESTAMP
                WHERE bucket=? AND text_sha256=?
                  AND status IN ('reserved','succeeded','failed')
                """,
                (
                    (
                        status,
                        request_id,
                        allocated_input[index],
                        allocated_cached[index],
                        allocated_output[index],
                        allocated_duration[index],
                        error,
                        bucket,
                        digest,
                    )
                    for index, (bucket, digest) in enumerate(items)
                ),
            )

    def save_teacher_batch(
        self,
        *,
        rows: list[tuple[str, str, str, str, dict[str, Any], str | None]],
    ) -> None:
        """Save teacher labels in one transaction.

        Each row is (text_sha256, representative_id, source_kind, label_config,
        result, request_id).
        """

        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO teacher_labels(
                    text_sha256,representative_review_id,source_kind,label_config,
                    result_json,call_request_id
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(text_sha256) DO UPDATE SET
                    representative_review_id=excluded.representative_review_id,
                    source_kind=excluded.source_kind,label_config=excluded.label_config,
                    result_json=excluded.result_json,call_request_id=excluded.call_request_id
                """,
                (
                    (digest, review_id, source_kind, label_config, canonical(result), request_id)
                    for digest, review_id, source_kind, label_config, result, request_id in rows
                ),
            )

    def finalize_text_batch(
        self,
        *,
        rows: list[
            tuple[str, dict[str, Any], str]
            | tuple[str, dict[str, Any], str, str]
        ],
    ) -> int:
        """Finalize representatives and all exact duplicates without cache chains.

        A fourth tuple value may override the materialized representative.  This is
        required for imported teacher labels: the deterministic first source row is
        not necessarily the row that was actually sent to the teacher model.  The
        override therefore keeps every non-cache record linked to a successful call.
        """

        if not rows:
            return 0
        hashes = [row[0] for row in rows]
        if len(hashes) != len(set(hashes)):
            raise ValueError("Each exact text may be finalized only once per batch")
        saved = 0
        with self.transaction() as connection:
            for item in rows:
                digest, result, label_config = item[:3]
                representative_override = item[3] if len(item) == 4 else None
                text_row = connection.execute(
                    "SELECT representative_review_id,review_text FROM texts WHERE text_sha256=?",
                    (digest,),
                ).fetchone()
                if text_row is None:
                    raise KeyError(f"Unknown text_sha256: {digest}")
                representative_id = str(
                    representative_override or text_row["representative_review_id"]
                )
                source_rows = connection.execute(
                    "SELECT * FROM source_rows WHERE text_sha256=? AND review_text=? ORDER BY rowid",
                    (digest, text_row["review_text"]),
                ).fetchall()
                if representative_id not in {
                    str(source["review_id"]) for source in source_rows
                }:
                    raise ValueError(
                        f"Representative {representative_id!r} is not an exact-text member"
                    )
                values = []
                for source in source_rows:
                    review_id = str(source["review_id"])
                    values.append(
                        (
                            review_id,
                            source["source_sha256"],
                            "completed",
                            result["topic"],
                            result["intent"],
                            result["sentiment"],
                            result["severity"],
                            canonical(result["entities"]),
                            result["evidence_quote"],
                            int(result["needs_review"]),
                            label_config,
                            None if review_id == representative_id else representative_id,
                            1 if review_id == representative_id else 0,
                        )
                    )
                connection.executemany(
                    """
                    INSERT INTO records(
                        review_id,source_sha256,status,topic,intent,sentiment,severity,
                        entities_json,evidence_quote,needs_review,label_config,cache_source_id,
                        attempt_count,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(review_id) DO UPDATE SET
                        source_sha256=excluded.source_sha256,status=excluded.status,
                        topic=excluded.topic,intent=excluded.intent,sentiment=excluded.sentiment,
                        severity=excluded.severity,entities_json=excluded.entities_json,
                        evidence_quote=excluded.evidence_quote,needs_review=excluded.needs_review,
                        label_config=excluded.label_config,cache_source_id=excluded.cache_source_id,
                        reason=NULL,attempt_count=excluded.attempt_count,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    values,
                )
                connection.execute(
                    """
                    INSERT INTO result_cache(text_sha256,label_config,source_review_id,result_json)
                    VALUES(?,?,?,?)
                    ON CONFLICT(text_sha256,label_config) DO UPDATE SET
                        source_review_id=excluded.source_review_id,result_json=excluded.result_json
                    """,
                    (
                        digest,
                        label_config,
                        representative_id,
                        canonical({key: value for key, value in result.items() if key != "review_id"}),
                    ),
                )
                saved += len(values)
        return saved
