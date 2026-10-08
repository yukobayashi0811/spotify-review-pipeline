# Pipeline architecture

The executable entry point is:

```bash
spotify-pipeline full-run --mode frozen --input data/spotify_reviews_18months.csv --config config/full.json
```

For the completed full state, `frozen` verifies the input/artifact hashes and all final counts, then regenerates only the deterministic grading adapter with zero model calls; it cannot overwrite adopted grouping or memo evidence. `config/cost_100_frozen.json` is the isolated fresh-input proof: every stage records its input hash, output hash, status, timestamps, and details in `stage_runs`, and its identical warm replay skips all unchanged stages. The training-mode path preserves the earlier staged teacher-label and active-learning workflow.

| Stage | Input | Output | Owner | Failure behavior | Stop condition |
|---|---|---|---|---|---|
| prepare | exact six-field source CSV | source rows, quarantine, deterministic profile | code | transaction rollback; source identity remains inspectable | malformed input or duplicate source ID |
| classify/enrich | review text, exact-text cache, frozen model SHA | one validated record per nonempty source ID | local classifier; existing bounded Luna teacher rows are reused | valid partial records persist; only invalid IDs (≤10) receive one retry; then quarantine | spend/output/concurrency caps or provider unavailable |
| verify | declared stratified sample and saved first-pass labels | independent verifier rows and agreement report | Luna verifier, coordinated by code | one retry of invalid IDs; temporary provider failure stops for resume | verifier cap or provider unavailable |
| group | complaint/cancellation exact texts and duplicate weights | stable code-owned membership, cluster artifact, model names | code clusters; Luna names/reviews coherence | one naming repair request; membership never changes from model output | 600-text evidence cap |
| rank | saved membership and severity | deterministic baseline ranking | code | fail closed on missing membership | all ranked intents accounted for |
| recommend | saved baseline/actionable ranking, topic rollup, issue descriptions, bounded evidence, sensitivity | memo, claims, claim provenance | Luna memo role; code validates and reconstructs code-owned IDs/numbers | one repair request; fail closed if prose/selection still invalid | two-call memo cap |
| export | completed state plus recovery checkpoints | nine-file `grading/` adapter | code | refuses incomplete final export | checker-ready folder |

```text
source CSV
   │
   ▼
[code: prepare/cache] ──► SQLite state + checkpoints
   │
   ▼
[frozen local classifier] ──validated batches──► records
   │                                │
   │ failure                        └─ valid rows saved
   └─ invalid IDs only ──one retry──► quarantine or resume-stop
   │
   ├─► [Luna verifier] ──► verifier_records / evaluation
   │
   └─► [pinned sentence embeddings + code weighted KMeans] ──► membership + model SHA
                         │
                         └─ bounded representatives ─► [Luna naming]
                                                      │
membership ─► [code rank] ─► ranking ─► [Luna memo] ─► validated claims
                                                      │
                                                      └─ code repair provenance
all saved artifacts ─────────────────────────────────────► grading export
                                                      │
                                                      ▼
                     [code: dashboard/db/load.py] verifies hashes, recomputes ranking/claims
                                                      │
                                                      ▼
                     PostgreSQL (Supabase, schema spotify): records, membership, issues,
                     memo, claims, evidence, run facts; SQL views for ranking/rollup/trend
                                                      │  read-only role
                                                      ▼
                     Vercel serverless API (/api/*) ──JSON──► public dashboard
```

The serving layer makes no model calls: the dashboard shows saved results, and the API recomputes each memo number from the stored records before displaying it.

The model/code boundary is deliberate: models interpret language, while code owns IDs, cache identity, membership, arithmetic, validation, retry limits, checkpoints, and export.
