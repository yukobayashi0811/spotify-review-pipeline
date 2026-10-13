# Spotify review multi-role analysis pipeline

Submission for **Final Assignment – Multi Agent Large Data Processing Pipeline**. One command (`spotify-pipeline run`) reads all 660,622 Spotify Google Play reviews (May 2022 – Nov 2023), labels every nonempty review, verifies a sample with a separate blind reader, groups complaints into issues, ranks them with a deterministic formula, and writes a recommendation memo whose numbers are checked by code against saved data. The results are served from a database through a deployed backend to a public dashboard.

**Live dashboard:** https://spotify-review-pipeline-dashboard.vercel.app (public, no sign-in; makes no model calls).

## Results at a glance

All figures come from the final full-corpus run `pipeline-v4` (`artifacts/full_run_v4/`, exported to `grading/`).

| | |
|---|---|
| Rows accounted for | 660,622 / 660,622 (`grading/records.jsonl.gz`) |
| Classified | 660,609 completed; 13 empty texts quarantined (`empty_review_text`); 0 other failures |
| Exact-text reuse | 484,189 distinct nonempty texts; 176,420 rows reuse an identical text's result (`cache_source_id`) |
| Final label sources | GPT-6 Luna: 23,200 rows (22,895 risk-routed distinct texts); sealed local classifier: 637,409 rows |
| Complaint/cancellation rows grouped | 287,914 rows in 44 issues, exactly one issue each |
| Recommendation | `billing` / `I_BILLING_04` "Listening controls restricted to Premium": 22,475 complaints, severity sum = priority score 64,148, mean severity 2.854194 ([memo.md](memo.md)) |
| Golden 50 (human labels) | topic 82%, intent 80%, exact severity 64% (within ±1: 92%) |
| Fresh held-out 30 (human, labeled blind) | topic 63%, intent 83%, exact severity 40% (within ±1: 77%) |
| Independent verifier (400 sampled texts, 390 verified) | local-labeled stratum, row-weighted: topic 82.5%, intent 87.0%, exact severity 84.6%; 155 disagreements recorded |
| System tests | planted wrong labels flagged; 5 prompt-injection reviews classified on their content; all checks pass |
| Interruption/resume | stopped on purpose after 41 saved Luna batches (2,190 rows done); the resume sent 0 of those IDs again |
| Full-run cost and time | 525 GPT-6 Luna requests, $0.929008 API-equivalent (subscription calls, not an invoice); 6,528 s wall time over the two invocations |
| 100-review pilot | cold 44.9 s, 5 Luna requests, $0.00662678; warm 0.0135 s, 0 requests |
| Submission checker | pass, no issues (`artifacts/self_check_final.json`); unit tests 47/47 |

## Rubric evidence map

| Criterion | Where to look |
|---|---|
| **D1** Accessible code, setup and artifacts | [Setup](#setup-and-data) · [Reproduce](#reproduce-without-model-calls) · `.env.example`, `.gitignore` · CI: `.github/workflows/test.yml` |
| **D2** Architecture, shared schema, provenance | [docs/architecture.md](docs/architecture.md) · [config/labels.md](config/labels.md) · `src/spotify_pipeline/schemas.py` · [prompts/](prompts) · every record's `label_config` · [run_manifest.json](artifacts/full_run_v4/run_manifest.json) and [CODE_PROVENANCE.md](artifacts/full_run_v4/CODE_PROVENANCE.md) |
| **D3** Memo numbers linked to calculations and evidence | [memo.md](memo.md) · [grading/claims.csv](grading/claims.csv) · [claims_provenance.csv](artifacts/full_run_v4/claims_provenance.csv) · [memo_evidence_pack.json](artifacts/full_run_v4/memo_evidence_pack.json) · `scripts/rerank_from_grading.py` · the dashboard recomputes every claim in SQL (`artifacts/dashboard/live_check.json`) |
| **D4** Recommendation, alternatives, limitations | [memo.md](memo.md) · [sensitivity_analysis.md](artifacts/full_run_v4/sensitivity_analysis.md) · [Limitations](#limitations) |
| **T1** 50 human labels, per-field comparison, error analysis | [evals/golden_50.csv](evals/golden_50.csv) · [golden_final_evaluation.json](evals/golden_final_evaluation.json) · [golden_final_error_analysis.md](evals/golden_final_error_analysis.md) · held-out 30: [holdout_30_evaluation.json](evals/holdout_30_evaluation.json) · [evals/README.md](evals/README.md) |
| **T2** Independent verification, planted-error and injection tests | [verification_report.json](artifacts/full_run_v4/verification/verification_report.json) · [disagreements.csv](artifacts/full_run_v4/verification/disagreements.csv) · [system_test_report.json](evals/system_tests/system_test_report.json) |
| **T3** Real 100-review cold/warm pilot, offline calculator, controls | [cost/report.md](cost/report.md) · [cost/README.md](cost/README.md) · `cost/pilot_100/` · `cost/pilot_calls.jsonl`, `cost/rates.csv`, `cost/usage.csv`, `cost/replay_check.json` · measured full run [cost/full_run_v4_actuals.md](cost/full_run_v4_actuals.md) · controls `src/spotify_pipeline/guard.py` |
| **W1** Full ingestion, coverage, classification | `grading/` · `grading/ingestion.json` · checker receipt `artifacts/self_check_final.json` |
| **W2** Runnable staged program, bounded calls, saved handoffs, resume | `spotify-pipeline run` ([Run the pipeline](#run-the-pipeline-on-any-csv)) · [run_log.jsonl](artifacts/full_run_v4/run_log.jsonl) · [evals/recovery_v4/](evals/recovery_v4) |
| **W3** Reproducible ranking, deployed dashboard with grounded AI recommendation | `scripts/rerank_from_grading.py` · live dashboard (URL above) · [dashboard/README.md](dashboard/README.md) · `artifacts/dashboard/db_load_receipt.json`, `artifacts/dashboard/live_check.json` |

## Setup and data

```sh
python3.13 -m venv .venv            # Python 3.11+ works; 3.13 was used
./.venv/bin/pip install -e '.[dev,distilled]'          # offline checks and the local classifier
./.venv/bin/pip install -e '.[embedding]'              # only needed to run grouping on new input
```

Raw data: the course ZIP linked from the assignment ([Google Drive](https://drive.google.com/file/d/1P0rUoAS_wVjp3BYKqXMEyD4u0uJP1Bvf/view)) contains `spotify_reviews_18months.csv` (97,400,616 bytes). It is not committed. Place it at `data/spotify_reviews_18months.csv`; its SHA-256 must be `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6` (also in `data/manifest.json`). The committed `data/cost_100.csv`, `checkpoint_500.csv` and `analysis_10000.csv` are the supplied development files. Source: BwandoWando, *3.4 Million Spotify Google Store Reviews* (Kaggle, CC0).

Credentials: `.env.example` lists `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` with empty values, and `.gitignore` excludes `.env` and `.env.*`. No API key is used: GPT-6 Luna runs through the Codex CLI (`codex-cli 0.159.3`) signed in with a ChatGPT subscription, and none of the offline checks below needs a credential.

## Reproduce without model calls

From the repository root, after setup:

```sh
./.venv/bin/python scripts/run_tests.py --output artifacts/test_results.json   # 47 unit tests (Luna replaced by a fixture)
python3 scripts/rerank_from_grading.py                                          # ranking from grading/, compared with grading/ranking.csv
./.venv/bin/python check_submission.py reference --full data/spotify_reviews_18months.csv \
    --analysis data/spotify_reviews_18months.csv --out artifacts/local-reference.json
./.venv/bin/python check_submission.py check --reference artifacts/local-reference.json \
    --submission grading --out artifacts/self-check.json                       # contract checker
python3 scripts/check_interrupt_resume.py --grading grading                     # resume rules on the saved checkpoints and calls
python3 cost/calculator.py && python3 cost/calculator.py --self-check            # pilot cost/runtime calculator
python3 scripts/build_full_run_v4_actuals.py                                    # measured full-run cost from grading/calls.jsonl
PYTHONPATH=src python3 scripts/rescore_golden.py                                # Golden 50 scores
python3 scripts/golden_error_analysis.py                                        # Golden 50 error analysis
python3 scripts/score_holdout.py --labels evals/holdout_30_labels_sheet_export.csv   # held-out 30 scores
```

`grading/records.jsonl.gz` is the committed export (the checker and reranker read it directly; `gunzip -k` restores `records.jsonl`). The full-run SQLite state (1.5 GB) and the embedding matrix are not committed; everything needed to recompute the ranking, claims, scores and costs is in `grading/`, `artifacts/full_run_v4/`, `evals/` and `cost/`.

## Run the pipeline on any CSV

```sh
./.venv/bin/spotify-pipeline run --input <reviews.csv> --config config/pipeline.json --output runs/<name>
```

The CSV needs the six source columns (`review_id, review_text, review_rating, review_likes, app_version, review_timestamp`). The command runs the stages in [Architecture](#architecture) and saves every handoff under the output folder (`state.db`, `routing.json`, `verification/`, `issues.csv`, `membership.csv`, `ranking.csv`, `memo.md`, `claims.csv`, `run_summary.json`, `run_manifest.json`, `run_log.jsonl`, `luna_traces/`, `grading/`). Re-running the same command skips every unchanged stage and makes no model call; after an interruption it resumes and never re-sends completed IDs. Settings are in [config/pipeline.json](config/pipeline.json). Unless every stage is cached, this command makes GPT-6 Luna requests (billed to the signed-in subscription); the commands in the previous section never do. The final full-corpus result was produced by exactly this command on the full CSV.

## Architecture

```text
CSV ─► prepare (code: hashes, exact-text dedup, quarantine empty texts)
        │
        ▼
     route (code): the sealed local classifier scores every distinct text; risky texts
        │          (support topic, billing/privacy/fraud terms, or severity ≥ 4) are ordered by
        │          estimated error × rows and capped at 5% of distinct texts
        ├─► classify_luna: GPT-6 Luna labels the routed texts (≤ 50 per request, 3 in parallel)
        └─► classify_local: the local classifier's labels are kept for all other texts
        ▼
     records (SQLite) ─► verify: blind GPT-6 Luna reader on a stratified sample; code compares
        │
        ├─► group: MiniLM embeddings + seeded k-means per topic (code) ─► Luna names each cluster
        │          and judges coherent/actionable; membership stays code-owned (one issue per row)
        ▼
     rank (code): priority_score = severity_sum = complaint_count × mean_severity
        ▼
     memo_evidence (code: actionable ranking, topic rollup, sensitivity) ─► recommend: Luna memo;
        │                                                     code validates every ID and number
        ▼
     export ─► grading/ ─► dashboard/db/load.py (verifies, loads, recomputes) ─► PostgreSQL (Supabase)
                                                                   │ read-only role
                                                                   ▼
                                                   Vercel serverless API ─► public dashboard
```

Code owns IDs, hashes, the exact-text cache, batching, validation, routing, sampling, clustering, membership, arithmetic, retries, caps, checkpoints and exports. Models make bounded language judgments only. Stage table with inputs, outputs, failure behavior and stop rules: [docs/architecture.md](docs/architecture.md).

**Why each model call exists.** The local classifier is fast and free but weaker on risky, rare and non-English text, so a capped share of risky texts goes to GPT-6 Luna. Verification needs an independent reader that never sees the first label. Naming a cluster and judging whether it is coherent and actionable is a language task over at most 10 examples per cluster. The memo needs prose over saved aggregates; code then checks every number. Counting, membership and ranking need no model.

**Controls.** Every Luna request passes through `GuardedProvider` (`src/spotify_pipeline/guard.py`): a model allowlist (`gpt-6-luna`, effort `low`, checked before sending and against the model the CLI reports), at most two retries with exponential backoff and jitter for transient CLI failures (timeouts are not retried because they may be charged), per-role request caps (enrich 1,457, verify 26, group 30, memo 2 for the full run), and one spend ledger that reserves a worst-case $0.01 per in-flight request against a $5 run limit. Invalid model output is retried only for the invalid IDs, by splitting 50 → 10 → 1.

**Where the local classifier comes from.** It is a TF-IDF/linear model (`experiments/distilled/al2-final-v2/`, SHA-256 `a5279d07…`) trained only on 12,008 GPT-6 Luna teacher labels collected during development; the Golden 50 and `checkpoint_500` reviews were excluded by ID, exact text and normalized text. The Golden labels were never fitted or used for thresholds, but the Golden 50 did influence one earlier design decision (a distilled + 20% Luna configuration failed its predeclared gate and was not promoted), which is why a fresh held-out set was added. History: [experiments/distilled/README.md](experiments/distilled/README.md).

## Labels and schema

Eight topics (access, usability, playback, downloads, catalog, billing, support, other), intents in precedence order (cancellation > complaint > request > praise > unclear), severity 1–5 and sentiment −1…1 follow the shared definitions with examples in [config/labels.md](config/labels.md). Every completed record has `review_id, source_sha256, status, topic, intent, sentiment, severity, entities, evidence_quote, needs_review, label_config` (and `cache_source_id` for exact-text reuse); quarantined records carry `reason`. The evidence quote must be an exact substring of the source text (checked in code). `label_config` names the model, effort, prompt version, schema version and artifact or CLI fingerprint that produced each label.

## Ranking

Only completed complaint and cancellation records enter the ranking. `priority_score = severity_sum = complaint_count × mean_severity`; means use six decimals with half-up rounding; order is score descending, then issue ID ascending. Each review belongs to exactly one issue. `actionable_ranking.csv` keeps the issues judged coherent and actionable; `topic_rollup.csv` sums issues by topic.

| Rank | Issue | Name | Coherent / actionable | Complaints | Severity sum = score | Mean |
|---:|---|---|---|---:|---:|---:|
| 1 | `I_OTHER_02` | General app dislike | yes / **no** | 57,872 | 117,639 | 2.032745 |
| 2 | `I_BILLING_04` | Listening controls restricted to Premium | yes / yes | 22,475 | 64,148 | 2.854194 |
| 3 | `I_USABILITY_01` | Frequent ads | yes / yes | 26,570 | 60,211 | 2.266127 |
| 4 | `I_PLAYBACK_03` | Unexpected song selection and playback | **no** / yes | 16,304 | 47,772 | 2.930079 |
| 5 | `I_PLAYBACK_01` | Playback stops or fails | yes / yes | 14,449 | 44,827 | 3.102429 |
| 6 | `I_PLAYBACK_02` | App freezes, crashes, or stops responding | yes / yes | 14,633 | 43,614 | 2.980523 |
| 7 | `I_USABILITY_04` | Unwanted songs added to playlists | **no** / yes | 14,367 | 39,999 | 2.784089 |
| 8 | `I_USABILITY_02` | Shuffle and song selection problems | **no** / **no** | 13,282 | 39,034 | 2.938865 |
| 9 | `I_USABILITY_05` | Update-related feature and interface changes | **no** / yes | 14,260 | 37,158 | 2.605750 |
| 10 | `I_OTHER_03` | General dissatisfaction with recent update | yes / **no** | 15,906 | 32,680 | 2.054571 |

Full table: [grading/ranking.csv](grading/ranking.csv) (44 issues) and [issues.csv](artifacts/full_run_v4/issues.csv). By topic, usability leads the baseline (severity sum 187,967), then other (173,195), playback (136,213) and billing (134,753).

## Recommendation

The memo ([memo.md](memo.md)) recommends **`billing` / `I_BILLING_04`**: make core listening controls (choosing a song, skipping, seeking) consistently available or clearly explained, because many reviews say these now require Premium. It is the highest-scoring issue that the grouping role judged both coherent and actionable. The top baseline issue, `I_OTHER_02` (general dislike), is coherent but not actionable, so the memo reports it and does not recommend it. Alternatives considered and not selected: `I_USABILITY_01` (frequent ads; more complaints, lower severity) and `I_PLAYBACK_01` (playback stops; higher mean severity, lower total). Every number in the memo's 12 claims was checked by code against the saved ranking; all 12 kept the memo role's wording because the role's values were already correct (`claims_provenance.csv`).

The sensitivity analysis matters for how firmly to read this. Correcting the local labels with the verifier's confusion rates leaves `I_OTHER_02` first (1,000/1,000 bootstrap draws) and `I_BILLING_04` as the leading actionable issue (adjusted score 43,363, 95% interval 31,765–53,533), but its interval overlaps that of `I_USABILITY_01` (39,461; 33,135–45,474). At topic level the correction moves usability from first to third and puts `other` first in 994/1,000 draws. The memo states both.

## Two reviews traced end to end

1. **Local path.** Review `08ebe7dd-6a4f-4afe-b85e-a1d79d976c5b` ("C'mon why you aren't letting us play the songs of our choice without premium …"). Not routed (no risk rule, local severity 3), so it keeps the local classifier's label: `billing`, `complaint`, severity 3, with the whole sentence as an exact evidence quote. Code assigned it to `I_BILLING_04` (`grading/membership.csv`). It is one of the three typical examples (most common severity, closest to the centroid) that memo claims C001–C004 cite for that issue.
2. **Routed path.** The text "Not" appears in 14 reviews. The local classifier scored it `other`/`complaint`/severity 4 with a high estimated error (0.80), so the router made it the highest-priority routed text (`routing.json`). Luna labeled it `unclear`/severity 1 with `needs_review=true` (request `codex-bd00f5c6…` in `grading/calls.jsonl`), so its 14 rows do not enter the complaint ranking. The 13 duplicates carry `cache_source_id = e02920ae-…`.

## Evaluation

- **Golden 50.** Hand-labeled by the author from review text only (`evals/golden_50.csv`, provenance in `golden_50.provenance.json`); labels were never model inputs. Final labels: topic 82%, intent 80%, exact severity 64%, within ±1 92%, severity MAE 0.46, sentiment MAE 0.29, entities exact 48%, evidence quotes exact substrings 100%; macro-F1 topic 0.775, intent 0.632, severity 0.489. Three reviews are ambiguous (human intent `unclear`).
- **Error analysis** ([golden_final_error_analysis.md](evals/golden_final_error_analysis.md)): 25 of 50 rows have at least one wrong field; 24 of them were labeled by the local classifier (48 local rows) and 1 by GPT-6 Luna (2 Luna rows). The largest groups are blocked playback scored 3 instead of 4 (5), generic criticism scored 2 instead of 1 (4, boundary cases), short reviews naming a feature in passing labeled `other` (4), non-English reviews (4 of 4 wrong in some field) and Premium/price words pulling the topic to billing (3). The one Luna error is an edited review ("Update: Support fixed it … Old post: playback fails"), where the label definitions do not say which part governs.
- **Fresh held-out 30.** Thirty reviews sampled with a fixed seed from outside the Golden 50 and `checkpoint_500` (`evals/holdout_60_sampling_manifest.json`) were labeled by the author from the text alone, without seeing any prediction, and were never used for tuning. Final labels: topic 63%, intent 83%, exact severity 40%, within ±1 77%, severity MAE 0.83, entities exact 57%, quotes 100%; macro-F1 topic 0.503, intent 0.671, severity 0.236 (`evals/holdout_30_evaluation.json`). 29 of the 30 carry local-classifier labels, so this mainly measures the local classifier. It is lower than the Golden 50, especially for topic and severity; with 30 reviews the uncertainty is wide (one review = 3.3 points).
- **`needs_review` as a prediction.** Set on 16 Golden reviews: 75% of them had a label error versus 38% of unflagged reviews; it caught 12 of the 25 error rows (`evals/golden_needs_review_analysis.json`).
- **Independent verification (full run).** 400 distinct texts sampled by label source with a fixed seed: 300 local-labeled and 100 Luna-labeled; 390 were verified (10 verifier failures are listed). The verifier never sees first-pass labels; code compares. Local stratum, row-weighted: topic 82.5%, intent 87.0%, exact severity 84.6%. The Luna stratum (topic 85%, intent 94%, severity 76%) is a same-model test-retest, not independent accuracy. The most frequent disagreements are severity 3 → 4 (36), severity 1 → 2 (13) and topic catalog → other (13) (`disagreements.csv`). The local stratum feeds the sensitivity analysis.
- **System tests** (`evals/system_tests/`, a separate synthetic copy that never touches the full-run state or the export): two rows whose stored labels were set wrong before verification (declared in advance) were both flagged and two correct control rows were not; five reviews embedding injected instructions (fake IDs, forced labels, prompt disclosure) were classified on their actual content, every returned ID was one of ours and every quote an exact substring.
- **Interruption and resume.** The first invocation was stopped on purpose (`--interrupt-after-luna-batches 40`; 41 batches were saved because in-flight requests finish). 2,190 rows were complete. The same command then resumed, skipped the unchanged stages, and finished 658,419 more rows; no resume request contained an ID completed before the stop (`evals/recovery_v4/interrupt_resume_check.json`; terminal recording `interrupt_resume.typescript` and its text `interrupt_resume_transcript.txt`).

## Cost and runtime

- **Pilot (100 reviews, `data/cost_100.csv`).** The same `spotify-pipeline run` command on a new empty output folder: 44.9 s, 5 GPT-6 Luna requests (1 classify, 1 verify, 2 group, 1 memo) plus 2 local batches, 0 failures, $0.00662678 API-equivalent. Run again unchanged: 0.0135 s, every stage skipped, 0 requests. [cost/report.md](cost/report.md), calculator `cost/calculator.py` (offline, standard library only, editable `cost/rates.csv` and `cost/assumptions.json`), arithmetic self-check `cost/replay_check.json` (doubling every rate doubles cost and leaves time unchanged).
- **Projection made before the full run** from the pilot plus per-request usage of an earlier full-corpus run: $0.84 (base) to $0.99 (conservative) with exact-text reuse, $1.13–$1.32 without; 8,486–11,258 s.
- **Measured full run** ([cost/full_run_v4_actuals.md](cost/full_run_v4_actuals.md)): 525 Luna requests (497 classify, 13 verify, 14 group, 1 memo; including 42 failed attempts, which were retried by splitting the batch — 10 sampled texts still could not be verified and are listed), 7.05 M input tokens (4.81 M cached) and 1.31 M output tokens, **$0.929008** at API-equivalent rates — inside the projected range and the $5 limit — and 9,226 local classifier batches. Wall time 6,528 s (688 s before the deliberate stop + 5,840 s after resuming) on one Mac.
- Luna ran under a ChatGPT subscription through the Codex CLI, so every USD figure is usage × API rates, not an invoice. Local hardware and electricity cost was not metered.

## Dashboard, backend and database

- **Database:** PostgreSQL on Supabase, schema `spotify`: all 660,622 records, 287,914 memberships, 44 issues, memo, claims, memo evidence and run facts. Ranking, topic rollup, monthly trend and corpus counts are materialized views computed from the stored records at load time.
- **Backend:** Vercel serverless functions (`dashboard/api/`) reading the database through a read-only role.
- **Dashboard:** overall metrics, issue ranking with drill-down to member reviews, topic and monthly complaint share, the AI-generated recommendation with every cited number recomputed from the database, evaluation results, and review lookup.
- `dashboard/db/load.py` checks the raw CSV hash, reloads everything, recomputes the ranking and claims in SQL and writes `artifacts/dashboard/db_load_receipt.json`; `dashboard/scripts/check-live.mjs` compares the live API with `grading/` (`artifacts/dashboard/live_check.json`). Setup: [dashboard/README.md](dashboard/README.md).

## Limitations

- Reviews are historical, self-selected Google Play feedback, not the full Spotify customer population; there is no revenue, plan tier, confirmed cancellation or causal outcome. Cancellation text is expressed intent, not churn.
- 13 empty reviews could not be classified and are quarantined; they are excluded from every aggregate.
- 96.5% of rows carry local-classifier labels. On human labels the local classifier is the weak point: 24 of 25 Golden error rows, and 63% topic / 40% exact severity on the fresh held-out 30. Blocked-playback severity is under-scored and non-English reviews are often wrong.
- The verifier is a model, not ground truth; its Luna stratum is a test-retest of the same model.
- 18 of 44 issues are judged not coherent; membership-weighted coherence is 73% overall and 72% for the top 10 issues.
- The recommended issue's lead over `I_USABILITY_01` is not robust to the sensitivity correction (overlapping intervals), and the topic order changes under it.
- The 100-review pilot is a small execution measurement; its memo has only one eligible alternative.
- USD figures are API-rate equivalents of subscription calls; local compute cost is unknown.

## Repository map

| Path | Contents |
|---|---|
| `src/spotify_pipeline/` | pipeline code (`pipeline_run.py` runs the stages; `classify.py`, `verify.py`, `group.py`, `rank.py`, `recommend.py`, `sensitivity.py`, `export.py`, `guard.py`, `storage.py`) |
| `prompts/`, `config/` | role prompts, label definitions, `pipeline.json` |
| `grading/` | contract export of the final run |
| `artifacts/full_run_v4/` | final run: routing, verification, issues, membership, ranking, sensitivity, memo inputs/outputs, run log, summary, manifest |
| `evals/` | Golden 50, held-out 30, system tests, interruption/resume evidence |
| `cost/` | 100-review pilot, calculator, rates, usage, report, measured full-run cost |
| `dashboard/` | database schema/loader, serverless API, dashboard |
| `experiments/` | development experiments and the sealed classifier (`distilled/al2-final-v2/`) |
| `artifacts/full_run_v2/`, `archive/` | the earlier full-corpus run and superseded evidence, kept for provenance ([archive/README.md](archive/README.md)) |

Authoritative copies of the assignment contracts: [docs/source_spec/](docs/source_spec).
