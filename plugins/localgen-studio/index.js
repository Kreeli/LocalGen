// localgen-studio — Host half.
//
// The LocalGen service (127.0.0.1:12138) guards every request with
// `LocalSecurity`: the Host header must be one of its allowed_hosts *with its
// own port*, and any Origin must match too. The Harness Web UI is served from a
// different port, so a browser fetch straight to LocalGen is rejected with 403
// LOCAL_POLICY_DENIED. Every call therefore goes through this Host half, which
// adds the Bearer token and returns only JSON or bytes to the page.
//
// Routes (all same-origin under /plugins/localgen-studio/):
//   GET  /state   → { baseUrl, tokenConfigured, source, version }
//   POST /config  → { baseUrl?, token? } save settings
//   POST /rpc     → { path, method?, body? } JSON proxy into /api/v1
//   GET  /raw     → ?path=… binary proxy (thumbnails, artifacts) for <img src>
//   POST /upload  → ?filename=… raw bytes → multipart POST /api/v1/images

import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs/promises';
import { spawn } from 'node:child_process';

export const name = 'localgen-studio';

/** `webServer` carries the routes; without it the plugin simply stays idle. */
export const inject = ['webServer'];

const PREFIX = '/plugins/localgen-studio';
const ROUTE_STATE = `${PREFIX}/state`;
const ROUTE_CONFIG = `${PREFIX}/config`;
const ROUTE_RPC = `${PREFIX}/rpc`;
const ROUTE_RAW = `${PREFIX}/raw`;
const ROUTE_UPLOAD = `${PREFIX}/upload`;
const ROUTE_OPEN = `${PREFIX}/open`;
const ROUTE_OPEN_IMAGE = `${PREFIX}/open-image`;

const DEFAULT_BASE = 'http://127.0.0.1:12138';
const DEFAULT_EXE = 'D:\\garbage pile\\local_image\\dist\\LocalGen\\LocalGen.exe';
const RECORD_KEY = 'localgen-studio/default';
const JSON_LIMIT = 2 * 2 ** 20;
const UPLOAD_LIMIT = 64 * 2 ** 20;
const RPC_TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 300_000;
const RAW_TIMEOUT_MS = 60_000;

// ── settings ────────────────────────────────────────────────────────────────
// The token is a secret: prefer the credentials service (read-modify-write),
// fall back to a 0600 JSON file so a profile without that service still works.

function settingsFile() {
  const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
  return path.join(home, 'localgen-studio', 'settings.json');
}

async function readSettingsFile() {
  try {
    const raw = await fs.readFile(settingsFile(), 'utf8');
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

async function writeSettingsFile(patch) {
  const file = settingsFile();
  await fs.mkdir(path.dirname(file), { recursive: true });
  const current = await readSettingsFile();
  const next = { ...current, ...patch };
  if (next.token === '' || next.token === null) delete next.token;
  await fs.writeFile(file, JSON.stringify(next, null, 2), { encoding: 'utf8', mode: 0o600 });
  return next;
}

async function readStored(ctx) {
  const credentials = ctx.get('credentials');
  let credentialToken;
  let credentialBase;
  if (credentials) {
    try {
      const record = await credentials.readRecord(RECORD_KEY);
      if (record && record.kind === 'api-key') {
        credentialToken = typeof record.key === 'string' && record.key.trim() ? record.key.trim() : undefined;
        credentialBase = record.env && typeof record.env.LOCALGEN_BASE_URL === 'string'
          ? record.env.LOCALGEN_BASE_URL.trim() || undefined
          : undefined;
      }
    } catch {
      // fall through to the file
    }
  }
  const file = await readSettingsFile();
  const fileToken = typeof file.token === 'string' && file.token.trim() ? file.token.trim() : undefined;
  const fileBase = typeof file.baseUrl === 'string' && file.baseUrl.trim() ? file.baseUrl.trim() : undefined;
  const fileExe = typeof file.exePath === 'string' && file.exePath.trim() ? file.exePath.trim() : undefined;
  // Credentials (when present) wins for baseUrl, but a token missing there
  // falls back to the file so a baseUrl-only update never loses the token.
  const token = credentialToken || fileToken;
  const baseUrl = credentialBase || fileBase;
  return { token, baseUrl, exePath: fileExe, source: (credentialToken || credentialBase) ? 'credentials' : 'file' };
}

async function writeStored(ctx, { token, baseUrl, exePath }) {
  // An empty string token means "leave unchanged", not "clear": the UI only
  // sends a non-empty token when the user actually typed one. This prevents a
  // baseUrl-only edit from wiping a previously stored token.
  const tokenGiven = typeof token === 'string' && token.trim() !== '';
  const credentials = ctx.get('credentials');
  if (credentials) {
    try {
      await credentials.modifyRecord(RECORD_KEY, async (current) => {
        const env = { ...(current && current.env ? current.env : {}) };
        if (baseUrl !== undefined) env.LOCALGEN_BASE_URL = baseUrl;
        const existingKey = current && typeof current.key === 'string' ? current.key : '';
        const key = tokenGiven ? token.trim() : existingKey;
        if (!key) return Object.keys(env).length ? { kind: 'api-key', env } : undefined;
        return { kind: 'api-key', key, env };
      });
      // Keep the file fallback in sync (never write an empty token).
      await writeSettingsFile({
        ...(baseUrl !== undefined ? { baseUrl } : {}),
        ...(tokenGiven ? { token: token.trim() } : {}),
        ...(exePath !== undefined ? { exePath } : {}),
      });
      return 'credentials';
    } catch {
      // fall through to the file
    }
  }
  await writeSettingsFile({
    ...(baseUrl !== undefined ? { baseUrl } : {}),
    ...(tokenGiven ? { token: token.trim() } : {}),
    ...(exePath !== undefined ? { exePath } : {}),
  });
  return 'file';
}

async function resolve(ctx) {
  const envToken = (process.env.LOCALGEN_TOKEN || '').trim();
  const envBase = (process.env.LOCALGEN_BASE_URL || '').trim();
  const stored = await readStored(ctx);
  const token = envToken || stored.token;
  const baseUrl = (envBase || stored.baseUrl || DEFAULT_BASE).replace(/\/+$/, '');
  return {
    token,
    baseUrl,
    exePath: stored.exePath,
    source: envToken ? 'env' : stored.source,
    tokenConfigured: Boolean(token),
  };
}

// ── http plumbing ───────────────────────────────────────────────────────────

async function readBody(req, limit) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw Object.assign(new Error('request body too large'), { status: 413 });
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8');
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
    'cache-control': 'no-store',
  });
  res.end(body);
}

function sendText(res, status, message) {
  sendJson(res, status, { error: { message } });
}

/** Reject cross-origin callers: this Host half fronts a token-bearing API. */
function sameOrigin(req) {
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    return new URL(origin).host === req.headers.host;
  } catch {
    return false;
  }
}

/** Only the read/write surface of LocalGen's own REST API may be proxied. */
function safeApiPath(value) {
  if (typeof value !== 'string' || !value.startsWith('/api/v1/')) return null;
  if (value.includes('..') || value.includes('\\') || value.includes('\n')) return null;
  return value;
}

function authHeaders(token, extra) {
  return { authorization: `Bearer ${token}`, ...(extra || {}) };
}

async function proxyJson({ baseUrl, token }, apiPath, method, body) {
  const headers = authHeaders(token, body === undefined ? {} : { 'content-type': 'application/json' });
  const response = await fetch(baseUrl + apiPath, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: { message: text.slice(0, 800) } };
    }
  }
  return { status: response.status, ok: response.ok, data };
}

// ── plugin ──────────────────────────────────────────────────────────────────

export function apply(ctx) {
  ctx.inject(['webServer'], (web) => {
    const route = (routePath, handler, label) =>
      web.effect(() => web.webServer.register({ kind: 'exact', path: routePath, handler }), label);

    route(ROUTE_STATE, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'GET') return sendText(res, 405, 'method not allowed');
      try {
        const resolved = await resolve(ctx);
        sendJson(res, 200, {
          version: '0.2.0',
          baseUrl: resolved.baseUrl,
          exePath: resolved.exePath || DEFAULT_EXE,
          tokenConfigured: resolved.tokenConfigured,
          tokenSource: resolved.source,
        });
      } catch (error) {
        sendText(res, 500, String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: state route');

    route(ROUTE_CONFIG, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'POST') return sendText(res, 405, 'method not allowed');
      try {
        const body = JSON.parse((await readBody(req, JSON_LIMIT)).toString('utf8') || '{}');
        const patch = {};
        if (typeof body.baseUrl === 'string') {
          const value = body.baseUrl.trim();
          if (value && !/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i.test(value)) {
            return sendText(res, 400, 'base URL 必须是本机 http(s) 地址，例如 http://127.0.0.1:12138');
          }
          patch.baseUrl = value || DEFAULT_BASE;
        }
        if (typeof body.token === 'string') patch.token = body.token.trim();
        if (typeof body.exePath === 'string') patch.exePath = body.exePath.trim();
        const stored = await writeStored(ctx, patch);
        const resolved = await resolve(ctx);
        sendJson(res, 200, {
          stored,
          baseUrl: resolved.baseUrl,
          exePath: resolved.exePath || DEFAULT_EXE,
          tokenConfigured: resolved.tokenConfigured,
          tokenSource: resolved.source,
        });
      } catch (error) {
        sendText(res, error && error.status === 413 ? 413 : 400,
          String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: config route');

    route(ROUTE_OPEN, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'POST') return sendText(res, 405, 'method not allowed');
      try {
        const { baseUrl, exePath } = await resolve(ctx);
        try {
          const probe = await fetch(baseUrl + '/api/v1/health', { signal: AbortSignal.timeout(2000) });
          if (probe.ok) {
            return sendJson(res, 200, { status: 'running', message: 'LocalGen 已在运行', baseUrl });
          }
        } catch {
          // not reachable — fall through to spawn
        }
        const exe = exePath || DEFAULT_EXE;
        try {
          await fs.access(exe);
        } catch {
          return sendJson(res, 404, { status: 'missing', message: '未找到 LocalGen 程序：' + exe });
        }
        const child = spawn(exe, [], { detached: true, stdio: 'ignore', windowsHide: false });
        child.on('error', () => {});
        child.unref();
        return sendJson(res, 200, { status: 'launched', message: '已启动 LocalGen：' + exe });
      } catch (error) {
        sendText(res, 500, String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: open route');

    // Download one artifact through the token'd proxy into a temp file and hand
    // it to the OS default viewer. The browser cannot launch a local app, and
    // <a download> would only save it, so this runs on the Host side.
    route(ROUTE_OPEN_IMAGE, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'POST') return sendText(res, 405, 'method not allowed');
      try {
        const body = JSON.parse((await readBody(req, JSON_LIMIT)).toString('utf8') || '{}');
        const apiPath = safeApiPath(body.path);
        if (!apiPath) return sendText(res, 400, '只允许打开 /api/v1/ 下的图片');
        const { token, baseUrl } = await resolve(ctx);
        if (!token) return sendJson(res, 428, { error: { code: 'TOKEN_MISSING', message: '还没有配置 LocalGen 访问令牌' } });
        const upstream = await fetch(baseUrl + apiPath, {
          headers: authHeaders(token),
          signal: AbortSignal.timeout(RAW_TIMEOUT_MS),
        });
        if (!upstream.ok) {
          return sendJson(res, 502, { error: { message: '下载图片失败：HTTP ' + upstream.status } });
        }
        const bytes = Buffer.from(await upstream.arrayBuffer());
        const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'localgen-studio-'));
        const name = (typeof body.name === 'string' && /^[\w.-]{1,80}$/.test(body.name)) ? body.name : 'localgen.png';
        const file = path.join(dir, name);
        await fs.writeFile(file, bytes);
        const child = spawn(process.env.COMSPEC || 'cmd.exe', ['/c', 'start', '', file], {
          detached: true, stdio: 'ignore', windowsHide: true,
        });
        child.on('error', () => {});
        child.unref();
        return sendJson(res, 200, { status: 'opened', file });
      } catch (error) {
        sendText(res, 500, String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: open-image route');

    route(ROUTE_RPC, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'POST') return sendText(res, 405, 'method not allowed');
      try {
        const body = JSON.parse((await readBody(req, JSON_LIMIT)).toString('utf8') || '{}');
        const apiPath = safeApiPath(body.path);
        if (!apiPath) return sendText(res, 400, '只允许代理 /api/v1/ 下的只读或生成接口');
        const method = String(body.method || 'GET').toUpperCase();
        if (!['GET', 'POST', 'PUT', 'DELETE'].includes(method)) return sendText(res, 400, '不支持的请求方法');
        const { token, baseUrl } = await resolve(ctx);
        if (!token) {
          return sendJson(res, 428, {
            error: {
              code: 'TOKEN_MISSING',
              message: '还没有配置 LocalGen 访问令牌，请先在面板顶部展开设置填入。',
              retryable: false,
            },
          });
        }
        const result = await proxyJson({ baseUrl, token }, apiPath, method, body.body);
        sendJson(res, 200, result);
      } catch (error) {
        const status = error && error.status ? error.status : 502;
        sendText(res, status, String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: rpc route');

    route(ROUTE_RAW, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'GET') return sendText(res, 405, 'method not allowed');
      try {
        const url = new URL(req.url || '', 'http://127.0.0.1');
        const apiPath = safeApiPath(url.searchParams.get('path'));
        if (!apiPath) return sendText(res, 400, '只允许读取 /api/v1/ 下的资源');
        const { token, baseUrl } = await resolve(ctx);
        if (!token) return sendText(res, 428, '还没有配置 LocalGen 访问令牌');
        const upstream = await fetch(baseUrl + apiPath, {
          headers: authHeaders(token),
          signal: AbortSignal.timeout(RAW_TIMEOUT_MS),
        });
        const buffer = Buffer.from(await upstream.arrayBuffer());
        res.writeHead(upstream.status, {
          'content-type': upstream.headers.get('content-type') || 'application/octet-stream',
          'content-length': buffer.length,
          'cache-control': apiPath.includes('/thumb')
            ? 'private, max-age=86400'
            : 'no-store',
          'x-content-type-options': 'nosniff',
        });
        res.end(buffer);
      } catch (error) {
        sendText(res, 502, String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: raw route');

    route(ROUTE_UPLOAD, async (req, res) => {
      if (!sameOrigin(req)) return sendText(res, 403, 'origin rejected');
      if (req.method !== 'POST') return sendText(res, 405, 'method not allowed');
      try {
        const url = new URL(req.url || '', 'http://127.0.0.1');
        const filename = (url.searchParams.get('filename') || 'upload.png').slice(0, 120);
        const { token, baseUrl } = await resolve(ctx);
        if (!token) return sendJson(res, 428, { error: { code: 'TOKEN_MISSING', message: '还没有配置 LocalGen 访问令牌' } });
        const bytes = await readBody(req, UPLOAD_LIMIT);
        if (!bytes.length) return sendText(res, 400, '上传内容为空');
        const form = new FormData();
        form.append('image', new Blob([bytes], { type: req.headers['content-type'] || 'application/octet-stream' }), filename);
        const upstream = await fetch(baseUrl + '/api/v1/images', {
          method: 'POST',
          headers: authHeaders(token),
          body: form,
          signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
        });
        const text = await upstream.text();
        let data = null;
        try { data = JSON.parse(text); } catch { data = { error: { message: text.slice(0, 800) } }; }
        sendJson(res, 200, { status: upstream.status, ok: upstream.ok, data });
      } catch (error) {
        const status = error && error.status === 413 ? 413 : 502;
        sendText(res, status, String(error && error.message ? error.message : error));
      }
    }, 'localgen-studio: upload route');
  });
}
