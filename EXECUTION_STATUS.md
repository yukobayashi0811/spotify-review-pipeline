# Execution status

Updated: 2026-10-07 (America/Los_Angeles)

## Complete

- Authoritative assignment contracts and dataset are present.
- Full 660,622-row ingestion is complete: 660,609 nonempty completed and 13 empty-text quarantined.
- Final classifications are complete without changing source IDs/values or the sealed local classifier.
- A real interruption/resume completed the remaining 249,035 rows and resent zero pre-interruption IDs.
- The adopted embedding grouping is complete: 41 issues and 288,524 one-to-one complaint/cancellation memberships.
- Deterministic ranking, actionable ranking, topic rollup, 300-local-verifier sensitivity analysis with 1,000 bootstraps, and a concrete usability recommendation are complete.
- Final memo has one priority, two alternatives, 12 code-validated baseline claims, evidence provenance, and explicit limitations.
- Final grading export passes the assignment self-check.
- A fresh 100-review frozen cold run and identical warm replay are complete: 176.246427s cold, 0.011639s warm, zero warm calls.
- Offline cost replay and doubled-rate check pass. Standard Luna API-rate equivalent is $0.46379610; conservative 1.25 estimate is $0.579745. These are estimates for subscription-authenticated calls, not invoices.
- Original, Asciinema, and timestamped-text interruption recordings are present.
- Unit tests pass 40/40; the old pytest failure cache was removed.
- Codex account snapshot after all requested Luna work: 90% used, 10% remaining, reset 2026-10-09T21:17:08Z. This is shared account usage, not project-only usage.
- The supplemental held-out 60 exercise is out of scope. It was added in a later review plan, but it is not required by the authoritative assignment contract. The required Golden 50 workflow is complete.
- Golden 50 human labels are in `evals/golden_50.csv`; every saved Golden prediction set and the final labels are scored against them with zero model calls (`scripts/rescore_golden.py`, `evals/golden_final_evaluation.json`).
- Dashboard, backend and database code is complete (`dashboard/`). A local PostgreSQL load passed every loader check, and the API and dashboard were checked against the grading export locally.

## Remaining steps

- Deploy the database (Supabase) and the backend/dashboard (Vercel), run `dashboard/scripts/check-live.mjs` against the live URL, and add the URL to the README.
- Publishing the repository and checking public links signed out remain user-controlled submission steps.

## Key receipts

- Overall: `artifacts/full_run_v2/run_report.json`
- Grading: `grading/` and `artifacts/self-check-stage3.json`
- Recommendation: `memo.md`, `artifacts/full_run_v2/memo_payload.json`
- Group comparison: `artifacts/full_run_v2/grouping_comparison.json`
- Sensitivity: `artifacts/full_run_v2/sensitivity_analysis.json`
- Cost: `cost/report.md`, `cost/frozen_100/`, `cost/double_rate_check/verification.json`
- Resume: `evals/recovery/interrupt_resume_check.json`, `.cast`, and transcript
- Tests: `artifacts/test_results.json`
- Fresh handoff validation: `artifacts/test_results_handoff.json`, `artifacts/self-check-handoff.json`
- Usage: `artifacts/full_run_v2/usage_after_all.json`

No execution, cost, usage, held-out result, or business outcome has been fabricated.
