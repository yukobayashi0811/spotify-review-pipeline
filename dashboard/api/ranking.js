// Baseline issue ranking recomputed in the database from reviews + issue_membership,
// compared field by field with the committed export (grading/ranking.csv).
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const rows = await sql`
    SELECT ir.rank, ir.issue_id, ir.complaint_count, ir.severity_sum, ir.mean_severity, ir.priority_score,
           i.topic, i.issue_name, i.description, i.coherent, i.actionable, i.flags,
           (sr.rank = ir.rank AND sr.complaint_count = ir.complaint_count AND sr.severity_sum = ir.severity_sum
            AND sr.mean_severity = ir.mean_severity AND sr.priority_score = ir.priority_score) AS matches_saved_export
    FROM spotify.issue_ranking ir
    JOIN spotify.issues i USING (issue_id)
    LEFT JOIN spotify.saved_ranking sr USING (issue_id)
    ORDER BY ir.rank`;
  return {
    rule: 'priority_score = severity_sum = complaint_count x mean_severity; completed complaint/cancellation records; ties by issue_id',
    all_match_saved_export: rows.every((r) => r.matches_saved_export === true),
    issues: rows,
  };
});
