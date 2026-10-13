// Overall metrics, read from the database (processed records) and run_facts (saved receipts).
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const [counts] = await sql`
    SELECT source_rows, completed, quarantined, exact_text_reused_rows,
           complaint_or_cancellation, issue_memberships
    FROM spotify.corpus_counts`;
  const sources = await sql`
    SELECT label_source, model, n FROM spotify.label_source_counts ORDER BY n DESC`;
  const issues = await sql`
    SELECT count(*)::int AS total,
           count(*) FILTER (WHERE coherent)::int AS coherent,
           count(*) FILTER (WHERE coherent AND actionable)::int AS coherent_actionable
    FROM spotify.issues`;
  const facts = await sql`SELECT key, value, source_file FROM spotify.run_facts`;
  const f = Object.fromEntries(facts.map((r) => [r.key, { value: r.value, source_file: r.source_file }]));
  return {
    counts,
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
    holdout_evaluation: f.holdout_evaluation || null,
    full_run_luna_usage: f.full_run_luna_usage || null,
  };
});
