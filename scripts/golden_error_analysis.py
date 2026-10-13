#!/usr/bin/env python3
"""Error analysis of the final labels against the Golden 50 human labels (no model calls).

Writes evals/golden_final_error_analysis.md and evals/golden_final_error_cases.csv.
Counts, confusion tables and the quote/entity checks are computed by code.
The failure category of each disagreement was assigned by reading the case
(analyst: the AI assistant that built the pipeline); the human labels in
evals/golden_50.csv are never changed.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPICS = ["access", "usability", "playback", "downloads", "catalog", "billing", "support", "other"]
INTENTS = ["cancellation", "complaint", "request", "praise", "unclear"]

# Failure categories assigned by reading each disagreement (review_id prefix -> category).
CATEGORY = {
    "947821dc": "blocked_core_task_scored_3_not_4",
    "47f1406d": "blocked_core_task_scored_3_not_4",
    "3ddb3f4e": "blocked_core_task_scored_3_not_4",
    "46c0b49f": "blocked_core_task_scored_3_not_4",
    "dceb14e7": "blocked_core_task_scored_3_not_4",
    "991b6b3a": "degraded_function_scored_2_not_3",
    "47428620": "generic_criticism_severity_1_vs_2",
    "1a41c7e1": "generic_criticism_severity_1_vs_2",
    "8cc4fad4": "generic_criticism_severity_1_vs_2",
    "9034d675": "generic_criticism_severity_1_vs_2",
    "ac6cd66d": "non_english_or_code_mixed",
    "46842184": "non_english_or_code_mixed",
    "372d4e67": "non_english_or_code_mixed",
    "36609c5c": "non_english_or_code_mixed",
    "1fc8b08f": "paid_plan_or_price_mention_pulled_to_billing",
    "d2f3874f": "paid_plan_or_price_mention_pulled_to_billing",
    "283b5843": "paid_plan_or_price_mention_pulled_to_billing",
    "5de7f95b": "boycott_or_political_intent",
    "292ce26a": "boycott_or_political_intent",
    "f4c8146a": "unclear_vs_praise",
    "b5ee7834": "specific_feature_vs_generic_other",
    "2aa566c6": "specific_feature_vs_generic_other",
    "99b93e31": "specific_feature_vs_generic_other",
    "b58dd6f0": "specific_feature_vs_generic_other",
    "723f07de": "edited_review_resolved_update",
}
CATEGORY_NOTES = {
    "blocked_core_task_scored_3_not_4": "Playback or song choice is blocked ('cannot play', 'plays a little then stops'); the human used 4 (core task blocked), the model 3 (degraded). The local classifier rarely predicts 4.",
    "degraded_function_scored_2_not_3": "Restricted functionality ('limited functionality ... too much') scored as an annoyance (2) instead of degraded function (3).",
    "generic_criticism_severity_1_vs_2": "Content-free criticism ('worst app', 'I hate this app'). The human used 1; the model used 2, which is what the shared definition gives for generic criticism, so these are boundary cases rather than clear model errors.",
    "non_english_or_code_mixed": "Indonesian, Tagalog and romanized Hindi/Urdu reviews. The TF-IDF classifier has little vocabulary for them; every such Golden case has at least one wrong field.",
    "paid_plan_or_price_mention_pulled_to_billing": "A Premium or price word moves the topic to billing even when the complaint is about ads or usability ('Duo Premium ... No ads at all!' is praise for usability). The contract says a paid-plan mention alone is not billing.",
    "boycott_or_political_intent": "Boycott or political statements: whether they express a personal departure (cancellation) or are a bare slogan (unclear) is ambiguous; the model chose unclear.",
    "unclear_vs_praise": "Content too thin to decide between praise and unclear.",
    "specific_feature_vs_generic_other": "Short praise or criticism that names a feature in passing (ads, diversity, updates, collections): the human picked the specific topic, the model 'other' or a neighbouring topic.",
    "edited_review_resolved_update": "An edited review whose 'Update:' says Support fixed the problem, followed by the 'Old Post' describing a blocking playback failure. The human labeled the original problem (playback complaint, severity 4); GPT-6 Luna labeled the update (support praise, severity 1). The label definitions do not say which part of an edited review governs, so this is a definition gap rather than a clear model error.",
}


# Reviews not written in English, identified by reading all 50 texts (review_id prefix -> language).
# "Avtar sungh" (44129679) is a personal name, not a language sample, and is not counted.
NON_ENGLISH = {
    "ac6cd66d": "Tagalog",
    "46842184": "Indonesian",
    "372d4e67": "romanized Hindi/Urdu",
    "36609c5c": "romanized Hindi/Urdu",
}


def matrix(rows: list[dict], field: str, labels: list[str]) -> list[str]:
    counts = Counter((row[f"gold_{field}"], row[f"pred_{field}"]) for row in rows)
    used = [label for label in labels if any(label in pair for pair in counts)]
    lines = ["| human \\ predicted | " + " | ".join(used) + " | n |", "|---|" + "---:|" * (len(used) + 1)]
    for gold in used:
        total = sum(counts[(gold, pred)] for pred in used)
        if total:
            lines.append(f"| **{gold}** | " + " | ".join(str(counts[(gold, pred)] or "") for pred in used) + f" | {total} |")
    return lines


def main() -> int:
    gold = {row["review_id"]: row for row in csv.DictReader((ROOT / "evals/golden_50.csv").open(encoding="utf-8"))}
    pred = {row["review_id"]: row for row in csv.DictReader((ROOT / "evals/golden_final_predictions.csv").open(encoding="utf-8"))}
    rows = []
    for review_id, g in gold.items():
        p = pred[review_id]
        text = g["review_text"]
        pred_entities = json.loads(p["entities"] or "[]")
        unsupported = [entity for entity in pred_entities if entity.lower() not in text.lower()]
        row = {
            "review_id": review_id,
            "label_source": "gpt-6-luna" if p["label_config"].startswith("gpt-6-luna") else "local_classifier",
            "language": NON_ENGLISH.get(review_id[:8], "English"),
            "needs_review": p["needs_review"],
            **{f"gold_{field}": g[field] for field in ("topic", "intent", "severity")},
            **{f"pred_{field}": p[field] for field in ("topic", "intent", "severity")},
            "quote_is_substring": p["evidence_quote"] in text,
            "quote_is_full_text": p["evidence_quote"].strip() == text.strip(),
            "pred_entities": p["entities"],
            "gold_entities": g["entities"],
            "unsupported_entities": json.dumps(unsupported, ensure_ascii=False),
            "entity_is_brand_only": pred_entities in (["Spotify"], ["spotify"]),
            "review_text": text.replace("\n", " "),
        }
        row["mismatch"] = ",".join(f for f in ("topic", "intent", "severity") if str(row[f"gold_{f}"]) != str(row[f"pred_{f}"]))
        row["category"] = CATEGORY.get(review_id[:8], "") if row["mismatch"] else ""
        rows.append(row)
    missing = [row["review_id"] for row in rows if row["mismatch"] and not row["category"]]
    if missing:
        raise SystemExit(f"Uncategorized disagreements: {missing}")

    with (ROOT / "evals/golden_final_error_cases.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    errors = [row for row in rows if row["mismatch"]]
    by_source = Counter(row["label_source"] for row in rows)
    err_by_source = Counter(row["label_source"] for row in errors)
    non_english = [row for row in rows if row["language"] != "English"]
    categories = Counter(row["category"] for row in errors)
    topic_support = Counter(row["gold_topic"] for row in rows)
    headline = json.loads((ROOT / "evals/golden_final_evaluation.json").read_text(encoding="utf-8"))["agreement"]
    within_1 = sum(abs(int(row["gold_severity"]) - int(row["pred_severity"])) <= 1 for row in rows) / len(rows)
    lines = [
        "# Golden 50: error analysis of the final labels",
        "",
        "Generated by `scripts/golden_error_analysis.py` (no model calls) from `evals/golden_50.csv` (human labels) and "
        "`evals/golden_final_predictions.csv` (the final full-run labels for those 50 reviews). Per-case detail: "
        "`evals/golden_final_error_cases.csv`. Headline agreement is in `evals/golden_final_evaluation.json` "
        f"(topic {headline['topic']:.2f}, intent {headline['intent']:.2f}, exact severity {headline['severity_exact']:.2f}, "
        f"severity within ±1 {within_1:.2f}).",
        "",
        "Failure categories below were assigned by reading each disagreement (analyst: the AI assistant that built the "
        "pipeline). The human labels are unchanged; where a disagreement looks like a boundary case in the shared "
        "definitions, that is stated rather than counted as a correction.",
        "",
        "## Where the errors are",
        "",
        f"- Rows with at least one wrong field (topic, intent or severity): {len(errors)} of {len(rows)}.",
        f"- By final label source: local classifier {err_by_source['local_classifier']} of {by_source['local_classifier']}; "
        f"GPT-6 Luna {err_by_source['gpt-6-luna']} of {by_source['gpt-6-luna']}.",
        f"- Not in English (identified by reading): {len(non_english)} rows ("
        + ", ".join(sorted({row['language'] for row in non_english})) + f"), "
        f"{sum(bool(row['mismatch']) for row in non_english)} with an error.",
        f"- `needs_review` was set on {sum(row['needs_review'] == 'true' for row in rows)} rows; "
        f"{sum(row['needs_review'] == 'true' for row in errors)} of the {len(errors)} error rows had it "
        "(see `evals/golden_needs_review_analysis.json`).",
        "",
        "| Category | Rows | What happens |",
        "|---|---:|---|",
    ]
    for category, count in categories.most_common():
        lines.append(f"| {category} | {count} | {CATEGORY_NOTES[category]} |")
    lines += [
        "",
        "## Confusion tables (human rows × predicted columns)",
        "",
        "### Topic",
        "",
        *matrix(rows, "topic", TOPICS),
        "",
        "Per-topic human counts: " + ", ".join(f"{topic} {topic_support[topic]}" for topic in TOPICS if topic_support[topic]) +
        ". Access and support have no Golden examples, and billing, downloads one each, so per-topic accuracy for them is not estimable from this set.",
        "",
        "### Intent",
        "",
        *matrix(rows, "intent", INTENTS),
        "",
        "### Severity",
        "",
        *matrix(rows, "severity", ["1", "2", "3", "4", "5"]),
        "",
        "## Evidence quotes and entities",
        "",
        f"- Quote is an exact substring of the review: {sum(row['quote_is_substring'] for row in rows)}/{len(rows)} (checked in code).",
        f"- Quote is the whole review text: {sum(row['quote_is_full_text'] for row in rows)}/{len(rows)}. The local classifier copies the text (or its first sentence) rather than selecting the clause that supports the label, so for long reviews a valid quote can be a weak justification; the quote is mechanically valid in every case, and whether it supports the label is part of the per-case review above.",
        f"- Predicted entities not found in the text (unsupported additions): {sum(row['unsupported_entities'] != '[]' for row in rows)} rows.",
        f"- Entity list is only the brand name 'Spotify': {sum(row['entity_is_brand_only'] for row in rows)} rows; the human lists name product features instead, which is the main reason entity exact match is {headline['entities_exact']:.2f}.",
        "",
        "## Disagreements",
        "",
        "| review_id | source | category | topic (human→pred) | intent | severity | text (first 110 chars) |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in errors:
        lines.append(
            f"| `{row['review_id'][:8]}` | {row['label_source']} | {row['category']} | "
            f"{row['gold_topic']}→{row['pred_topic']} | {row['gold_intent']}→{row['pred_intent']} | "
            f"{row['gold_severity']}→{row['pred_severity']} | {row['review_text'][:110].replace('|', '/')} |"
        )
    lines += [
        "",
        "## What changed because of this analysis",
        "",
        "- The billing boundary (`premium-only controls are billing; a paid-plan mention alone is not`) was added to the verifier prompt and `config/labels.md` so the verifier applies the same rule as the classifier.",
        "- The risk router sends rows the local classifier scores as severity ≥ 4, support, or billing/privacy terms to GPT-6 Luna; blocked-playback reviews that the local model scores 3 are not caught by that rule, which is a known limitation.",
        "- Non-English reviews are a known weakness of the local classifier and are reported as a limitation rather than filtered out.",
        "",
    ]
    (ROOT / "evals/golden_final_error_analysis.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"errors": len(errors), "categories": categories, "non_english": len(non_english)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
