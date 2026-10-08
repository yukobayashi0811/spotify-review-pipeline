# Sensitivity analysis

This analysis uses only the 300 `final_source=local` verifier records. It applies word-bucket-specific, sample-weighted confusion matrices with Laplace smoothing and 1,000 stratified bootstrap repetitions.

## Rank stability

- Baseline top issue: `I_USABILITY_04`; adjusted top issue: `I_OTHER_01`; unchanged: `false`.
- Baseline top topic: `usability`; adjusted top topic: `usability`; unchanged: `true`.
- Baseline top issue was first in 46.6% of bootstrap runs.
- Baseline top topic was first in 100.0% of bootstrap runs.

## Top adjusted issues

| Rank | Issue | Baseline score | Adjusted score (95% interval) |
|---:|---|---:|---:|
| 1 | `I_OTHER_01` | 79,374 | 54,712.8 (45,167.6–62,759.7) |
| 2 | `I_USABILITY_04` | 81,312 | 53,879.4 (42,886.8–64,692.8) |
| 3 | `I_USABILITY_03` | 62,311 | 43,250.2 (35,250.1–49,371.0) |
| 4 | `I_BILLING_01` | 61,783 | 30,520.8 (21,670.4–38,933.0) |
| 5 | `I_PLAYBACK_08` | 52,762 | 25,970.2 (17,699.2–33,802.4) |
| 6 | `I_BILLING_03` | 46,161 | 25,021.6 (18,948.5–30,541.9) |
| 7 | `I_ACCESS_03` | 30,777 | 19,936.1 (8,115.1–20,877.7) |
| 8 | `I_OTHER_03` | 28,576 | 19,811.6 (15,526.6–23,272.6) |
| 9 | `I_OTHER_04` | 28,466 | 19,619.7 (16,374.2–22,461.3) |
| 10 | `I_USABILITY_01` | 23,542 | 16,369.8 (12,984.6–18,734.8) |

## Topic sensitivity

| Rank | Topic | Baseline score | Adjusted score (95% interval) |
|---:|---|---:|---:|
| 1 | usability | 187,397 | 238,095.4 (202,189.4–279,498.3) |
| 2 | other | 172,230 | 161,560.3 (133,273.9–183,947.0) |
| 3 | playback | 142,494 | 99,830.5 (72,203.6–128,866.7) |
| 4 | billing | 135,844 | 79,425.3 (59,792.7–100,746.8) |
| 5 | catalog | 50,465 | 63,302.4 (43,651.3–88,120.5) |
| 6 | downloads | 20,969 | 36,506.5 (18,197.7–55,910.8) |
| 7 | access | 38,533 | 35,377.8 (19,314.4–45,694.1) |
| 8 | support | 1,591 | 554.3 (137.0–3,186.1) |

## Limitations

- The verifier is an independent model pass, not human ground truth.
- The local stratum has 300 distinct texts; sparse cells depend on smoothing.
- Bootstrap intervals represent verifier-sample uncertainty under this correction model, not total model uncertainty.
- Issue corrections cannot recover false-negative complaints into a specific issue because they were never grouped.
- These corrected estimates are sensitivity outputs and are not claims.csv evidence.
