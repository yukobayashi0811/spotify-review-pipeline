# Performance investigation

All comparisons below are measured on the same Apple Silicon machine with MLX-LM 0.31.3. API charge was zero. Hardware and electricity costs remain unknown.

## Adopted changes

1. Prefill the JSON opening delimiter and disable unsupported free-form reasoning before the structured response.
2. Stream tokens and stop immediately after the first complete top-level JSON object.
3. Keep batch size at 10. Batch 20 was slower on the same reviews and changed 15% of topic/intent/severity triples.
4. Keep one worker. Two warm workers delivered only a 5.4% wall-time gain while roughly doubling resident model copies.
5. Repair only an ungrounded quote that matches no review in the batch by copying the exact source text and marking the record `needs_review=true`. Reject quotes grounded in a different review as suspected ID misalignment and use bounded retry/splitting.
6. Fill omitted top-issue memo metric claims only from the saved deterministic ranking, then run the same strict claim validator.

## End-to-end result

| Metric | Prior measured pilot | Optimized measured pilot | Change |
|---|---:|---:|---:|
| Cold wall time, 100 reviews | 1,575.783 s | 439.289 s | 3.587x faster |
| Classification wall time | 1,382.740 s | 354.724 s | 3.898x faster |
| Warm wall time | 76.075 s | 41.599 s | 1.829x faster |
| Saved call output tokens | 42,415 | 15,646 | 63.1% lower |
| Failed model attempts | 2 | 0 | eliminated in this pilot |
| Full exact-reuse projection | 83.738 days | 23.003 days | 3.640x faster |

The initial full projection includes preparation, classification of 484,189 distinct exact texts, a 10% verification sample over the completed population, grouping, ranking, and memo generation. It was refreshed after 500 reviews and must be refreshed again after 10,000.

## Batch and worker decisions

| Test | Configuration A | Configuration B | Decision |
|---|---:|---:|---|
| Same 20 reviews, warm model | batch 10: 58.383 s | batch 20: 73.297 s | batch 10 |
| Same 20 reviews, same cache condition | 1 worker: 58.383 s | 2 workers: 55.403 s | 1 worker; 1.054x is below the 1.10x adoption threshold |

See `artifacts/performance_benchmark.json` and `artifacts/worker_benchmark.json` for the call-level measurements and limitations.

## Smaller-model screening

| Candidate | Measured result | Decision |
|---|---|---|
| `mlx-community/Qwen3-0.6B-4bit` | 1.316 s for one review, but returned severity 0, an empty quote, and an unsupported topic for a neutral review | reject |
| `mlx-community/Qwen3-1.7B-4bit` | 12.307 s for 10 reviews, but omitted `needs_review` for every record and made clear topic errors | reject |
| `mlx-community/gemma-3-1b-it-4bit` | 3.766 s for one review, but returned a schema description instead of the required record envelope | reject |

These screens establish only that the tested configurations are not drop-in replacements. They are not golden-set accuracy estimates.

## Quality checkpoint

The pinned Gemma configuration completed a label-isolated 50-review benchmark in 149.070 seconds with zero failures: topic agreement 86%, intent agreement 90%, exact severity agreement 62%, severity MAE 0.40, and exact-source quote membership 100%. The Golden 50 was labeled by hand.

The result was sufficient to run the 500-review engineering checkpoint with batch 10 and one worker. Small or absent support for rare Golden classes limits per-class conclusions.

## 500-review checkpoint

| Metric | Measured result |
|---|---:|
| Completed / quarantined | 500 / 0 |
| Distinct texts / direct cache reuses | 479 / 21 |
| Wall time | 2,458.466 s |
| Classification | 2,173.673 s |
| Blind verification | 231.879 s for 50 reviews |
| Group + rank + memo | 52.908 s |
| Successful / failed model attempts | 64 / 2 |
| Input / output tokens | 90,065 / 66,690 |
| API charge | $0.00 |
| Verification topic / intent / severity exact | 88% / 90% / 90% |
| Verification severity MAE | 0.10 |

The two failed attempts came from one invalid ten-review batch. Bounded retry retained both failures, then single-review recovery completed every record. Four invalid evidence quotes were replaced with the exact source text and flagged for review. Playback ranked first in the checkpoint with priority score 169.

`artifacts/checkpoint_500_validation.json` independently reconciles the checkpoint input, SQLite state, role-call logs, membership, ranking, claims, verification, and cost report. All 13 checks pass.

The local 500-review refresh projected 28.977 days for the full exact-text-reuse path, or 36.222 days under the 1.25 conservative multiplier. At that point it projected 11.95 hours for the local 10,000 stage, or 14.94 hours conservatively. The separate three-worker Codex path subsequently completed the 10,000 checkpoint; its measured projection supersedes these local-only values for the Codex development route.

## Codex subscription path

The initial `gpt-5.6-sol` experiments established that three isolated subscription-authenticated CLI workers could accelerate the bounded development path without API-key charges. Those retained experiments remain historical evidence under `experiments/codex_3agent/`.

After updating to Codex CLI 0.159.3, `gpt-6-luna` became available through the same ChatGPT subscription login. A one-review preflight completed in 4.185 seconds with a valid exact-source quote, 11,248 input tokens, 76 output tokens, no tool events, and no API credentials. Purchased-credit balance was zero; incremental monetary cost remains unknown because subscription Usage is not an API-key charge.

The first Luna 500 checkpoint exposed occasional UUID copying errors and missing records in 50-review structured outputs. Exact array cardinality prevented silent omission but did not prevent an ID typo. The adopted fix sends deterministic batch-local IDs (`R001`–`R050`), validates every returned key and record, then maps the result back to the authoritative source ID. Source rows, final records, ordered call logs, and evidence quotes remain unchanged and traceable.

On the same 150 reviews, the pre-fix path used eight calls and 143.113 seconds; the compact-ID path used exactly three calls and 50.587 seconds, a 2.829x wall-time speedup. On 500 reviews, classification improved from 340.905 seconds and 37 calls to 154.218 seconds and 12 calls. The optimized 500 run completed the full role sequence and passed all 18 independent checks.

## Optimized GPT-6 Luna staged checkpoints

| Stage | Classification result | Warm replay | Downstream / validation |
|---|---:|---:|---:|
| 100, batch 50, 3 workers | 100/100; 65.314 s; 3 calls | 0.076 s; 0 calls | verification 10/10; 18 checks pass |
| 500, compact IDs | 500/500; 154.218 s; 12 calls | 0.067 s; 0 calls | 76.328 s; verification 50/50; 18 checks pass |
| 10,000, compact IDs | 10,000/10,000; 2,914.892 s; 191 classification calls | 0.164 s; 0 calls | 1,001.327 s; verification 1,000/1,000; 18 checks pass |

The optimized 10,000 classification run processed 8,448 distinct texts and reused 1,552 exact-text results. Four 50-review responses duplicated one batch-local ID. Strict validation retained each failure and recovered every affected row through bounded `50 -> 10 -> 1` splitting. Final state: 10,000 completed, zero quarantined, valid merge.

Across all roles, the checkpoint contains 213 calls and 213 matching trace directories: 187 successful and four failed enrichment calls, 20 successful verification calls, one grouping call, and one memo call. Measured usage was 2,681,963 input tokens and 468,928 output tokens. Verifier agreement was 90.3% topic, 93.1% intent, 87.3% exact severity, and 0.131 severity MAE. All 1,000 selected records were verified without a failed verifier call.

Deterministic ranking placed `I_USABILITY` first with 1,181 complaint/cancellation records, severity sum and priority score 3,003, and mean severity 2.542760. The checkpoint memo contains four code-validated claims. These are development results, not full-corpus or independent-human-golden evidence.

## Full-corpus projection from the optimized checkpoint

The measured three-worker classification rate projects 46.407 hours for the known 484,189 distinct nonempty full-corpus texts. The measured verifier rate projects 17.889 hours for the required 66,061-review sample. Fixed preparation, grouping, ranking, and memo stages add 0.007 hours.

The resulting projection is 64.303 hours, or 80.379 hours with the required 1.25 multiplier. This is a projection, not an execution. The full CSV is present and its deterministic ingestion is complete. The account-wide Usage snapshot after the run was 12% used with purchased-credit balance zero and two reset credits untouched. That snapshot is not attributed solely to this experiment and is not converted into dollars or per-review Usage.

Machine-readable evidence is in `experiments/codex_luna/checkpoint-10000-normal-usage-v2/`, including `benchmark_report.json`, `warm_replay.json`, `downstream/end_to_end_report.json`, `validation_report.json`, and `full-corpus-projection.json`.

## Local-first + Luna escalation experiment

The clean hybrid 100-review checkpoint used batch 50 for both providers. Local Gemma processed all 100 distinct texts in 236.064 seconds. The deterministic risk gate routed 39 texts to Luna, which processed them in 37.135 seconds. End-to-end classification took 273.257 seconds, completed 100/100 with zero quarantine, and passed all 14 validator checks. Warm replay took 0.772 seconds and processed zero new local or Luna records.

The route gate therefore reduced Luna-classified reviews by 61% on this sample. It did not improve wall time: the hybrid was 4.18x slower than the saved 65.314-second three-worker Luna-only 100 checkpoint. Applying the measured small-sample rates to 484,189 distinct full-corpus texts projects 317.499 local hours plus 49.946 Luna escalation hours. Required verification raises the projection to 385.340 hours, or 481.676 hours conservatively. This is a high-uncertainty projection, not an execution.

The label-isolated Golden 50 run routed 18 of 49 distinct texts and measured topic 84%, intent 92%, exact severity 66%, severity MAE 0.34, and quote membership 100%. The same-run local result was 80% / 92% / 66% / 0.36; the saved Codex three-worker (gpt-5.6-sol) result was 92% / 88% / 74% / 0.26. Because the hybrid conserves Luna Usage but fails the speed gate and trails the Codex model on topic/severity quality, it was not advanced to 500 reviews. Evidence is in `experiments/hybrid/`.

## Distilled text classifier + bounded Luna

The faster experiment trained word/character TF-IDF linear heads on 7,853 Luna pseudo-labeled texts. The first 500 and Golden 50 were excluded by review ID, exact-text hash, and normalized-text hash before fitting or threshold selection. Normalized duplicate groups were reduced to one example and conflicting groups were removed.

Training took 7.95 seconds, and 500-review inference took 0.032 seconds. On the held-out first 500, teacher fidelity was 82.0% topic, 86.2% intent, and 87.2% exact severity. These compare with saved Luna pseudo-labels and are not human accuracy.

On the 100-review pilot, a hard 20% cap admitted exactly 20 of 61 route candidates. Local classification took 0.021 seconds, Luna escalation 23.288 seconds, and total classification 23.343 seconds. Warm replay took 0.017 seconds with zero work. The downstream role sequence took 36.222 seconds cold and 0.006 seconds warm with zero repeat calls.

Against Golden 50, the fixed policy scored 80% topic, 80% intent, and 64% exact severity; macro-F1 was 0.745, 0.615, and 0.480. The path passes the speed/Usage objective but fails the quality promotion gate, so it was not advanced to 500. Golden is not used for further tuning after this result. Evidence is in `experiments/distilled/`.
