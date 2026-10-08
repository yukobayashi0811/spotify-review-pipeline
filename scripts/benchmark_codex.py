from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.codex_benchmark import (  # noqa: E402
    ACKNOWLEDGEMENT,
    run_development_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded, development-only Codex subscription benchmark."
    )
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "cost_100.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=12)
    parser.add_argument("--workers", type=int, choices=(1, 3), required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--effort", default="low", choices=("low", "medium", "high"))
    parser.add_argument("--codex", default="/Users/Yu/.local/bin/codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--batch-size", type=int, default=4, help="1-50 reviews per call")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--acknowledge",
        required=True,
        help=f"Must equal {ACKNOWLEDGEMENT!r}.",
    )
    args = parser.parse_args()
    report = run_development_benchmark(
        input_path=args.input,
        output_dir=args.output,
        prompt_path=PROJECT_ROOT / "prompts" / "classify.md",
        sample_size=args.sample_size,
        workers=args.workers,
        model=args.model,
        effort=args.effort,
        executable=args.codex,
        expected_cli_version=args.expected_cli_version,
        batch_size=args.batch_size,
        timeout_seconds=args.timeout_seconds,
        acknowledgement=args.acknowledge,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
