// The AI-generated recommendation (memo role output) with every cited number
// recomputed from the database and the cited review evidence attached.
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const [memo] = await sql`
    SELECT memo_id, priority_issue_id, priority_topic, priority, rationale, baseline_top_handling,
           sensitivity_interpretation, alternative_issue_ids, alternatives, limitations, model,
           label_config, request_id, markdown
    FROM spotify.memo WHERE memo_id = 'final'`;
  if (!memo) return { __status: 404, error: 'memo_not_loaded' };
  const claims = await sql`
    SELECT c.claim_id, c.issue_id, c.metric, c.value, c.sentence, c.review_ids,
           c.provenance_source, c.provenance_reason, c.request_id,
           CASE c.metric
             WHEN 'complaint_count' THEN ir.complaint_count::text
             WHEN 'severity_sum'    THEN ir.severity_sum::text
             WHEN 'mean_severity'   THEN ir.mean_severity
             WHEN 'priority_score'  THEN ir.priority_score::text
           END AS db_value
    FROM spotify.claims c LEFT JOIN spotify.issue_ranking ir USING (issue_id)
    ORDER BY c.claim_id`;
  const issueIds = [memo.priority_issue_id, ...memo.alternative_issue_ids];
  const issues = await sql`
    SELECT i.issue_id, i.topic, i.issue_name, i.description, i.coherent, i.actionable,
           ir.rank, ir.complaint_count, ir.severity_sum, ir.mean_severity, ir.priority_score
    FROM spotify.issues i JOIN spotify.issue_ranking ir USING (issue_id)
    WHERE i.issue_id = ANY(${issueIds})`;
  const reviewIds = [...new Set(claims.flatMap((c) => c.review_ids))];
  const evidence = reviewIds.length
    ? await sql`
        SELECT r.review_id, r.review_text, r.evidence_quote, r.severity, r.intent, m.issue_id
        FROM spotify.reviews r LEFT JOIN spotify.issue_membership m USING (review_id)
        WHERE r.review_id = ANY(${reviewIds}::uuid[])`
    : [];
  return {
    memo,
    claims: claims.map((c) => ({ ...c, matches_database: c.db_value === c.value })),
    all_claims_match_database: claims.every((c) => c.db_value === c.value),
    issues,
    evidence,
    provenance_note:
      'Generated once by the memo role from saved aggregates and a bounded evidence pack; issue IDs and numbers were validated by code. This page makes no model calls.',
  };
});
