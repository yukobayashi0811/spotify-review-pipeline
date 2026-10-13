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
from spotify_pipeline.distilled import (  # noqa: E402
    DISTILLED_MODEL_VERSION,
    DistilledTextProvider,
)
from spotify_pipeline.hybrid import RoutingPolicy, run_hybrid_checkpoint  # noqa: E402
from spotify_pipeline.io_utils import file_sha  # noqa: E402
from spotify_pipeline.providers import CodexExecProvider  # noqa: E402


ACKNOWLEDGEMENT = "RUN-DISTILLED-LUNA-CHECKPOINT"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded text-only distilled classifier and escalate at most a "
            "declared fraction of risky reviews to subscription-authenticated Codex Luna."
        )
    )
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "cost_100.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument(
        "--artifact",
        type=Path,
        default=(
            PROJECT_ROOT
            / "experiments"
            / "distilled"
            / "pilot-v1"
            / "distilled_model.joblib"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=(
            PROJECT_ROOT / "experiments" / "distilled" / "pilot-v1" / "model_manifest.json"
        ),
    )
    parser.add_argument("--max-escalation-fraction", type=float, default=0.20)
    parser.add_argument(
        "--routing-mode",
        default="uncertainty",
        choices=("uncertainty", "risk"),
        help="Select Luna rows by local uncertainty (recommended) or legacy risk rules.",
    )
    parser.add_argument(
        "--uncertainty-metric",
        default="mean_margin",
        choices=("minimum_confidence", "minimum_margin", "mean_margin"),
    )
    parser.add_argument("--luna-model", default="gpt-6-luna")
    parser.add_argument("--luna-effort", default="low", choices=("low", "medium", "high"))
    parser.add_argument("--luna-batch-size", type=int, default=20)
    parser.add_argument(
        "--luna-workers",
        type=int,
        default=1,
        choices=(1, 3),
        help="Use three isolated Codex workers for the 10,000-review stage.",
    )
    parser.add_argument("--luna-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--acknowledge", required=True)
    args = parser.parse_args()
    if args.acknowledge != ACKNOWLEDGEMENT:
        raise SystemExit(f"--acknowledge must equal {ACKNOWLEDGEMENT!r}")
    if not 0.0 <= args.max_escalation_fraction <= 1.0:
        raise SystemExit("--max-escalation-fraction must be between 0 and 1")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    artifact_sha = file_sha(args.artifact)
    if manifest.get("artifact_sha256") != artifact_sha:
        raise SystemExit("Artifact SHA-256 does not match the sealed model manifest")
    output = args.output
    local_provider = DistilledTextProvider(
        artifact_path=args.artifact,
        expected_sha256=artifact_sha,
    )
    escalation_provider = CodexExecProvider(
        trace_root=output / "escalation" / "traces",
        executable=args.codex,
        expected_version=args.expected_cli_version,
        ephemeral=True,
    )

    def escalation_provider_factory(trace_root: Path) -> CodexExecProvider:
        return CodexExecProvider(
            trace_root=trace_root,
            executable=args.codex,
            expected_version=args.expected_cli_version,
            ephemeral=True,
        )

    local_config = ModelConfig(
        model=DISTILLED_MODEL_VERSION,
        effort="none",
        batch_size=50,
        max_workers=1,
        max_attempts=2,
        max_output_tokens=1,
        timeout_seconds=60.0,
        spend_limit_usd=1.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=(
            f"artifact-{artifact_sha}:manifest-{file_sha(args.manifest)}:"
            f"teacher-{manifest['training_report']['labels_sha256']}:"
            f"sklearn-{manifest['scikit_learn_version']}"
        ),
    )
    escalation_config = ModelConfig(
        model=args.luna_model,
        effort=args.luna_effort,
        batch_size=args.luna_batch_size,
        max_workers=args.luna_workers,
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
            f"workers-{args.luna_workers}:"
            "distilled-hybrid-escalation-v1"
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
        escalation_provider_factory=(
            escalation_provider_factory if args.luna_workers > 1 else None
        ),
        policy=RoutingPolicy(
            routing_mode=args.routing_mode,
            uncertainty_metric=args.uncertainty_metric,
            max_escalation_fraction=args.max_escalation_fraction,
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
