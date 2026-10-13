#!/usr/bin/env python3
"""Rebuild the full-corpus decision memo with the memo-v3 role (1-2 GPT-6 Luna requests).

Inputs are the saved full-corpus aggregates only (ranking, actionable ranking,
topic rollup, issue metadata, coverage, monthly trend, verification and
sensitivity summaries) plus three typical member reviews per issue chosen by
code. The memo role cannot change any ID or number: code validates every claim
against the saved ranking. Explicit paid command:

    python scripts/rebuild_memo.py --state artifacts/full_run_v2/state.db \
        --artifacts artifacts/full_run_v2 --acknowledge REBUILD-DECISION-MEMO-V3
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.classify import ModelConfig  # noqa: E402
from spotify_pipeline.guard import GuardedProvider  # noqa: E402
from spotify_pipeline.io_utils import write_json  # noqa: E402
from spotify_pipeline.providers import CodexExecProvider  # noqa: E402
from spotify_pipeline.recommend import MEMO_BUCKET, run_recommendation  # noqa: E402
from spotify_pipeline.storage import Store  # noqa: E402


ACKNOWLEDGEMENT = "REBUILD-DECISION-MEMO-V3"


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
    archive = args.artifacts / "memo_v2_superseded"
    if not archive.exists():
        archive.mkdir()
        for name in ("memo.md", "memo_payload.json", "memo_evidence_pack.json", "claims.csv",
                     "claims_provenance.csv"):
            if (args.artifacts / name).exists():
                shutil.copyfile(args.artifacts / name, archive / name)
    raw = CodexExecProvider(
        trace_root=args.artifacts / "memo_v3_traces",
        executable=args.codex,
        expected_version=args.expected_cli_version,
        ephemeral=True,
    )
    provider = GuardedProvider(
        raw, allowlist={"gpt-6-luna": ("low",)}, max_calls_by_role={"memo": 2},
        spend_limit_usd=0.10,
    )
    config = ModelConfig(
        model="gpt-6-luna", effort="low", batch_size=50, max_workers=1,
        max_attempts=2, max_output_tokens=6000, timeout_seconds=300,
        spend_limit_usd=0.10, fallback_model=None, fallback_fraction=0,
        input_rate_per_million=0.10, cached_input_rate_per_million=0.01,
        output_rate_per_million=0.50,
        execution_fingerprint=(
            f"{args.expected_cli_version.replace(' ', '-')}:subscription:ephemeral:"
            "empty-read-only:tools-disabled:memo-v3"
        ),
    )
    with Store(args.state) as store:
        before = int(store.connection.execute(
            "SELECT COUNT(*) FROM luna_ledger WHERE bucket=?", (MEMO_BUCKET,)
        ).fetchone()[0])
    started = time.perf_counter()
    result = run_recommendation(
        state_path=args.state,
        provider=provider,
        prompt_path=PROJECT_ROOT / "prompts/memo.md",
        config=config,
        run_id=f"memo-v3-{uuid.uuid4()}",
        artifacts_dir=args.artifacts,
    )
    shutil.copyfile(args.artifacts / "memo.md", PROJECT_ROOT / "memo.md")
    with Store(args.state) as store:
        after = int(store.connection.execute(
            "SELECT COUNT(*) FROM luna_ledger WHERE bucket=?", (MEMO_BUCKET,)
        ).fetchone()[0])
        repairs = store.get_metadata("memo_code_repairs", [])
    report = {
        "result": result,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "luna_requests_reserved": after - before,
        "code_repairs": repairs,
        "previous_memo_archived_in": str(archive.relative_to(PROJECT_ROOT)) if archive.is_relative_to(PROJECT_ROOT) else str(archive),
        "guard": provider.runtime_metadata()["guard"],
    }
    write_json(args.artifacts / "memo_v3_run.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
