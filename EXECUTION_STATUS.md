# Execution status

Updated: 2026-10-12 (America/Los_Angeles)

## Complete

- Final full-corpus run with the final pipeline (`spotify-pipeline run`, `pipeline-v4`) on all 660,622 rows: 660,609 completed, 13 empty texts quarantined, 0 other failures. 22,895 risk-routed distinct texts (23,200 rows) labeled by GPT-6 Luna, 637,409 rows by the sealed local classifier. Receipts: `artifacts/full_run_v4/run_summary.json`, `run_log.jsonl`, `run_manifest.json`, `CODE_PROVENANCE.md`.
- Deliberate interruption after 41 saved Luna batches and a resume with the same command; the resume re-sent 0 completed IDs (`evals/recovery_v4/`).
- Independent blind verification of 400 sampled texts (390 verified), sensitivity analysis on the 290 verified local-labeled texts with 1,000 bootstrap draws.
- Embedding grouping: 44 issues, 287,914 one-to-one complaint/cancellation memberships; deterministic ranking, actionable ranking and topic rollup.
- Memo: priority `billing` / `I_BILLING_04`, two alternatives, 12 code-validated claims, limitations including coverage and quarantined rows.
- Grading export in `grading/`; contract checker: pass, no issues (`artifacts/self_check_final.json`). Ranking regenerated from the export matches (`artifacts/rerank_from_grading.csv`).
- Golden 50 rescored against the final labels (topic 82%, intent 80%, exact severity 64%) with a categorized error analysis.
- Fresh held-out 30 labeled blind by the author and scored (topic 63%, intent 83%, exact severity 40%).
- 100-review pilot of the same command: cold 44.9 s with 5 Luna requests, warm 0.0135 s with 0 requests; offline calculator and its self-check pass.
- Measured full-run cost: 525 Luna requests, $0.929008 API-equivalent (subscription calls, not an invoice), 6,528 s wall time.
- System tests (planted errors, prompt injection): pass.
- Unit tests: 47/47 pass (`artifacts/test_results.json`).

## Remaining steps

- Reload the dashboard database from the final run (`dashboard/db/load.py`) and re-run the live check (`dashboard/scripts/check-live.mjs`).
- Commit and push; check the public links signed out.

## Key receipts

- Run: `artifacts/full_run_v4/`
- Grading: `grading/`, `artifacts/self_check_final.json`
- Recommendation: `memo.md`, `artifacts/full_run_v4/memo_payload.json`, `claims_provenance.csv`
- Sensitivity: `artifacts/full_run_v4/sensitivity_analysis.md`
- Evaluation: `evals/golden_final_evaluation.json`, `evals/golden_final_error_analysis.md`, `evals/holdout_30_evaluation.json`
- Verification: `artifacts/full_run_v4/verification/`
- System tests: `evals/system_tests/system_test_report.json`
- Resume: `evals/recovery_v4/interrupt_resume_check.json`, recording and transcript
- Cost: `cost/report.md`, `cost/full_run_v4_actuals.md`, `cost/replay_check.json`
- Tests: `artifacts/test_results.json`

The earlier full-corpus run and its evidence are kept for provenance in `artifacts/full_run_v2/` and `archive/`. No execution, cost, usage, evaluation result or business outcome has been fabricated.
