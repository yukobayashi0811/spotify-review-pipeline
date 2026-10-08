// Overall metrics, read from the database (processed records) and run_facts (saved receipts).
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const status = await sql`SELECT status, count(*)::int AS n FROM spotify.reviews GROUP BY status`;
  const sources = await sql`
    SELECT lc.label_source, lc.model, count(*)::int AS n
    FROM spotify.reviews r JOIN spotify.label_configs lc USING (label_config_id)
    GROUP BY lc.label_source, lc.model ORDER BY n DESC`;
  const [{ membership, complaints, reused }] = await sql`
    SELECT (SELECT count(*)::int FROM spotify.issue_membership) AS membership,
           (SELECT count(*)::int FROM spotify.reviews
              WHERE status = 'completed' AND intent IN ('complaint','cancellation')) AS complaints,
           (SELECT count(*)::int FROM spotify.reviews WHERE cache_source_id IS NOT NULL) AS reused`;
  const issues = await sql`
    SELECT count(*)::int AS total,
           count(*) FILTER (WHERE coherent)::int AS coherent,
           count(*) FILTER (WHERE coherent AND actionable)::int AS coherent_actionable
    FROM spotify.issues`;
  const facts = await sql`SELECT key, value, source_file FROM spotify.run_facts`;
  const f = Object.fromEntries(facts.map((r) => [r.key, { value: r.value, source_file: r.source_file }]));
  const counts = Object.fromEntries(status.map((r) => [r.status, r.n]));
  return {
    counts: {
      source_rows: (counts.completed || 0) + (counts.quarantined || 0),
      completed: counts.completed || 0,
      quarantined: counts.quarantined || 0,
      exact_text_reused_rows: reused,
      complaint_or_cancellation: complaints,
      issue_memberships: membership,
    },
    label_sources: sources,
    issues: issues[0],
    source: f.source,
    ingestion: f.ingestion,
    verification: f.verification,
    sensitivity: f.sensitivity && {
      value: { rank_stability: f.sensitivity.value.rank_stability, method: f.sensitivity.value.method },
      source_file: f.sensitivity.source_file,
    },
    grouping_comparison: f.grouping_comparison,
    system_tests: f.system_tests,
    interrupt_resume: f.interrupt_resume,
    cost_pilot: f.cost_pilot,
    cost_projection: f.cost_projection,
    golden_evaluation: f.golden_evaluation || null,
  };
});
