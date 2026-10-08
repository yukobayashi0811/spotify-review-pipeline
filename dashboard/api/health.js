// Connectivity and row-count check (not cached).
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const [row] = await sql`
    SELECT now() AS checked_at, source_rows AS reviews, issue_memberships AS memberships, issues
    FROM spotify.corpus_counts`;
  return { ok: true, database: 'postgresql', commit: process.env.VERCEL_GIT_COMMIT_SHA || null, ...row };
}, { cacheSeconds: 0 });
