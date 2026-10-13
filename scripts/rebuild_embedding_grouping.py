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
from spotify_pipeline.group import NAMING_BUCKET, run_grouping  # noqa: E402
from spotify_pipeline.io_utils import write_json  # noqa: E402
from spotify_pipeline.providers import CodexExecProvider  # noqa: E402
from spotify_pipeline.storage import Store  # noqa: E402


ACKNOWLEDGEMENT = "REBUILD-EMBEDDING-GROUPING-V3"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--acknowledge", required=True)
    args = parser.parse_args()
    if args.acknowledge != ACKNOWLEDGEMENT:
        raise SystemExit(f"--acknowledge must equal {ACKNOWLEDGEMENT!r}")

    provider = CodexExecProvider(
        trace_root=args.artifacts / "grouping_v3_traces",
        executable=args.codex,
        expected_version=args.expected_cli_version,
        ephemeral=True,
    )
    config = ModelConfig(
        model="gpt-6-luna",
        effort="low",
        batch_size=50,
        max_workers=1,
        max_attempts=2,
        max_output_tokens=4000,
        timeout_seconds=300,
        spend_limit_usd=1,
        fallback_model=None,
        fallback_fraction=0,
        input_rate_per_million=0,
        cached_input_rate_per_million=0,
        output_rate_per_million=0,
        execution_fingerprint=(
            f"{args.expected_cli_version.replace(' ', '-')}:subscription:ephemeral:"
            "empty-read-only:tools-disabled:embedding-group-v3"
        ),
    )
    with Store(args.state) as store:
        before = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM luna_ledger WHERE bucket=?", (NAMING_BUCKET,)
            ).fetchone()[0]
        )
    started = time.perf_counter()
    result = run_grouping(
        state_path=args.state,
        provider=provider,
        prompt_path=PROJECT_ROOT / "prompts/group.md",
        config=config,
        run_id=f"embedding-group-v3-{uuid.uuid4()}",
        artifacts_dir=args.artifacts,
    )
    with Store(args.state) as store:
        after = int(
            store.connection.execute(
                "SELECT COUNT(*) FROM luna_ledger WHERE bucket=?", (NAMING_BUCKET,)
            ).fetchone()[0]
        )
    report = {
        "status": "completed",
        "result": result,
        "new_luna_distinct_texts": after - before,
        "luna_distinct_texts_total_for_rebuild": after,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "provider_runtime": provider.runtime_metadata(),
    }
    write_json(args.artifacts / "grouping_v3_run.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
