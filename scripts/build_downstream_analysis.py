from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.downstream_analysis import (  # noqa: E402
    build_actionable_and_topic_outputs,
    compare_groupings,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    args = parser.parse_args()
    outputs = build_actionable_and_topic_outputs(
        state_path=args.state, artifacts_dir=args.artifacts
    )
    comparison = compare_groupings(
        previous_dir=args.previous,
        current_dir=args.artifacts,
        output_path=args.artifacts / "grouping_comparison.json",
    )
    if comparison["adopted"] != "embedding_v2":
        raise RuntimeError("The comparison selected TF-IDF; do not silently retain embedding membership")
    report = {
        "comparison": comparison,
        "actionable_issue_count": len(outputs["actionable_ranking"]),
        "topic_count": len(outputs["topic_rollup"]),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
