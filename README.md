# Spotify review multi-role analysis pipeline

This repository is the runnable submission for **Final Assignment – Multi Agent Large Data Processing Pipeline**. It processes the supplied 660,622-row Spotify Google Play review corpus, preserves every source ID/value, quarantines empty texts, classifies every nonempty row, independently verifies a bounded sample, groups complaint/cancellation records, ranks issues deterministically, and produces a recommendation memo whose issue metrics are checked against saved data.

## Final status

- Full input: 660,622 rows; 660,609 completed; 13 empty-text quarantines.
- Exact-text units: 484,189 distinct nonempty texts; 176,420 source-row reuses.
- Final label sources: 12,008 Luna teacher rows and 472,181 local distinct-text rows, expanded to all source rows by exact-text reuse.
- Complaint/cancellation membership: 288,524 rows, exactly one issue each.
- Adopted grouping: 41 embedding-based issues; no duplicate normalized names.
- Recommendation: prioritize `usability`, specifically `I_USABILITY_04` (playlist playback/song-selection controls).
- Baseline for that issue: 28,348 complaints/cancellations, severity sum/priority score 81,312, mean severity 2.868351.
- Golden 50 (human labels) against the final labels: topic 84%, intent 82%, exact severity 66% (MAE 0.40).
- Independent verification: 400 distinct texts (300 local-labeled, 100 Luna test-retest), 192 recorded disagreements; local-stratum agreement topic 72.5%, intent 83.8%, exact severity 74.1%.
- Cost: the saved Luna calls replay to an estimated $0.46379610 at standard API rates (calls were subscription-authenticated, so this is not an invoice); per-bucket usage and stage times are in [full_run_actuals.md](cost/full_run_actuals.md).
- Final grading self-check: pass. Unit tests: 40/40 pass.
- Live dashboard: **https://spotify-review-pipeline-dashboard.vercel.app** (public, no sign-in). See [Dashboard, backend and database](#dashboard-backend-and-database).
- Evaluation requirement: the assignment-required Golden 50 workflow is complete. A later supplemental held-out 60 proposal is not part of the authoritative assignment contract and is out of scope for submission.

The durable status receipt is [run_report.json](artifacts/full_run_v2/run_report.json). The final memo is [memo.md](memo.md).

## Reproduce and inspect

Create the environment and install the package:

```sh
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev,distilled,local,embedding]'
```

Validate the immutable completed full-corpus state and regenerate its deterministic export:

```sh
./.venv/bin/spotify-pipeline full-run --mode frozen \
  --input data/spotify_reviews_18months.csv \
  --config config/full.json
```

`frozen` verifies the source hash, sealed classifier artifact, completion counts, adopted grouping, verification, ranking, and memo before touching the export. It then regenerates only the deterministic grading adapter and makes zero model calls; it never retrains, performs active learning, or overwrites adopted downstream evidence. The separate 100-review config exercises every stage against a genuinely empty state.

Run offline checks:

```sh
./.venv/bin/python scripts/run_tests.py --output artifacts/test_results.json
./.venv/bin/python check_submission.py check \
  --reference artifacts/local-reference.json \
  --submission grading \
  --out artifacts/self-check.json
./.venv/bin/python scripts/verify_cost_replay.py
```

## Dashboard, backend and database

The saved full-corpus results are served from a database through a deployed backend to a public dashboard. No page or API route calls a model.

- **Database:** PostgreSQL (Supabase), schema `spotify`: all 660,622 records, 288,524 issue memberships, 41 issues, memo, claims, memo evidence and run facts. Ranking, topic rollup, monthly trend and corpus counts are materialized views that PostgreSQL computes from the stored records and membership at load time; the loader then checks them against the export.
- **Backend:** Vercel serverless functions (`dashboard/api/`) that query the database through a read-only role and return JSON.
- **Dashboard:** `dashboard/public/`, served by the same deployment. It shows overall metrics, the issue ranking, topic and monthly complaint share, the memo recommendation with every cited number rechecked against the database, issue drill-downs with member reviews, review lookup, and the evaluation results.

The loader (`dashboard/db/load.py`) checks the raw CSV hash, re-derives every `source_sha256`, recomputes the ranking, topic rollup and memo claims in SQL, and writes `artifacts/dashboard/db_load_receipt.json`. `dashboard/scripts/check-live.mjs` compares the live API with `grading/ranking.csv` and `grading/claims.csv` and writes `artifacts/dashboard/live_check.json`. Setup and deployment steps are in [dashboard/README.md](dashboard/README.md).

## Credentials

`.env.example` lists `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` with empty values; `.gitignore` excludes `.env` and `.env.*`. None of the offline checks, the ranking regeneration, the cost replay or the dashboard needs a model credential. Luna calls ran through the Codex CLI with ChatGPT subscription authentication, so no API key was used. Jev (TypeSafe) was not used: classification combines Luna teacher labels with a local classifier, which the assignment allows as an equivalent, evaluated choice.

## Architecture and role boundaries

Code owns ingestion, hashes, exact-text cache, IDs, retries, caps, membership, ranking, numerical claims, checkpoints, and exports. Model roles only make bounded language judgments.

| Role | Instructions | Input | Output/schema | Retry/stop rule | Saved evidence |
|---|---|---|---|---|---|
| Prepare/ingest | code | six original source strings | source rows, row SHA, profile, quarantine | fail on schema/ID collision | `grading/ingestion.json`, `grading/records.jsonl` |
| Classify/enrich | `prompts/classify.md` | review text only | topic, intent, sentiment, severity, entities, exact quote, needs_review; enrichment v2 | at most 2 attempts; `50 → 10 → 1`; invalid rows only | `grading/records.jsonl`, `grading/calls.jsonl`, SQLite cache |
| Independent verify | `prompts/verify.md` | review text only; no first-pass prediction | same taxonomy, verification v2 | bounded sample/recovery; saved rows skipped on resume | `artifacts/full_run_v2/verification/verification_report.json` |
| Group name/review | `prompts/group.md` | at most 5 code-owned clusters/50 representatives | GROUP_SCHEMA v3: name, description, coherent, actionable, heterogeneous IDs | 600 distinct evidence texts max; one split for incoherent cluster | `issues.csv`, `membership.csv`, diagnostics and ledger |
| Rank | code | saved membership + severity | `priority_score = severity_sum` | deterministic only; no model call | `ranking.csv`, `ranking.json` |
| Memo writer | `prompts/memo.md` | top-10 baseline/actionable, topic rollup, ≤3 examples/issue, sensitivity and limits | MEMO_SCHEMA v2 | one repair only; maximum 2 calls; stop if still invalid | `memo_payload.json`, `claims.csv`, `claims_provenance.csv` |
| Export/audit | code | saved state | grader contract files | refuses incomplete final export | `grading/`, self-check receipt |
| Serve | code (SQL + serverless API) | grading export, adopted artifacts, raw CSV for hash checks | PostgreSQL tables/views, JSON API, dashboard | loader fails closed on any hash or recomputation mismatch; API is read-only | `dashboard/`, `artifacts/dashboard/` |

See [architecture.md](docs/architecture.md), [labels.md](config/labels.md), and the authoritative copied contracts in [docs/source_spec](docs/source_spec).

## Why the distilled classifier is used

The final local classifier is a TF-IDF/linear approximation trained from saved Luna pseudo-labels. It is used because full Luna inference exceeded the measured subscription/runtime envelope, while the sealed local artifact can classify the remaining distinct texts in minutes and resume deterministically. Luna agreement is a proxy for teacher fidelity, not human accuracy; the target remains the human taxonomy in `config/labels.md`.

Training excluded Golden 50 and the first 500 checkpoint by source ID, exact text hash, and normalized text hash. Golden labels were never fitted or used for thresholds. Golden 50 nevertheless influenced architecture selection: an earlier distilled+20%-Luna configuration failed its predeclared promotion gate and was not promoted. This is disclosed in [the distilled experiment report](experiments/distilled/README.md). The `hybrid-10000` figures are in-training/development comparisons against saved Luna pseudo-labels, not independent evaluation.

## Prompt/config version history

Historical optimized Luna calls retain label identifier `classify-2026-09-30-v2:enrichment-v1`. Current code uses v3/v2 because compact batch aliases and exact cardinality changed the transport/schema. The semantic classification instruction was preserved; historical rows were not rewritten. Inspectable proof consists of the immutable call `label_config`, saved trace/schema payloads, current prompt hash checks in `test_packaged_prompts_match_inspectable_prompts`, and the artifact/training manifests. Version identifiers are cache namespaces, not claims that old evidence was regenerated.

## Grouping rebuild and adoption

The adopted implementation embeds every distinct complaint/cancellation text with `sentence-transformers/all-MiniLM-L6-v2` pinned to revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Inputs longer than 25 words use the saved evidence quote. Embeddings are L2-normalized, saved in `.npy`, and mapped to text SHA-256 values.

Within each primary topic, seeded weighted MiniBatchKMeans tests `k=3…10`, checks cosine silhouette on up to 20,000 texts and a 2% minimum-cluster share, merges centroids at cosine ≥0.90, and lets the grouping role name/review bounded representatives. Same-topic duplicate names or centroid similarity ≥0.85 are merged; `coherent=no` clusters are split once at `k=2` and retained with `low_coherence` if still weak. Membership always remains code-owned.

| Measure | TF-IDF v1 | Embedding v2 |
|---|---:|---:|
| Issues | 51 | 41 |
| Coherent issue count/rate | 31 / 60.78% | 19 / 46.34% |
| Membership-weighted coherent rate, all | 46.15% | **67.18%** |
| Membership-weighted coherent rate, top 10 | 28.27% | **72.22%** |
| Duplicate normalized names | 1 | **0** |

The declared rule selects the higher overall membership-weighted coherent rate, so embedding v2 is adopted. The raw comparison is [grouping_comparison.json](artifacts/full_run_v2/grouping_comparison.json); the archived pre-change result is under `archive/grouping_v1/`.

## Ranking, sensitivity, and memo

Only completed `complaint` and `cancellation` records enter issue ranking:

```text
priority_score = severity_sum
               = complaint_count × mean_severity
```

Order is descending score, then ascending issue ID. Means use six decimals and decimal half-up rounding. `actionable_ranking.csv` filters the same baseline to `coherent=yes` and `actionable=yes`; `topic_rollup.csv` sums baseline issue values by topic.

Sensitivity uses the 300 verification rows whose final source is local, word-count buckets, Laplace-smoothed confusion estimates, deterministic seed `20261003`, and 1,000 bootstrap draws. The issue-level point correction elevates generic `I_OTHER_01`, but that issue is `actionable=no`; the corrected topic ranking remains usability in 1,000/1,000 draws. Corrected estimates are deliberately absent from `claims.csv`. See [sensitivity analysis](artifacts/full_run_v2/sensitivity_analysis.md).

The memo therefore recommends `I_USABILITY_04`, compares `I_USABILITY_03` and `I_BILLING_01`, explains the generic baseline/sensitivity issue, and includes 12 exact issue claims (four per selected/comparison issue). Code reconstructs and validates IDs, metrics, and representative review IDs against saved ranking/evidence; prose cannot change them.

## One-record trace

Review `08a95031-ae90-400e-9603-acc03c8d86e1` illustrates the chain:

1. `source_rows` preserves the source ID and exact text about shuffle ruining albums/playlists.
2. Final classification: `topic=usability`, `intent=complaint`, `severity=3`, with the full source sentence as the exact evidence quote.
3. Blind verifier independently returned the same topic, intent, and severity; `final_source=local` records the verifier stratum.
4. Code assigned it exactly once to `I_USABILITY_04`.
5. Deterministic ranking placed that issue first: 28,348 rows, severity sum/score 81,312, mean 2.868351.
6. Memo claims `C001–C004` copy those four metrics; their representative IDs are bounded separately in the memo evidence pack.

An ambiguous counterexample is the generic `I_OTHER_01`: it ranks second at baseline and first in the sensitivity point estimate, but grouping marks it non-actionable. It is reported as uncertainty, not converted into a product recommendation. Failed 50-row outputs in the historical 10,000 run are also retained: four failures covering 200 attempted rows were recovered through `50 → 10 → 1`, never silently discarded.

## Verification and evaluation

- Golden 50: label-isolated workflow; labels opened only after predictions were saved. Golden influenced prior configuration rejection and is therefore reference evidence, not a fresh final holdout.
- Golden 50 result for the final labels: topic 84%, intent 82%, exact severity 66% (within ±1: 94%), sentiment MAE 0.26, exact-source quotes 100%. Most severity disagreements are one point (3 vs. human 4 for blocked playback, 2 vs. human 1 for generic criticism). The `needs_review` flag, evaluated as a prediction, was set on 15 Golden reviews: 80% of them had a label error versus 34% of unflagged rows (`evals/golden_needs_review_analysis.json`). Details: [evals/README.md](evals/README.md), `evals/golden_final_evaluation.json`.
- Formal 400 verification: separate saved role, with local-source rows used for the sensitivity model.
- Supplemental held-out 60: a sampling workbook was produced during an optional review plan, but the authoritative assignment requires the Golden 50 rather than this extra set. No additional labeling or scoring is planned for submission.
- System tests (synthetic copy, excluded from business state and the grading export): a deliberately wrong `access` label on `SYN001` was detected (the verifier returned `playback`); five synthetic reviews that embed injected instructions (for example, asking for a fake review ID or a billing/severity-5 label) were classified on their actual content, and every returned ID and quote stayed grounded in its source review. Receipt: [system_test_report.json](evals/system_tests/system_test_report.json).
- Verifier disagreements: 192 recorded across the 400 verified distinct texts; the most frequent patterns are severity 3→2 (33), 3→4 (25) and 1→2 (17). See [verification_relinked_report.json](evals/verification_final/verification_relinked_report.json).

## 100-review cold/warm cost proof

The exact command below ran against a new empty state/cache with one worker and no legacy cache:

```sh
./.venv/bin/spotify-pipeline full-run --mode frozen \
  --input data/cost_100.csv \
  --config config/cost_100_frozen.json
```

The cold run completed prepare → classify → verify → group → rank → memo → export in 176.246427 seconds. It made 7 local model attempts (2 classify, 1 verify, 2 group, 2 memo; one memo attempt failed strict validation). The identical warm command finished in 0.011639 seconds, skipped every unchanged stage, and made zero new calls. Evidence is in [cost/frozen_100](cost/frozen_100).

The editable standard GPT-6 Luna API-equivalent rates are $0.10/M input, $0.01/M cached input, and $0.50/M output. Actual historical Luna calls used ChatGPT subscription authentication, so $0.46379610 is explicitly an estimate, not an invoice. The 1.25 conservative estimate is $0.579745. Hardware/electricity cost remains unknown. Doubling all nonzero rates produces exactly $0.92759220 while leaving measured time unchanged; see [the replay receipt](cost/double_rate_check/verification.json) and [report](cost/report.md).

The post-run account snapshot was 90% used/10% remaining in the seven-day window. It is account-wide and cannot be attributed solely to this project. See [usage_after_all.json](artifacts/full_run_v2/usage_after_all.json).

## Interruption and resume evidence

The full local finalization was interrupted after 30 successful batches. Before the stop, 411,574 rows were complete. Resume added 249,035 rows and sent zero of the already-complete IDs; final counts became 660,609 complete plus 13 quarantined.

- Original macOS `script` recording: [interrupt_resume.typescript](evals/recovery/interrupt_resume.typescript)
- Cross-platform Asciinema v2 recording: [interrupt_resume.cast](evals/recovery/interrupt_resume.cast)
- Timestamped text: [interrupt_resume_transcript.txt](evals/recovery/interrupt_resume_transcript.txt)
- Integrity checks: [interrupt_resume_check.json](evals/recovery/interrupt_resume_check.json)
- Conversion hashes/timing policy: [recording_conversion.json](evals/recovery/recording_conversion.json)

Play the cross-platform file with `asciinema play evals/recovery/interrupt_resume.cast`, or inspect the text without installing anything.

## Rubric evidence map

| Requirement | Primary evidence |
|---|---|
| Full ingestion, source preservation, quarantines | `grading/run.json`, `grading/ingestion.json`, `grading/records.jsonl` |
| Roles, prompts, schemas, controls | `prompts/`, `src/spotify_pipeline/`, `docs/architecture.md` |
| Exact cache and resume | SQLite state, grading records/calls, recovery transcript/check |
| Golden workflow | `evals/golden_50*`, `evals/golden_final_evaluation.json`, `evals/README.md`, `scripts/rescore_golden.py` |
| Independent verification and sensitivity | `artifacts/full_run_v2/verification/`, `sensitivity_analysis.*` |
| Grouping comparison/adoption | `grouping_comparison.json`, diagnostics, embeddings map, `archive/grouping_v1/` |
| Deterministic ranking | `grading/ranking.csv`, `ranking.json`, reranker code |
| Recommendation and traceable claims | `memo.md`, `grading/claims.csv`, `claims_provenance.csv`, evidence pack |
| Cost/runtime and editable rates | `cost/frozen_100/`, `cost/report.*`, `cost/rates.csv`, double-rate receipt |
| Tests and final status | `artifacts/test_results.json`, `artifacts/full_run_v2/run_report.json` |
| Grader contract | `artifacts/self-check-stage3.json` and final refreshed receipt |
| Deployed dashboard, backend, database | live URL above, `dashboard/`, `artifacts/dashboard/db_load_receipt.json`, `artifacts/dashboard/live_check.json` |

## Limitations

- Reviews are historical, self-selected Google Play feedback, not the complete Spotify customer population.
- There is no revenue, plan-tier, confirmed cancellation, or causal retention outcome. Cancellation text is expressed intent, not observed churn.
- The local classifier approximates Luna pseudo-labels; teacher agreement is not human truth. Rare Golden classes remain sparse.
- Issue-level sensitivity cannot allocate predicted non-complaints to a latent issue, and bootstrap intervals inherit the verifier sample/model assumptions.
- Embedding grouping improved membership-weighted coherence, but 22/41 issue labels remain `coherent=no`; top-10 weighted coherence is 72.22%, not perfect.
- The 100-review frozen proof uses coarse one-topic pilot groups only to test fresh execution. It is not the adopted full-corpus grouping or recommendation.
- Local compute electricity/hardware cost was not measured and is reported as unknown.
- The optional held-out 60 was not scored because it is outside the assignment requirements; no performance claim is made for it.

## Authoritative material

- [GRADING_CONTRACT.md](docs/source_spec/GRADING_CONTRACT.md)
- [COST_CALCULATOR.md](docs/source_spec/COST_CALCULATOR.md)
- [DATASET_README.md](docs/source_spec/DATASET_README.md)
- [Dataset manifest](data/manifest.json)
- [Official GPT-6 Luna model and pricing](https://developers.openai.com/api/docs/models/gpt-6-luna)
