# Golden-set workflow

## Human Golden 50

`golden_50.csv` is the 50-review human gold set. Every label (topic, intent, sentiment, severity, entities, evidence_quote) was assigned by hand from the review text, following `config/labels.md`, in the labeling workbook `golden_50_labeling_completed.xlsx`. No model prediction or AI-generated label was viewed or used. Provenance and file hashes are in `golden_50.provenance.json`.

`golden_50_to_label.csv` is the selection file and `golden_50_labeling.xlsx` the blank labeling template.

## Leakage-safe scoring

The model receives only `artifacts/golden_prediction_input.csv`. Its manifest proves that no label fields were copied. Training code excludes all Golden review IDs, exact-text SHA-256 values, and NFKC/casefold/whitespace-normalized text SHA-256 values before fitting or threshold selection.

Predictions must be saved before `golden_50.csv` is opened for scoring:

```sh
spotify-pipeline golden-export-predictions \
  --state artifacts/runtime/golden.db \
  --output evals/golden_predictions.csv

spotify-pipeline golden-score \
  --golden evals/golden_50.csv \
  --predictions evals/golden_predictions.csv \
  --output evals/evaluation.json \
  --cases evals/evaluation_cases.csv
```

Every saved Golden prediction file in the repository, and the final full-corpus labels of the 50 Golden reviews, can be scored again with zero model calls:

```sh
PYTHONPATH=src python3 scripts/rescore_golden.py
```

## Final classifications

`golden_final_evaluation.json` scores the final labels of the 50 Golden reviews taken from the grading export (`golden_final_predictions.csv`). The Golden texts were part of the full-corpus run; their labels were never model inputs.

| Metric | All 50 | Local classifier (44) | Luna teacher (6) |
|---|---:|---:|---:|
| Topic agreement | 84% | 81.8% | 100% |
| Intent agreement | 82% | 79.5% | 100% |
| Exact severity agreement | 66% | 61.4% | 100% |
| Severity within ±1 | 94% | 93.2% | 100% |
| Severity MAE | 0.40 | | |
| Sentiment MAE | 0.26 | | |
| Entities exact match | 46% | | |
| Evidence quote is an exact source substring | 100% | | |
| Macro-F1 topic / intent / severity | 0.790 / 0.641 / 0.538 | | |

Most severity disagreements are one point: the final labels give 3 where the human label is 4 (blocked playback or song selection) and 2 where the human label is 1 (generic criticism with no reported functional problem). The six Luna-labeled rows are too few for a separate accuracy claim.

## Development evaluations

| Saved predictions | Evaluation | Topic | Intent | Severity exact |
|---|---|---:|---:|---:|
| Local Gemma | `golden_evaluation_local_gemma.json` | 86% | 90% | 62% |
| Codex three-worker, gpt-5.6-sol | `experiments/codex_3agent/quality-50-parallel/golden_evaluation.json` | 92% | 88% | 74% |
| Codex single worker, gpt-5.6-sol | `experiments/codex_3agent/quality-50-batch50-single/golden_evaluation.json` | 94% | 92% | 76% |
| Local Gemma + Luna hybrid | `experiments/hybrid/quality-50-local-luna-v3/golden_evaluation.json` | 84% | 92% | 66% |
| Same run, local only | `experiments/hybrid/quality-50-local-luna-v3/local_golden_evaluation.json` | 80% | 92% | 66% |
| Distilled local classifier, pilot v1 | `experiments/distilled/pilot-v1/golden_50_evaluation.json` | 80% | 76% | 64% |
| Distilled + 20% Luna hybrid | `experiments/distilled/hybrid-golden-50-v1/golden_evaluation.json` | 80% | 80% | 64% |

`golden_error_analysis_local_gemma.md` describes the Gemma disagreements.
