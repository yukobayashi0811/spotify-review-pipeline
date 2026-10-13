# Pipeline architecture

## Entry points

| Purpose | Command | Model calls |
|---|---|---|
| Run the whole pipeline on any input CSV | `spotify-pipeline run --input <csv> [--config config/pipeline.json] [--output runs/<name>]` | GPT-6 Luna (Codex CLI) for routed texts, verification, issue naming, memo; none when every stage is cached |
| Regenerate the ranking from the committed export | `python3 scripts/rerank_from_grading.py` | none |
| Check the resume rules on the saved checkpoints and calls | `python3 scripts/check_interrupt_resume.py --grading grading` | none |
| Offline cost calculator | `python3 cost/calculator.py` | none |

`spotify-pipeline run` is the final configuration (`pipeline-v4`). The full-corpus result in `grading/` and `artifacts/full_run_v4/` was produced by this command on the full CSV (interrupted once on purpose and resumed), and the 100-review cost pilot runs the same command. The local classifier it uses was built earlier from GPT-6 Luna teacher labels; see [How the local classifier was built](#how-the-local-classifier-was-built).

## Stages, owners and stop rules (`spotify-pipeline run`)

| # | Stage | Owner | Input | Output (saved) | Failure behavior | Stop / cap |
|---|---|---|---|---|---|---|
| 1 | prepare | code | six source fields of every row | `source_rows`, row SHA-256, profile (`ingestion.json`), empty texts quarantined (`empty_review_text`), exact-text index | duplicate ID or different input for an existing state → stop | — |
| 2 | route | code + sealed local classifier (TF-IDF/linear, SHA `a5279d07…`) | every distinct nonempty text | local scores and calibrated error probability; `routing.json`: risky texts (local topic support, a billing/privacy/fraud/support term, or local severity ≥ 4), ordered by error probability × rows × risk weight | — | at most 5% of distinct texts (`max_fraction_of_distinct_texts`) |
| 3 | classify_luna | GPT-6 Luna, effort `low`, 3 requests in parallel | routed texts only, ≤ 50 per request | one validated record per routed text, copied to exact duplicates with `cache_source_id` | invalid output → only the invalid IDs are retried by split (50→10→1); a routed text whose Luna call still fails keeps its local label (recorded in `routing_outcome.json`) | per-role request cap, spend cap; every batch saved; resume skips completed IDs |
| 4 | classify_local | sealed local classifier | all texts not labeled by Luna, batches of 50 | one validated record per distinct text, copied to exact duplicates | invalid rows retried by split, then quarantined | per-batch save; resume skips completed IDs |
| 5 | verify | GPT-6 Luna (separate prompt), blind | review text only; first-pass labels never sent | `verifier_records`, `verification_report.json` (agreement overall, by label source, and row-weighted), `disagreements.csv` | split retry; unverified rows listed as failures | sample = 10% of rows, min 10, max 400, stratified by label source (25% Luna), SHA-256 order under a fixed seed |
| 6 | group | code + GPT-6 Luna | distinct complaint/cancellation texts | pinned MiniLM embeddings, seeded k-means per topic (k = 3…10, or one cluster for a topic with < 30 texts), `membership.csv` (exactly one issue per row), Luna names/coherent/actionable from ≤ 10 representatives per cluster | invalid naming → one retry; incoherent clusters split once | ≤ 600 evidence texts, ≤ 30 naming requests |
| 7 | rank | code | membership + severity | `ranking.csv`: `priority_score = severity_sum = complaint_count × mean_severity`, ties by issue ID | — | deterministic |
| 8 | memo_evidence | code | ranking, issues, verifier records | actionable ranking, topic rollup, sensitivity analysis (when ≥ 30 local-labeled texts were verified) | — | — |
| 9 | recommend | GPT-6 Luna memo role | saved aggregates, coverage, monthly trend, 3 typical reviews per issue | `memo_payload.json`, `memo.md`, `claims.csv`, `claims_provenance.csv` | code checks every ID and number; one repair request | two requests |
| 10 | export | code | saved state | nine-file `grading/` folder | refuses an incomplete export | — |

Every model request goes through `GuardedProvider` (`src/spotify_pipeline/guard.py`): model allowlist (`gpt-6-luna` with the configured effort only, checked before sending), bounded retry with exponential backoff and jitter for transient CLI failures (two retries; timeouts are not retried because they may be charged), per-role request caps, and one run-level spend ledger that reserves a worst-case amount before each request at the editable API-equivalent rates. Each stage is cached by a fingerprint of its inputs and settings in `stage_runs`; a re-run with unchanged inputs skips every stage and makes no request.

## Where code decides and where a model judges

- **Code**: reading the CSV, hashing, deduplication, cache identity, batching, ID validation, quote-substring checks, routing, sampling, clustering, membership, all counts and arithmetic, claim validation, retries, caps, checkpoints, exports, the database load and the API.
- **Local classifier**: topic, intent, sentiment, severity, entities and evidence quote for every distinct text (trained only on saved Luna teacher labels).
- **GPT-6 Luna**: labels for routed risky texts; blind verification; names and coherent/actionable judgments for clusters; the memo prose. It never changes an ID, a membership or a number.

## How the local classifier was built

The teacher labels and the classifier come from development work before the final run (the earlier full-corpus run is kept in `artifacts/full_run_v2/` and `archive/v2_full_run_evidence/`):

```text
legacy 10,000-review Luna run ──(8,448 teacher texts)──┐
full-run Luna buckets H 60 · R 500 · AL1 1,500 · AL2 1,500 ─┤──► 12,008 teacher labels
                                                           ▼
           train TF-IDF/linear (Golden 50 and checkpoint_500 excluded by ID/exact/normalized text)
                                                           ▼
           sealed artifact al2-final-v2 ──► classify the remaining 472,181 distinct texts
                                                           ▼
           earlier full-corpus labels (superseded): Luna for the 12,008 teacher texts,
                         local classifier for the rest
```

Active learning (AL1/AL2) chose teacher texts by calibrated error probability with a diversity constraint; H took frequent ambiguous texts; R took risky texts. The final run (`pipeline-v4`) uses the sealed artifact unchanged, but labels with GPT-6 Luna the risky texts that its router selects (22,895 distinct texts, 23,200 rows) and keeps local labels for the other 637,409 rows.

## Data flow, storage and serving

```text
input CSV
   │ prepare (code)
   ▼
SQLite state (source_rows, texts, records, result_cache, calls, verifier_records,
              membership, issue_names, stage_runs, routing, luna_ledger, metadata)
   │            ▲ every batch saved atomically; resume reads it
   ├─► route (local scores + code rule) ─► classify_luna (Luna, ≤5%) ─► classify_local (the rest)
   ├─► verify (Luna, blind sample) ─► agreement + disagreements
   ├─► group (embeddings + k-means in code; Luna names) ─► membership
   ├─► rank (code) ─► ranking ─► recommend (Luna memo; code-validated claims)
   └─► export (code) ─► grading/ (records, membership, ranking, claims, calls, checkpoints)
                              │
                              ▼  dashboard/db/load.py (code): verifies hashes, loads, recomputes
                     PostgreSQL on Supabase, schema `spotify`
                     (reviews, issues, membership, memo, claims, evidence, run facts;
                      materialized views for ranking, topic rollup, monthly trend)
                              │  read-only role, server-side connection
                              ▼
                     Vercel serverless API (/api/*) ──JSON──► public dashboard
```

The serving layer makes no model calls: the dashboard shows saved results, and the API recomputes each memo number from the stored records before displaying it.

## Stop and retry paths

- Invalid model output: valid rows of the batch are kept; only the invalid IDs are retried by splitting 50 → 10 → 1; rows still invalid are quarantined with the reason and attempt count (routed texts keep their local label instead).
- Transient CLI failure (rate limit, network): up to two retries with backoff and jitter, each logged as a failed call; then the stage stops and the run can be resumed.
- Timeout: not retried (possible charge); the reservation is kept as an unknown charge.
- Caps: a request that would exceed the role's request cap or the spend cap is refused before it is sent; work saved so far stays.
- Interruption: re-running the same command resumes; completed IDs are never sent again (`evals/recovery_v4/`).
