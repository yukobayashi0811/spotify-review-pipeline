from __future__ import annotations

import argparse
import io
import json
import platform
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run unit tests and save a machine-readable receipt.")
    parser.add_argument("--output", type=Path, default=Path("artifacts/test_results.json"))
    args = parser.parse_args()
    started = time.perf_counter()
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    receipt = {
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "successful": result.wasSuccessful(),
        "wall_seconds": round(time.perf_counter() - started, 6),
        "model_calls": 0,
        "test_log": stream.getvalue().splitlines(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(stream.getvalue(), end="")
    print(f"Saved test receipt to {args.output}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
