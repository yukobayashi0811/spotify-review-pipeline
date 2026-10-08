# GPT-6 Luna 10,000-review checkpoint

**Status:** completed development checkpoint; not a full-corpus grading export.

This run used `gpt-6-luna` at low effort through ChatGPT subscription authentication, Codex CLI 0.159.3, three isolated workers, batches of 50, exact-text caching, exact-cardinality schemas, and deterministic batch-local IDs restored to the preserved source IDs after validation. No API credentials or purchased credits were used. Incremental monetary cost is unknown.

The run logs retain the pre-change logical labels `prompt 2026-09-30-v2` and `enrichment-v1`, although the saved trace schemas and actual inputs show the compact-ID/exact-cardinality execution. The source was promoted after this run to `prompt 2026-10-02-v3` and `enrichment-v2` so future cache namespaces cannot mix the formats. Historical logs were not rewritten.

## Measured result

| Measure | Result |
|---|---:|
| Input rows / completed | 10,000 / 10,000 |
| Exact-text cache hits | 1,552 |
| Classification wall time | 2,914.892 s |
| Classification calls | 191: 187 succeeded, 4 failed and recovered |
| Warm replay | 0.164 s; 0 new calls |
| Downstream wall time | 1,001.327 s |
| Independent verification | 1,000 / 1,000; 20 calls; 0 failures |
| Topic / intent agreement | 90.3% / 93.1% |
| Exact severity / MAE | 87.3% / 0.131 |
| Complaint/cancellation memberships | 4,395 |
| All-role calls / trace directories | 213 / 213 |
| Measured input / output tokens | 2,681,963 / 468,928 |
| Independent validator | 18 / 18 checks passed |

The four failed classification calls each contained a duplicated short batch ID. The strict validator rejected the whole response, retained the failed-call evidence, and recovered the affected rows through bounded `50 -> 10 -> 1` splitting. No record was quarantined.

## Deterministic ranking

The required baseline ranks complaint and cancellation records by `severity_sum`, with `issue_id` as the stable tie-break. `I_USABILITY` ranks first with 1,181 records, severity sum and priority score 3,003, and mean severity 2.542760. This is a 10,000-review development result, not a full-corpus conclusion.

The traceable checkpoint memo is in `downstream/artifacts/memo.md`. Its four numerical claims were checked against the saved ranking and bounded evidence pack.

## Projection and remaining gates

The measured path projects 46.407 hours for 484,189 distinct full-corpus texts, 17.889 hours for 66,061 verification reviews, and 64.303 hours total. The required 1.25 conservative multiplier gives 80.379 hours. The calculation is saved in `full-corpus-projection.json`.

The projection is not an execution. The full CSV is absent, and this development harness deliberately writes no final grading export. Independent human-Golden evaluation is complete.
