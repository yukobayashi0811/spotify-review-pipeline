from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .io_utils import write_csv, write_json
from .storage import Store


def mean_string(total: int, count: int) -> str:
    return str(
        (Decimal(total) / Decimal(count)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
    )


def rerank(
    *, state_path: str | Path, output_csv: str | Path, summary_json: str | Path
) -> list[dict[str, Any]]:
    with Store(state_path) as store:
        rows = store.connection.execute(
            """
            SELECT m.issue_id,COUNT(*) AS complaint_count,SUM(r.severity) AS severity_sum
            FROM membership m JOIN records r USING(review_id)
            WHERE r.status='completed' AND r.intent IN ('complaint','cancellation')
            GROUP BY m.issue_id
            """
        )
        ranking = [
            {
                "issue_id": row["issue_id"],
                "complaint_count": int(row["complaint_count"]),
                "severity_sum": int(row["severity_sum"]),
                "mean_severity": mean_string(
                    int(row["severity_sum"]), int(row["complaint_count"])
                ),
                "priority_score": int(row["severity_sum"]),
            }
            for row in rows
        ]
        ranking.sort(key=lambda row: (-row["priority_score"], row["issue_id"]))
        for index, row in enumerate(ranking, 1):
            row["rank"] = index
        ranking = [
            {
                key: row[key]
                for key in (
                    "rank",
                    "issue_id",
                    "complaint_count",
                    "severity_sum",
                    "mean_severity",
                    "priority_score",
                )
            }
            for row in ranking
        ]
        store.set_metadata("ranking", ranking)
    write_csv(
        output_csv,
        [
            "rank",
            "issue_id",
            "complaint_count",
            "severity_sum",
            "mean_severity",
            "priority_score",
        ],
        ranking,
    )
    write_json(
        summary_json,
        {
            "formula": "priority_score = complaint_count * mean_severity = severity_sum",
            "tie_break": "priority_score descending, then issue_id ascending",
            "issue_count": len(ranking),
            "ranking": ranking,
        },
    )
    return ranking
