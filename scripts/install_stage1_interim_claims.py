from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.storage import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install code-owned, temporary claims solely for the stage-1 grading audit."
    )
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    with Store(args.state) as store:
        ranking = store.get_metadata("ranking", [])
        if not ranking:
            raise RuntimeError("Ranking is not available")
        top = ranking[0]
        metrics = ("complaint_count", "severity_sum", "mean_severity", "priority_score")
        payload = {
            "priority": "Interim stage-1 grading validation; replaced by the memo role in stage 3",
            "rationale": "These code-owned claims only prove ranking/export consistency.",
            "alternatives": [],
            "limitations": ["Not a decision memo."],
            "claims": [
                {
                    "claim_id": f"STAGE1_{metric.upper()}",
                    "issue_id": top["issue_id"],
                    "metric": metric,
                    "value": str(top[metric]),
                    "sentence": f"Interim audit claim: {metric}={top[metric]}.",
                    "review_ids": [],
                }
                for metric in metrics
            ],
        }
        store.set_metadata("memo_payload", payload)
        store.set_metadata("memo_input_hash", "stage1-interim-must-be-replaced")
        store.set_metadata(
            "memo_claims_provenance",
            [
                {
                    "claim_id": claim["claim_id"],
                    "source": "code_stage1_interim",
                    "reason": "temporary ranking/export consistency audit",
                }
                for claim in payload["claims"]
            ],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
