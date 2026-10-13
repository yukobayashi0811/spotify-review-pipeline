from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.classify import ModelConfig  # noqa: E402
from spotify_pipeline.group import run_grouping  # noqa: E402
from spotify_pipeline.io_utils import write_json  # noqa: E402
from spotify_pipeline.providers import CodexExecProvider  # noqa: E402
from spotify_pipeline.rank import rerank  # noqa: E402
from spotify_pipeline.recommend import run_recommendation  # noqa: E402
from spotify_pipeline.verify import run_verification  # noqa: E402


ACKNOWLEDGEMENT = "RUN-DISTILLED-LUNA-DOWNSTREAM"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run blind verification, grouping, deterministic ranking, and memo roles."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--luna-model", default="gpt-6-luna")
    parser.add_argument("--luna-effort", default="low", choices=("low", "medium", "high"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--luna-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--verification-sample-fraction", type=float, default=0.10)
    parser.add_argument("--verification-minimum-sample", type=int, default=10)
    parser.add_argument("--verification-maximum-sample", type=int)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--acknowledge", required=True)
    args = parser.parse_args()
    if args.acknowledge != ACKNOWLEDGEMENT:
        raise SystemExit(f"--acknowledge must equal {ACKNOWLEDGEMENT!r}")

    state_path = args.output / "final" / "state.db"
    if not state_path.is_file():
        raise SystemExit(f"Hybrid final state not found: {state_path}")
    artifacts = args.output / "downstream"
    artifacts.mkdir(parents=True, exist_ok=True)
    provider = CodexExecProvider(
        trace_root=artifacts / "traces",
        executable=args.codex,
        expected_version=args.expected_cli_version,
        ephemeral=True,
    )
    config = ModelConfig(
        model=args.luna_model,
        effort=args.luna_effort,
        batch_size=args.batch_size,
        max_workers=1,
        max_attempts=2,
        max_output_tokens=4000,
        timeout_seconds=args.luna_timeout_seconds,
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=(
            f"{args.expected_cli_version.replace(' ', '-')}:subscription:ephemeral:"
            "empty-read-only:tools-disabled:distilled-downstream-v1"
        ),
    )
    run_id = f"distilled-downstream-{uuid.uuid4()}"
    started = time.perf_counter()
    verification = run_verification(
        state_path=state_path,
        provider=provider,
        prompt_path=PROJECT_ROOT / "prompts" / "verify.md",
        config=config,
        run_id=run_id,
        output_path=artifacts / "verification_report.json",
        sample_fraction=args.verification_sample_fraction,
        minimum_sample=args.verification_minimum_sample,
        maximum_sample=args.verification_maximum_sample,
    )
    grouping = run_grouping(
        state_path=state_path,
        provider=provider,
        prompt_path=PROJECT_ROOT / "prompts" / "group.md",
        config=config,
        run_id=run_id,
        artifacts_dir=artifacts,
    )
    ranking = rerank(
        state_path=state_path,
        output_csv=artifacts / "ranking.csv",
        summary_json=artifacts / "ranking.json",
    )
    recommendation = run_recommendation(
        state_path=state_path,
        provider=provider,
        prompt_path=PROJECT_ROOT / "prompts" / "memo.md",
        config=config,
        run_id=run_id,
        artifacts_dir=artifacts,
    )
    report = {
        "status": "completed",
        "development_only": True,
        "grading_eligible": False,
        "run_id": run_id,
        "verification": verification,
        "grouping": grouping,
        "ranking": ranking,
        "recommendation": recommendation,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "provider_runtime": provider.runtime_metadata(),
        "billing_mode": "chatgpt_subscription",
        "monetary_cost_usd": "unknown",
    }
    report_path = artifacts / (
        "warm_report.json"
        if not verification["pending_at_start"]
        and not grouping["model_called"]
        and not recommendation["model_called"]
        else "cold_report.json"
    )
    write_json(report_path, report)
    write_json(artifacts / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
