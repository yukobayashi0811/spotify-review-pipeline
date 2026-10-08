from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from spotify_pipeline.io_utils import write_json
from spotify_pipeline.providers import LocalMLXProvider
from spotify_pipeline.schemas import ENRICHMENT_SCHEMA, validate_enrichment_payload


def _call(
    provider: LocalMLXProvider,
    *,
    rows: list[dict[str, str]],
    prompt: str,
    model: str,
    max_output_tokens: int,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    result = provider.call(
        role="enrich",
        model=model,
        effort="none",
        developer_prompt=prompt,
        user_payload={
            "reviews": [
                {"review_id": row["review_id"], "review_text": row["review_text"]}
                for row in rows
            ]
        },
        schema=ENRICHMENT_SCHEMA,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    values = validate_enrichment_payload(
        result.payload, {row["review_id"]: row for row in rows}
    )
    labels = {
        value["review_id"]: [value["topic"], value["intent"], value["severity"]]
        for value in values
    }
    return (
        {
            "review_count": len(rows),
            "duration_seconds": round(result.duration_seconds, 6),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "valid_records": len(values),
        },
        labels,
    )


def _baseline(calls_path: Path) -> dict[str, Any] | None:
    if not calls_path.exists():
        return None
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    enrich = [row for row in calls if row.get("role") == "enrich"]
    successful_tens = [
        row
        for row in enrich
        if row.get("outcome") == "succeeded" and len(row.get("review_ids", [])) == 10
    ]
    return {
        "source": str(calls_path),
        "all_enrich_calls": len(enrich),
        "failed_enrich_calls": sum(row.get("outcome") == "failed" for row in enrich),
        "successful_batch10_calls": len(successful_tens),
        "successful_batch10_mean_seconds": round(
            sum(float(row["duration_seconds"]) for row in successful_tens)
            / len(successful_tens),
            6,
        )
        if successful_tens
        else None,
        "successful_batch10_output_tokens_per_review": round(
            sum(int(row.get("output_tokens") or 0) for row in successful_tens)
            / sum(len(row["review_ids"]) for row in successful_tens),
            6,
        )
        if successful_tens
        else None,
    }


def _markdown(report: dict[str, Any]) -> str:
    base = report.get("baseline") or {}
    rows = report["variants"]
    lines = [
        "# Local MLX performance benchmark",
        "",
        "This is measured development evidence, not a full-corpus execution.",
        "All optimized variants use the same first 20 reviews after one unreported warm-up call.",
        "",
        "| Variant | Calls | Reviews | Seconds | Output tokens | Seconds/review |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| batch={row['batch_size']} | {row['call_count']} | {row['review_count']} | "
            f"{row['duration_seconds']:.3f} | {row['output_tokens']} | "
            f"{row['seconds_per_review']:.3f} |"
        )
    lines.extend(
        [
            "",
            f"- Legacy successful batch-10 mean: {base.get('successful_batch10_mean_seconds')} seconds/call.",
            f"- Legacy output tokens/review: {base.get('successful_batch10_output_tokens_per_review')}.",
            f"- Batch 10 vs 20 topic/intent/severity triple agreement: {report['label_triples_agreement']:.3f}.",
            "- A batch-size choice still requires the human-labeled golden-set evaluation; agreement is stability, not accuracy.",
            "- Full-corpus projections below are linear scenarios from this 20-review sample, not completed runs.",
            "",
            "| Variant | Projected days for 484,189 distinct texts |",
            "|---|---:|",
        ]
    )
    for row in rows:
        lines.append(f"| batch={row['batch_size']} | {row['projected_distinct_text_days']:.3f} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local JSON generation and batch size.")
    parser.add_argument("--input", default="data/cost_100.csv")
    parser.add_argument("--calls", default="cost/pilot_calls.jsonl")
    parser.add_argument("--output-json", default="artifacts/performance_benchmark.json")
    parser.add_argument("--output-md", default="artifacts/performance_benchmark.md")
    parser.add_argument("--model", default="mlx-community/gemma-4-e4b-it-4bit")
    parser.add_argument("--revision", default="475b9088d29754a3379866cf5aeb6b41acd313c2")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-output-tokens", type=int, default=6000)
    args = parser.parse_args()

    rows = list(csv.DictReader(Path(args.input).open(encoding="utf-8")))[:20]
    if len(rows) != 20:
        raise ValueError("Benchmark input must contain at least 20 rows")
    prompt = Path("src/spotify_pipeline/prompt_text/classify.md").read_text(encoding="utf-8")
    provider = LocalMLXProvider(model_revision=args.revision)

    # Warm model loading and Metal kernels. This call is excluded from both alternatives.
    _call(
        provider,
        rows=rows[:1],
        prompt=prompt,
        model=args.model,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
    )

    variants: list[dict[str, Any]] = []
    labels_by_batch: dict[int, dict[str, list[Any]]] = {}
    for batch_size in (10, 20):
        calls: list[dict[str, Any]] = []
        labels: dict[str, list[Any]] = {}
        for start in range(0, len(rows), batch_size):
            call, call_labels = _call(
                provider,
                rows=rows[start : start + batch_size],
                prompt=prompt,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout_seconds,
            )
            calls.append(call)
            labels.update(call_labels)
        seconds = sum(float(call["duration_seconds"]) for call in calls)
        output_tokens = sum(int(call["output_tokens"] or 0) for call in calls)
        seconds_per_review = seconds / len(rows)
        variants.append(
            {
                "batch_size": batch_size,
                "call_count": len(calls),
                "review_count": len(rows),
                "duration_seconds": round(seconds, 6),
                "output_tokens": output_tokens,
                "seconds_per_review": round(seconds_per_review, 6),
                "projected_distinct_text_days": round(
                    seconds_per_review * 484_189 / 86_400, 6
                ),
                "calls": calls,
            }
        )
        labels_by_batch[batch_size] = labels

    labels10 = labels_by_batch[10]
    labels20 = labels_by_batch[20]
    report = {
        "status": "measured_development_benchmark",
        "population": "first 20 rows of the required 100-review cost sample",
        "model": args.model,
        "model_revision": args.revision,
        "temperature": 0.0,
        "optimization": "prefilled JSON opening plus stop on first complete top-level JSON object",
        "baseline": _baseline(Path(args.calls)),
        "variants": variants,
        "label_triples_agreement": round(
            sum(labels10[key] == labels20[key] for key in labels10) / len(labels10), 6
        ),
        "runtime": provider.runtime_metadata(),
        "limitations": [
            "The benchmark covers 20 reviews, not the 500, 10,000, or full-corpus stages.",
            "Linear projections exclude verification, grouping, memo writing, retries, and interruption overhead.",
            "Batch stability is not human-label accuracy; use the sealed golden 50 before selecting a production batch size.",
        ],
    }
    write_json(args.output_json, report)
    output_md = Path(args.output_md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
