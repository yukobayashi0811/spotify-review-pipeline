from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import time
import traceback
from pathlib import Path
from typing import Any

from spotify_pipeline.io_utils import write_json
from spotify_pipeline.providers import LocalMLXProvider
from spotify_pipeline.schemas import ENRICHMENT_SCHEMA, validate_enrichment_payload


def _worker(
    worker_id: int,
    rows: list[dict[str, str]],
    prompt: str,
    model: str,
    revision: str,
    max_output_tokens: int,
    timeout_seconds: float,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    try:
        provider = LocalMLXProvider(model_revision=revision)

        def call(values: list[dict[str, str]]) -> dict[str, Any]:
            result = provider.call(
                role="enrich",
                model=model,
                effort="none",
                developer_prompt=prompt,
                user_payload={
                    "reviews": [
                        {"review_id": row["review_id"], "review_text": row["review_text"]}
                        for row in values
                    ]
                },
                schema=ENRICHMENT_SCHEMA,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
            validated = validate_enrichment_payload(
                result.payload, {row["review_id"]: row for row in values}
            )
            return {
                "worker_id": worker_id,
                "review_count": len(values),
                "duration_seconds": round(result.duration_seconds, 6),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "valid_records": len(validated),
            }

        call(rows[:1])
        ready.put({"worker_id": worker_id, "status": "ready"})
        if not start.wait(timeout_seconds + 60):
            raise TimeoutError("Timed out waiting for synchronized worker start")
        measured = call(rows)
        measured["runtime"] = provider.runtime_metadata()
        results.put({"status": "succeeded", "result": measured})
    except Exception as error:
        results.put(
            {
                "status": "failed",
                "worker_id": worker_id,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare one versus two warm local MLX workers.")
    parser.add_argument("--input", default="data/cost_100.csv")
    parser.add_argument("--single-worker-report", default="artifacts/performance_benchmark.json")
    parser.add_argument("--output-json", default="artifacts/worker_benchmark.json")
    parser.add_argument("--output-md", default="artifacts/worker_benchmark.md")
    parser.add_argument("--model", default="mlx-community/gemma-4-e4b-it-4bit")
    parser.add_argument("--revision", default="475b9088d29754a3379866cf5aeb6b41acd313c2")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-output-tokens", type=int, default=6000)
    args = parser.parse_args()

    rows = list(csv.DictReader(Path(args.input).open(encoding="utf-8")))[:20]
    if len(rows) != 20:
        raise ValueError("Worker benchmark input must contain at least 20 rows")
    prompt = Path("src/spotify_pipeline/prompt_text/classify.md").read_text(encoding="utf-8")
    single = json.loads(Path(args.single_worker_report).read_text(encoding="utf-8"))
    single_batch10 = next(row for row in single["variants"] if row["batch_size"] == 10)

    context = mp.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_worker,
            args=(
                index,
                rows[index * 10 : (index + 1) * 10],
                prompt,
                args.model,
                args.revision,
                args.max_output_tokens,
                args.timeout_seconds,
                ready,
                start,
                results,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    ready_rows = [ready.get(timeout=300) for _ in processes]
    started = time.perf_counter()
    start.set()
    worker_rows = [results.get(timeout=args.timeout_seconds + 60) for _ in processes]
    wall_seconds = time.perf_counter() - started
    for process in processes:
        process.join(timeout=30)
    failed = [row for row in worker_rows if row["status"] != "succeeded"]
    if failed:
        raise RuntimeError(json.dumps(failed, ensure_ascii=False))

    sequential_seconds = float(single_batch10["duration_seconds"])
    speedup = sequential_seconds / wall_seconds
    report = {
        "status": "measured_development_benchmark",
        "population": "same first 20 rows as the batch-size benchmark",
        "model": args.model,
        "model_revision": args.revision,
        "comparison": {
            "one_warm_worker_seconds": round(sequential_seconds, 6),
            "two_warm_workers_wall_seconds": round(wall_seconds, 6),
            "speedup_ratio": round(speedup, 6),
            "adopt_two_workers": speedup >= 1.1,
            "adoption_rule": "Adopt only if measured speedup is at least 10% on the same 20 reviews.",
        },
        "ready": ready_rows,
        "workers": [row["result"] for row in worker_rows],
        "limitations": [
            "This is a 20-review throughput probe, not the required 500-review checkpoint.",
            "Two workers double resident model copies and may reduce interactive system headroom.",
            "Production concurrency remains disabled unless this probe shows a material gain and the 500-review checkpoint confirms it.",
        ],
    }
    write_json(args.output_json, report)
    decision = "adopt for the 500-review checkpoint" if speedup >= 1.1 else "keep one worker"
    Path(args.output_md).write_text(
        "# Local worker benchmark\n\n"
        "This is measured development evidence on the same 20 reviews. Model warm-up is excluded.\n\n"
        "| Configuration | Wall seconds |\n|---|---:|\n"
        f"| one warm worker, two sequential batch-10 calls | {sequential_seconds:.3f} |\n"
        f"| two warm workers, one batch-10 call each | {wall_seconds:.3f} |\n\n"
        f"Measured speedup: {speedup:.3f}x. Decision: **{decision}**.\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
