# Mandatory 1,000-text Luna gate

> Historical snapshot written when the run paused at its mandatory gate. Approval was given afterwards (the file `APPROVED_AFTER_1000` now exists) and the run continued to 3,960 new Luna texts; final figures of that earlier run are in `archive/v2_full_run_evidence/cost/full_run_actuals.md` and `artifacts/full_run_v2/run_report.json`. The final submission run is `artifacts/full_run_v4/`.

Status: **approval required; no later Luna stage has been started**.

- Distinct new texts sent: 1,000
- Successful labels: 1,000
- Failed labels: 0
- Allocation: H 60, R 500, AL1 440
- Successful calls: 20 (50 texts each)
- Input tokens: 269,194
- Cached input tokens: 199,680
- Output tokens: 51,332
- Sum of model-call durations: 994.515 seconds
- Gate wall-clock time with three workers: 442.001 seconds
- Account Codex Usage: 61% before, 63% after (+2 percentage points)
- Purchased credit balance: 0 before and after

The full-corpus local preflight processed 660,622 source rows and 484,189
distinct non-empty texts. Its first-run wall time was 168.874 seconds and peak
memory was approximately 282 MB. The resumed preflight completed in 6.857
seconds without rescoring saved texts.

The selected Luna pool has zero review-ID, exact-text, and normalized-text
overlap with checkpoint_500 and the label-free Golden input. Golden labels were
not opened. The local v2 model was trained on 7,853 leakage-safe legacy Luna
pseudo-labels and its checkpoint_500 teacher fidelity is topic 0.820, intent
0.874, and severity exact 0.882 (mean 0.859). These are not human-accuracy
claims.

Before continuing, explicit approval is required. Approval is represented by
the file `artifacts/full_run_v2/APPROVED_AFTER_1000`; it has not been created.

