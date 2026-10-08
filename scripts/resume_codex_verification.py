from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.classify import ModelConfig  # noqa: E402
from spotify_pipeline.codex_benchmark import (  # noqa: E402
    ACKNOWLEDGEMENT,
    _execution_fingerprint,
)
from spotify_pipeline.io_utils import write_json  # noqa: E402
from spotify_pipeline.providers import CodexExecProvider  # noqa: E402
from spotify_pipeline.verify import run_verification  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume only missing Codex verification rows")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--codex", default="/Users/Yu/.local/bin/codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--acknowledge", required=True, help=f"Must equal {ACKNOWLEDGEMENT!r}.")
    args = parser.parse_args()
    if args.acknowledge != ACKNOWLEDGEMENT:
        raise RuntimeError(f"Acknowledgement must equal {ACKNOWLEDGEMENT!r}")

    plan = json.loads((args.output / "experiment_plan.json").read_text(encoding="utf-8"))
    downstream = args.output / "downstream"
    provider = CodexExecProvider(
        trace_root=downstream / "traces",
        executable=args.codex,
        expected_version=args.expected_cli_version,
        ephemeral=True,
    )
    config = ModelConfig(
        model=plan["model"],
        effort=plan["effort"],
        batch_size=int(plan["batch_size"]),
        max_workers=1,
        max_attempts=1,
        max_output_tokens=4000,
        timeout_seconds=float(plan["timeout_seconds"]),
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=_execution_fingerprint(args.expected_cli_version),
    )
    started = time.perf_counter()
    result = run_verification(
        state_path=args.output / "consolidated" / "state.db",
        provider=provider,
        prompt_path=PROJECT_ROOT / "prompts" / "verify.md",
        config=config,
        run_id=f"{plan['experiment_id']}-verification-resume",
        output_path=downstream / "artifacts" / "verification_report.json",
    )
    report = {
        "development_only": True,
        "grading_eligible": False,
        "experiment_id": plan["experiment_id"],
        "wall_seconds": round(time.perf_counter() - started, 6),
        "result": result,
        "provider_runtime": provider.runtime_metadata(),
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
    }
    write_json(downstream / "verification-resume-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if result["verified"] != result["selected"] or result["failures"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
