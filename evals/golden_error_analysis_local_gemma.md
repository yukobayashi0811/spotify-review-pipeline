# Local Gemma evaluation against Golden 50

This is a development benchmark: the saved predictions of the pinned local Gemma model (`golden_predictions_local_gemma.csv`, written before any Golden label was opened) scored against the human Golden 50 labels in `golden_50.csv`.

## Result

| Metric | Result |
|---|---:|
| Reviews | 50 |
| Missing predictions | 0 |
| Topic agreement | 86% |
| Intent agreement | 90% |
| Exact severity agreement | 62% |
| Severity MAE | 0.40 |
| Sentiment MAE | 0.19 |
| Evidence quote is an exact source substring | 100% |
| Rows with any topic, intent, or severity mismatch | 25 |

18 of the 19 severity mismatches differ by one point. The two dominant patterns are severity 3 instead of 4 when playback or song selection is blocked (6 rows) and severity 2 instead of 1 for generic criticism with no reported functional problem (6 rows). One row differs by two points (4 → 2, free tier described as unusable because of ads).

## Main disagreement patterns

- Specific positive features were sometimes collapsed to `other`, including catalog breadth and reduced advertising.
- `lyrics` and `offline mode` were assigned to `playback` instead of `catalog` and `downloads`.
- Vague update complaints moved between `other` and `usability`.
- Multilingual, boycott, and very short reviews caused most intent disagreement.
- A request expressed with negative wording was classified as a complaint.

## Scale decision

The assignment sets no automatic accuracy threshold. These results supported using the pinned local model for the 500-review engineering checkpoint, with the following limits:

- do not tune prompts on these 50 examples or copy their text into prompts;
- keep blind verification and saved disagreement evidence enabled;
- inspect the 500-review severity distribution and verification disagreements before the 10,000-review stage;
- report limited rare-class support with the metrics.

The machine-readable results are `golden_evaluation_local_gemma.json` and `golden_evaluation_cases_local_gemma.csv`.
