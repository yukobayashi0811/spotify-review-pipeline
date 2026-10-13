#!/usr/bin/env python3
"""Load the saved, committed pipeline outputs into the dashboard database.

Zero model calls. The script only reads files that already exist in this
repository (plus the raw source CSV, which is not committed) and copies them into
PostgreSQL. It then recomputes the baseline ranking and the memo claims inside the
database and compares them with the committed grading export. The receipt is
written to artifacts/dashboard/db_load_receipt.json; it never contains the
connection string.

Usage (from the repository root):

    export DATABASE_URL='postgresql://...'   # Supabase "Session pooler" or direct URI
    python dashboard/db/load.py

Requires: Python 3.10+, psycopg 3 (`pip install "psycopg[binary]"`).
The raw CSV must be at data/spotify_reviews_18months.csv (SHA-256 is checked).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOURCE_FIELDS = ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp"]
EXPECTED = {"source_rows": 660622, "completed": 660609, "quarantined": 13}
DEFAULT_RUN_DIR = "artifacts/full_run_v4"


# Refreshed in dependency order after the COPY (topic_rollup reads issue_ranking).
MATERIALIZED_VIEWS = (
    "issue_ranking",
    "topic_rollup",
    "monthly_trend",
    "corpus_counts",
    "label_source_counts",
    "issue_member_order",
)


def canonical(value) -> str:
    # Same convention as check_submission.py row_sha().
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def row_sha(row: dict) -> str:
    return hashlib.sha256(canonical([row[k] for k in SOURCE_FIELDS]).encode("utf-8")).hexdigest()


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def open_records(grading: Path):
    plain, compressed = grading / "records.jsonl", grading / "records.jsonl.gz"
    if plain.exists():
        return plain, plain.open(encoding="utf-8")
    return compressed, gzip.open(compressed, "rt", encoding="utf-8")


def label_source(label_config: str) -> tuple[str, str]:
    model = label_config.split(":", 1)[0]
    if model.startswith("gpt-6-luna"):
        return "gpt-6-luna", model
    if model.startswith("distilled-"):
        return "local_classifier", model
    raise ValueError(f"Unrecognised label_config: {label_config[:80]}")


def to_int(value: str):
    value = (value or "").strip()
    return int(float(value)) if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--raw-csv", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--run-dir", default=DEFAULT_RUN_DIR, help="Saved full-corpus run folder")
    args = parser.parse_args()

    try:
        import psycopg
    except ImportError:
        print('psycopg 3 is required: pip install "psycopg[binary]"', file=sys.stderr)
        return 2

    root = args.root.resolve()
    grading = root / "grading"
    full = root / args.run_dir
    run_rel = args.run_dir.rstrip("/")
    expected = dict(EXPECTED)
    expected["membership_rows"] = len(read_csv(grading / "membership.csv"))
    raw_csv = args.raw_csv or root / "data" / "spotify_reviews_18months.csv"
    receipt_path = args.receipt or root / "artifacts" / "dashboard" / "db_load_receipt.json"
    url = os.environ.get(args.database_url_env)
    if not url:
        print(f"Set {args.database_url_env} to the database connection string.", file=sys.stderr)
        return 2

    started = time.perf_counter()
    run = read_json(grading / "run.json")
    inputs: dict[str, str] = {}

    print("Checking raw source CSV ...", flush=True)
    raw_sha = file_sha(raw_csv)
    if raw_sha != run["analysis_sha256"]:
        print(f"Raw CSV SHA-256 {raw_sha} does not match grading/run.json", file=sys.stderr)
        return 1
    inputs["data/spotify_reviews_18months.csv"] = raw_sha
    source: dict[str, dict] = {}
    with raw_csv.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            source[row["review_id"]] = row
    if len(source) != EXPECTED["source_rows"]:
        print(f"Unexpected source row count {len(source)}", file=sys.stderr)
        return 1

    records_path, stream = open_records(grading)
    inputs[str(records_path.relative_to(root))] = file_sha(records_path)
    configs: dict[str, list] = {}
    with stream:
        for line in stream:
            rec = json.loads(line)
            if rec.get("label_config"):
                configs.setdefault(rec["label_config"], [len(configs) + 1, 0])[1] += 1

    small_files = {
        "grading/run.json": grading / "run.json",
        "grading/ingestion.json": grading / "ingestion.json",
        "grading/membership.csv": grading / "membership.csv",
        "grading/ranking.csv": grading / "ranking.csv",
        "grading/claims.csv": grading / "claims.csv",
        "grading/calls.jsonl": grading / "calls.jsonl",
        f"{run_rel}/issues.csv": full / "issues.csv",
        f"{run_rel}/claims_provenance.csv": full / "claims_provenance.csv",
        f"{run_rel}/memo_payload.json": full / "memo_payload.json",
        f"{run_rel}/memo_evidence_pack.json": full / "memo_evidence_pack.json",
        f"{run_rel}/memo.md": full / "memo.md",
        f"{run_rel}/topic_rollup.csv": full / "topic_rollup.csv",
        f"{run_rel}/run_summary.json": full / "run_summary.json",
        f"{run_rel}/sensitivity_analysis.json": full / "sensitivity_analysis.json",
        f"{run_rel}/verification/verification_report.json": full / "verification" / "verification_report.json",
        "artifacts/full_run_v2/grouping_comparison.json": root / "artifacts" / "full_run_v2" / "grouping_comparison.json",
        "evals/system_tests/system_test_report.json": root / "evals" / "system_tests" / "system_test_report.json",
        "evals/recovery_v4/interrupt_resume_check.json": root / "evals" / "recovery_v4" / "interrupt_resume_check.json",
        "cost/report.json": root / "cost" / "report.json",
    }
    golden_eval = root / "evals" / "golden_final_evaluation.json"
    if golden_eval.exists():
        small_files["evals/golden_final_evaluation.json"] = golden_eval
    holdout_eval = root / "evals" / "holdout_30_evaluation.json"
    if holdout_eval.exists():
        small_files["evals/holdout_30_evaluation.json"] = holdout_eval
    actuals_path = root / "cost" / "full_run_v4_actuals.json"
    if actuals_path.exists():
        small_files["cost/full_run_v4_actuals.json"] = actuals_path
    for name, path in small_files.items():
        inputs[name] = file_sha(path)

    payload = read_json(full / "memo_payload.json")
    pack = read_json(full / "memo_evidence_pack.json")
    run_summary = read_json(full / "run_summary.json")
    provenance = {row["claim_id"]: row for row in read_csv(full / "claims_provenance.csv")}
    payload_claims = {claim["claim_id"]: claim for claim in payload["claims"]}

    print("Connecting and loading ...", flush=True)
    sha_mismatches = 0
    with psycopg.connect(url, autocommit=False) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        conn.execute("SET statement_timeout = 0")
        conn.execute((Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8"))
        with conn.cursor() as cur:
            for config, (config_id, count) in configs.items():
                kind, model = label_source(config)
                cur.execute(
                    "INSERT INTO spotify.label_configs VALUES (%s,%s,%s,%s,%s)",
                    (config_id, config, kind, model, count),
                )

            _, stream = open_records(grading)
            columns = ("review_id, source_sha256, status, quarantine_reason, topic, intent, sentiment, severity, "
                       "needs_review, entities, evidence_quote, label_config_id, cache_source_id, review_text, "
                       "review_rating, review_likes, app_version, review_timestamp")
            with stream, cur.copy(f"COPY spotify.reviews ({columns}) FROM STDIN") as copy:
                for line in stream:
                    rec = json.loads(line)
                    src = source[rec["review_id"]]
                    if row_sha(src) != rec["source_sha256"]:
                        sha_mismatches += 1
                    completed = rec["status"] == "completed"
                    copy.write_row((
                        rec["review_id"], rec["source_sha256"], rec["status"], rec.get("reason"),
                        rec.get("topic"), rec.get("intent"), rec.get("sentiment"), rec.get("severity"),
                        rec.get("needs_review"),
                        json.dumps(rec.get("entities"), ensure_ascii=False) if completed else None,
                        rec.get("evidence_quote"),
                        configs[rec["label_config"]][0] if rec.get("label_config") else None,
                        rec.get("cache_source_id"),
                        src["review_text"], to_int(src["review_rating"]), to_int(src["review_likes"]),
                        src["app_version"] or None, src["review_timestamp"] or None,
                    ))
            if sha_mismatches:
                raise SystemExit(f"{sha_mismatches} source_sha256 mismatches between raw CSV and records")

            for row in read_csv(full / "issues.csv"):
                cur.execute(
                    "INSERT INTO spotify.issues VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (row["issue_id"], row["topic"], row["issue_name"], row["description"],
                     row["coherent"] == "yes", row["actionable"] == "yes",
                     [flag for flag in row["flags"].split(";") if flag], row["label_config"]),
                )
            with cur.copy("COPY spotify.issue_membership (issue_id, review_id) FROM STDIN") as copy:
                for row in read_csv(grading / "membership.csv"):
                    copy.write_row((row["issue_id"], row["review_id"]))
            for row in read_csv(grading / "ranking.csv"):
                cur.execute(
                    "INSERT INTO spotify.saved_ranking VALUES (%s,%s,%s,%s,%s,%s)",
                    (int(row["rank"]), row["issue_id"], int(row["complaint_count"]), int(row["severity_sum"]),
                     row["mean_severity"], int(row["priority_score"])),
                )

            memo_request_ids = sorted({row.get("request_id") for row in provenance.values() if row.get("request_id")})
            memo_calls = []
            with (grading / "calls.jsonl").open(encoding="utf-8") as calls:
                for line in calls:
                    call = json.loads(line)
                    if call.get("role") == "memo" and call.get("request_id") in memo_request_ids:
                        memo_calls.append(call)
            if len(memo_calls) != 1 or memo_calls[0].get("outcome") != "succeeded":
                raise SystemExit("Expected exactly one succeeded memo call matching claims_provenance.csv")
            cur.execute(
                "INSERT INTO spotify.memo VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("final", payload["priority_issue_id"], payload["priority_topic"], payload["priority"],
                 payload["rationale"], payload["baseline_top_handling"], payload["sensitivity_interpretation"],
                 payload["alternative_issue_ids"], json.dumps(payload["alternatives"], ensure_ascii=False),
                 json.dumps(payload["limitations"], ensure_ascii=False),
                 (full / "memo.md").read_text(encoding="utf-8"),
                 memo_calls[0]["model"], memo_calls[0].get("label_config"), memo_calls[0]["request_id"],
                 json.dumps(payload, ensure_ascii=False)),
            )
            for row in read_csv(grading / "claims.csv"):
                extra = payload_claims.get(row["claim_id"], {})
                prov = provenance.get(row["claim_id"], {})
                cur.execute(
                    "INSERT INTO spotify.claims VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (row["claim_id"], row["issue_id"], row["metric"], row["value"], extra.get("sentence"),
                     extra.get("review_ids", []), prov.get("source"), prov.get("reason"), prov.get("request_id")),
                )
            for issue_id, items in pack["evidence"].items():
                for ordinal, item in enumerate(items, 1):
                    cur.execute(
                        "INSERT INTO spotify.memo_evidence VALUES (%s,%s,%s,%s,%s)",
                        (issue_id, ordinal, item["review_id"], item.get("severity"), item.get("evidence_quote")),
                    )

            verification = read_json(full / "verification" / "verification_report.json")
            sensitivity = read_json(full / "sensitivity_analysis.json")
            cost = read_json(root / "cost" / "report.json")
            by_source = verification.get("by_label_source", {})

            def verifier_stratum(key: str) -> dict:
                value = by_source.get(key) or {}
                return {
                    "distinct_texts": value.get("verified_texts", 0),
                    "row_weighted_topic_agreement": value.get("row_weighted_topic_agreement"),
                    "row_weighted_intent_agreement": value.get("row_weighted_intent_agreement"),
                    "row_weighted_severity_exact": value.get("row_weighted_severity_exact_agreement"),
                    "quote_supported_rate": None,
                }

            label_counts: dict[str, int] = {}
            for config, (_, count) in configs.items():
                kind = label_source(config)[0]
                label_counts[kind] = label_counts.get(kind, 0) + count
            measured = cost.get("measured_pilot", {})
            luna_usage = run_summary.get("new_model_calls_this_run", {}).get("luna_usage_tokens", {})
            facts = {
                "source": (run, "grading/run.json"),
                "ingestion": (read_json(grading / "ingestion.json"), "grading/ingestion.json"),
                "state_counts": (run_summary.get("counts"), f"{run_rel}/run_summary.json"),
                "final_label_sources": (label_counts, "grading/records.jsonl.gz"),
                "verification": ({
                    "selection": verification.get("sampling"),
                    "metrics_by_final_source": {
                        "local": verifier_stratum("local_classifier"),
                        "luna": verifier_stratum("gpt-6-luna"),
                    },
                    "disagreement_count": len(verification.get("disagreements", [])),
                }, f"{run_rel}/verification/verification_report.json"),
                "sensitivity": ({
                    "method": sensitivity.get("method"),
                    "rank_stability": sensitivity.get("rank_stability"),
                    "topics": sensitivity.get("topics"),
                }, f"{run_rel}/sensitivity_analysis.json"),
                "grouping_comparison": (
                    read_json(root / "artifacts" / "full_run_v2" / "grouping_comparison.json"),
                    "artifacts/full_run_v2/grouping_comparison.json",
                ),
                "system_tests": (read_json(root / "evals" / "system_tests" / "system_test_report.json").get("checks"), "evals/system_tests/system_test_report.json"),
                "interrupt_resume": (read_json(root / "evals" / "recovery_v4" / "interrupt_resume_check.json"), "evals/recovery_v4/interrupt_resume_check.json"),
                "cost_pilot": ({
                    "cold_wall_seconds": measured.get("cold", {}).get("wall_seconds"),
                    "cold_api_equivalent_usd": measured.get("cold", {}).get("api_equivalent_usd"),
                    "warm_wall_seconds": measured.get("warm", {}).get("wall_seconds"),
                    "warm_new_enrichment_calls": measured.get("warm", {}).get("new_enrichment_requests"),
                }, "cost/report.json"),
                "cost_projection": (cost.get("projections"), "cost/report.json"),
                "full_run_luna_usage": ({
                    "tokens": luna_usage,
                    "requests_by_role": run_summary.get("new_model_calls_this_run", {}).get("luna_by_role"),
                    "note": "last invocation of the full run; see the run folder's run_log.jsonl for every invocation",
                }, f"{run_rel}/run_summary.json"),
            }
            if actuals_path.exists():
                actuals = read_json(actuals_path)
                facts["full_run_luna_usage"] = ({
                    "requests_total": actuals.get("luna_requests_total"),
                    "failed_attempts_total": actuals.get("luna_failed_attempts_total"),
                    "requests_by_role": {
                        role: value.get("attempts")
                        for role, value in (actuals.get("luna_requests_by_role") or {}).items()
                    },
                    "api_equivalent_usd": actuals.get("api_equivalent_usd"),
                    "wall_seconds": actuals.get("wall_seconds"),
                    "note": "every invocation of the full run (the deliberately interrupted one and the resume)",
                }, "cost/full_run_v4_actuals.json")
            if golden_eval.exists():
                facts["golden_evaluation"] = (read_json(golden_eval), "evals/golden_final_evaluation.json")
            if holdout_eval.exists():
                facts["holdout_evaluation"] = (read_json(holdout_eval), "evals/holdout_30_evaluation.json")
            for key, (value, source_file) in facts.items():
                cur.execute(
                    "INSERT INTO spotify.run_facts VALUES (%s,%s,%s)",
                    (key, json.dumps(value, ensure_ascii=False), source_file),
                )
            # Materialized views are computed by PostgreSQL from the rows just loaded.
            for view in MATERIALIZED_VIEWS:
                cur.execute(f"REFRESH MATERIALIZED VIEW spotify.{view}")
        conn.commit()
        conn.autocommit = True
        # VACUUM sets the visibility map after the bulk COPY; ANALYZE refreshes planner stats.
        conn.execute("VACUUM (ANALYZE) spotify.reviews")
        conn.execute("VACUUM (ANALYZE) spotify.issue_membership")
        conn.execute("ANALYZE")
        # The schema is recreated on every load, so re-grant the read-only API role if it exists.
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader'").fetchone():
            conn.execute("GRANT USAGE ON SCHEMA spotify TO dashboard_reader")
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA spotify TO dashboard_reader")
            print("Re-granted read access to dashboard_reader.", flush=True)

        print("Verifying database against the committed export ...", flush=True)
        checks: dict[str, dict] = {}
        counts = dict(conn.execute("SELECT status, count(*) FROM spotify.reviews GROUP BY status").fetchall())
        counts["source_rows"] = sum(counts.values())
        counts["membership_rows"] = conn.execute("SELECT count(*) FROM spotify.issue_membership").fetchone()[0]
        checks["row_counts"] = {"expected": expected, "actual": counts,
                                "passed": all(counts.get(k) == v for k, v in expected.items())}
        checks["source_sha256_matches_raw_csv"] = {"mismatches": sha_mismatches, "passed": sha_mismatches == 0}

        fields = ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"]
        db_rank = [dict(zip(fields, map(str, row))) for row in conn.execute(
            f"SELECT {', '.join(fields)} FROM spotify.issue_ranking ORDER BY rank").fetchall()]
        saved_rank = [{k: row[k] for k in fields} for row in read_csv(grading / "ranking.csv")]
        checks["ranking_recomputed_in_db_equals_grading_ranking_csv"] = {
            "issues": len(db_rank), "passed": db_rank == saved_rank}

        db_by_issue = {row["issue_id"]: row for row in db_rank}
        claim_results = []
        for row in read_csv(grading / "claims.csv"):
            actual = db_by_issue.get(row["issue_id"], {}).get(row["metric"])
            claim_results.append({"claim_id": row["claim_id"], "expected": row["value"], "db": actual,
                                  "passed": actual == row["value"]})
        checks["claims_recomputed_in_db"] = {"claims": claim_results,
                                             "passed": all(item["passed"] for item in claim_results)}

        topic_fields = ["rank", "topic", "complaint_count", "severity_sum", "mean_severity", "priority_score"]
        db_topics = [dict(zip(topic_fields, map(str, row))) for row in conn.execute(
            f"SELECT {', '.join(topic_fields)} FROM spotify.topic_rollup ORDER BY rank").fetchall()]
        saved_topics = [{k: row[k] for k in topic_fields} for row in read_csv(full / "topic_rollup.csv")]
        checks["topic_rollup_equals_saved"] = {"passed": db_topics == saved_topics}

        memo_issue = conn.execute("SELECT priority_issue_id FROM spotify.memo").fetchone()[0]
        checks["memo_priority_issue_in_db_ranking"] = {"issue_id": memo_issue, "passed": memo_issue in db_by_issue}

        size = conn.execute(
            "SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'spotify' AND c.relkind IN ('r','m')"
        ).fetchone()[0]

    passed = all(check["passed"] for check in checks.values())
    receipt = {
        "status": "pass" if passed else "fail",
        "loaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_calls": 0,
        "database": {"server_version": server_version, "schema": "spotify", "total_relation_size": size},
        "input_files_sha256": inputs,
        "checks": checks,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "note": "The connection string is intentionally not recorded.",
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: receipt[k] for k in ("status", "database", "wall_seconds")}, indent=2))
    for name, check in checks.items():
        print(f"  {'PASS' if check['passed'] else 'FAIL'}  {name}")
    print(f"Receipt: {receipt_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
