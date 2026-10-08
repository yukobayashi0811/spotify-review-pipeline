// Topic rollup of the baseline ranking, plus the saved sensitivity estimates per topic.
import { db, route } from './_lib/db.js';

export default route(async () => {
  const sql = db();
  const topics = await sql`SELECT * FROM spotify.topic_rollup ORDER BY rank`;
  const [fact] = await sql`SELECT value, source_file FROM spotify.run_facts WHERE key = 'sensitivity'`;
  return {
    topics,
    sensitivity: fact ? { topics: fact.value.topics, rank_stability: fact.value.rank_stability, source_file: fact.source_file } : null,
  };
});
