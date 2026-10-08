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

from spotify_pipeline.io_utils import write_json  # noqa: E402
from spotify_pipeline.storage import Store  # noqa: E402


def snapshot(state: Path, output: Path) -> None:
    with Store(state) as store:
        write_json(output, {"completed_ids": store.completed_ids()})


def status(state: Path) -> None:
    with sqlite3.connect(state) as connection:
        row = connection.execute(
            """
            SELECT
              SUM(status='completed'),SUM(status='quarantined'),
              (SELECT COUNT(*) FROM source_rows WHERE TRIM(review_text)<>'')
                - SUM(status='completed')
            FROM records
            """
        ).fetchone()
    print(json.dumps({"completed": row[0], "quarantined": row[1], "pending_rows": row[2]}))


def verify(
    state: Path,
    before_path: Path,
    after_path: Path,
    initial_report_path: Path,
    resume_report_path: Path,
    output: Path,
) -> int:
    before = set(json.loads(before_path.read_text(encoding="utf-8"))["completed_ids"])
    after = set(json.loads(after_path.read_text(encoding="utf-8"))["completed_ids"])
    initial_run = json.loads(initial_report_path.read_text(encoding="utf-8"))["run_id"]
    resume_run = json.loads(resume_report_path.read_text(encoding="utf-8"))["run_id"]
    with sqlite3.connect(state) as connection:
        connection.execute("CREATE TEMP TABLE before_ids(review_id TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO before_ids VALUES(?)", ((value,) for value in before))
        connection.execute("CREATE TEMP TABLE after_ids(review_id TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO after_ids VALUES(?)", ((value,) for value in after))
        connection.execute(
            """
            CREATE TEMP TABLE successful_initial_ids AS
            SELECT DISTINCT CAST(j.value AS TEXT) review_id
            FROM calls c,json_each(c.review_ids_json) j
            WHERE c.role='enrich' AND c.outcome='succeeded' AND c.phase='initial'
            """
        )
        before_missing_initial_trace = int(connection.execute(
            """
            SELECT COUNT(*) FROM before_ids b JOIN records r USING(review_id)
            LEFT JOIN successful_initial_ids i
              ON i.review_id=COALESCE(r.cache_source_id,r.review_id)
            WHERE i.review_id IS NULL
            """
        ).fetchone()[0])
        resume_sent_before = int(connection.execute(
            """
            SELECT COUNT(*) FROM calls c,json_each(c.review_ids_json) j
            JOIN before_ids b ON b.review_id=CAST(j.value AS TEXT)
            WHERE c.run_id=? AND c.role='enrich' AND c.outcome='succeeded'
            """, (resume_run,)
        ).fetchone()[0])
        connection.execute(
            """
            CREATE TEMP TABLE resume_ids AS
            SELECT DISTINCT CAST(j.value AS TEXT) review_id
            FROM calls c,json_each(c.review_ids_json) j
            WHERE c.run_id=? AND c.role='enrich' AND c.outcome='succeeded'
            """, (resume_run,)
        )
        added_count = len(after - before)
        added_missing_resume_trace = int(connection.execute(
            """
            SELECT COUNT(*) FROM after_ids a
            LEFT JOIN before_ids b USING(review_id)
            JOIN records r ON r.review_id=a.review_id
            LEFT JOIN resume_ids i ON i.review_id=COALESCE(r.cache_source_id,r.review_id)
            WHERE b.review_id IS NULL AND i.review_id IS NULL
            """
        ).fetchone()[0])
        initial_call_count = int(connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id=? AND outcome='succeeded'", (initial_run,)
        ).fetchone()[0])
        resume_call_count = int(connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id=? AND outcome='succeeded'", (resume_run,)
        ).fetchone()[0])
    checks = {
        "before_all_ids_trace_to_successful_initial_enrich": before_missing_initial_trace == 0,
        "resume_sent_zero_before_ids": resume_sent_before == 0,
        "after_minus_before_nonempty": added_count > 0,
        "after_minus_before_all_trace_to_resume_enrich": added_missing_resume_trace == 0,
    }
    report = {
        "initial_run_id": initial_run,
        "resume_run_id": resume_run,
        "before_completed_ids": len(before),
        "after_completed_ids": len(after),
        "after_minus_before": added_count,
        "initial_successful_calls": initial_call_count,
        "resume_successful_calls": resume_call_count,
        "before_ids_missing_initial_trace": before_missing_initial_trace,
        "resume_sent_before_ids": resume_sent_before,
        "added_ids_missing_resume_trace": added_missing_resume_trace,
        "checks": checks,
        "passed": all(checks.values()),
        "trace_rule": "Cache rows trace through their direct cache_source_id to the sent representative.",
    }
    write_json(output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--state", type=Path, required=True)
    snap.add_argument("--output", type=Path, required=True)
    show = sub.add_parser("status")
    show.add_argument("--state", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--state", type=Path, required=True)
    check.add_argument("--before", type=Path, required=True)
    check.add_argument("--after", type=Path, required=True)
    check.add_argument("--initial-report", type=Path, required=True)
    check.add_argument("--resume-report", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(args.state, args.output)
        return 0
    if args.command == "status":
        status(args.state)
        return 0
    return verify(args.state, args.before, args.after, args.initial_report,
                  args.resume_report, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
