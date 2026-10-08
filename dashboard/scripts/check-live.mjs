// Verifies a running dashboard against the committed grading export. Zero model calls.
//
//   node dashboard/scripts/check-live.mjs https://<your-app>.vercel.app
//
// Fetches /api/health, /api/overview, /api/ranking and /api/recommendation, compares
// every ranking row and every memo claim with grading/ranking.csv and grading/claims.csv,
// and writes artifacts/dashboard/live_check.json (run from the repository root).
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const base = (process.argv[2] || '').replace(/\/$/, '');
if (!/^https?:\/\//.test(base)) {
  console.error('Usage: node dashboard/scripts/check-live.mjs <dashboard base URL>');
  process.exit(2);
}
const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function parseCsv(text) {
  const [header, ...lines] = text.trim().split(/\r?\n/);
  const keys = header.split(',');
  return lines.map((line) => Object.fromEntries(line.split(',').map((v, i) => [keys[i], v])));
}
async function get(path) {
  const started = Date.now();
  const res = await fetch(`${base}/api/${path}`, { headers: { Accept: 'application/json' } });
  const body = await res.json();
  return { status: res.status, ms: Date.now() - started, body };
}

const savedRanking = parseCsv(await readFile(join(root, 'grading', 'ranking.csv'), 'utf8'));
const savedClaims = parseCsv(await readFile(join(root, 'grading', 'claims.csv'), 'utf8'));
const fields = ['rank', 'issue_id', 'complaint_count', 'severity_sum', 'mean_severity', 'priority_score'];

const [health, overview, ranking, reco, page] = await Promise.all([
  get('health'), get('overview'), get('ranking'), get('recommendation'),
  fetch(base).then(async (r) => ({ status: r.status, html: await r.text() })),
]);

const liveRanking = ranking.body.issues.map((r) => Object.fromEntries(fields.map((k) => [k, String(r[k])])));
const rankingMismatches = savedRanking
  .map((row, i) => ({ row, live: liveRanking[i] }))
  .filter(({ row, live }) => !live || fields.some((k) => row[k] !== live[k]))
  .map(({ row }) => row.issue_id);

const byIssue = Object.fromEntries(liveRanking.map((r) => [r.issue_id, r]));
const claimResults = savedClaims.map((c) => {
  const live = reco.body.claims.find((x) => x.claim_id === c.claim_id);
  return {
    claim_id: c.claim_id,
    expected: c.value,
    displayed_value: live?.value ?? null,
    database_value: live?.db_value ?? null,
    ranking_value: byIssue[c.issue_id]?.[c.metric] ?? null,
    passed: live?.value === c.value && live?.db_value === c.value && byIssue[c.issue_id]?.[c.metric] === c.value,
  };
});

const counts = overview.body.counts || {};
const checks = {
  page_served: { status: page.status, passed: page.status === 200 && page.html.includes('/app.js') },
  health: { status: health.status, body: health.body, passed: health.status === 200 && health.body.ok === true },
  counts_from_database: {
    actual: counts,
    passed: counts.source_rows === 660622 && counts.completed === 660609 && counts.quarantined === 13 && counts.issue_memberships === 288524,
  },
  ranking_equals_grading_ranking_csv: { issues: liveRanking.length, mismatches: rankingMismatches, passed: liveRanking.length === savedRanking.length && rankingMismatches.length === 0 },
  memo_claims_equal_claims_csv_and_database: { claims: claimResults, passed: claimResults.every((c) => c.passed) },
  recommendation_present: { priority_issue_id: reco.body.memo?.priority_issue_id, passed: Boolean(reco.body.memo?.priority) },
};
const passed = Object.values(checks).every((c) => c.passed);
const receipt = {
  status: passed ? 'pass' : 'fail',
  checked_at_utc: new Date().toISOString(),
  dashboard_url: base,
  deployed_commit: health.body.commit ?? null,
  model_calls: 0,
  response_ms: { health: health.ms, overview: overview.ms, ranking: ranking.ms, recommendation: reco.ms },
  checks,
};
const out = join(root, 'artifacts', 'dashboard', 'live_check.json');
await mkdir(dirname(out), { recursive: true });
await writeFile(out, `${JSON.stringify(receipt, null, 2)}\n`);
for (const [name, check] of Object.entries(checks)) console.log(`${check.passed ? 'PASS' : 'FAIL'}  ${name}`);
console.log(`Receipt: ${out}`);
process.exit(passed ? 0 : 1);
