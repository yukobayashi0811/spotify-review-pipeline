from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import write_csv, write_json
from .rank import mean_string
from .storage import Store


def build_actionable_and_topic_outputs(
    *, state_path: str | Path, artifacts_dir: str | Path
) -> dict[str, Any]:
    artifacts = Path(artifacts_dir)
    with Store(state_path) as store:
        ranking = store.get_metadata("ranking", [])
        metadata = {
            str(row["issue_id"]): dict(row)
            for row in store.connection.execute(
                "SELECT issue_id,topic,issue_name,description,coherent,actionable "
                "FROM issue_names"
            )
        }
    actionable = []
    for row in ranking:
        issue = metadata[row["issue_id"]]
        if issue["coherent"] == "yes" and issue["actionable"] == "yes":
            actionable.append({**row, "baseline_rank": row["rank"], **issue})
            actionable[-1].pop("rank", None)
    for index, row in enumerate(actionable, 1):
        row["actionable_rank"] = index
    write_csv(
        artifacts / "actionable_ranking.csv",
        [
            "actionable_rank", "baseline_rank", "issue_id", "topic", "issue_name", "description",
            "coherent", "actionable", "complaint_count", "severity_sum",
            "mean_severity", "priority_score",
        ],
        actionable,
    )

    topic_values: dict[str, dict[str, int]] = {}
    for row in ranking:
        topic = metadata[row["issue_id"]]["topic"]
        value = topic_values.setdefault(topic, {"complaint_count": 0, "severity_sum": 0})
        value["complaint_count"] += int(row["complaint_count"])
        value["severity_sum"] += int(row["severity_sum"])
    topic_rollup = [
        {
            "topic": topic,
            "complaint_count": value["complaint_count"],
            "severity_sum": value["severity_sum"],
            "mean_severity": mean_string(value["severity_sum"], value["complaint_count"]),
            "priority_score": value["severity_sum"],
        }
        for topic, value in topic_values.items()
    ]
    topic_rollup.sort(key=lambda row: (-row["priority_score"], row["topic"]))
    for index, row in enumerate(topic_rollup, 1):
        row["rank"] = index
    write_csv(
        artifacts / "topic_rollup.csv",
        [
            "rank", "topic", "complaint_count", "severity_sum",
            "mean_severity", "priority_score",
        ],
        topic_rollup,
    )
    return {"actionable_ranking": actionable, "topic_rollup": topic_rollup, "issues": metadata}


def compare_groupings(
    *, previous_dir: str | Path, current_dir: str | Path, output_path: str | Path
) -> dict[str, Any]:
    previous = _group_metrics(Path(previous_dir))
    current = _group_metrics(Path(current_dir))
    adopted = "embedding_v2" if (
        current["membership_weighted_coherent_rate_overall"]
        > previous["membership_weighted_coherent_rate_overall"]
    ) else "tfidf_v1"
    report = {
        "comparison_rule": (
            "Adopt the version with the higher overall membership-weighted coherent rate"
        ),
        "tfidf_v1": previous,
        "embedding_v2": current,
        "adopted": adopted,
        "adoption_reason": (
            f"{adopted} has the higher membership-weighted coherent rate: "
            f"embedding={current['membership_weighted_coherent_rate_overall']:.6f}, "
            f"tfidf={previous['membership_weighted_coherent_rate_overall']:.6f}."
        ),
        "top10_below_70_percent_limitation_required": (
            previous["membership_weighted_coherent_rate_top10"] < 0.70
            and current["membership_weighted_coherent_rate_top10"] < 0.70
        ),
    }
    write_json(output_path, report)
    return report


def _group_metrics(directory: Path) -> dict[str, Any]:
    with (directory / "issues.csv").open(encoding="utf-8-sig", newline="") as stream:
        issues = {row["issue_id"]: row for row in csv.DictReader(stream)}
    with (directory / "membership.csv").open(encoding="utf-8-sig", newline="") as stream:
        counts = Counter(row["issue_id"] for row in csv.DictReader(stream))
    with (directory / "ranking.csv").open(encoding="utf-8-sig", newline="") as stream:
        ranking = list(csv.DictReader(stream))
    total = sum(counts.values())
    coherent_total = sum(
        count for issue_id, count in counts.items() if issues[issue_id]["coherent"] == "yes"
    )
    top10 = ranking[:10]
    top10_total = sum(int(row["complaint_count"]) for row in top10)
    top10_coherent = sum(
        int(row["complaint_count"])
        for row in top10
        if issues[row["issue_id"]]["coherent"] == "yes"
    )
    names = Counter(
        (row["topic"], _normalized_name(row["issue_name"])) for row in issues.values()
    )
    duplicates = [
        {"topic": topic, "normalized_name": name, "cluster_count": count}
        for (topic, name), count in sorted(names.items())
        if count > 1
    ]
    coherent_count = sum(row["coherent"] == "yes" for row in issues.values())
    return {
        "issue_count": len(issues),
        "coherent_issue_count": coherent_count,
        "coherent_issue_ratio": coherent_count / len(issues),
        "membership_weighted_coherent_rate_overall": coherent_total / total,
        "membership_weighted_coherent_rate_top10": top10_coherent / top10_total,
        "top10_issue_ids": [row["issue_id"] for row in top10],
        "top10_incoherent_issue_ids": [
            row["issue_id"]
            for row in top10
            if issues[row["issue_id"]]["coherent"] != "yes"
        ],
        "duplicate_normalized_names": duplicates,
        "duplicate_normalized_name_count": sum(row["cluster_count"] - 1 for row in duplicates),
    }


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
