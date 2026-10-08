from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from .constants import SOURCE_FIELDS
from .io_utils import file_sha, read_csv, row_sha, text_sha, write_json
from .storage import Store


def ingest(
    csv_path: str | Path,
    state_path: str | Path,
    report_path: str | Path,
    chunk_size: int = 5000,
) -> dict[str, Any]:
    csv_path = Path(csv_path)
    input_file_sha = file_sha(csv_path)
    counts = Counter(records=0, duplicate_review_ids=0, empty_review_text=0, missing_app_version=0)
    months: Counter[str] = Counter()
    ratings: Counter[str] = Counter()
    seen_ids: set[str] = set()
    first_review: str | None = None
    last_review: str | None = None
    parsed_digest = hashlib.sha256()
    batch: list[tuple[Any, ...]] = []
    empty_rows: list[tuple[str, str]] = []

    with Store(state_path) as store:
        previous = store.get_metadata("ingestion_report")
        if previous and previous.get("file_sha256") != input_file_sha:
            raise ValueError(
                "State file belongs to a different input CSV. Use a new state path to prevent cross-input cache contamination."
            )
        for row in read_csv(csv_path):
            digest = row_sha(row)
            parsed_digest.update(bytes.fromhex(digest))
            review_id = row["review_id"]
            duplicate = review_id in seen_ids
            counts["records"] += 1
            counts["duplicate_review_ids"] += int(duplicate)
            seen_ids.add(review_id)
            empty = not row["review_text"].strip()
            counts["empty_review_text"] += int(empty)
            counts["missing_app_version"] += int(not row["app_version"].strip())
            months[row["review_timestamp"][:7]] += 1
            ratings[row["review_rating"]] += 1
            first_review = min(first_review, row["review_timestamp"]) if first_review else row["review_timestamp"]
            last_review = max(last_review, row["review_timestamp"]) if last_review else row["review_timestamp"]
            batch.append(
                tuple(row[field] for field in SOURCE_FIELDS)
                + (digest, text_sha(row["review_text"]))
            )
            if len(batch) >= chunk_size:
                store.upsert_source_rows(batch)
                batch.clear()
            if empty:
                empty_rows.append((review_id, digest))
        if batch:
            store.upsert_source_rows(batch)
        for review_id, digest in empty_rows:
            store.quarantine(review_id, digest, "empty_review_text")
        distinct_nonempty = int(
            store.connection.execute(
                "SELECT COUNT(DISTINCT review_text) FROM source_rows WHERE TRIM(review_text)<>''"
            ).fetchone()[0]
        )
        nonempty = counts["records"] - counts["empty_review_text"]
        report = {
            "file_sha256": input_file_sha,
            "parsed_rows_sha256": parsed_digest.hexdigest(),
            "counts": dict(counts),
            "reviews_by_month": dict(months),
            "reviews_by_rating": dict(ratings),
            "first_review": first_review,
            "last_review": last_review,
            "exact_text": {
                "distinct_nonempty_texts": distinct_nonempty,
                "reusable_nonempty_rows": nonempty - distinct_nonempty,
            },
        }
        store.set_metadata("ingestion_report", report)
        store.set_metadata("input_csv", str(csv_path.resolve()))
        store.set_metadata("input_fields", list(SOURCE_FIELDS))
    write_json(report_path, report)
    return report
