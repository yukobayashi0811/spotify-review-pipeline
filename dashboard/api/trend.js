// Monthly review volume and complaint/cancellation share, computed from processed records.
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const months = await sql`SELECT * FROM spotify.monthly_trend`;
  return {
    note: 'First (2022-05) and last (2023-11) calendar months are partial windows.',
    months: months.map((m) => ({
      ...m,
      complaint_share: m.completed ? m.complaint_or_cancellation / m.completed : null,
    })),
  };
});
