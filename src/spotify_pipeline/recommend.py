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


MEMO_SCHEMA_VERSION = "memo-v2"
MEMO_PROMPT_VERSION = "2026-10-03-v2"
MEMO_BUCKET = "M_EMBED_V3"
ALLOWED_PRIORITY_TOPICS = {"access", "usability", "playback", "billing", "support"}
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
        raise ValueError("Memo v2 requires top_n=10 and at most three representatives per issue")
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
        evidence: dict[str, list[dict[str, Any]]] = {}
        for issue_id in sorted(derived["issues"]):
            evidence[issue_id] = [
                dict(row)
                for row in store.connection.execute(
                    """
                    SELECT r.review_id,r.severity,r.evidence_quote
                    FROM membership m JOIN records r USING(review_id)
                    WHERE m.issue_id=?
                    ORDER BY r.severity DESC,r.review_id ASC LIMIT ?
                    """,
                    (issue_id, evidence_per_issue),
                )
            ]

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
                "Only baseline issue metrics may appear in claims. Topic rollups and corrected "
                "sensitivity values must be described separately and never exported as claims."
            ),
        }
        input_hash = hashlib.sha256(canonical(pack).encode("utf-8")).hexdigest()
        cached_hash = store.get_metadata("memo_v2_input_hash")
        cached_memo = store.get_metadata("memo_v2_payload")
        if cached_hash == input_hash and cached_memo:
            memo = _validate_memo(cached_memo, ranking_all, evidence, derived["issues"])
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
    memo_config: str,
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
        raise RuntimeError("Memo v2 requires two distinct ledger sentinels for its two-call cap")
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
            candidate = _repair_code_owned_memo_fields(
                result.payload, ranking=ranking, evidence=evidence, issues=issues
            )
            memo = _validate_memo(candidate, ranking, evidence, issues)
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
            provenance = [
                {
                    "claim_id": claim["claim_id"],
                    "source": "memo_role_validated_against_saved_baseline",
                    "reason": "model prose; code-validated issue ID, metric, value, and evidence IDs",
                    "request_id": result.request_id,
                }
                for claim in memo["claims"]
            ]
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
                    request_id=result.request_id if result else f"memo-v2-failed-{attempt + 1}",
                    succeeded=False,
                    input_tokens=result.input_tokens if result else None,
                    cached_input_tokens=result.cached_input_tokens if result else None,
                    output_tokens=result.output_tokens if result else None,
                    duration_seconds=result.duration_seconds if result else 0,
                    error=last_error,
                )
            if attempt == 1:
                raise RuntimeError(
                    "Memo v2 failed strict validation after the single allowed repair: "
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
        raise RuntimeError("Memo v2 Luna ledger call cap is exhausted")
    return admitted


def _repair_code_owned_memo_fields(
    payload: dict[str, Any], *, ranking: list[dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]], issues: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Normalize code-verifiable IDs and metrics without changing model rationale.

    The model already selects ``priority_topic`` and ``priority_issue_id``. If its
    human-readable priority sentence omits both literal identifiers, prefix those
    saved choices. Alternatives are restricted to the highest-ranked eligible
    issues, and numerical claims are rebuilt verbatim from the saved baseline.
    This prevents an otherwise useful local memo from inventing IDs or numbers.
    """

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
    if not isinstance(issue_id, str) or issue_id not in issues:
        return repaired
    by_issue = {str(row["issue_id"]): row for row in ranking}
    eligible = [
        str(row["issue_id"])
        for row in ranking
        if str(row["issue_id"]) != issue_id
        and issues[str(row["issue_id"])]["coherent"] == "yes"
        and issues[str(row["issue_id"])]["actionable"] == "yes"
    ]
    supplied = repaired.get("alternative_issue_ids")
    selected: list[str] = []
    if isinstance(supplied, list):
        for value in supplied:
            if value in eligible and value not in selected:
                selected.append(value)
    for value in eligible:
        if len(selected) >= 2:
            break
        if value not in selected:
            selected.append(value)
    repaired["alternative_issue_ids"] = selected[:2]
    repaired["alternatives"] = [
        (
            f"{value} was not selected because the deterministic pilot baseline ranks "
            f"{value} below {issue_id}; it remains an eligible follow-up."
        )
        for value in selected[:2]
    ]
    claims: list[dict[str, Any]] = []
    for selected_issue in [issue_id, *selected[:2]]:
        row = by_issue[selected_issue]
        review_ids = [item["review_id"] for item in evidence[selected_issue]][:3]
        for metric in METRICS:
            claim_id = f"C{len(claims) + 1:03d}"
            value = str(row[metric])
            claims.append({
                "claim_id": claim_id,
                "issue_id": selected_issue,
                "metric": metric,
                "value": value,
                "sentence": f"{selected_issue} has baseline {metric}={value}.",
                "review_ids": review_ids,
            })
    repaired["claims"] = claims
    return repaired


def _validate_memo(
    payload: dict[str, Any],
    ranking: list[dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]],
    issues: dict[str, dict[str, Any]],
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
    if (
        priority_metadata["topic"] != payload["priority_topic"]
        or priority_metadata["coherent"] != "yes"
        or priority_metadata["actionable"] != "yes"
    ):
        raise ValueError("Priority issue must be coherent, actionable, and match priority_topic")
    priority_text = payload["priority"].casefold()
    if priority_issue.casefold() not in priority_text and payload["priority_topic"] not in priority_text:
        raise ValueError("Priority text must contain the chosen topic or issue ID")
    for key in ("alternative_issue_ids", "alternatives", "limitations", "claims"):
        if not isinstance(payload[key], list):
            raise ValueError(f"Memo {key} must be a list")
    alternatives = payload["alternative_issue_ids"]
    if len(alternatives) < 2 or len(alternatives) != len(set(alternatives)):
        raise ValueError("Memo must contain at least two unique alternative issue IDs")
    if priority_issue in alternatives:
        raise ValueError("Priority issue cannot also be an alternative")
    for issue_id in alternatives:
        if issue_id not in issues:
            raise ValueError(f"Unknown alternative issue ID: {issue_id}")
        if issues[issue_id]["coherent"] != "yes" or issues[issue_id]["actionable"] != "yes":
            raise ValueError("Alternative issues must be coherent and actionable")
    if len(payload["alternatives"]) < 2 or any(
        not isinstance(value, str) or not value.strip() for value in payload["alternatives"]
    ):
        raise ValueError("Memo must explain at least two alternatives")
    for issue_id in alternatives:
        if not any(issue_id in value for value in payload["alternatives"]):
            raise ValueError(f"Alternatives prose must explain why {issue_id} was not selected")
    if len(payload["limitations"]) < 4 or any(
        not isinstance(value, str) or not value.strip() for value in payload["limitations"]
    ):
        raise ValueError("Memo must include the required material limitations")

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
