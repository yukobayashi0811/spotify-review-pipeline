// One issue: metadata, recomputed metrics, severity mix, memo evidence, and member reviews.
import { db, route, ISSUE_ID } from './_lib/db.js';

export default route(async (query) => {
  const id = String(query.id || '');
  if (!ISSUE_ID.test(id)) return { __status: 400, error: 'invalid_issue_id' };
  const limit = Math.min(Math.max(parseInt(query.limit, 10) || 20, 1), 100);
  const order = query.order === 'severity' ? 'severity' : 'sample';
  const offset = Math.max(parseInt(query.offset, 10) || 0, 0);
  const sql = db();
  const [issue] = await sql`
    SELECT i.*, ir.rank, ir.complaint_count, ir.severity_sum, ir.mean_severity, ir.priority_score
    FROM spotify.issues i LEFT JOIN spotify.issue_ranking ir USING (issue_id)
    WHERE i.issue_id = ${id}`;
  if (!issue) return { __status: 404, error: 'issue_not_found' };
  const severity = await sql`
    SELECT severity, count(*)::int AS n
    FROM spotify.issue_member_order
    WHERE issue_id = ${id} GROUP BY severity ORDER BY severity`;
  const evidence = await sql`
    SELECT e.ordinal, e.review_id, e.severity, e.evidence_quote, r.review_text, r.intent, r.review_timestamp
    FROM spotify.memo_evidence e JOIN spotify.reviews r USING (review_id)
    WHERE e.issue_id = ${id} ORDER BY e.ordinal`;
  const rankColumn = order === 'severity' ? sql`o.severity_order` : sql`o.sample_order`;
  const members = await sql`
    SELECT r.review_id, r.severity, r.intent, r.sentiment, r.evidence_quote, r.review_text,
           r.review_rating, r.review_timestamp, lc.label_source, r.cache_source_id
    FROM spotify.issue_member_order o
    JOIN spotify.reviews r USING (review_id)
    LEFT JOIN spotify.label_configs lc USING (label_config_id)
    WHERE o.issue_id = ${id} AND ${rankColumn} > ${offset} AND ${rankColumn} <= ${offset + limit}
    ORDER BY ${rankColumn}`;
  return { issue, severity_distribution: severity, memo_evidence: evidence, members, order, limit, offset };
});
