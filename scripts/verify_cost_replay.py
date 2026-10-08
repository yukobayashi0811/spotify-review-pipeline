#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from pathlib import Path

from spotify_pipeline.costs import replay_cost
from spotify_pipeline.io_utils import write_csv, write_json


ROOT = Path(__file__).resolve().parents[1]
COST = ROOT / "cost"


def main() -> None:
    base = replay_cost(
        calls_path=COST / "all_measured_calls.jsonl",
        rates_path=COST / "rates.csv",
        usage_path=COST / "usage.csv",
        report_path=COST / "report.md",
        run_metadata_path=COST / "pilot_run.json",
    )
    with (COST / "rates.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row["price_per_unit"] = f"{float(row['price_per_unit']) * 2:.12f}"
    doubled_dir = COST / "double_rate_check"
    doubled_dir.mkdir(parents=True, exist_ok=True)
    write_csv(doubled_dir / "rates_2x.csv", list(rows[0]), rows)
    doubled = replay_cost(
        calls_path=COST / "all_measured_calls.jsonl",
        rates_path=doubled_dir / "rates_2x.csv",
        usage_path=doubled_dir / "usage.csv",
        report_path=doubled_dir / "report.md",
        run_metadata_path=COST / "pilot_run.json",
    )
    base_api = float(base["one_time_fixed"]["api_equivalent_usd"])
    doubled_api = float(doubled["one_time_fixed"]["api_equivalent_usd"])
    receipt = {
        "status": "pass" if abs(doubled_api - 2 * base_api) < 1e-8 else "fail",
        "offline_replay": True,
        "base_api_equivalent_usd": base_api,
        "doubled_api_equivalent_usd": doubled_api,
        "ratio": doubled_api / base_api if base_api else None,
        "base_cold_wall_seconds": base["pilot"]["cold_wall_seconds"],
        "doubled_cold_wall_seconds": doubled["pilot"]["cold_wall_seconds"],
        "runtime_unchanged": (
            base["pilot"]["cold_wall_seconds"] == doubled["pilot"]["cold_wall_seconds"]
        ),
        "note": "Only editable monetary rates changed; saved calls and measured runtimes were replayed offline.",
    }
    if receipt["status"] != "pass" or not receipt["runtime_unchanged"]:
        raise RuntimeError(f"Cost replay verification failed: {receipt}")
    write_json(doubled_dir / "verification.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
