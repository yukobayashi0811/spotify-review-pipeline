# Sensitivity analysis

This analysis uses only the 290 `final_source=local` verifier records. It applies word-bucket-specific, sample-weighted confusion matrices with Laplace smoothing and 1,000 stratified bootstrap repetitions.

## Rank stability

- Baseline top issue: `I_OTHER_02`; adjusted top issue: `I_OTHER_02`; unchanged: `true`.
- Baseline top topic: `usability`; adjusted top topic: `other`; unchanged: `false`.
- Baseline top issue was first in 100.0% of bootstrap runs.
- Baseline top topic was first in 0.0% of bootstrap runs.

## Top adjusted issues

| Rank | Issue | Baseline score | Adjusted score (95% interval) |
|---:|---|---:|---:|
| 1 | `I_OTHER_02` | 117,639 | 106,646.1 (97,793.9–112,240.7) |
| 2 | `I_BILLING_04` | 64,148 | 43,362.9 (31,764.6–53,532.5) |
| 3 | `I_USABILITY_01` | 60,211 | 39,460.6 (33,134.9–45,473.8) |
| 4 | `I_PLAYBACK_02` | 43,614 | 32,918.8 (24,932.2–39,012.5) |
| 5 | `I_PLAYBACK_03` | 47,772 | 32,698.7 (22,900.4–41,884.2) |
| 6 | `I_PLAYBACK_01` | 44,827 | 31,539.7 (22,956.9–39,362.5) |
| 7 | `I_OTHER_03` | 32,680 | 29,061.0 (26,916.3–30,808.1) |
| 8 | `I_OTHER_01` | 22,876 | 21,557.3 (19,329.4–22,682.9) |
| 9 | `I_USABILITY_05` | 37,158 | 20,837.7 (15,245.4–25,724.5) |
| 10 | `I_USABILITY_02` | 39,034 | 20,791.1 (14,173.6–26,762.7) |

## Topic sensitivity

| Rank | Topic | Baseline score | Adjusted score (95% interval) |
|---:|---|---:|---:|
| 1 | other | 173,195 | 217,324.0 (193,769.4–243,629.6) |
| 2 | billing | 134,753 | 159,338.4 (118,381.1–197,912.8) |
| 3 | usability | 187,967 | 142,767.6 (113,604.7–175,246.9) |
| 4 | playback | 136,213 | 115,838.7 (87,546.3–145,439.0) |
| 5 | catalog | 50,758 | 37,480.9 (28,182.7–49,123.1) |
| 6 | downloads | 20,559 | 16,875.6 (8,239.1–22,538.4) |
| 7 | access | 34,887 | 5,465.9 (4,405.7–9,192.6) |
| 8 | support | 809 | 4,462.0 (4,320.7–6,956.5) |

## Limitations

- The verifier is an independent model pass, not human ground truth.
- The local stratum has 290 verified texts; sparse cells depend on smoothing.
- Bootstrap intervals represent verifier-sample uncertainty under this correction model, not total model uncertainty.
- Issue corrections cannot recover false-negative complaints into a specific issue because they were never grouped.
- These corrected estimates are sensitivity outputs and are not claims.csv evidence.
