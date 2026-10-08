# Codex 10,000-review development checkpoint

Status: **complete and independently validated** (`development_only=true`, `grading_eligible=false`).

## Outcome

- Classification: 10,000 / 10,000 completed, zero quarantined.
- Exact-text cache: 1,552 direct reuses from 8,448 distinct texts.
- Classification calls: 234 total; 217 succeeded and 17 failed attempts retained.
- Warm replay: 0.075573 seconds and zero new model calls.
- Verification: 1,000 / 1,000 after a resume sent only the missing 50.
- Verification agreement: topic 91.7%, intent 94.3%, severity exact 90.8%, severity MAE 0.092.
- Grouping: 4,570 complaint/cancellation records, eight stable primary-topic issues.
- Deterministic top issue: `I_USABILITY`, 1,159 records, severity sum / priority score 2,970, mean severity 2.562554.
- Memo: usability priority with four validated ranking claims.
- Independent validation: 18 / 18 checks passed; 257 calls matched 257 trace metadata files.
- Measured usage across roles: 3,072,944 input and 736,254 output tokens.
- Billing: ChatGPT subscription; incremental monetary cost is unknown.

## Real interruption and recovery evidence

The first classification invocation was safely stopped after the previous single-review fallback became too slow. A later resume stopped on the provider usage limit at 4,056 completed records. After a user-applied reset, the final resume completed all records without resending completed IDs.

The updated bounded recovery path is `50 -> 10 -> 1`. It was exercised twice by real 50-review responses containing unknown IDs. Both were recovered with five ten-review calls, avoiding 50 singleton calls.

The first downstream verifier pass saved 950 of 1,000 selected rows because one malformed 50-review response failed validation. `scripts/resume_codex_verification.py` reused the exact verifier label configuration, skipped all 950 saved IDs, and processed only the missing 50.

## Evidence files

- `benchmark_report.json`: classification totals and timing scope.
- `interruption-before-resume.json`: operator interruption snapshot.
- `usage-limit-interruption-01.json`: provider-limit interruption snapshot.
- `warm_replay.json`: zero-call exact-cache replay.
- `downstream/end_to_end_report.json`: first full downstream invocation.
- `downstream/verification-resume-report.json`: missing-only verifier resume.
- `downstream/artifacts/verification_report.json`: final 1,000-review report.
- `downstream/artifacts/ranking.csv`: deterministic ranking.
- `downstream/artifacts/memo.md`: traceable recommendation memo.
- `validation.json`: independent 18-check receipt.
- `full-corpus-projection.json`: explicit projection and blockers; not an execution result.

## Full-corpus boundary

The full 660,622-row source CSV is not present in the current project or course workspace. From the stable measured segment, the full path projects to 95.893 hours, or 119.866 hours with the 1.25 conservative multiplier. No full-corpus execution, ranking, cost, or recommendation is claimed.
