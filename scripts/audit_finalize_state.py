from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.io_utils import file_sha, write_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--finalization-report", type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.state) as connection:
        connection.row_factory = sqlite3.Row
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        connection.execute("DROP TABLE IF EXISTS temp.successful_enrich_evidence")
        connection.execute(
            """
            CREATE TEMP TABLE successful_enrich_evidence AS
            SELECT DISTINCT je.value AS review_id,c.label_config
            FROM calls c,json_each(c.review_ids_json) je
            WHERE c.role='enrich' AND c.outcome='succeeded'
            """
        )
        connection.execute(
            "CREATE INDEX temp.evidence_idx ON successful_enrich_evidence(review_id,label_config)"
        )
        counts = dict(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM source_rows) AS source_rows,
                  (SELECT COUNT(*) FROM records) AS record_rows,
                  (SELECT COUNT(*) FROM records WHERE status='completed') AS completed,
                  (SELECT COUNT(*) FROM records WHERE status='quarantined') AS quarantined,
                  (SELECT COUNT(*) FROM texts) AS distinct_nonempty_texts,
                  (SELECT COUNT(*) FROM luna_ledger WHERE status='succeeded') AS luna_succeeded
                """
            ).fetchone()
        )
        partial_groups = [
            dict(row)
            for row in connection.execute(
                """
                WITH finalized AS (
                  SELECT t.text_sha256,t.review_text,t.group_size,
                         SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed_rows
                  FROM texts t
                  JOIN source_rows s
                    ON s.text_sha256=t.text_sha256 AND s.review_text=t.review_text
                  LEFT JOIN records r ON r.review_id=s.review_id
                  GROUP BY t.text_sha256,t.review_text,t.group_size
                )
                SELECT text_sha256,group_size,completed_rows
                FROM finalized
                WHERE completed_rows>0 AND completed_rows<>group_size
                ORDER BY text_sha256
                LIMIT 100
                """
            )
        ]
        partial_group_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT t.text_sha256
                  FROM texts t JOIN source_rows s
                    ON s.text_sha256=t.text_sha256 AND s.review_text=t.review_text
                  LEFT JOIN records r ON r.review_id=s.review_id
                  GROUP BY t.text_sha256,t.review_text,t.group_size
                  HAVING SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END)>0
                     AND SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END)<>t.group_size
                )
                """
            ).fetchone()[0]
        )
        missing_call_evidence = [
            dict(row)
            for row in connection.execute(
                """
                SELECT r.review_id,r.label_config
                FROM records r
                LEFT JOIN successful_enrich_evidence e
                  ON e.review_id=r.review_id AND e.label_config=r.label_config
                WHERE r.status='completed' AND r.cache_source_id IS NULL
                  AND e.review_id IS NULL
                ORDER BY r.review_id LIMIT 100
                """
            )
        ]
        missing_call_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM records r
                LEFT JOIN successful_enrich_evidence e
                  ON e.review_id=r.review_id AND e.label_config=r.label_config
                WHERE r.status='completed' AND r.cache_source_id IS NULL
                  AND e.review_id IS NULL
                """
            ).fetchone()[0]
        )
        cache_chain_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM records child JOIN records parent
                  ON child.cache_source_id=parent.review_id
                WHERE child.cache_source_id IS NOT NULL AND parent.cache_source_id IS NOT NULL
                """
            ).fetchone()[0]
        )
        label_configs = {
            row[0]: int(row[1])
            for row in connection.execute(
                "SELECT COALESCE(label_config,'<NULL>'),COUNT(*) FROM records GROUP BY label_config"
            )
        }
        empty_quarantine = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM records r JOIN source_rows s USING(review_id)
                WHERE r.status='quarantined' AND r.reason='empty_review_text'
                  AND TRIM(s.review_text)=''
                """
            ).fetchone()[0]
        )
        verification_rows = int(
            connection.execute("SELECT COUNT(*) FROM verifier_records").fetchone()[0]
        )
        verification_missing_final = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM verifier_records v
                LEFT JOIN records r USING(review_id)
                WHERE r.review_id IS NULL OR r.status<>'completed'
                """
            ).fetchone()[0]
        )
        verification_label_mismatches = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM verifier_records v
                JOIN source_rows s USING(review_id)
                JOIN records r USING(review_id)
                LEFT JOIN router_scores rs USING(text_sha256)
                LEFT JOIN teacher_labels tl USING(text_sha256)
                WHERE CASE WHEN v.final_source='local' THEN
                    r.topic<>rs.topic OR r.intent<>rs.intent OR r.severity<>rs.severity
                  ELSE
                    r.topic<>json_extract(tl.result_json,'$.topic')
                    OR r.intent<>json_extract(tl.result_json,'$.intent')
                    OR r.severity<>CAST(json_extract(tl.result_json,'$.severity') AS INTEGER)
                  END
                """
            ).fetchone()[0]
        )
        complete_counts_ok = (
            counts["source_rows"] == 660622
            and counts["completed"] == 660609
            and counts["quarantined"] == 13
            and empty_quarantine == 13
        )
    report = {
        "state_path": str(args.state.resolve()),
        "state_sha256": file_sha(args.state),
        "integrity_check": integrity,
        "counts": counts,
        "partial_finalized_exact_text_groups": partial_group_count,
        "partial_group_examples": partial_groups,
        "completed_representatives_missing_successful_same_config_enrich_call": missing_call_count,
        "missing_call_examples": missing_call_evidence,
        "cache_chain_rows": cache_chain_count,
        "label_config_counts": label_configs,
        "empty_review_text_quarantines": empty_quarantine,
        "complete_counts_ok": complete_counts_ok,
        "verification_400": {
            "rows": verification_rows,
            "missing_final_records": verification_missing_final,
            "final_label_mismatches_against_comparison_baseline": verification_label_mismatches,
            "baseline_rule": (
                "local rows use router_scores; Luna test-retest rows use teacher_labels"
            ),
            "passed": (
                verification_rows == 400
                and verification_missing_final == 0
                and verification_label_mismatches == 0
            ),
        },
        "passed": (
            integrity == ["ok"]
            and partial_group_count == 0
            and missing_call_count == 0
            and cache_chain_count == 0
            and (not args.require_complete or complete_counts_ok)
            and (
                not args.require_complete
                or (
                    verification_rows == 400
                    and verification_missing_final == 0
                    and verification_label_mismatches == 0
                )
            )
        ),
    }
    write_json(args.output, report)
    if args.finalization_report:
        finalization = json.loads(args.finalization_report.read_text(encoding="utf-8"))
        finalization["post_finalize_audit"] = report
        write_json(args.finalization_report, finalization)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
