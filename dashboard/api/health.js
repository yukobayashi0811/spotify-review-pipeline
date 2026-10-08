// Connectivity and row-count check (not cached).
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const [row] = await sql`
    SELECT now() AS checked_at,
           (SELECT count(*)::int FROM spotify.reviews) AS reviews,
           (SELECT count(*)::int FROM spotify.issue_membership) AS memberships,
           (SELECT count(*)::int FROM spotify.issues) AS issues`;
  return { ok: true, database: 'postgresql', ...row };
}, { cacheSeconds: 0 });
