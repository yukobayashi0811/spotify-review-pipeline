-- Spotify review pipeline: dashboard database schema (PostgreSQL 15+ / Supabase).
--
-- Everything lives in the `spotify` schema. Supabase's Data API exposes only the
-- `public` schema by default, so these tables are not reachable with the public
-- anon key; the deployed backend reads them through a server-side connection.
--
-- The loader (db/load.py) drops and recreates this schema from the saved,
-- committed pipeline outputs. It never calls a model.

DROP SCHEMA IF EXISTS spotify CASCADE;
CREATE SCHEMA spotify;

-- Exact label configurations used for final records (provider/model provenance).
CREATE TABLE spotify.label_configs (
    label_config_id smallint PRIMARY KEY,
    label_config    text NOT NULL UNIQUE,
    label_source    text NOT NULL CHECK (label_source IN ('gpt-6-luna', 'local_classifier')),
    model           text NOT NULL,
    record_count    integer NOT NULL
);

-- Processed review data: one row per original source review ID (660,622 rows).
CREATE TABLE spotify.reviews (
    review_id         uuid PRIMARY KEY,
    source_sha256     char(64) NOT NULL,
    status            text NOT NULL CHECK (status IN ('completed', 'quarantined')),
    quarantine_reason text,
    topic             text CHECK (topic IN ('access','usability','playback','downloads','catalog','billing','support','other')),
    intent            text CHECK (intent IN ('cancellation','complaint','request','praise','unclear')),
    sentiment         real CHECK (sentiment BETWEEN -1 AND 1),
    severity          smallint CHECK (severity BETWEEN 1 AND 5),
    needs_review      boolean,
    entities          jsonb,
    evidence_quote    text,
    label_config_id   smallint REFERENCES spotify.label_configs,
    cache_source_id   uuid,
    review_text       text NOT NULL,
    review_rating     smallint,
    review_likes      integer,
    app_version       text,
    review_timestamp  timestamp
);

-- Issues produced by the grouping role (names/descriptions/coherence by the model,
-- membership by code).
CREATE TABLE spotify.issues (
    issue_id     text PRIMARY KEY,
    topic        text NOT NULL,
    issue_name   text NOT NULL,
    description  text NOT NULL,
    coherent     boolean NOT NULL,
    actionable   boolean NOT NULL,
    flags        text[] NOT NULL DEFAULT '{}',
    label_config text NOT NULL
);

-- Code-owned membership: exactly one issue per complaint/cancellation review.
CREATE TABLE spotify.issue_membership (
    review_id uuid PRIMARY KEY REFERENCES spotify.reviews,
    issue_id  text NOT NULL REFERENCES spotify.issues
);

-- The exported baseline ranking (grading/ranking.csv). Kept only so the API can
-- show that the ranking it recomputes from `reviews` + `issue_membership`
-- matches the committed export field by field.
CREATE TABLE spotify.saved_ranking (
    rank            integer NOT NULL,
    issue_id        text PRIMARY KEY REFERENCES spotify.issues,
    complaint_count integer NOT NULL,
    severity_sum    integer NOT NULL,
    mean_severity   text NOT NULL,
    priority_score  integer NOT NULL
);

-- AI-generated recommendation memo (memo role output, validated by code).
CREATE TABLE spotify.memo (
    memo_id                    text PRIMARY KEY,
    priority_issue_id          text NOT NULL REFERENCES spotify.issues,
    priority_topic             text NOT NULL,
    priority                   text NOT NULL,
    rationale                  text NOT NULL,
    baseline_top_handling      text NOT NULL,
    sensitivity_interpretation text NOT NULL,
    alternative_issue_ids      text[] NOT NULL,
    alternatives               jsonb NOT NULL,
    limitations                jsonb NOT NULL,
    markdown                   text NOT NULL,
    model                      text NOT NULL,
    label_config               text,
    request_id                 text,
    payload                    jsonb NOT NULL
);

-- Numerical claims cited by the memo (grading/claims.csv) plus provenance.
CREATE TABLE spotify.claims (
    claim_id          text PRIMARY KEY,
    issue_id          text NOT NULL REFERENCES spotify.issues,
    metric            text NOT NULL CHECK (metric IN ('complaint_count','severity_sum','mean_severity','priority_score')),
    value             text NOT NULL,
    sentence          text,
    review_ids        uuid[] NOT NULL DEFAULT '{}',
    provenance_source text,
    provenance_reason text,
    request_id        text
);

-- Bounded evidence pack that the memo role received (representative reviews per issue).
CREATE TABLE spotify.memo_evidence (
    issue_id       text NOT NULL REFERENCES spotify.issues,
    ordinal        smallint NOT NULL,
    review_id      uuid NOT NULL REFERENCES spotify.reviews,
    severity       smallint,
    evidence_quote text,
    PRIMARY KEY (issue_id, ordinal)
);

-- Run-level facts copied verbatim from saved receipts (verification, sensitivity,
-- grouping comparison, cost/runtime). Each row names its source file.
CREATE TABLE spotify.run_facts (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    source_file text NOT NULL
);

CREATE INDEX membership_issue_idx ON spotify.issue_membership (issue_id);

-- The aggregates below are MATERIALIZED VIEWS: PostgreSQL computes them from the
-- stored `reviews` and `issue_membership` rows when db/load.py refreshes them after
-- loading, so the API reads small precomputed relations instead of scanning
-- 660,622 rows on every request. The loader then checks them against the export.

-- Baseline ranking recomputed from processed data:
--   priority_score = severity_sum = complaint_count * mean_severity
-- Completed complaint/cancellation records only; ties broken by issue ID.
-- numeric round() rounds half away from zero, i.e. half-up for these positive means.
CREATE MATERIALIZED VIEW spotify.issue_ranking AS
WITH agg AS (
    SELECT m.issue_id,
           count(*)::integer        AS complaint_count,
           sum(r.severity)::integer AS severity_sum
    FROM spotify.issue_membership m
    JOIN spotify.reviews r USING (review_id)
    WHERE r.status = 'completed' AND r.intent IN ('complaint', 'cancellation')
    GROUP BY m.issue_id
)
SELECT row_number() OVER (ORDER BY a.severity_sum DESC, a.issue_id ASC)::integer AS rank,
       a.issue_id,
       a.complaint_count,
       a.severity_sum,
       to_char(round(a.severity_sum::numeric / a.complaint_count, 6), 'FM999990.000000') AS mean_severity,
       a.severity_sum AS priority_score
FROM agg a
WITH NO DATA;

-- Topic rollup of the same baseline (an analytical view, not a claims metric).
CREATE MATERIALIZED VIEW spotify.topic_rollup AS
WITH agg AS (
    SELECT i.topic,
           sum(ir.complaint_count)::integer AS complaint_count,
           sum(ir.severity_sum)::integer    AS severity_sum,
           count(*)::integer                AS issue_count
    FROM spotify.issue_ranking ir
    JOIN spotify.issues i USING (issue_id)
    GROUP BY i.topic
)
SELECT row_number() OVER (ORDER BY severity_sum DESC, topic ASC)::integer AS rank,
       topic, issue_count, complaint_count, severity_sum,
       to_char(round(severity_sum::numeric / complaint_count, 6), 'FM999990.000000') AS mean_severity,
       severity_sum AS priority_score
FROM agg
WITH NO DATA;

-- Monthly volume and complaint share (first and last calendar months are partial).
CREATE MATERIALIZED VIEW spotify.monthly_trend AS
SELECT to_char(date_trunc('month', review_timestamp), 'YYYY-MM') AS month,
       count(*)::integer AS source_rows,
       count(*) FILTER (WHERE status = 'completed')::integer AS completed,
       count(*) FILTER (WHERE status = 'completed' AND intent IN ('complaint', 'cancellation'))::integer AS complaint_or_cancellation
FROM spotify.reviews
GROUP BY 1
ORDER BY 1
WITH NO DATA;

CREATE UNIQUE INDEX issue_ranking_issue_idx ON spotify.issue_ranking (issue_id);

-- Corpus-level counts shown on the dashboard and by /api/health.
CREATE MATERIALIZED VIEW spotify.corpus_counts AS
SELECT (SELECT count(*) FROM spotify.reviews)::integer AS source_rows,
       (SELECT count(*) FROM spotify.reviews WHERE status = 'completed')::integer AS completed,
       (SELECT count(*) FROM spotify.reviews WHERE status = 'quarantined')::integer AS quarantined,
       (SELECT count(*) FROM spotify.reviews WHERE cache_source_id IS NOT NULL)::integer AS exact_text_reused_rows,
       (SELECT count(*) FROM spotify.reviews
         WHERE status = 'completed' AND intent IN ('complaint', 'cancellation'))::integer AS complaint_or_cancellation,
       (SELECT count(*) FROM spotify.issue_membership)::integer AS issue_memberships,
       (SELECT count(*) FROM spotify.issues)::integer AS issues
WITH NO DATA;

-- Final label source per completed row (Luna teacher vs local classifier).
CREATE MATERIALIZED VIEW spotify.label_source_counts AS
SELECT lc.label_source, lc.model, count(*)::integer AS n
FROM spotify.reviews r JOIN spotify.label_configs lc USING (label_config_id)
GROUP BY lc.label_source, lc.model
WITH NO DATA;

-- Member ordering for issue drill-downs: a stable pseudo-random sample order
-- (md5 of the review ID) and a severity order, so paging never sorts a whole issue.
CREATE MATERIALIZED VIEW spotify.issue_member_order AS
SELECT m.issue_id,
       m.review_id,
       r.severity,
       row_number() OVER (PARTITION BY m.issue_id ORDER BY md5(m.review_id::text))::integer AS sample_order,
       row_number() OVER (PARTITION BY m.issue_id ORDER BY r.severity DESC, m.review_id)::integer AS severity_order
FROM spotify.issue_membership m
JOIN spotify.reviews r USING (review_id)
WITH NO DATA;

CREATE INDEX member_sample_idx ON spotify.issue_member_order (issue_id, sample_order);
CREATE INDEX member_severity_idx ON spotify.issue_member_order (issue_id, severity_order);
