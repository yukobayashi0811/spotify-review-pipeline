#!/usr/bin/env python3
"""Give every issue-level baseline number in the memo prose a claim ID (no model calls).

The memo role's 12 claims cover the recommended issue and its two alternatives. Its
prose also states one more baseline number (the priority score of the top,
non-actionable issue) without a claim ID. The assignment asks for claim IDs on
every material issue-level number, so this script:

1. scans the prose of ``memo.md`` for an issue ID and one of that issue's baseline
   values (complaint_count, severity_sum, mean_severity, priority_score) in the
   same sentence that is not yet a claim;
2. appends a claim for it to ``claims.csv`` (run folder and ``grading/``), with the
   value copied from the saved ranking and three typical evidence review IDs from
   the memo evidence pack (``claims_code_linked.json``);
3. records the addition in ``claims_provenance.csv`` as code-linked, and writes the
   claim ID next to the number in ``memo.md``.

The memo role's own output (``memo_payload.json``) is not changed. Re-running is a
no-op once every number is linked. The checker then recomputes every claim.

Usage: python3 scripts/link_memo_claims.py [--run-dir artifacts/full_run_v4]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRICS = ("complaint_count", "severity_sum", "mean_severity", "priority_score")
KEYWORDS = {
    "priority_score": ("priority score", "score"),
    "severity_sum": ("severity sum",),
    "mean_severity": ("mean severity",),
    "complaint_count": ("complaint", "rows"),
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=ROOT / "artifacts/full_run_v4")
    parser.add_argument("--grading", type=Path, default=ROOT / "grading")
    args = parser.parse_args()
    run = args.run_dir
    memo_path = run / "memo.md"
    memo = memo_path.read_text(encoding="utf-8")
    ranking = {row["issue_id"]: row for row in read_csv(args.grading / "ranking.csv")}
    claims = read_csv(run / "claims.csv")
    claim_fields = list(claims[0])
    provenance = read_csv(run / "claims_provenance.csv")
    prov_fields = list(provenance[0])
    memo_request = provenance[0]["request_id"]
    pack = json.loads((run / "memo_evidence_pack.json").read_text(encoding="utf-8"))
    linked_path = run / "claims_code_linked.json"
    linked = json.loads(linked_path.read_text(encoding="utf-8")) if linked_path.exists() else []
    claimed = {(row["issue_id"], row["value"]) for row in claims}

    # Prose only: stop before the code-rendered claim list.
    prose_end = memo.find("## Traceable baseline issue claims")
    prose = memo[:prose_end] if prose_end >= 0 else memo
    additions = []
    for sentence in re.split(r"(?<=[.;])\s+", prose):
        for issue_id in dict.fromkeys(re.findall(r"I_[A-Z]+_\d+", sentence)):
            row = ranking.get(issue_id)
            if not row:
                continue
            for metric in METRICS:
                value = row[metric]
                pattern = re.compile(r"(?<![\d.,])" + re.escape(value) + r"(?![\d]|\.\d|,\d)")
                match = pattern.search(sentence)
                if not match or (issue_id, value) in claimed:
                    continue
                lowered = sentence.lower()
                if metric in ("severity_sum", "priority_score") and row["severity_sum"] == row["priority_score"]:
                    metric = "priority_score" if "priority score" in lowered or "score" in lowered else "severity_sum"
                claimed.add((issue_id, value))
                additions.append((issue_id, metric, value, sentence.strip().split("\n")[-1]))

    next_number = max(int(row["claim_id"][1:]) for row in claims) + 1
    for issue_id, metric, value, sentence in additions:
        claim_id = f"C{next_number:03d}"
        next_number += 1
        claims.append({"claim_id": claim_id, "issue_id": issue_id, "metric": metric, "value": value})
        provenance.append({
            "claim_id": claim_id,
            "source": "code_linked_after_run_from_saved_baseline",
            "reason": "number stated in the memo role's prose without a claim ID; code added the claim "
                      "from the saved ranking and annotated memo.md",
            "request_id": memo_request,
        })
        evidence = [item["review_id"] for item in pack["evidence"].get(issue_id, [])][:3]
        linked.append({"claim_id": claim_id, "issue_id": issue_id, "metric": metric, "value": value,
                       "sentence": sentence, "review_ids": evidence})
        annotated = re.sub(
            r"(?<![\d.,])" + re.escape(value) + r"(?![\d]|\.\d|,\d)(?! \[C\d{3}\])",
            f"{value} [{claim_id}]", sentence, count=1,
        )
        memo = memo.replace(sentence, annotated, 1)
        bullet = (f"- Baseline {metric.replace('_', ' ')} of {issue_id}, stated in the prose above; "
                  f"linked by code after the run. [{claim_id}; {issue_id}; {metric}={value}; "
                  f"reviews: {', '.join(evidence)}]")
        last_bullet = max(memo.rfind(f"[{row['claim_id']};") for row in claims[:-1])
        line_end = memo.find("\n", last_bullet)
        memo = memo[:line_end + 1] + bullet + "\n" + memo[line_end + 1:]
    if additions:
        write_csv(run / "claims.csv", claims, claim_fields)
        write_csv(args.grading / "claims.csv", claims, claim_fields)
        write_csv(run / "claims_provenance.csv", provenance, prov_fields)
        linked_path.write_text(json.dumps(linked, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        memo_path.write_text(memo, encoding="utf-8")
        (ROOT / "memo.md").write_text(memo, encoding="utf-8")
    print(json.dumps({"added": [{"claim_id": item["claim_id"], "issue_id": item["issue_id"], "metric": item["metric"],
                                 "value": item["value"]} for item in linked], "model_calls": 0}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
