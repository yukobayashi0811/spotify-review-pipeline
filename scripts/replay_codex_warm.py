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
    replay_development_warm,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay a completed Codex development experiment from exact-result cache."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-cli-version", default="codex-cli 0.159.3")
    parser.add_argument("--acknowledge", required=True, help=f"Must equal {ACKNOWLEDGEMENT!r}.")
    args = parser.parse_args()
    report = replay_development_warm(
        output_dir=args.output,
        prompt_path=PROJECT_ROOT / "prompts" / "classify.md",
        executable=args.codex,
        expected_cli_version=args.expected_cli_version,
        acknowledgement=args.acknowledge,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
