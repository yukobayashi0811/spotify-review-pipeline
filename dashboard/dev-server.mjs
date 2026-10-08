// Local stand-in for Vercel: serves public/ and routes /api/<name> to api/<name>.js.
// Usage: DATABASE_URL=... DATABASE_SSL=disable node dev-server.mjs  (port 3000)
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
const port = Number(process.env.PORT || 3000);

http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    const api = url.pathname.match(/^\/api\/([a-z]+)$/);
    if (api) {
      const mod = await import(join(root, 'api', `${api[1]}.js`));
      req.query = Object.fromEntries(url.searchParams);
      await mod.default(req, res);
      return;
    }
    const path = url.pathname === '/' ? '/index.html' : url.pathname;
    const file = normalize(join(root, 'public', path));
    if (!file.startsWith(join(root, 'public'))) throw Object.assign(new Error('forbidden'), { code: 'ENOENT' });
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch (error) {
    res.writeHead(error.code === 'ENOENT' || error.code === 'ERR_MODULE_NOT_FOUND' ? 404 : 500);
    res.end('not found');
  }
}).listen(port, () => console.log(`http://localhost:${port}`));
