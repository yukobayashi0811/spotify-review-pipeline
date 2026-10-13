from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .constants import TOPICS
from .io_utils import atomic_write_text, write_json
from .storage import Store


WORD_BUCKETS = ("00-05", "06-15", "16-40", "41+")
BOOTSTRAP_SEED = 20261003


MIN_LOCAL_VERIFIED = 30

def run_sensitivity_analysis(
    *, state_path: str | Path, output_json: str | Path, output_md: str | Path,
    iterations: int = 1_000, alpha: float = 1.0,
) -> dict[str, Any]:
    import numpy as np

    topic_index = {topic: index for index, topic in enumerate(TOPICS)}
    bucket_index = {bucket: index for index, bucket in enumerate(WORD_BUCKETS)}
    with Store(state_path) as store:
        verification = [
            dict(row)
            for row in store.connection.execute(
                """
                SELECT vr.review_id,vr.topic AS true_topic,vr.intent AS true_intent,
                       vr.sample_weight,r.topic AS predicted_topic,
                       r.intent AS predicted_intent,t.word_count
                FROM verifier_records vr
                JOIN records r USING(review_id)
                JOIN source_rows s USING(review_id)
                JOIN texts t USING(text_sha256)
                WHERE vr.final_source='local'
                ORDER BY vr.review_id
                """
            )
        ]
        if len(verification) < MIN_LOCAL_VERIFIED:
            raise RuntimeError(
                f"Sensitivity analysis requires at least {MIN_LOCAL_VERIFIED} final_source=local "
                "verifier records; "
                f"found {len(verification)}"
            )
        ranking = store.get_metadata("ranking", [])
        issue_topic = {
            str(row["issue_id"]): str(row["topic"])
            for row in store.connection.execute("SELECT issue_id,topic FROM issue_names")
        }
        issue_cells = [
            dict(row)
            for row in store.connection.execute(
                """
                SELECT m.issue_id,r.topic,
                       CASE WHEN t.word_count<=5 THEN '00-05'
                            WHEN t.word_count<=15 THEN '06-15'
                            WHEN t.word_count<=40 THEN '16-40' ELSE '41+' END AS word_bucket,
                       COUNT(*) AS row_count,SUM(r.severity) AS severity_sum
                FROM membership m JOIN records r USING(review_id)
                JOIN source_rows s USING(review_id) JOIN texts t USING(text_sha256)
                GROUP BY m.issue_id,r.topic,word_bucket
                ORDER BY m.issue_id,word_bucket
                """
            )
        ]
        corpus_cells = [
            dict(row)
            for row in store.connection.execute(
                """
                SELECT r.topic,r.intent,
                       CASE WHEN t.word_count<=5 THEN '00-05'
                            WHEN t.word_count<=15 THEN '06-15'
                            WHEN t.word_count<=40 THEN '16-40' ELSE '41+' END AS word_bucket,
                       COUNT(*) AS row_count,SUM(r.severity) AS severity_sum
                FROM records r JOIN source_rows s USING(review_id)
                JOIN texts t USING(text_sha256)
                WHERE r.status='completed'
                GROUP BY r.topic,r.intent,word_bucket
                """
            )
        ]

    prepared = []
    for row in verification:
        prepared.append(
            {
                "bucket": bucket_index[_word_bucket(int(row["word_count"]))],
                "predicted_binary": int(_is_complaint(row["predicted_intent"])),
                "true_binary": int(_is_complaint(row["true_intent"])),
                "predicted_topic": topic_index[row["predicted_topic"]],
                "true_topic": topic_index[row["true_topic"]],
                "weight": float(row["sample_weight"] or 1.0),
            }
        )
    intent_counts, topic_counts = _confusion_counts(prepared)
    intent_probabilities, topic_probabilities = _smoothed_probabilities(
        intent_counts, topic_counts, alpha
    )
    point_issues = _estimate_issues(
        issue_cells, intent_probabilities, topic_probabilities, topic_index, bucket_index
    )
    point_topics = _estimate_topics(
        corpus_cells, intent_probabilities, topic_probabilities, topic_index, bucket_index
    )

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    by_bucket: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in prepared:
        by_bucket[row["bucket"]].append(row)
    issue_samples: dict[str, dict[str, list[float]]] = {
        issue_id: {"adjusted_complaint_count": [], "adjusted_priority_score": []}
        for issue_id in point_issues
    }
    topic_samples: dict[str, dict[str, list[float]]] = {
        topic: {"adjusted_complaint_count": [], "adjusted_priority_score": []}
        for topic in point_topics
    }
    issue_top = Counter()
    topic_top = Counter()
    for _ in range(iterations):
        sample: list[dict[str, Any]] = []
        for bucket in range(len(WORD_BUCKETS)):
            rows = by_bucket[bucket]
            choices = rng.integers(0, len(rows), size=len(rows))
            sample.extend(rows[int(index)] for index in choices)
        boot_intent_counts, boot_topic_counts = _confusion_counts(sample)
        boot_intent, boot_topic = _smoothed_probabilities(
            boot_intent_counts, boot_topic_counts, alpha
        )
        estimated_issues = _estimate_issues(
            issue_cells, boot_intent, boot_topic, topic_index, bucket_index
        )
        estimated_topics = _estimate_topics(
            corpus_cells, boot_intent, boot_topic, topic_index, bucket_index
        )
        issue_top[max(estimated_issues, key=lambda key: estimated_issues[key]["adjusted_priority_score"])] += 1
        topic_top[max(estimated_topics, key=lambda key: estimated_topics[key]["adjusted_priority_score"])] += 1
        for issue_id, value in estimated_issues.items():
            for metric in issue_samples[issue_id]:
                issue_samples[issue_id][metric].append(float(value[metric]))
        for topic, value in estimated_topics.items():
            for metric in topic_samples[topic]:
                topic_samples[topic][metric].append(float(value[metric]))

    baseline_by_issue = {str(row["issue_id"]): row for row in ranking}
    issue_results = []
    for issue_id, value in point_issues.items():
        baseline = baseline_by_issue[issue_id]
        issue_results.append(
            {
                "issue_id": issue_id,
                "topic": issue_topic[issue_id],
                "baseline_rank": int(baseline["rank"]),
                "baseline_complaint_count": int(baseline["complaint_count"]),
                "baseline_priority_score": int(baseline["priority_score"]),
                **value,
                "adjusted_complaint_count_95ci": _interval(
                    issue_samples[issue_id]["adjusted_complaint_count"]
                ),
                "adjusted_priority_score_95ci": _interval(
                    issue_samples[issue_id]["adjusted_priority_score"]
                ),
            }
        )
    issue_results.sort(key=lambda row: (-row["adjusted_priority_score"], row["issue_id"]))
    for rank, row in enumerate(issue_results, 1):
        row["adjusted_rank"] = rank

    baseline_topic: dict[str, dict[str, int]] = defaultdict(
        lambda: {"complaint_count": 0, "priority_score": 0}
    )
    for row in ranking:
        topic = issue_topic[str(row["issue_id"])]
        baseline_topic[topic]["complaint_count"] += int(row["complaint_count"])
        baseline_topic[topic]["priority_score"] += int(row["priority_score"])
    baseline_topic_order = sorted(
        baseline_topic, key=lambda topic: (-baseline_topic[topic]["priority_score"], topic)
    )
    baseline_topic_rank = {topic: index + 1 for index, topic in enumerate(baseline_topic_order)}
    topic_results = []
    for topic, value in point_topics.items():
        topic_results.append(
            {
                "topic": topic,
                "baseline_rank": baseline_topic_rank.get(topic),
                "baseline_complaint_count": baseline_topic[topic]["complaint_count"],
                "baseline_priority_score": baseline_topic[topic]["priority_score"],
                **value,
                "adjusted_complaint_count_95ci": _interval(
                    topic_samples[topic]["adjusted_complaint_count"]
                ),
                "adjusted_priority_score_95ci": _interval(
                    topic_samples[topic]["adjusted_priority_score"]
                ),
            }
        )
    topic_results.sort(key=lambda row: (-row["adjusted_priority_score"], row["topic"]))
    for rank, row in enumerate(topic_results, 1):
        row["adjusted_rank"] = rank

    baseline_top_issue = str(ranking[0]["issue_id"])
    adjusted_top_issue = issue_results[0]["issue_id"]
    baseline_top_topic = baseline_topic_order[0]
    adjusted_top_topic = topic_results[0]["topic"]
    report = {
        "method": {
            "verification_population": "verifier_records final_source=local only",
            "verification_distinct_texts": len(prepared),
            "word_buckets": list(WORD_BUCKETS),
            "smoothing": f"Laplace alpha={alpha} on P(verifier label | saved prediction, word bucket)",
            "sample_weights_used": True,
            "bootstrap_iterations": iterations,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "assumption": (
                "Within each word bucket, intent and topic correction factors are applied "
                "independently. Issue estimates can remove likely false positives or topic "
                "misassignments but cannot allocate predicted non-complaints into a latent issue."
            ),
            "use": "sensitivity analysis only; not exported as memo claims",
        },
        "confusion_matrices": _serialize_confusions(
            intent_counts, topic_counts, intent_probabilities, topic_probabilities
        ),
        "issues": issue_results,
        "topics": topic_results,
        "rank_stability": {
            "baseline_top_issue": baseline_top_issue,
            "adjusted_top_issue": adjusted_top_issue,
            "top_issue_unchanged": baseline_top_issue == adjusted_top_issue,
            "baseline_top_issue_bootstrap_top_probability": issue_top[baseline_top_issue] / iterations,
            "adjusted_top_issue_bootstrap_top_probability": issue_top[adjusted_top_issue] / iterations,
            "baseline_top_topic": baseline_top_topic,
            "adjusted_top_topic": adjusted_top_topic,
            "top_topic_unchanged": baseline_top_topic == adjusted_top_topic,
            "baseline_top_topic_bootstrap_top_probability": topic_top[baseline_top_topic] / iterations,
            "adjusted_top_topic_bootstrap_top_probability": topic_top[adjusted_top_topic] / iterations,
            "issue_bootstrap_top_frequency": dict(issue_top.most_common()),
            "topic_bootstrap_top_frequency": dict(topic_top.most_common()),
        },
        "limitations": [
            "The verifier is an independent model pass, not human ground truth.",
            f"The local stratum has {len(prepared)} verified texts; sparse cells depend on smoothing.",
            "Bootstrap intervals represent verifier-sample uncertainty under this correction model, not total model uncertainty.",
            "Issue corrections cannot recover false-negative complaints into a specific issue because they were never grouped.",
            "These corrected estimates are sensitivity outputs and are not claims.csv evidence.",
        ],
    }
    write_json(output_json, report)
    atomic_write_text(output_md, _render_markdown(report))
    return report


def _confusion_counts(rows: list[dict[str, Any]]) -> tuple[Any, Any]:
    import numpy as np

    intent = np.zeros((len(WORD_BUCKETS), 2, 2), dtype=np.float64)
    topic = np.zeros((len(WORD_BUCKETS), len(TOPICS), len(TOPICS)), dtype=np.float64)
    for row in rows:
        intent[row["bucket"], row["predicted_binary"], row["true_binary"]] += row["weight"]
        topic[row["bucket"], row["predicted_topic"], row["true_topic"]] += row["weight"]
    return intent, topic


def _smoothed_probabilities(intent: Any, topic: Any, alpha: float) -> tuple[Any, Any]:
    intent_smoothed = intent + alpha
    topic_smoothed = topic + alpha
    return (
        intent_smoothed / intent_smoothed.sum(axis=2, keepdims=True),
        topic_smoothed / topic_smoothed.sum(axis=2, keepdims=True),
    )


def _estimate_issues(
    cells: list[dict[str, Any]], intent_probability: Any, topic_probability: Any,
    topic_index: dict[str, int], bucket_index: dict[str, int],
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = defaultdict(
        lambda: {"adjusted_complaint_count": 0.0, "adjusted_priority_score": 0.0}
    )
    for cell in cells:
        bucket = bucket_index[cell["word_bucket"]]
        topic = topic_index[cell["topic"]]
        factor = float(intent_probability[bucket, 1, 1] * topic_probability[bucket, topic, topic])
        output[cell["issue_id"]]["adjusted_complaint_count"] += int(cell["row_count"]) * factor
        output[cell["issue_id"]]["adjusted_priority_score"] += int(cell["severity_sum"]) * factor
    for value in output.values():
        count = value["adjusted_complaint_count"]
        value["adjusted_mean_severity"] = value["adjusted_priority_score"] / count if count else 0.0
        for key in value:
            value[key] = round(float(value[key]), 6)
    return dict(output)


def _estimate_topics(
    cells: list[dict[str, Any]], intent_probability: Any, topic_probability: Any,
    topic_index: dict[str, int], bucket_index: dict[str, int],
) -> dict[str, dict[str, float]]:
    output = {
        topic: {"adjusted_complaint_count": 0.0, "adjusted_priority_score": 0.0}
        for topic in TOPICS
    }
    for cell in cells:
        bucket = bucket_index[cell["word_bucket"]]
        predicted_topic = topic_index[cell["topic"]]
        predicted_binary = int(_is_complaint(cell["intent"]))
        complaint_probability = float(intent_probability[bucket, predicted_binary, 1])
        for topic, target in topic_index.items():
            factor = complaint_probability * float(topic_probability[bucket, predicted_topic, target])
            output[topic]["adjusted_complaint_count"] += int(cell["row_count"]) * factor
            output[topic]["adjusted_priority_score"] += int(cell["severity_sum"]) * factor
    for value in output.values():
        count = value["adjusted_complaint_count"]
        value["adjusted_mean_severity"] = value["adjusted_priority_score"] / count if count else 0.0
        for key in value:
            value[key] = round(float(value[key]), 6)
    return output


def _serialize_confusions(intent: Any, topic: Any, intent_p: Any, topic_p: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    binary_labels = ("not_complaint_or_cancellation", "complaint_or_cancellation")
    for bucket_index, bucket in enumerate(WORD_BUCKETS):
        output[bucket] = {
            "intent_binary": {
                predicted: {
                    true: {
                        "weighted_count": float(intent[bucket_index, pred_index, true_index]),
                        "smoothed_probability": float(intent_p[bucket_index, pred_index, true_index]),
                    }
                    for true_index, true in enumerate(binary_labels)
                }
                for pred_index, predicted in enumerate(binary_labels)
            },
            "topic": {
                predicted: {
                    true: {
                        "weighted_count": float(topic[bucket_index, pred_index, true_index]),
                        "smoothed_probability": float(topic_p[bucket_index, pred_index, true_index]),
                    }
                    for true_index, true in enumerate(TOPICS)
                }
                for pred_index, predicted in enumerate(TOPICS)
            },
        }
    return output


def _interval(values: list[float]) -> dict[str, float]:
    import numpy as np

    lower, upper = np.quantile(values, [0.025, 0.975])
    return {"lower": round(float(lower), 6), "upper": round(float(upper), 6)}


def _is_complaint(intent: str) -> bool:
    return intent in {"complaint", "cancellation"}


def _word_bucket(word_count: int) -> str:
    if word_count <= 5:
        return "00-05"
    if word_count <= 15:
        return "06-15"
    if word_count <= 40:
        return "16-40"
    return "41+"


def _render_markdown(report: dict[str, Any]) -> str:
    stability = report["rank_stability"]
    lines = [
        "# Sensitivity analysis",
        "",
        f"This analysis uses only the {report['method']['verification_distinct_texts']} "
        "`final_source=local` verifier records. It applies word-bucket-specific, "
        "sample-weighted confusion matrices with Laplace smoothing and "
        f"{report['method']['bootstrap_iterations']:,} stratified bootstrap repetitions.",
        "",
        "## Rank stability",
        "",
        f"- Baseline top issue: `{stability['baseline_top_issue']}`; adjusted top issue: "
        f"`{stability['adjusted_top_issue']}`; unchanged: `{str(stability['top_issue_unchanged']).lower()}`.",
        f"- Baseline top topic: `{stability['baseline_top_topic']}`; adjusted top topic: "
        f"`{stability['adjusted_top_topic']}`; unchanged: `{str(stability['top_topic_unchanged']).lower()}`.",
        f"- Baseline top issue was first in "
        f"{stability['baseline_top_issue_bootstrap_top_probability']:.1%} of bootstrap runs.",
        f"- Baseline top topic was first in "
        f"{stability['baseline_top_topic_bootstrap_top_probability']:.1%} of bootstrap runs.",
        "",
        "## Top adjusted issues",
        "",
        "| Rank | Issue | Baseline score | Adjusted score (95% interval) |",
        "|---:|---|---:|---:|",
    ]
    for row in report["issues"][:10]:
        interval = row["adjusted_priority_score_95ci"]
        lines.append(
            f"| {row['adjusted_rank']} | `{row['issue_id']}` | {row['baseline_priority_score']:,} | "
            f"{row['adjusted_priority_score']:,.1f} ({interval['lower']:,.1f}–{interval['upper']:,.1f}) |"
        )
    lines.extend(
        [
            "",
            "## Topic sensitivity",
            "",
            "| Rank | Topic | Baseline score | Adjusted score (95% interval) |",
            "|---:|---|---:|---:|",
        ]
    )
    for row in report["topics"]:
        interval = row["adjusted_priority_score_95ci"]
        lines.append(
            f"| {row['adjusted_rank']} | {row['topic']} | {row['baseline_priority_score']:,} | "
            f"{row['adjusted_priority_score']:,.1f} ({interval['lower']:,.1f}–{interval['upper']:,.1f}) |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {value}" for value in report["limitations"])
    return "\n".join(lines) + "\n"
