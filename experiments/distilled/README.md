# Distilled text classifier + bounded Codex Luna experiment

> **Status (final):** this folder holds the development history of the local classifier. The model used for the final full-corpus run (`pipeline-v4`, which labels all texts except the risk-routed ones) is **`al2-final-v2/`** (`distilled-tfidf-linear-v2`, artifact SHA-256 `a5279d07…`): the same TF-IDF/linear design retrained on 12,008 saved GPT-6 Luna teacher labels (the legacy 10,000-review run plus the H, R, AL1 and AL2 buckets of the full run; see `al2-final-v2/model_manifest.json` and `archive/v2_full_run_evidence/cost/full_run_actuals.md`). Golden 50, checkpoint_500 and their exact/normalized texts were excluded from training. The `pilot-v1` model and the "distilled + 20% Luna" configuration described below were **not** promoted: that configuration failed its predeclared Golden-50 promotion gate, which is why the final design adds active-learning teacher labels and risk routing instead.

This experiment tested a faster alternative to per-review local generation.

## Leakage-safe training

- Teacher: saved 10,000-review Codex Luna classifications; these are pseudo-labels, not human truth.
- Holdout: the first 500 reviews are excluded from training and threshold selection.
- Golden isolation: the 50 label-free Golden source rows are excluded before fitting.
- Exclusion keys: review ID, exact UTF-8 text SHA-256, and NFKC/casefold/whitespace-normalized text SHA-256.
- Normalized duplicates are reduced to one representative; 13 conflicting normalized-label groups are removed.
- Final training rows: 7,853.
- Golden labels are opened only after candidate predictions are saved.

The sealed model, hashes, dependency versions, validation split results, and exclusion counts are in `pilot-v1/model_manifest.json`. `pilot-v1/leakage_audit.json` reports zero train overlap with the held-out 500 or Golden 50 on IDs, exact text, and normalized text.

## Measured results

| Check | Result |
|---|---:|
| Model training | 7.95 s in the first measured run |
| Holdout 500 inference | 0.032 s |
| Holdout 500 teacher fidelity, topic / intent / severity | 82.0% / 86.2% / 87.2% |
| Local-only Golden topic / intent / severity | 80% / 76% / 64% |
| Local-only Golden topic / intent / severity macro-F1 | 0.735 / 0.586 / 0.486 |
| 100-review bounded hybrid | 100/100, zero failures, exactly 20 Luna-routed rows |
| 100-review hybrid cold / warm | 23.343 s / 0.017 s |
| 100-review local → hybrid teacher fidelity | topic 83→87%; intent 81→85%; severity 86→90% |
| Downstream cold | verify 10, group 46 complaints, rank, memo; 36.222 s |
| Downstream warm | 0.006 s; zero verify/group/memo calls |
| Golden bounded hybrid topic / intent / severity | 80% / 80% / 64% |
| Golden bounded hybrid topic / intent / severity macro-F1 | 0.745 / 0.615 / 0.480 |
| 500-policy calibration/audit split | 250 / 250; Golden labels not loaded |
| Legacy risk router, audit mean accuracy / macro-F1 | 89.733% / 0.831464 |
| Selected mean-margin router, audit mean accuracy / macro-F1 | 91.733% / 0.884033 |
| Uncertainty-routed 100 cold-or-resume / warm | 23.324 s / 0.017 s |
| Uncertainty-routed 100 cap / completion | 20/100 Luna; 100/100 complete; zero failures |
| Legacy vs uncertainty 100 mean saved-teacher accuracy | 87.333% → 88.000% |
| Legacy vs uncertainty 100 mean saved-teacher macro-F1 | 0.795208 → 0.778842 |

The original hard routing cap admitted 20 of 61 risk candidates on the 100-review pilot and 10 of 25 candidates on Golden 50. The revised policy ranks all locally classified reviews by the mean probability margin across topic, intent, and severity, then admits at most 20% before any Luna call. Codex ran through ChatGPT subscription authentication with no API key; incremental monetary cost is unknown and is not reported as zero.

The fresh 100-review comparison is mixed: mean agreement with the earlier saved Luna run increased slightly, while mean macro-F1 decreased because severity agreement fell. Luna output is not deterministic across runs, so this comparison does not override the fixed 500-review routing study and does not establish human-label accuracy.

## Decision

The original experiment passed the speed and bounded-Usage objectives but failed its human-label quality promotion gate. The revised routing rule was selected without Golden labels, using a deterministic calibration half of `checkpoint_500.csv` and saved Luna pseudo-labels. On the fixed audit half it materially outperformed the legacy router, but this establishes teacher fidelity rather than human accuracy. Promotion still requires an independent human evaluation set or the instructor's private sample.

## Reproduction

```sh
python scripts/train_distilled_classifier.py \
  --output experiments/distilled/pilot-v1

python scripts/evaluate_uncertainty_routing.py \
  --output experiments/distilled/routing-v2

python scripts/run_distilled_hybrid_checkpoint.py \
  --output experiments/distilled/hybrid-100-v1 \
  --sample-size 100 \
  --max-escalation-fraction 0.20 \
  --routing-mode uncertainty \
  --uncertainty-metric mean_margin \
  --acknowledge RUN-DISTILLED-LUNA-CHECKPOINT

python scripts/run_distilled_downstream.py \
  --output experiments/distilled/hybrid-100-v1 \
  --acknowledge RUN-DISTILLED-LUNA-DOWNSTREAM
```

Rerunning the last two commands against unchanged output directories performs warm replay. The saved reports must show zero new work.
