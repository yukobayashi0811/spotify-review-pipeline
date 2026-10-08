// Shared database access for the serverless API.
// The connection string comes only from the DATABASE_URL environment variable
// (set in Vercel project settings); it is never sent to the browser.
import postgres from 'postgres';

let client;

export function db() {
  if (!client) {
    const raw = process.env.DATABASE_URL;
    if (!raw) throw new Error('DATABASE_URL is not configured');
    // postgres.js forwards unknown URL query parameters to the server as settings,
    // so drop hints meant for other clients (e.g. Supabase's Vercel "workaround" flag).
    const url = new URL(raw);
    for (const key of ['workaround', 'pgbouncer', 'supa']) url.searchParams.delete(key);
    client = postgres(url.toString(), {
      ssl: process.env.DATABASE_SSL === 'disable' ? false : 'require',
      prepare: false, // required by Supabase's transaction pooler
      max: 1,
      idle_timeout: 20,
      connect_timeout: 10,
      types: { bigint: postgres.BigInt }, // never silently lose precision
    });
  }
  return client;
}

// Converts BigInt values (if any) to Numbers when safe, otherwise to strings.
function plain(value) {
  return JSON.parse(
    JSON.stringify(value, (_, v) =>
      typeof v === 'bigint' ? (Number.isSafeInteger(Number(v)) ? Number(v) : v.toString()) : v,
    ),
  );
}

export function route(fn, { cacheSeconds = 300 } = {}) {
  return async (req, res) => {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    if (req.method !== 'GET') {
      res.statusCode = 405;
      res.end(JSON.stringify({ error: 'method_not_allowed' }));
      return;
    }
    try {
      const body = await fn(req.query || {});
      res.setHeader('Cache-Control', `public, s-maxage=${cacheSeconds}, stale-while-revalidate=86400`);
      res.statusCode = body && body.__status ? body.__status : 200;
      if (body && body.__status) delete body.__status;
      res.end(JSON.stringify(plain(body)));
    } catch (error) {
      console.error(error);
      res.statusCode = 500;
      res.end(JSON.stringify({ error: 'server_error', detail: error.code || 'unexpected' }));
    }
  };
}

export const ISSUE_ID = /^I_[A-Z]+_\d{2}$/;
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
