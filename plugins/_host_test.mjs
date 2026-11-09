/**
 * Offline harness for the localgen-studio Host half.
 *
 * Builds a fake Cordis context that records webServer routes, then drives each
 * route with minimal req/res doubles against the live isolated LocalGen.
 */
import { apply } from './localgen-studio/index.js';

const routes = new Map();

const webServer = {
  register(route) {
    routes.set(route.path, route.handler);
    return () => routes.delete(route.path);
  },
};

const settings = new Map();
const credentials = {
  async readRecord(key) {
    return settings.get(key);
  },
  async modifyRecord(key, mutate) {
    const next = await mutate(settings.get(key));
    if (next === undefined) return settings.get(key);
    settings.set(key, next);
    return next;
  },
};

const ctx = {
  inject(_names, callback) {
    callback(webCtx);
  },
  get(name) {
    return name === 'credentials' ? credentials : undefined;
  },
};

const webCtx = {
  webServer,
  effect(callback) {
    return callback();
  },
};

apply(ctx, {});

function res() {
  const out = { status: 0, headers: {}, body: null };
  const handle = {
    writeHead(status, headers) {
      out.status = status;
      out.headers = headers || {};
    },
    end(buffer) {
      out.body = buffer === undefined ? null : buffer;
      handle.done();
    },
  };
  let resolve;
  const finished = new Promise((r) => { resolve = r; });
  handle.done = resolve;
  return { res: handle, out, finished };
}

function req({ method = 'GET', url = '/', headers = {}, body = null } = {}) {
  const chunks = body === null ? [] : [Buffer.isBuffer(body) ? body : Buffer.from(body)];
  return {
    method,
    url,
    headers,
    [Symbol.asyncIterator]() {
      let i = 0;
      return {
        async next() {
          if (i >= chunks.length) return { done: true, value: undefined };
          return { done: false, value: chunks[i++] };
        },
      };
    },
  };
}

async function hit(path, options) {
  // The real webServer matches on pathname; the query string stays in req.url.
  const pathname = path.split('?')[0];
  const handler = routes.get(pathname);
  if (!handler) throw new Error(`no route ${pathname}`);
  const { res: response, out, finished } = res();
  await handler(req(options), response);
  await finished;
  const text = out.body === null ? '' : out.body.toString('utf8');
  let json = null;
  try { json = JSON.parse(text); } catch { /* binary */ }
  return { status: out.status, json, text, headers: out.headers, bytes: out.body ? out.body.length : 0 };
}

const TOKEN = process.argv[2];
const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
}

check('routes registered', routes.size === 5, [...routes.keys()].join(', '));

// 1. state before token
let r = await hit('/plugins/localgen-studio/state');
check('state reports missing token', r.status === 200 && r.json.tokenConfigured === false, JSON.stringify(r.json));

// 2. rpc without token → 428
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ path: '/api/v1/libraries', method: 'GET' }),
});
check('rpc without token → 428 TOKEN_MISSING', r.status === 428 && r.json.error.code === 'TOKEN_MISSING', `status=${r.status}`);

// 3. save config (credentials path)
r = await hit('/plugins/localgen-studio/config', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ baseUrl: 'http://127.0.0.1:18793', token: TOKEN }),
});
check('config saved', r.status === 200 && r.json.tokenConfigured === true, `stored=${r.json && r.json.stored}`);

// 4. reject non-local base url
r = await hit('/plugins/localgen-studio/config', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ baseUrl: 'https://evil.example.com' }),
});
check('non-local base URL rejected', r.status === 400, r.status + ' ' + r.text.slice(0, 80));

// 5. reject path outside /api/v1
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ path: '/health', method: 'GET' }),
});
check('non /api/v1 path rejected', r.status === 400, r.status);

// 6. reject traversal
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ path: '/api/v1/../secret', method: 'GET' }),
});
check('traversal rejected', r.status === 400, r.status);

// 7. cross-origin rejected
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST',
  headers: { 'content-type': 'application/json', host: '127.0.0.1:56420', origin: 'http://evil.example.com' },
  body: JSON.stringify({ path: '/api/v1/libraries', method: 'GET' }),
});
check('cross-origin rejected', r.status === 403, r.status);

// 8. libraries proxy
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ path: '/api/v1/libraries', method: 'GET' }),
});
check('libraries proxy', r.status === 200 && r.json.ok && r.json.data.poses.count === 1439,
  JSON.stringify(r.json && r.json.data));

// 9. pose catalog with filters
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ path: '/api/v1/libraries/poses?limit=3&query=站姿', method: 'GET' }),
});
const firstPose = r.json && r.json.data && r.json.data.items && r.json.data.items[0];
check('pose catalog paged', r.status === 200 && r.json.data.items.length === 3,
  `total=${r.json && r.json.data && r.json.data.total} first=${firstPose && firstPose.id}`);
check('pose item has angle fields',
  Boolean(firstPose && firstPose.code && firstPose.angle && firstPose.title),
  firstPose ? `${firstPose.code}/${firstPose.angle}/${firstPose.title}` : 'none');
check('pose catalog exposes collections',
  Array.isArray(r.json.data.collections) && r.json.data.collections.length > 0,
  JSON.stringify(r.json.data.collections));

// 10. garment catalog
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({ path: '/api/v1/libraries/garments?limit=2', method: 'GET' }),
});
const firstGarment = r.json.data.items[0];
check('garment catalog', r.status === 200 && r.json.data.items.length === 2,
  `total=${r.json.data.total} first=${firstGarment.id}/${firstGarment.description}`);

// 11. thumbnail via raw route (binary)
r = await hit('/plugins/localgen-studio/raw?path=' + encodeURIComponent(`/api/v1/libraries/poses/${encodeURIComponent(firstPose.id)}/thumb`),
  { headers: { host: '127.0.0.1:56420' } });
check('pose thumb raw bytes', r.status === 200 && r.bytes > 1000 && /image\//.test(r.headers['content-type'] || ''),
  `${r.bytes} bytes ${r.headers['content-type']}`);

// 12. garment thumb
r = await hit('/plugins/localgen-studio/raw?path=' + encodeURIComponent(`/api/v1/libraries/garments/${encodeURIComponent(firstGarment.id)}/thumb`),
  { headers: { host: '127.0.0.1:56420' } });
check('garment thumb raw bytes', r.status === 200 && r.bytes > 1000, `${r.bytes} bytes ${r.headers['content-type']}`);

// 13. upload reference image through the multipart proxy
const png = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAF0lEQVR42mNkYPhfz0BEwDgqMPwVAgB2lAX9V7m4WQAAAABJRU5ErkJggg==',
  'base64');
r = await hit('/plugins/localgen-studio/upload?filename=probe.png', {
  method: 'POST', body: png,
  headers: { host: '127.0.0.1:56420', 'content-type': 'image/png' },
});
check('upload proxy stages reference', r.status === 200 && r.json.ok && /^[0-9a-f]{32}\.png$/.test(r.json.data.name),
  JSON.stringify(r.json && r.json.data));
const imageToken = r.json && r.json.data && r.json.data.name;

// 14. /api/v1/edit routing (semantic endpoint; engine is ready so expect 202 or a routed error)
r = await hit('/plugins/localgen-studio/rpc', {
  method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
  body: JSON.stringify({
    path: '/api/v1/edit', method: 'POST',
    body: { prompt: 'probe routing only', image: imageToken, pose: firstPose.id, size: [768, 768] },
  }),
});
const editData = r.json && r.json.data;
check('edit routes to pose-swap', Boolean(editData && (editData.routed_to === 'pose-swap')),
  JSON.stringify(editData && { routed: editData.routed_to, status: editData.status, job: editData.job_id, code: editData.error && editData.error.code }));

// 15. status poll through the proxy
if (editData && editData.job_id) {
  r = await hit('/plugins/localgen-studio/rpc', {
    method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
    body: JSON.stringify({ path: `/api/v1/jobs/${editData.job_id}`, method: 'GET' }),
  });
  check('job poll', r.status === 200 && r.json.data.job_id === editData.job_id,
    `status=${r.json.data.status} progress=${JSON.stringify(r.json.data.progress)}`);
  // cancel so the isolated engine is free for the next step
  r = await hit('/plugins/localgen-studio/rpc', {
    method: 'POST', headers: { 'content-type': 'application/json', host: '127.0.0.1:56420' },
    body: JSON.stringify({ path: `/api/v1/jobs/${editData.job_id}/cancel`, method: 'POST' }),
  });
  check('cancel works', r.status === 200, `status=${r.json.data && r.json.data.status}`);
}

const failed = results.filter((item) => !item.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exitCode = failed.length ? 1 : 0;
