from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.storage import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--recovery", type=Path, required=True)
    parser.add_argument("--grading", type=Path, required=True)
    args = parser.parse_args()
    before_path = args.recovery / "checkpoint_before.json"
    after_path = args.recovery / "checkpoint_after.json"
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    with Store(args.state) as store:
        store.set_metadata("checkpoint_before", before)
        store.set_metadata("checkpoint_after", after)
    args.grading.mkdir(parents=True, exist_ok=True)
    shutil.copy2(before_path, args.grading / "checkpoint_before.json")
    shutil.copy2(after_path, args.grading / "checkpoint_after.json")
    print(json.dumps({"before": len(before["completed_ids"]),
                      "after": len(after["completed_ids"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
