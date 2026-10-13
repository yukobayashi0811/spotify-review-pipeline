from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .classify import ModelConfig, _call_event, _failed_result_event
from .downstream_analysis import build_actionable_and_topic_outputs
from .io_utils import atomic_write_text, canonical, write_csv, write_json
from .providers import Provider
from .schemas import MEMO_SCHEMA
from .storage import Store


MEMO_SCHEMA_VERSION = "memo-v3"
MEMO_PROMPT_VERSION = "2026-10-12-v3"
MEMO_BUCKET = "M_MEMO_V3"
TYPICAL_EVIDENCE_RULE = (
    "Per issue: the most common severity among member rows (ties -> higher severity); "
    "among distinct member texts with that severity, the three closest to the issue "
    "centroid by cosine similarity of the saved grouping embeddings."
)
ALLOWED_PRIORITY_TOPICS = {"access", "usability", "playback", "billing", "support"}
PRIORITY_RULES = ("coherent_and_actionable", "actionable_only_fallback", "any_issue_fallback")


def _rule_accepts(rule: str, value: dict[str, Any]) -> bool:
    if rule == "coherent_and_actionable":
        return value["coherent"] == "yes" and value["actionable"] == "yes"
    if rule == "actionable_only_fallback":
        return value["actionable"] == "yes"
    return True


def priority_eligibility(
    issues: dict[str, dict[str, Any]], ranking: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """Which issues the memo may recommend, using the strictest rule that leaves one.

    Normally the priority must be coherent and actionable and sit in an allowed topic.
    A small input (such as the 100-review pilot) can leave no such issue; then the rule
    relaxes to actionable issues, then to any ranked issue in an allowed topic, and the
    memo must say so in its limitations. The full-corpus run uses the strict rule.
    """
    ranked = [str(row["issue_id"]) for row in ranking]
    for rule in PRIORITY_RULES:
        eligible = [
            issue_id for issue_id in ranked
            if issue_id in issues
            and issues[issue_id]["topic"] in ALLOWED_PRIORITY_TOPICS
            and _rule_accepts(rule, issues[issue_id])
        ]
        if eligible:
            return rule, eligible
    raise ValueError("No ranked issue in access, usability, playback, billing or support")
METRICS = ("complaint_count", "severity_sum", "mean_severity", "priority_score")


def run_recommendation(
    *,
    state_path: str | Path,
    provider: Provider,
    prompt_path: str | Path,
    config: ModelConfig,
    run_id: str,
    artifacts_dir: str | Path,
    top_n: int = 10,
    evidence_per_issue: int = 3,
) -> dict[str, Any]:
    if top_n != 10 or evidence_per_issue != 3:
        raise ValueError("The memo role requires top_n=10 and at most three representatives per issue")
    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    memo_config = f"{config.model}:{config.effort}:memo-{MEMO_PROMPT_VERSION}:{MEMO_SCHEMA_VERSION}"
    if config.execution_fingerprint:
        memo_config += f":{config.execution_fingerprint}"

    derived = build_actionable_and_topic_outputs(
        state_path=state_path, artifacts_dir=artifacts
    )
    with Store(state_path) as store:
        ranking_all = store.get_metadata("ranking", [])
        if not ranking_all:
            raise ValueError("No ranking is available; run grouping and ranking first")
        evidence = _typical_evidence(
            store, artifacts, sorted(derived["issues"]), per_issue=evidence_per_issue
        )
        coverage = _coverage(store)
        trend = _monthly_trend(store)

        sensitivity = _read_required_json(artifacts / "sensitivity_analysis.json")
        comparison = _read_required_json(artifacts / "grouping_comparison.json")
        verification = _read_required_json(
            artifacts / "verification" / "verification_report.json"
        )
        sensitivity_summary = {
            "rank_stability": sensitivity["rank_stability"],
            "top_adjusted_issues": sensitivity["issues"][:10],
            "adjusted_topics": sensitivity["topics"],
            "limitations": sensitivity["limitations"],
        }
        pack = {
            # The provider adapter recognizes this key and permits up to ten.
            "ranking": ranking_all[:10],
            "actionable_ranking": derived["actionable_ranking"][:10],
            "topic_rollup": derived["topic_rollup"],
            "issues": derived["issues"],
            "evidence": evidence,
            "evidence_selection_rule": TYPICAL_EVIDENCE_RULE,
            "coverage": coverage,
            "monthly_trend": trend,
            "verification_summary": verification["metrics"],
            "sensitivity_summary": sensitivity_summary,
            "grouping_comparison": comparison,
            "dataset_limits": [
                "Historical self-selected app reviews are not the complete customer population.",
                "No revenue, plan tier, confirmed cancellation, or causal retention data is available.",
                "Cancellation language is expressed intent, not observed churn.",
                "Issue-level sensitivity correction cannot allocate predicted non-complaints into a latent issue.",
                "The adopted grouping retains low_coherence issues even after one bounded split.",
            ],
            "claim_rule": (
                "Only baseline issue metrics may appear in claims. Topic rollups, monthly "
                "trend shares, coverage counts and corrected sensitivity values must be "
                "described separately and never exported as claims."
            ),
        }
        rule, eligible_priority = priority_eligibility(derived["issues"], ranking_all)
        if rule != "coherent_and_actionable":
            # Added only when the strict rule leaves no candidate, so a normal run's pack is unchanged.
            pack["priority_eligibility"] = {
                "rule": rule,
                "eligible_priority_issue_ids": eligible_priority,
                "instruction": (
                    "No ranked issue in an allowed topic is both coherent and actionable. Choose the "
                    "priority from eligible_priority_issue_ids and state this in the limitations."
                ),
            }
        input_hash = hashlib.sha256(canonical(pack).encode("utf-8")).hexdigest()
        cached_hash = store.get_metadata("memo_v2_input_hash")
        cached_memo = store.get_metadata("memo_v2_payload")
        if cached_hash == input_hash and cached_memo:
            memo = _validate_memo(
                cached_memo, ranking_all, evidence, derived["issues"], coverage=coverage
            )
            provenance = store.get_metadata("memo_v2_claims_provenance", [])
            model_called = False
        else:
            memo, provenance = _call_memo_with_one_repair(
                store=store,
                provider=provider,
                prompt=prompt,
                pack=pack,
                ranking=ranking_all,
                evidence=evidence,
                issues=derived["issues"],
                config=config,
                run_id=run_id,
                memo_config=memo_config,
                coverage=coverage,
            )
            store.set_metadata("memo_v2_input_hash", input_hash)
            store.set_metadata("memo_v2_payload", memo)
            store.set_metadata("memo_v2_claims_provenance", provenance)
            # The grading adapter reads the generic key.
            store.set_metadata("memo_input_hash", input_hash)
            store.set_metadata("memo_payload", memo)
            store.set_metadata("memo_claims_provenance", provenance)
            model_called = True

    write_json(artifacts / "memo_payload.json", memo)
    write_json(artifacts / "memo_evidence_pack.json", pack)
    write_csv(
        artifacts / "claims.csv",
        ["claim_id", "issue_id", "metric", "value"],
        [
            {key: claim[key] for key in ("claim_id", "issue_id", "metric", "value")}
            for claim in memo["claims"]
        ],
    )
    write_csv(
        artifacts / "claims_provenance.csv",
        ["claim_id", "source", "reason", "request_id"],
        provenance,
    )
    atomic_write_text(artifacts / "memo.md", _render_memo(memo))
    return {
        "model_called": model_called,
        "input_hash": input_hash,
        "claims": len(memo["claims"]),
        "priority": memo["priority"],
        "priority_topic": memo["priority_topic"],
        "priority_issue_id": memo["priority_issue_id"],
        "alternatives": memo["alternative_issue_ids"],
    }


def _call_memo_with_one_repair(
    *, store: Store, provider: Provider, prompt: str, pack: dict[str, Any],
    ranking: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]],
    issues: dict[str, dict[str, Any]], config: ModelConfig, run_id: str,
    memo_config: str, coverage: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    review_ids = [
        item["review_id"] for items in evidence.values() for item in items
    ][:50]
    ledger_hashes = [
        row[0]
        for row in store.connection.execute(
            "SELECT DISTINCT text_sha256 FROM source_rows WHERE review_id IN (%s) "
            "ORDER BY review_id"
            % ",".join("?" for _ in review_ids),
            review_ids,
        )
    ][:2]
    use_luna_ledger = provider.name == "codex-exec-subscription"
    if use_luna_ledger and len(ledger_hashes) < 2:
        # Very small inputs can have fewer than two evidence texts; any two distinct source
        # texts serve as the two-call ledger sentinels.
        for (digest,) in store.connection.execute(
            "SELECT DISTINCT text_sha256 FROM source_rows WHERE text_sha256 IS NOT NULL ORDER BY text_sha256 LIMIT 3"
        ):
            if digest not in ledger_hashes and len(ledger_hashes) < 2:
                ledger_hashes.append(digest)
    if use_luna_ledger and len(ledger_hashes) < 2:
        raise RuntimeError("The memo role requires two distinct ledger sentinels for its two-call cap")
    last_error = ""
    for attempt in range(2):
        admitted = (
            _reserve_memo_call(store, ledger_hashes[attempt])
            if use_luna_ledger
            else []
        )
        request_pack = dict(pack)
        if last_error:
            request_pack["repair_request"] = (
                "The prior response failed strict validation. Correct exactly this error and "
                "return the complete schema again: " + last_error
            )
        result = None
        try:
            result = provider.call(
                role="memo",
                model=config.model,
                effort=config.effort,
                developer_prompt=prompt,
                user_payload=request_pack,
                schema=MEMO_SCHEMA,
                max_output_tokens=min(config.max_output_tokens, 6000),
                timeout_seconds=config.timeout_seconds,
            )
            repairs: list[dict[str, str]] = []
            candidate = _repair_code_owned_memo_fields(
                result.payload, ranking=ranking, evidence=evidence, issues=issues,
                repairs=repairs,
            )
            memo = _validate_memo(candidate, ranking, evidence, issues, coverage=coverage)
            event = _call_event(
                result=result, run_id=run_id, role="memo", review_ids=review_ids,
                model=config.model, effort=config.effort, phase="initial",
                outcome="succeeded", label_config=memo_config,
            )
            event["prompt_version"] = MEMO_PROMPT_VERSION
            event["schema_version"] = MEMO_SCHEMA_VERSION
            store.log_call(event)
            if use_luna_ledger:
                store.settle_luna(
                    bucket=MEMO_BUCKET,
                    text_hashes=admitted,
                    request_id=result.request_id,
                    succeeded=True,
                    input_tokens=result.input_tokens,
                    cached_input_tokens=result.cached_input_tokens,
                    output_tokens=result.output_tokens,
                    duration_seconds=result.duration_seconds,
                )
            code_filled = {
                item["claim_id"] for item in repairs if item.get("field") == "claim"
            }
            provenance = [
                {
                    "claim_id": claim["claim_id"],
                    "source": (
                        "code_template_from_saved_baseline"
                        if claim["claim_id"] in code_filled
                        else "memo_role_validated_against_saved_baseline"
                    ),
                    "reason": (
                        "the memo role omitted or misstated this metric; code wrote the "
                        "sentence and value from the saved ranking"
                        if claim["claim_id"] in code_filled
                        else "model prose; code-validated issue ID, metric, value, and evidence IDs"
                    ),
                    "request_id": result.request_id,
                }
                for claim in memo["claims"]
            ]
            store.set_metadata("memo_code_repairs", repairs)
            return memo, provenance
        except Exception as error:
            last_error = f"{type(error).__name__}: {error}"
            if result is not None:
                event = _failed_result_event(
                    result=result, run_id=run_id, role="memo", review_ids=review_ids,
                    model=config.model, effort=config.effort, phase="initial",
                    label_config=memo_config, error=last_error,
                )
                event["prompt_version"] = MEMO_PROMPT_VERSION
                event["schema_version"] = MEMO_SCHEMA_VERSION
                store.log_call(event)
            if use_luna_ledger:
                store.settle_luna(
                    bucket=MEMO_BUCKET,
                    text_hashes=admitted,
                    request_id=result.request_id if result else f"memo-v3-failed-{attempt + 1}",
                    succeeded=False,
                    input_tokens=result.input_tokens if result else None,
                    cached_input_tokens=result.cached_input_tokens if result else None,
                    output_tokens=result.output_tokens if result else None,
                    duration_seconds=result.duration_seconds if result else 0,
                    error=last_error,
                )
            if attempt == 1:
                raise RuntimeError(
                    "Memo failed strict validation after the single allowed repair: "
                    + last_error
                ) from error
    raise AssertionError("unreachable")


def _reserve_memo_call(store: Store, digest: str) -> list[str]:
    base = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM luna_ledger WHERE bucket<>? "
            "AND status IN ('reserved','succeeded')",
            (MEMO_BUCKET,),
        ).fetchone()[0]
    )
    admitted = store.reserve_luna(
        bucket=MEMO_BUCKET,
        text_hashes=[digest],
        bucket_limit=2,
        global_limit=base + 2,
    )
    if admitted != [digest]:
        raise RuntimeError("The memo role's two-call Luna ledger cap is exhausted")
    return admitted


def _repair_code_owned_memo_fields(
    payload: dict[str, Any], *, ranking: list[dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]], issues: dict[str, dict[str, Any]],
    repairs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Make code-verifiable fields exact without replacing valid model prose.

    * ``priority`` gets the chosen topic/issue prefixed if it names neither.
    * ``alternative_issue_ids`` keeps the model's eligible choices; only when fewer
      than two are valid does code add the next eligible issues (preferring one
      outside the priority topic), with template prose recorded as code-filled.
    * Each required (issue, metric) claim keeps the model's sentence when its
      value already equals the saved baseline; otherwise code writes the sentence
      and value from the saved ranking and records the repair.

    Every repair is appended to ``repairs`` so provenance shows which text came
    from the memo role and which from code.
    """

    log = repairs if repairs is not None else []
    repaired = deepcopy(payload)
    if not isinstance(repaired, dict):
        return repaired
    topic = repaired.get("priority_topic")
    issue_id = repaired.get("priority_issue_id")
    priority = repaired.get("priority")
    if all(isinstance(value, str) and value.strip() for value in (topic, issue_id, priority)):
        folded = priority.casefold()
        if topic.casefold() not in folded and issue_id.casefold() not in folded:
            repaired["priority"] = f"{topic} / {issue_id}: {priority}"
            log.append({"field": "priority", "claim_id": "", "action": "prefixed topic/issue"})
    if not isinstance(issue_id, str) or issue_id not in issues:
        return repaired
    by_issue = {str(row["issue_id"]): row for row in ranking}
    rule, _ = priority_eligibility(issues, ranking)
    eligible = [
        str(row["issue_id"])
        for row in ranking
        if str(row["issue_id"]) != issue_id
        and str(row["issue_id"]) in issues
        and _rule_accepts(rule, issues[str(row["issue_id"])])
    ]
    supplied = repaired.get("alternative_issue_ids")
    supplied_prose = repaired.get("alternatives")
    prose = [value for value in supplied_prose if isinstance(value, str)] if isinstance(
        supplied_prose, list
    ) else []
    selected: list[str] = []
    if isinstance(supplied, list):
        for value in supplied:
            if value in eligible and value not in selected:
                selected.append(value)
    if len(selected) < 2:
        priority_topic = issues[issue_id]["topic"]
        has_other_topic = any(issues[value]["topic"] != priority_topic for value in selected)
        ordered = sorted(
            eligible,
            key=lambda value: (
                has_other_topic or issues[value]["topic"] == priority_topic,
                eligible.index(value),
            ),
        )
        for value in ordered:
            if len(selected) >= 2:
                break
            if value not in selected:
                selected.append(value)
                log.append({
                    "field": "alternative", "claim_id": "",
                    "action": f"code added eligible alternative {value}",
                })
    kept_prose = [
        text for text in prose if any(value in text for value in selected)
    ]
    for value in selected:
        if not any(value in text for text in kept_prose):
            row, top = by_issue[value], by_issue[issue_id]
            kept_prose.append(
                f"{value} ({issues[value]['topic']}) was not selected: its baseline "
                f"priority score is {row['priority_score']} versus {top['priority_score']} "
                f"for {issue_id}. [code-filled]"
            )
            log.append({
                "field": "alternative_prose", "claim_id": "",
                "action": f"code wrote the reason for {value}",
            })
    repaired["alternative_issue_ids"] = selected
    repaired["alternatives"] = kept_prose
    limitations = repaired.get("limitations")
    if rule != "coherent_and_actionable" and isinstance(limitations, list) and not any(
        isinstance(value, str) and ("coheren" in value.casefold() or "actionab" in value.casefold())
        for value in limitations
    ):
        limitations.append(
            "No ranked issue in an allowed topic was judged both coherent and actionable, so the "
            f"priority was chosen under the relaxed rule '{rule}'. [code-filled]"
        )
        log.append({"field": "limitation", "claim_id": "", "action": f"code stated the {rule} priority rule"})

    model_claims: dict[tuple[str, str], dict[str, Any]] = {}
    raw_claims = repaired.get("claims")
    if isinstance(raw_claims, list):
        for claim in raw_claims:
            if (
                isinstance(claim, dict)
                and isinstance(claim.get("issue_id"), str)
                and claim.get("metric") in METRICS
            ):
                model_claims.setdefault((claim["issue_id"], claim["metric"]), claim)
    claims: list[dict[str, Any]] = []
    for selected_issue in [issue_id, *selected]:
        row = by_issue[selected_issue]
        allowed = [item["review_id"] for item in evidence.get(selected_issue, [])]
        for metric in METRICS:
            claim_id = f"C{len(claims) + 1:03d}"
            value = str(row[metric])
            original = model_claims.get((selected_issue, metric))
            sentence = original.get("sentence") if original else None
            cited = original.get("review_ids") if original else None
            if (
                original is not None
                and str(original.get("value")) == value
                and isinstance(sentence, str)
                and sentence.strip()
            ):
                review_ids = [rid for rid in (cited or []) if rid in allowed] or allowed[:3]
                claims.append({
                    "claim_id": claim_id, "issue_id": selected_issue, "metric": metric,
                    "value": value, "sentence": sentence, "review_ids": review_ids[:3],
                })
                continue
            claims.append({
                "claim_id": claim_id, "issue_id": selected_issue, "metric": metric,
                "value": value,
                "sentence": f"{selected_issue} has baseline {metric} {value}.",
                "review_ids": allowed[:3],
            })
            log.append({
                "field": "claim", "claim_id": claim_id,
                "action": (
                    "memo role omitted this metric" if original is None
                    else f"memo role value {original.get('value')!r} replaced by saved {value!r}"
                ),
            })
    repaired["claims"] = claims
    return repaired


def _validate_memo(
    payload: dict[str, Any],
    ranking: list[dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]],
    issues: dict[str, dict[str, Any]],
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(MEMO_SCHEMA["required"]):
        raise ValueError("Memo response does not match the required top-level schema")
    string_fields = (
        "priority_topic", "priority_issue_id", "priority", "rationale",
        "baseline_top_handling", "sensitivity_interpretation",
    )
    if not all(isinstance(payload[key], str) and payload[key].strip() for key in string_fields):
        raise ValueError("Memo required string fields must be nonempty")
    if payload["priority_topic"] not in ALLOWED_PRIORITY_TOPICS:
        raise ValueError("Memo must recommend access, usability, playback, billing, or support")
    priority_issue = payload["priority_issue_id"]
    if priority_issue not in issues:
        raise ValueError("Priority issue ID is unknown")
    priority_metadata = issues[priority_issue]
    rule, eligible_priority = priority_eligibility(issues, ranking)
    if priority_metadata["topic"] != payload["priority_topic"] or priority_issue not in eligible_priority:
        raise ValueError(
            "Priority issue must match priority_topic and be one of the eligible issues "
            f"({rule}): {', '.join(eligible_priority[:10])}"
        )
    priority_text = payload["priority"].casefold()
    if priority_issue.casefold() not in priority_text and payload["priority_topic"] not in priority_text:
        raise ValueError("Priority text must contain the chosen topic or issue ID")
    for key in ("alternative_issue_ids", "alternatives", "limitations", "claims"):
        if not isinstance(payload[key], list):
            raise ValueError(f"Memo {key} must be a list")
    alternatives = payload["alternative_issue_ids"]
    ranked_ids = {str(row["issue_id"]) for row in ranking}
    eligible_alternatives = sum(
        1
        for issue_id, value in issues.items()
        if issue_id != priority_issue
        and issue_id in ranked_ids
        and _rule_accepts(rule, value)
    )
    # Two alternatives are required whenever the ranking has two eligible issues besides
    # the priority; a very small input (for example the 100-review pilot) may have fewer.
    required_alternatives = min(2, eligible_alternatives)
    if len(alternatives) < required_alternatives or len(alternatives) != len(set(alternatives)):
        raise ValueError(
            f"Memo must contain at least {required_alternatives} unique alternative issue IDs"
        )
    if priority_issue in alternatives:
        raise ValueError("Priority issue cannot also be an alternative")
    for issue_id in alternatives:
        if issue_id not in issues:
            raise ValueError(f"Unknown alternative issue ID: {issue_id}")
        if not _rule_accepts(rule, issues[issue_id]):
            raise ValueError(f"Alternative issues must satisfy the priority rule ({rule})")
    if len(payload["alternatives"]) < required_alternatives or any(
        not isinstance(value, str) or not value.strip() for value in payload["alternatives"]
    ):
        raise ValueError(f"Memo must explain at least {required_alternatives} alternatives")
    for issue_id in alternatives:
        if not any(issue_id in value for value in payload["alternatives"]):
            raise ValueError(f"Alternatives prose must explain why {issue_id} was not selected")
    priority_topic = issues[priority_issue]["topic"]
    other_topic_eligible = any(
        issue_id != priority_issue
        and value["topic"] != priority_topic
        and _rule_accepts(rule, value)
        and issue_id in {str(row["issue_id"]) for row in ranking}
        for issue_id, value in issues.items()
    )
    if other_topic_eligible and all(
        issues[issue_id]["topic"] == priority_topic for issue_id in alternatives
    ):
        raise ValueError(
            "At least one alternative must come from a topic other than the priority topic"
        )
    if len(payload["limitations"]) < 4 or any(
        not isinstance(value, str) or not value.strip() for value in payload["limitations"]
    ):
        raise ValueError("Memo must include the required material limitations")
    if rule != "coherent_and_actionable" and not any(
        "coheren" in value.casefold() or "actionab" in value.casefold() for value in payload["limitations"]
    ):
        raise ValueError(
            "Limitations must state that no coherent and actionable issue was available "
            f"and the priority was chosen under {rule}"
        )

    if coverage and int(coverage.get("quarantined_rows", 0)) > 0:
        count = str(coverage["quarantined_rows"])
        if not any(count in value for value in payload["limitations"]):
            raise ValueError(
                f"Limitations must disclose the {count} quarantined (unclassified) rows"
            )
    by_issue = {str(row["issue_id"]): row for row in ranking}
    required_issues = {priority_issue, *alternatives}
    seen_claim_ids: set[str] = set()
    supplied: set[tuple[str, str]] = set()
    for claim in payload["claims"]:
        if not isinstance(claim, dict) or set(claim) != {
            "claim_id", "issue_id", "metric", "value", "sentence", "review_ids"
        }:
            raise ValueError("Invalid memo claim schema")
        if not claim["claim_id"] or claim["claim_id"] in seen_claim_ids:
            raise ValueError("Claim IDs must be unique and nonempty")
        seen_claim_ids.add(claim["claim_id"])
        target = by_issue.get(claim["issue_id"])
        if target is None or claim["issue_id"] not in required_issues:
            raise ValueError("Claim references an unsupported issue")
        if claim["metric"] not in METRICS:
            raise ValueError("Claim metric is unsupported")
        if str(target[claim["metric"]]) != claim["value"]:
            raise ValueError(f"Unsupported numerical claim {claim['claim_id']}")
        allowed_review_ids = {item["review_id"] for item in evidence[claim["issue_id"]]}
        if (
            not isinstance(claim["review_ids"], list)
            or any(review_id not in allowed_review_ids for review_id in claim["review_ids"])
        ):
            raise ValueError("Claim cites a review outside the bounded evidence pack")
        if not isinstance(claim["sentence"], str) or not claim["sentence"].strip():
            raise ValueError("Claim sentence must be nonempty")
        supplied.add((claim["issue_id"], claim["metric"]))
    required = {(issue_id, metric) for issue_id in required_issues for metric in METRICS}
    if supplied != required:
        missing = sorted(required - supplied)
        extra = sorted(supplied - required)
        raise ValueError(f"Memo claim coverage mismatch; missing={missing}, extra={extra}")
    return payload


def _render_memo(memo: dict[str, Any]) -> str:
    lines = [
        "# Spotify next-quarter product recommendation",
        "",
        f"**Priority:** {memo['priority']}",
        "",
        f"**Selected topic / issue:** `{memo['priority_topic']}` / `{memo['priority_issue_id']}`",
        "",
        memo["rationale"],
        "",
        "## Baseline and sensitivity interpretation",
        "",
        memo["baseline_top_handling"],
        "",
        memo["sensitivity_interpretation"],
        "",
        "## Traceable baseline issue claims",
        "",
    ]
    for claim in memo["claims"]:
        reviews = ", ".join(claim["review_ids"]) or "no representative review ID cited"
        lines.append(
            f"- {claim['sentence']} [{claim['claim_id']}; {claim['issue_id']}; "
            f"{claim['metric']}={claim['value']}; reviews: {reviews}]"
        )
    lines.extend([
        "",
        "Representative review IDs are typical members of each issue (the most common "
        "severity, closest to the issue centroid), not its most severe reviews. Each "
        "number above is copied by code from the saved baseline ranking.",
    ])
    lines.extend(["", "## Alternatives not selected", ""])
    lines.extend(f"- {item}" for item in memo["alternatives"])
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in memo["limitations"])
    return "\n".join(lines) + "\n"


def _read_required_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required memo evidence is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _coverage(store: Store) -> dict[str, Any]:
    """Denominators and label sources the memo must state (code-computed)."""
    source = store.source_count()
    statuses = {
        str(row[0]): int(row[1])
        for row in store.connection.execute("SELECT status,COUNT(*) FROM records GROUP BY status")
    }
    reasons = {
        str(row[0]): int(row[1])
        for row in store.connection.execute(
            "SELECT reason,COUNT(*) FROM records WHERE status='quarantined' GROUP BY reason"
        )
    }
    sources: dict[str, int] = {}
    for label_config, count in store.connection.execute(
        "SELECT label_config,COUNT(*) FROM records WHERE status='completed' GROUP BY label_config"
    ):
        sources[_label_source(str(label_config))] = (
            sources.get(_label_source(str(label_config)), 0) + int(count)
        )
    complaint = int(store.connection.execute(
        "SELECT COUNT(*) FROM records WHERE status='completed' "
        "AND intent IN ('complaint','cancellation')"
    ).fetchone()[0])
    return {
        "source_rows": source,
        "classified_rows": statuses.get("completed", 0),
        "quarantined_rows": statuses.get("quarantined", 0),
        "quarantine_reasons": reasons,
        "unaccounted_rows": source - sum(statuses.values()),
        "complaint_or_cancellation_rows": complaint,
        "label_sources_by_row": dict(sorted(sources.items())),
    }


def _label_source(label_config: str) -> str:
    if label_config.startswith("distilled-tfidf-linear"):
        return "local_classifier"
    if label_config.startswith("gpt-6-luna"):
        return "gpt-6-luna"
    return label_config.split(":", 1)[0] or "unknown"


def _monthly_trend(store: Store) -> dict[str, Any]:
    rows = [
        {
            "month": str(row[0]),
            "classified_rows": int(row[1]),
            "complaint_or_cancellation_rows": int(row[2]),
        }
        for row in store.connection.execute(
            """
            SELECT substr(s.review_timestamp,1,7) AS month,
                   COUNT(*) AS classified,
                   SUM(r.intent IN ('complaint','cancellation')) AS complaints
            FROM records r JOIN source_rows s USING(review_id)
            WHERE r.status='completed'
            GROUP BY month ORDER BY month
            """
        )
    ]
    for index, row in enumerate(rows):
        row["share"] = (
            f"{row['complaint_or_cancellation_rows'] / row['classified_rows']:.6f}"
            if row["classified_rows"] else None
        )
        row["partial_month"] = index in (0, len(rows) - 1)
    return {
        "rule": (
            "share = complaint_or_cancellation_rows / classified_rows per calendar month; "
            "the first and last months of the input are treated as partial"
        ),
        "months": rows,
        "context_only": "Trend figures may qualify the recommendation but are never claims.",
    }


def _typical_evidence(
    store: Store, artifacts: Path, issue_ids: list[str], *, per_issue: int
) -> dict[str, list[dict[str, Any]]]:
    """Select typical (not most severe) member reviews for every issue."""
    embeddings = _embedding_lookup(artifacts)
    evidence: dict[str, list[dict[str, Any]]] = {}
    for issue_id in issue_ids:
        severity_counts = store.connection.execute(
            """
            SELECT r.severity,COUNT(*) AS n
            FROM membership m JOIN records r USING(review_id)
            WHERE m.issue_id=? GROUP BY r.severity
            ORDER BY n DESC, r.severity DESC LIMIT 1
            """,
            (issue_id,),
        ).fetchone()
        if severity_counts is None:
            evidence[issue_id] = []
            continue
        modal = int(severity_counts["severity"])
        candidates = [
            dict(row)
            for row in store.connection.execute(
                """
                SELECT MIN(r.review_id) AS review_id, r.severity, s.text_sha256,
                       MIN(r.evidence_quote) AS evidence_quote
                FROM membership m JOIN records r USING(review_id)
                JOIN source_rows s USING(review_id)
                WHERE m.issue_id=? AND r.severity=?
                GROUP BY s.text_sha256
                """,
                (issue_id, modal),
            )
        ]
        scored = None
        if embeddings is not None:
            vectors, index_by_hash, centroids = embeddings
            centroid = centroids.get(issue_id)
            if centroid is not None and all(
                row["text_sha256"] in index_by_hash for row in candidates
            ):
                import numpy as np

                positions = [index_by_hash[row["text_sha256"]] for row in candidates]
                similarity = np.asarray(vectors[positions], dtype=np.float32) @ np.asarray(
                    centroid, dtype=np.float32
                )
                scored = [
                    (float(value), row) for value, row in zip(similarity.tolist(), candidates)
                ]
                scored.sort(key=lambda item: (-item[0], item[1]["review_id"]))
        if scored is None:
            ordered = sorted(
                candidates,
                key=lambda row: hashlib.sha256(row["review_id"].encode()).hexdigest(),
            )
            evidence[issue_id] = [
                {
                    "review_id": row["review_id"], "severity": row["severity"],
                    "evidence_quote": row["evidence_quote"],
                    "selection": "modal_severity_hash_order_no_embeddings",
                }
                for row in ordered[:per_issue]
            ]
        else:
            evidence[issue_id] = [
                {
                    "review_id": row["review_id"], "severity": row["severity"],
                    "evidence_quote": row["evidence_quote"],
                    "selection": "modal_severity_closest_to_centroid",
                    "centroid_cosine": round(value, 6),
                }
                for value, row in scored[:per_issue]
            ]
    return evidence


def _embedding_lookup(artifacts: Path) -> tuple[Any, dict[str, int], dict[str, Any]] | None:
    array = artifacts / "group_embeddings.npy"
    mapping = artifacts / "group_embedding_map.csv"
    model = artifacts / "grouping_model.joblib"
    if not (array.is_file() and mapping.is_file() and model.is_file()):
        return None
    import csv

    import joblib
    import numpy as np

    with mapping.open(encoding="utf-8", newline="") as stream:
        index_by_hash = {
            row["text_sha256"]: int(row["embedding_index"]) for row in csv.DictReader(stream)
        }
    bundle = joblib.load(model)
    centroids = bundle.get("final_centroids", {}) if isinstance(bundle, dict) else {}
    return np.load(array, mmap_mode="r"), index_by_hash, centroids
