from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .classify import ModelConfig, _call_event, _failed_result_event, chunks
from .constants import TOPICS
from .group import _validate_group_payload
from .io_utils import canonical, write_csv, write_json
from .providers import Provider
from .schemas import GROUP_SCHEMA
from .storage import Store


PILOT_GROUP_VERSION = "pilot-topic-groups-v1"


def run_pilot_grouping(
    *,
    state_path: str | Path,
    provider: Provider,
    prompt_path: str | Path,
    config: ModelConfig,
    run_id: str,
    artifacts_dir: str | Path,
) -> dict[str, Any]:
    """Bounded grouping proof for the frozen 100-review execution.

    This is intentionally not the full-corpus embedding selection. Code owns one
    complaint/cancellation cluster per fixed primary topic; the independent local
    grouping role only names and assesses those code-owned clusters.
    """

    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    group_config = (
        f"{config.model}:{config.effort}:group-{PILOT_GROUP_VERSION}:group-v3"
        f":{config.execution_fingerprint}"
    )
    with Store(state_path) as store:
        issue_inputs: list[dict[str, Any]] = []
        members: dict[str, list[str]] = {}
        for topic in TOPICS:
            rows = list(
                store.connection.execute(
                    """
                    SELECT r.review_id,r.severity,r.evidence_quote,s.review_text
                    FROM records r JOIN source_rows s USING(review_id)
                    WHERE r.status='completed' AND r.intent IN ('complaint','cancellation')
                      AND r.topic=?
                    ORDER BY r.severity DESC,r.review_id ASC
                    """,
                    (topic,),
                )
            )
            if not rows:
                continue
            issue_id = f"I_{topic.upper()}_01"
            members[issue_id] = [str(row["review_id"]) for row in rows]
            issue_inputs.append(
                {
                    "issue_id": issue_id,
                    "topic": topic,
                    "examples": [
                        {
                            "review_id": str(row["review_id"]),
                            "review_text": str(row["review_text"]),
                        }
                        for row in rows[:10]
                    ],
                }
            )
        if len(issue_inputs) < 3:
            raise RuntimeError(
                "Frozen 100 proof needs at least three complaint/cancellation topic groups "
                "so the memo can compare one priority and two alternatives"
            )

        names: dict[str, dict[str, Any]] = {}
        for batch in chunks(issue_inputs, 5):
            result = None
            try:
                result = provider.call(
                    role="group",
                    model=config.model,
                    effort=config.effort,
                    developer_prompt=prompt,
                    user_payload={"issues": batch},
                    schema=GROUP_SCHEMA,
                    max_output_tokens=min(config.max_output_tokens, 2500),
                    timeout_seconds=config.timeout_seconds,
                )
                values = _validate_group_payload(result.payload, batch)
                store.log_call(
                    _call_event(
                        result=result,
                        run_id=run_id,
                        role="group",
                        review_ids=[
                            example["review_id"]
                            for issue in batch
                            for example in issue["examples"]
                        ],
                        model=config.model,
                        effort=config.effort,
                        phase="pilot_topic_groups",
                        outcome="succeeded",
                        label_config=group_config,
                    )
                )
                for value in values:
                    names[value["issue_id"]] = value
            except Exception as error:
                if result is not None:
                    store.log_call(
                        _failed_result_event(
                            result=result,
                            run_id=run_id,
                            role="group",
                            review_ids=[
                                example["review_id"]
                                for issue in batch
                                for example in issue["examples"]
                            ],
                            model=config.model,
                            effort=config.effort,
                            phase="pilot_topic_groups",
                            label_config=group_config,
                            error=f"{type(error).__name__}: {error}",
                        )
                    )
                raise

        with store.transaction() as connection:
            connection.execute("DELETE FROM membership")
            connection.execute("DELETE FROM issue_names")
            for issue in issue_inputs:
                value = names[issue["issue_id"]]
                connection.execute(
                    """
                    INSERT INTO issue_names(
                      issue_id,topic,issue_name,description,label_config,coherent,actionable,flags_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        issue["issue_id"], issue["topic"], value["issue_name"],
                        value["description"], group_config, value["coherent"],
                        value["actionable"], canonical(["pilot_topic_group"]),
                    ),
                )
                connection.executemany(
                    "INSERT INTO membership(issue_id,review_id) VALUES(?,?)",
                    [(issue["issue_id"], review_id) for review_id in members[issue["issue_id"]]],
                )

        missing = [
            issue["issue_id"]
            for issue in issue_inputs
            if names[issue["issue_id"]]["coherent"] != "yes"
            or names[issue["issue_id"]]["actionable"] != "yes"
        ]
        if len(issue_inputs) - len(missing) < 3:
            raise RuntimeError(
                "Local grouping assessment produced fewer than three coherent/actionable "
                f"pilot issues: excluded={missing}"
            )
        issues = [
            {
                "issue_id": issue["issue_id"],
                "topic": issue["topic"],
                "issue_name": names[issue["issue_id"]]["issue_name"],
                "description": names[issue["issue_id"]]["description"],
                "coherent": names[issue["issue_id"]]["coherent"],
                "actionable": names[issue["issue_id"]]["actionable"],
                "flags_json": canonical(["pilot_topic_group"]),
                "label_config": group_config,
            }
            for issue in issue_inputs
        ]
        membership = [
            {"issue_id": issue_id, "review_id": review_id}
            for issue_id in sorted(members)
            for review_id in sorted(members[issue_id])
        ]

    write_csv(
        artifacts / "issues.csv",
        [
            "issue_id", "topic", "issue_name", "description", "coherent",
            "actionable", "flags_json", "label_config",
        ],
        issues,
    )
    write_csv(artifacts / "membership.csv", ["issue_id", "review_id"], membership)
    report = {
        "version": PILOT_GROUP_VERSION,
        "scope": "100-review execution proof; not the adopted full-corpus embedding grouping",
        "membership_owner": "code: one complaint/cancellation issue per fixed primary topic",
        "naming_and_review_owner": provider.name,
        "issue_count": len(issues),
        "membership_count": len(membership),
        "issues": issues,
    }
    write_json(artifacts / "group_summary.json", report)
    return report
