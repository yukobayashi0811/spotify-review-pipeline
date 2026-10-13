# Dashboard, backend and database

The deployed dashboard shows the saved full-corpus results: overall metrics, the issue ranking, the topic rollup, monthly complaint share, the AI-generated recommendation (memo role output) with every cited number rechecked against the database, and traceable issue/review evidence.

```text
committed outputs (grading/, artifacts/full_run_v4/, evals/, cost/) + raw CSV
        │  dashboard/db/load.py  (zero model calls; verifies hashes and recomputes ranking/claims)
        ▼
PostgreSQL on Supabase, schema `spotify`
  reviews (660,622) · issue_membership (287,914) · issues (44) · memo · claims · memo_evidence · run_facts
  materialized views: issue_ranking · topic_rollup · monthly_trend · corpus_counts
                      · label_source_counts · issue_member_order
                      ← computed by PostgreSQL from reviews + membership at load time
        │  read-only role `dashboard_reader`, server-side connection only
        ▼
Vercel serverless functions  /api/overview /api/ranking /api/topics /api/trend
                             /api/recommendation /api/issue /api/review /api/health
        │  JSON
        ▼
Static dashboard (public/index.html + app.js), same Vercel deployment
```

No page or API route calls a model. The recommendation text was produced once by the pipeline's memo role (`artifacts/full_run_v4/memo_payload.json`); the API recomputes each cited number from `reviews` + `issue_membership` and reports whether it still matches.

## 1. Load the database (Supabase)

1. Create a Supabase project and keep its database password.
2. In **Connect**, copy the **Session pooler** URI (port 5432, works over IPv4).
3. From the repository root, with the raw CSV at `data/spotify_reviews_18months.csv`:

   ```sh
   ./.venv/bin/pip install "psycopg[binary]==3.3.6"
   export DATABASE_URL='postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:5432/postgres'
   ./.venv/bin/python dashboard/db/load.py
   ```

   The loader checks the raw CSV SHA-256, re-derives every `source_sha256`, recreates the `spotify` schema, loads all tables in one transaction, refreshes the materialized views, recomputes the ranking, topic rollup and memo claims in SQL, re-grants the read-only role if it exists, and writes `artifacts/dashboard/db_load_receipt.json`. It exits non-zero if any check fails. Locally it loads in about 40 seconds; against a Supabase free-tier project from a laptop it took about 4 minutes. It uses about 260 MB.

4. In the Supabase SQL editor, run `dashboard/db/roles.sql` after replacing the password. Later reloads re-grant read access to `dashboard_reader` automatically.

The `spotify` schema is not exposed through Supabase's public Data API, so the anon key cannot read it.

## 2. Deploy the backend and dashboard (Vercel)

1. Import the GitHub repository into Vercel. Set **Root Directory** to `dashboard` and **Framework Preset** to *Other*. No build command is needed.
2. Add one environment variable, `DATABASE_URL`: the **Transaction pooler** URI (port 6543) using the read-only role, e.g. `postgresql://dashboard_reader.<project-ref>:<reader-password>@<region>.pooler.supabase.com:6543/postgres`. If the pooler rejects the custom role, use the default `postgres.<project-ref>` transaction-pooler URI instead; either way the value stays server-side in Vercel.
3. Deploy. `vercel.json` adds security headers and a daily cron request to `/api/health`, which keeps the free Supabase project from pausing for inactivity.
4. Verify the live deployment against the committed export (zero model calls):

   ```sh
   node dashboard/scripts/check-live.mjs https://<your-app>.vercel.app
   ```

   This writes `artifacts/dashboard/live_check.json`, comparing all 44 ranking rows and all 12 memo claims with `grading/ranking.csv` and `grading/claims.csv`.

## Local run

```sh
cd dashboard && npm install
DATABASE_URL='postgres://...' DATABASE_SSL=disable npm run dev   # http://localhost:3000
```

`dev-server.mjs` serves `public/` and routes `/api/<name>` to `api/<name>.js`, mirroring Vercel.

## Files

| Path | Purpose |
|---|---|
| `db/schema.sql` | Tables, constraints and the materialized views |
| `db/load.py` | Loader and database-side verification (receipt in `artifacts/dashboard/`) |
| `db/roles.sql` | Read-only role for the deployed backend |
| `api/*.js` | Serverless backend; `api/_lib/db.js` holds the shared connection |
| `public/` | Dashboard (plain HTML/CSS/JS, no build step) |
| `scripts/check-live.mjs` | Live consistency check against the grading export |
| `vercel.json` | Output directory, headers and the keep-alive cron |
