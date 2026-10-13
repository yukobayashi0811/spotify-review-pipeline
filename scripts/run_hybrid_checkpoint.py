from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.classify import ModelConfig  # noqa: E402
from spotify_pipeline.hybrid import RoutingPolicy, run_hybrid_checkpoint  # noqa: E402
from spotify_pipeline.providers import CodexExecProvider, LocalMLXProvider  # noqa: E402


ACKNOWLEDGEMENT = "RUN-HYBRID-LOCAL-LUNA-CHECKPOINT"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded local-first checkpoint with risk-gated Codex Luna escalation."
    )
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "cost_100.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--local-model", default="mlx-community/gemma-4-e4b-it-4bit")
    parser.add_argument(
        "--local-model-revision",
        default="475b9088d29754a3379866cf5aeb6b41acd313c2",
    )
    parser.add_argument("--local-batch-size", type=int, default=50)
    parser.add_argument("--local-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--luna-model", default="gpt-6-luna")
    parser.add_argument("--luna-effort", default="low", choices=("low", "medium", "high"))
    parser.add_argument("--luna-batch-size", type=int, default=50)
    parser.add_argument("--luna-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--acknowledge", required=True)
    args = parser.parse_args()
    if args.acknowledge != ACKNOWLEDGEMENT:
        raise SystemExit(f"--acknowledge must equal {ACKNOWLEDGEMENT!r}")

    output = args.output
    local_provider = LocalMLXProvider(
        model_revision=args.local_model_revision,
        temperature=0.0,
    )
    escalation_provider = CodexExecProvider(
        trace_root=output / "escalation" / "traces",
        executable=args.codex,
        expected_version=args.expected_cli_version,
        ephemeral=True,
    )
    local_config = ModelConfig(
        model=args.local_model,
        effort="none",
        batch_size=args.local_batch_size,
        max_workers=1,
        max_attempts=2,
        max_output_tokens=6000,
        timeout_seconds=args.local_timeout_seconds,
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        execution_fingerprint=(
            f"mlx-lm-0.31.3:{args.local_model_revision}:"
            f"batch-{args.local_batch_size}:hybrid-local-v2"
        ),
    )
    escalation_config = ModelConfig(
        model=args.luna_model,
        effort=args.luna_effort,
        batch_size=args.luna_batch_size,
        max_workers=1,
        max_attempts=2,
        max_output_tokens=6000,
        timeout_seconds=args.luna_timeout_seconds,
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=(
            f"{args.expected_cli_version.replace(' ', '-')}:subscription:ephemeral:"
            f"empty-read-only:tools-disabled:batch-{args.luna_batch_size}:"
            "hybrid-escalation-v3"
        ),
    )
    result = run_hybrid_checkpoint(
        input_path=args.input,
        output_dir=output,
        prompt_path=PROJECT_ROOT / "prompts" / "classify.md",
        sample_size=args.sample_size,
        local_provider=local_provider,
        local_config=local_config,
        escalation_provider=escalation_provider,
        escalation_config=escalation_config,
        policy=RoutingPolicy(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
