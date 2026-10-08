// One processed review record with its provenance and issue membership.
import { db, route, UUID } from './_lib/db.js';

export default route(async (query) => {
  const id = String(query.id || '');
  if (!UUID.test(id)) return { __status: 400, error: 'invalid_review_id' };
  const sql = db();
  const [review] = await sql`
    SELECT r.review_id, r.source_sha256, r.status, r.quarantine_reason, r.topic, r.intent, r.sentiment,
           r.severity, r.needs_review, r.entities, r.evidence_quote, r.cache_source_id, r.review_text,
           r.review_rating, r.review_likes, r.app_version, r.review_timestamp,
           lc.label_source, lc.model, lc.label_config,
           m.issue_id, i.issue_name
    FROM spotify.reviews r
    LEFT JOIN spotify.label_configs lc USING (label_config_id)
    LEFT JOIN spotify.issue_membership m USING (review_id)
    LEFT JOIN spotify.issues i USING (issue_id)
    WHERE r.review_id = ${id}`;
  if (!review) return { __status: 404, error: 'review_not_found' };
  return { review };
});
