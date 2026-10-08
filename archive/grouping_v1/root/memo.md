# Spotify next-quarter product recommendation

**Status: development-only recommendation from the 10,000-review checkpoint; not the final full-corpus memo.**

## Priority

Focus next-quarter effort on `I_USABILITY` — controls, navigation, and interruptions.

Under the required `complaint_count × mean_severity = severity_sum` baseline, `I_USABILITY` ranks first in the saved 10,000-review checkpoint with 1,181 complaint/cancellation records, a severity sum and priority score of 3,003, and mean severity 2.542760. Representative saved reviews report unwanted automatic playback and missing playlists. `I_PLAYBACK` is a narrower alternative focused on songs failing to play, but ranks third with a score of 2,167.

## Traceable claims

- `C001`: `I_USABILITY` complaint count = 1,181. Evidence review IDs: `91132204-877d-4ac1-9d08-20d03f1414f6`, `d87536c5-6fe0-4b38-b0b1-c47936e4a0d8`, `ee11e1df-cb73-4c3a-b5e8-7268c7872b01`.
- `C002`: `I_USABILITY` severity sum = 3,003. Same bounded evidence IDs.
- `C003`: `I_USABILITY` mean severity = 2.542760. Same bounded evidence IDs.
- `C004`: `I_USABILITY` priority score = 3,003. Same bounded evidence IDs.

The authoritative checkpoint artifacts are `experiments/codex_luna/checkpoint-10000-normal-usage-v2/downstream/artifacts/ranking.json`, `memo_evidence_pack.json`, `memo_payload.json`, and `memo.md`. The independent validator recomputed the ranking and confirmed all four claims.

## Limitations

- This is a deterministic 10,000-review development sample, not the required 660,622-row full-corpus result. It must not be copied into `grading/` as a final export.
- Reviews are historical and self-selected, not the complete customer population.
- The data has no complete revenue, plan-tier, confirmed-churn, or causal retention measure.
- Cancellation language is expressed intent, not observed churn.
- The canonical independent human Golden 50 is complete; rare-class support is limited.
