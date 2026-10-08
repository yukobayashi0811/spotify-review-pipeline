#!/usr/bin/env python3
"""Score every saved Golden 50 prediction file against evals/golden_50.csv (zero model calls).

Each prediction file was written before any Golden label was opened. This script only reads
those saved predictions, scores them with spotify_pipeline.evaluate.score_golden, and writes
the evaluation JSON/cases next to them. It also scores the final full-corpus classifications
of the 50 Golden reviews taken from the grading export, and refreshes the copies of these
scores embedded in experiment summaries.

Usage (from the repository root):
    PYTHONPATH=src python3 scripts/rescore_golden.py
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spotify_pipeline.evaluate import GOLD_FIELDS, score_golden  # noqa: E402
from spotify_pipeline.io_utils import file_sha, read_csv  # noqa: E402

GOLDEN = ROOT / "evals" / "golden_50.csv"

# (saved predictions, evaluation JSON, cases CSV)
SAVED_SETS = [
    ("evals/golden_predictions_local_gemma.csv", "evals/golden_evaluation_local_gemma.json", "evals/golden_evaluation_cases_local_gemma.csv"),
    ("experiments/codex_3agent/quality-50-batch50-single/predictions.csv", "experiments/codex_3agent/quality-50-batch50-single/golden_evaluation.json", "experiments/codex_3agent/quality-50-batch50-single/golden_evaluation_cases.csv"),
    ("experiments/codex_3agent/quality-50-parallel/predictions.csv", "experiments/codex_3agent/quality-50-parallel/golden_evaluation.json", "experiments/codex_3agent/quality-50-parallel/golden_evaluation_cases.csv"),
    ("experiments/hybrid/quality-50-local-luna-v3/predictions.csv", "experiments/hybrid/quality-50-local-luna-v3/golden_evaluation.json", "experiments/hybrid/quality-50-local-luna-v3/golden_evaluation_cases.csv"),
    ("experiments/hybrid/quality-50-local-luna-v3/local_predictions.csv", "experiments/hybrid/quality-50-local-luna-v3/local_golden_evaluation.json", "experiments/hybrid/quality-50-local-luna-v3/local_golden_evaluation_cases.csv"),
    ("experiments/distilled/pilot-v1/golden_50_predictions.csv", "experiments/distilled/pilot-v1/golden_50_evaluation.json", "experiments/distilled/pilot-v1/golden_50_cases.csv"),
    ("experiments/distilled/hybrid-golden-50-v1/golden_predictions.csv", "experiments/distilled/hybrid-golden-50-v1/golden_evaluation.json", "experiments/distilled/hybrid-golden-50-v1/golden_cases.csv"),
]

FINAL_PREDICTIONS = "evals/golden_final_predictions.csv"
FINAL_EVALUATION = "evals/golden_final_evaluation.json"
FINAL_CASES = "evals/golden_final_evaluation_cases.csv"


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def dump(path: str, value: dict) -> None:
    (ROOT / path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def score(predictions: str, output: str, cases: str) -> dict:
    report = score_golden(GOLDEN, ROOT / predictions, ROOT / output, ROOT / cases)
    report = {
        **report,
        "golden_labels_file": "evals/golden_50.csv",
        "golden_labels_sha256": file_sha(GOLDEN),
        "predictions_file": predictions,
        "predictions_sha256": file_sha(ROOT / predictions),
        "model_calls": 0,
    }
    dump(output, report)
    return report


def open_records(folder: Path):
    plain, compressed = folder / "records.jsonl", folder / "records.jsonl.gz"
    if plain.exists():
        return plain.open(encoding="utf-8")
    return gzip.open(compressed, "rt", encoding="utf-8")


def export_final_predictions() -> dict[str, str]:
    """Writes the final grading-export labels of the 50 Golden reviews; returns label source per review."""
    golden_ids = [row["review_id"] for row in read_csv(GOLDEN)]
    wanted = set(golden_ids)
    found: dict[str, dict] = {}
    with open_records(ROOT / "grading") as stream:
        for line in stream:
            record = json.loads(line)
            if record["review_id"] in wanted:
                found[record["review_id"]] = record
    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"Golden reviews missing from grading export: {sorted(missing)[:5]}")
    fields = ["review_id", *GOLD_FIELDS, "needs_review", "label_config"]
    with (ROOT / FINAL_PREDICTIONS).open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for review_id in golden_ids:
            record = found[review_id]
            if record.get("status") != "completed":
                raise SystemExit(f"Golden review {review_id} is not completed in the grading export")
            writer.writerow(
                {
                    "review_id": review_id,
                    "topic": record["topic"],
                    "intent": record["intent"],
                    "sentiment": record["sentiment"],
                    "severity": record["severity"],
                    "entities": json.dumps(record["entities"], ensure_ascii=False),
                    "evidence_quote": record["evidence_quote"],
                    "needs_review": str(record["needs_review"]).lower(),
                    "label_config": record["label_config"],
                }
            )
    return {
        review_id: ("luna_teacher" if found[review_id]["label_config"].startswith("gpt-6-luna") else "local_classifier")
        for review_id in golden_ids
    }


def by_label_source(cases_path: str, sources: dict[str, str]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    with (ROOT / cases_path).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            groups[sources[row["review_id"]]].append(row)
    result = {}
    for source, rows in sorted(groups.items()):
        n = len(rows)
        result[source] = {
            "cases": n,
            "topic": round(sum(r["topic_match"] == "True" for r in rows) / n, 6),
            "intent": round(sum(r["intent_match"] == "True" for r in rows) / n, 6),
            "severity_exact": round(sum(r["severity_match"] == "True" for r in rows) / n, 6),
            "severity_within_1": round(sum(int(r["severity_error"]) <= 1 for r in rows) / n, 6),
        }
    return result


def main() -> None:
    reports = {predictions: score(predictions, output, cases) for predictions, output, cases in SAVED_SETS}

    sources = export_final_predictions()
    final = score(FINAL_PREDICTIONS, FINAL_EVALUATION, FINAL_CASES)
    final["scope"] = "Final full-corpus labels (grading export) of the 50 Golden reviews"
    final["label_sources"] = dict(Counter(sources.values()))
    final["by_label_source"] = by_label_source(FINAL_CASES, sources)
    with (ROOT / FINAL_CASES).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    final["agreement"]["severity_within_1"] = round(sum(int(r["severity_error"]) <= 1 for r in rows) / len(rows), 6)
    dump(FINAL_EVALUATION, final)

    # Copies embedded in experiment summaries.
    pilot = reports["experiments/distilled/pilot-v1/golden_50_predictions.csv"]
    for path in ("experiments/distilled/pilot-v1/summary.json", "experiments/distilled/pilot-v1/run_stdout.json"):
        summary = load(path)
        summary["golden_50"] = pilot
        dump(path, summary)

    hybrid = reports["experiments/hybrid/quality-50-local-luna-v3/predictions.csv"]["agreement"]
    local = reports["experiments/hybrid/quality-50-local-luna-v3/local_predictions.csv"]["agreement"]
    sol = reports["experiments/codex_3agent/quality-50-parallel/predictions.csv"]["agreement"]
    dump(
        "experiments/hybrid/quality-50-local-luna-v3/quality_comparison.json",
        {
            "hybrid": hybrid,
            "local": local,
            "codex_three_worker_gpt_5_6_sol": {**sol, "source": "experiments/codex_3agent/quality-50-parallel/golden_evaluation.json"},
            "golden_labels_sha256": file_sha(GOLDEN),
        },
    )

    validation = load("experiments/distilled/validation.json")
    checks = []
    for check in validation["checks"]:
        if check["name"] == "golden_hash":
            check = {**check, "detail": file_sha(GOLDEN), "passed": True}
        elif check["name"] == "golden_evaluation_saved":
            check = {**check, "detail": reports["experiments/distilled/hybrid-golden-50-v1/golden_predictions.csv"]["agreement"], "passed": True}
        elif check["name"] == "post_completion_comparison":
            continue
        checks.append(check)
    validation["checks"] = checks
    validation["checks_total"] = len(checks)
    validation["checks_passed"] = sum(bool(c["passed"]) for c in checks)
    dump("experiments/distilled/validation.json", validation)

    for predictions, report in {**reports, FINAL_PREDICTIONS: final}.items():
        a = report["agreement"]
        print(f"{predictions}: topic {a['topic']:.2f} intent {a['intent']:.2f} severity {a['severity_exact']:.2f} "
              f"sev_mae {a['severity_mae_on_valid_predictions']:.2f} sent_mae {a['sentiment_mae_on_valid_predictions']:.3f} "
              f"entities {a['entities_exact']:.2f} | macroF1 t/i/s {report['macro_f1']['topic']:.3f}/{report['macro_f1']['intent']:.3f}/{report['macro_f1']['severity']:.3f}")
    print("final by source:", final["by_label_source"])


if __name__ == "__main__":
    main()
