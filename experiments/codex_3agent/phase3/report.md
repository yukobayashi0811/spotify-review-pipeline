# Phase 3 Codex subscription preflight

Status: **adapter, three-worker speedup, label-isolated quality run, and 100/500/10,000 staged checkpoints validated**.

This is development evidence only. It is not a grading export and does not replace the assignment's formal saved program, 100-review cold/warm calculator, independent golden evaluation, or full-corpus run.

## Controls exercised

- Pinned `codex-cli 0.152.0` and verified `Logged in using ChatGPT`.
- Removed API-key variables from the child process.
- Used an empty read-only workspace with optional tool surfaces disabled.
- Used JSONL event output, a strict output schema, a bounded 180-second timeout, one attempt, a 2 MB result cap, and complete per-attempt trace files.
- Kept each worker's source, state database, calls, outputs, and traces separate.
- Did not load or pass golden labels.
- Recorded billing mode as `chatgpt_subscription`; monetary incremental cost remains `unknown`.

## Preflight results

1. `gpt-6.1-sol` was rejected before inference because this model was not supported for the current ChatGPT-authenticated CLI. The failed attempt and provider response are retained under `../preflight-one/`.
2. `gpt-5.6-sol` at low effort succeeded on one synthetic review under `../preflight-one-supported/`.

The successful result classified “Music will not play.” as `playback` / `complaint` / severity `4`, returned the exact sentence as `evidence_quote`, and passed merge validation. It completed in 4.367 seconds with 10,725 measured input tokens, 64 output tokens, no cached input tokens, no tool event, and no API key.

The saved preflight worker summary contains a `$0.0022005` field named `spend_ledger_usd_including_unknown_reservations`. That is the generic pipeline's conservative reservation computed from editable API proxy rates; it is not a measured or inferred charge. The benchmark now sets those proxy rates to zero, while continuing to report subscription monetary cost as `unknown`.

The high input-token count comes from the Codex execution context around a very small payload. It is therefore unsafe to assume that per-review token use resembles a direct API classification call.

## Twelve-review comparison

The operator explicitly replaced the 73% soft stop with an instruction to continue until the service limit is actually reached. The decision is retained in `user_override.json`.

| Metric | One worker | Three workers |
|---|---:|---:|
| Completed / failed | 12 / 0 | 12 / 0 |
| Wall time | 29.573 s | 11.485 s |
| Calls | 3 | 3 |
| Input tokens | 32,632 | 32,616 |
| Cached input tokens | 8,448 | 16,896 |
| Output tokens | 1,157 | 1,230 |
| Merge validation | pass | pass |

Three workers were 2.575x faster, reduced wall time by 61.16%, and reached 85.83% parallel efficiency. Input-token volume was effectively unchanged. Both runs had exact-source quotes for all records. Topic/intent/severity triples agreed for 10/12 records; intent and severity agreed for all 12, while topic agreed for 10.

The speed result supports three independent workers for bounded Codex development classification. It does not establish that either run is correct, so adoption was followed by a separate label-isolated quality run.

## Fifty-review quality run

The three workers processed the previously sanitized 50-review input without loading labels. Exact duplicate texts stayed on one shard. The run completed 50/50 records in 37.783 seconds with three calls, 34,495 input tokens, 4,772 output tokens, one exact-text cache reuse, no final failures, no evidence-quote repairs, and a valid merge.

Only after predictions were saved were they scored against the independent human Golden 50:

| Metric | Codex three-worker | Existing local model |
|---|---:|---:|
| Topic agreement | 92% | 86% |
| Intent agreement | 88% | 90% |
| Severity exact | 74% | 62% |
| Severity MAE | 0.26 | 0.40 |
| Exact-source quote membership | 100% | 100% |

The Golden labels are independent human gold, but this Codex run remains development evidence and must not be promoted into `grading/` or the formal `cost/` evidence without the full-corpus gate.

## 100- and 500-review checkpoints

The 100-review batch-50 run completed 100/100 classifications in 51.766 seconds using three calls. Its exact-result-cache replay took 0.064 seconds with zero new calls. All downstream roles completed, and the independent validator passed all 16 checks.

The 500-review batch-50 run completed 500/500 classifications in 234.858 seconds using 12 calls, with 21 direct exact-text cache reuses and zero final failures. Warm replay took 0.067 seconds with zero calls. Downstream processing took 90.065 seconds; verification completed 50/50 with 98% topic agreement, 98% intent agreement, 90% exact severity agreement, and 0.10 severity MAE. The independent validator again passed all 16 checks.

## 10,000-review checkpoint

The 10,000 stage read 10,000 source rows containing 8,448 distinct exact texts. It was interrupted twice and resumed from saved worker state:

1. The first invocation was safely stopped after a timed-out 50-review batch entered the former one-review-at-a-time recovery path.
2. The first resume stopped when the provider reported the account usage limit at 4,056 completed records.
3. After a user-applied reset, the final resume completed the remaining 5,944 records in 2,948.350 seconds.

The pipeline now uses bounded `50 -> 10 -> 1` recovery. Two malformed 50-review outputs containing unknown IDs were retained as failed calls and successfully recovered with five ten-review calls each. The final state contains:

| Metric | Result |
|---|---:|
| Completed / quarantined | 10,000 / 0 |
| Distinct texts / exact cache reuses | 8,448 / 1,552 |
| Classification calls | 234 |
| Successful / failed enrichment calls | 217 / 17 |
| Classification input / output tokens | 2,779,887 / 656,078 |
| Final successful resume wall time | 2,948.350 s |
| Merge validation | pass |

Total classification wall time is unavailable because the two interrupted outer invocations ended before writing their timing reports. This value is intentionally `null`, not reconstructed from timestamps.

Warm replay completed all 10,000 records in 0.075573 seconds with zero new model calls.

The downstream pass grouped 4,570 complaint/cancellation records and created eight stable topic issues. The initial verifier pass saved 950/1,000 after one malformed 50-review response. A verifier-only resume reused the same label configuration, skipped those 950 IDs, and sent only the missing 50. The final verifier report has no failures:

| Verification metric | Result |
|---|---:|
| Selected / verified | 1,000 / 1,000 |
| Topic agreement | 91.7% |
| Intent agreement | 94.3% |
| Severity exact | 90.8% |
| Severity MAE | 0.092 |

Deterministic ranking placed `I_USABILITY` first with 1,159 complaint/cancellation records, severity sum and priority score 2,970, and mean severity 2.562554. The memo role selected usability and emitted four numeric claims that the claim validator matched to the saved ranking and cited review IDs.

The final independent validation passed all 18 checks. It reconciled 10,000 source rows, 10,000 completed records, 4,570 memberships, eight ranking rows, 257 role calls, and 257 trace metadata files. Aggregate measured usage across roles was 3,072,944 input tokens and 736,254 output tokens. Subscription monetary cost remains `unknown`.

## Full-corpus gate

The final stable classification segment projects 68.252 hours for the known 484,189 full-corpus distinct texts. The 1,000-review verifier timing projects 27.634 hours for 66,061 full-corpus verification reviews. Total projected runtime is 95.893 hours, or 119.866 hours at 1.25x.

The full 660,622-row CSV is not present in the current project or course workspace. The full run was therefore not started. The projection is retained in `../checkpoint-10000-parallel/full-corpus-projection.json` and must not be presented as an execution result.
