# Local MLX + Codex Luna hybrid checkpoints

This directory contains the measured local-first classification experiment requested after the Luna-only 10,000 checkpoint. It is development evidence and is not the final grading export.

## Adopted flow

1. Pinned local Gemma classifies every distinct exact review text.
2. Code applies a narrow internal-consistency repair and a deterministic risk gate.
3. Only `needs_review`, high-severity, cancellation, unclear, negative-`other`, high-risk-topic, or rating-contradiction cases go to subscription-authenticated `gpt-6-luna`.
4. Code selects the final result, records every route reason and both label sources, and writes one hybrid cache namespace.
5. Exact duplicate texts reuse the same final classification.

The model never sees golden labels. Codex runs in an empty read-only workspace with tools disabled and API-key variables removed. A quote that belongs to another review is treated as ID misalignment and causes bounded retry/splitting; it is no longer hidden by quote repair.

## Measured gates

| Checkpoint | Result |
|---|---:|
| 20-review preflight v2 | 20/20; seven Luna-routed texts (35%); 53.830 s |
| Clean 100-review cold | 100/100; 39 Luna-routed texts (39%); 273.257 s |
| 100-review warm replay | zero local/Luna records processed; 0.772 s |
| 100-review validation | 14/14 checks pass |
| 100-review agreement with saved Luna-only baseline | topic 87%; intent 92%; severity exact 91% |
| Label-isolated Golden 50 | 50/50; 18/49 distinct texts routed (36.7%); 125.730 s |
| Hybrid Golden 50 quality | topic 84%; intent 92%; severity exact 66%; severity MAE 0.34 |
| Local-only result from the same run | topic 80%; intent 92%; severity exact 66%; severity MAE 0.36 |
| Saved Codex three-worker benchmark (gpt-5.6-sol) | topic 92%; intent 88%; severity exact 74%; severity MAE 0.26 |

The Golden 50 labels were read only after predictions were saved. They were independently labeled by hand.

## Decision

The hybrid reduces Luna-classified reviews by about 61% on the 100-review sample and modestly improves the same-run local baseline. It is not a wall-time optimization: local Gemma dominates runtime, making the clean hybrid run 4.18x slower than the saved 65.314-second three-worker Luna-only 100-review checkpoint. The linear full-corpus classification projection is 367.444 hours before required verification. Therefore this route is implemented and validated for Usage conservation, but it does not pass the speed gate for a 500-review scale-up.

Primary evidence:

- `checkpoint-100-local-luna-v3/hybrid_cold_report.json`
- `checkpoint-100-local-luna-v3/hybrid_warm_report.json`
- `checkpoint-100-local-luna-v3/routing_decisions.csv`
- `checkpoint-100-local-luna-v3/validation_report.json`
- `checkpoint-100-local-luna-v3/luna_baseline_comparison.json`
- `quality-50-local-luna-v3/golden_evaluation.json`
- `quality-50-local-luna-v3/local_golden_evaluation.json`
- `quality-50-local-luna-v3/quality_comparison.json`
