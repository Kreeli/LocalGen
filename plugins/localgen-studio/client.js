window.__ModuleLoader__.load({
  id: 'localgen-studio',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useCallback, useEffect, useMemo, useRef, useState } = React;

    const PREFIX = '/plugins/localgen-studio';
    const ROUTE_STATE = `${PREFIX}/state`;
    const ROUTE_CONFIG = `${PREFIX}/config`;
    const ROUTE_RPC = `${PREFIX}/rpc`;
    const ROUTE_RAW = `${PREFIX}/raw`;
    const ROUTE_UPLOAD = `${PREFIX}/upload`;
const ROUTE_OPEN = `${PREFIX}/open`;

    const PAGE_SIZE = 60;
    const POLL_MS = 2500;
    const TERMINAL = ['succeeded', 'failed', 'cancelled', 'interrupted'];
    const ACTIVE = ['queued', 'submitting', 'running', 'cancelling'];

    // ── transport ───────────────────────────────────────────────────────────
    // Every request is same-origin: the Host half adds the Bearer token and
    // talks to LocalGen, whose Origin allowlist would reject this page directly.

    async function rpc(path, method, body) {
      const response = await fetch(ROUTE_RPC, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path, method: method || 'GET', body }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const message = payload && payload.error ? payload.error.message : `HTTP ${response.status}`;
        throw new Error(message);
      }
      return payload;
    }

    async function callApi(path, method, body) {
      const result = await rpc(path, method, body);
      if (!result.ok) {
        const detail = result.data && result.data.error ? result.data.error : null;
        const message = detail ? detail.message || detail.code : `HTTP ${result.status}`;
        const error = new Error(message);
        error.code = detail && detail.code;
        error.routedTo = detail && detail.routed_to;
        error.routeReason = detail && detail.route_reason;
        throw error;
      }
      return result.data;
    }

    function rawUrl(apiPath) {
      return `${ROUTE_RAW}?path=${encodeURIComponent(apiPath)}`;
    }

    async function uploadReference(file) {
      const response = await fetch(`${ROUTE_UPLOAD}?filename=${encodeURIComponent(file.name || 'reference.png')}`, {
        method: 'POST',
        headers: { 'content-type': file.type || 'application/octet-stream' },
        body: await file.arrayBuffer(),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const message = payload && payload.error ? payload.error.message : `HTTP ${response.status}`;
        throw new Error(message);
      }
      if (!payload.ok) {
        const detail = payload.data && payload.data.error ? payload.data.error : null;
        throw new Error(detail ? detail.message || detail.code : '上传失败');
      }
      return payload.data;
    }

    // Open the LocalGen desktop app: host probes health first, only spawns
    // when nothing is listening (InstanceLock would make a second launch fail).
    async function openLocalGen() {
      const response = await fetch(ROUTE_OPEN, { method: 'POST' });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error((data && data.error && data.error.message) || ('HTTP ' + response.status));
      return data;
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    function sizeKey(width, height) {
      return `${width}x${height}`;
    }
    function parseSizeKey(key) {
      const parts = String(key || '').split('x');
      const w = parseInt(parts[0], 10);
      const h = parseInt(parts[1], 10);
      if (!Number.isInteger(w) || !Number.isInteger(h)) return null;
      return [w, h];
    }
    function fmtTime(iso) {
      if (!iso) return '—';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return '—';
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    function fmtElapsed(seconds) {
      if (!Number.isFinite(seconds)) return '';
      const s = Math.max(0, Math.round(seconds));
      const m = Math.floor(s / 60);
      const r = s % 60;
      return m > 0 ? `${m} 分 ${r} 秒` : `${r} 秒`;
    }
    const STATUS_TEXT = {
      queued: '排队中', submitting: '提交中', running: '运行中', cancelling: '取消中',
      succeeded: '成功', failed: '失败', cancelled: '已取消', interrupted: '已中断',
    };
    const STATUS_COLOR = {
      queued: 'var(--dsw-alias-state-idle-primary)',
      submitting: 'var(--dsw-alias-state-warn-primary)',
      running: 'var(--dsw-alias-state-warn-primary)',
      cancelling: 'var(--dsw-alias-state-warn-primary)',
      succeeded: 'var(--dsw-alias-state-success-primary)',
      failed: 'var(--dsw-alias-state-error-primary)',
      cancelled: 'var(--dsw-alias-label-secondary)',
      interrupted: 'var(--dsw-alias-state-error-primary)',
    };

    // ── styles ──────────────────────────────────────────────────────────────

    const CSS = `
.lgs-root { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--dsw-alias-bg-base); color: var(--dsw-alias-label-primary); font-size: 13px; }
.lgs-head { display: flex; align-items: center; gap: 10px; padding: 12px 18px; border-bottom: 1px solid var(--dsw-alias-border-l1); flex: none; }
.lgs-head h1 { font-size: 15px; font-weight: 600; margin: 0; }
.lgs-head .lgs-sub { color: var(--dsw-alias-label-secondary); font-size: 12px; }
.lgs-head .lgs-spacer { flex: 1; }
.lgs-body { flex: 1; min-height: 0; overflow: auto; padding: 16px 18px 40px; }
.lgs-tabs { display: flex; gap: 4px; padding: 10px 18px 0; border-bottom: 1px solid var(--dsw-alias-border-l1); flex: none; }
.lgs-tab { border: 1px solid transparent; border-bottom: none; background: transparent; color: var(--dsw-alias-label-secondary); border-radius: 8px 8px 0 0; padding: 8px 16px; font-size: 13px; cursor: pointer; }
.lgs-tab[data-on="1"] { color: var(--dsw-alias-label-primary); background: var(--dsw-alias-bg-layer-1); border-color: var(--dsw-alias-border-l1); font-weight: 600; }
.lgs-grid2 { display: grid; grid-template-columns: minmax(0, 340px) minmax(0, 1fr); gap: 18px; align-items: start; }
@media (max-width: 900px) { .lgs-grid2 { grid-template-columns: minmax(0, 1fr); } }
.lgs-card { border: 1px solid var(--dsw-alias-border-l1); border-radius: 12px; background: var(--dsw-alias-bg-layer-1); padding: 14px; }
.lgs-card + .lgs-card { margin-top: 14px; }
.lgs-card h2 { font-size: 12px; font-weight: 600; color: var(--dsw-alias-label-secondary); margin: 0 0 10px; letter-spacing: .04em; }
.lgs-btn { border: 1px solid var(--dsw-alias-border-l2); background: var(--dsw-alias-bg-layer-2); color: var(--dsw-alias-label-primary); border-radius: 8px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
.lgs-btn:hover:not(:disabled) { border-color: var(--dsw-alias-brand-primary); }
.lgs-btn:disabled { opacity: .45; cursor: not-allowed; }
.lgs-btn-primary { background: var(--dsw-alias-brand-primary); border-color: var(--dsw-alias-brand-primary); color: #fff; font-weight: 600; padding: 9px 16px; width: 100%; font-size: 13px; }
.lgs-btn-primary:hover:not(:disabled) { filter: brightness(1.08); }
.lgs-btn-danger { color: var(--dsw-alias-state-error-primary); border-color: var(--dsw-alias-state-error-primary); }
.lgs-btn-danger:hover:not(:disabled) { background: var(--dsw-alias-state-error-primary); color: #fff; }
.lgs-slot { display: flex; align-items: center; gap: 10px; }
.lgs-slot .lgs-thumb { width: 54px; height: 72px; border-radius: 8px; object-fit: cover; background: var(--dsw-alias-bg-layer-2); border: 1px solid var(--dsw-alias-border-l1); flex: none; }
.lgs-slot .lgs-meta { flex: 1; min-width: 0; }
.lgs-slot .lgs-name { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lgs-slot .lgs-hint { font-size: 11.5px; color: var(--dsw-alias-label-secondary); margin-top: 2px; }
.lgs-input, .lgs-select, .lgs-textarea { width: 100%; box-sizing: border-box; background: var(--dsw-alias-bg-layer-2); color: var(--dsw-alias-label-primary); border: 1px solid var(--dsw-alias-border-l1); border-radius: 8px; padding: 7px 10px; font-size: 12.5px; font-family: inherit; }
.lgs-textarea { resize: vertical; min-height: 76px; line-height: 1.6; }
.lgs-input:focus, .lgs-select:focus, .lgs-textarea:focus { outline: none; border-color: var(--dsw-alias-brand-primary); }
.lgs-row { display: flex; gap: 8px; }
.lgs-row > * { min-width: 0; }
.lgs-label { font-size: 11.5px; color: var(--dsw-alias-label-secondary); margin: 10px 0 5px; display: block; }
.lgs-note { font-size: 11.5px; color: var(--dsw-alias-label-secondary); line-height: 1.6; }
.lgs-warn { color: var(--dsw-alias-state-warn-primary); }
.lgs-error { color: var(--dsw-alias-state-error-primary); font-size: 12px; line-height: 1.6; word-break: break-word; }
.lgs-ok { color: var(--dsw-alias-state-success-primary); }
.lgs-progress { height: 6px; border-radius: 3px; background: var(--dsw-alias-bg-layer-2); overflow: hidden; margin: 10px 0 6px; }
.lgs-progress > i { display: block; height: 100%; background: var(--dsw-alias-brand-primary); transition: width .4s ease; }
.lgs-result { margin-top: 12px; border-radius: 10px; overflow: hidden; border: 1px solid var(--dsw-alias-border-l1); background: var(--dsw-alias-bg-layer-2); }
.lgs-result img { display: block; width: 100%; height: auto; }
.lgs-overlay { position: fixed; inset: 0; z-index: 900; background: rgba(0,0,0,.55); display: flex; align-items: center; justify-content: center; padding: 24px; }
.lgs-modal { width: min(1180px, 96vw); height: min(860px, 92vh); display: flex; flex-direction: column; background: var(--dsw-alias-bg-overlay); border: 1px solid var(--dsw-alias-border-l2); border-radius: 14px; overflow: hidden; box-shadow: 0 24px 64px rgba(0,0,0,.4); }
.lgs-modal-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--dsw-alias-border-l1); flex: none; }
.lgs-modal-head h3 { margin: 0; font-size: 14px; font-weight: 600; }
.lgs-filters { display: flex; gap: 8px; padding: 10px 16px; border-bottom: 1px solid var(--dsw-alias-border-l1); flex: none; flex-wrap: wrap; }
.lgs-filters .lgs-input { flex: 2 1 220px; width: auto; }
.lgs-filters .lgs-select { flex: 1 1 130px; width: auto; }
.lgs-scroll { flex: 1; min-height: 0; overflow: auto; padding: 14px 16px 24px; }
.lgs-cards { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
@media (max-width: 1100px) { .lgs-cards { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 820px) { .lgs-cards { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.lgs-tile { border: 1px solid var(--dsw-alias-border-l1); border-radius: 9px; overflow: hidden; background: var(--dsw-alias-bg-layer-1); cursor: pointer; display: flex; flex-direction: column; }
.lgs-tile:hover { border-color: var(--dsw-alias-brand-primary); }
.lgs-tile[data-active="1"] { border-color: var(--dsw-alias-brand-primary); box-shadow: 0 0 0 1px var(--dsw-alias-brand-primary) inset; }
.lgs-tile img { display: block; width: 100%; aspect-ratio: 3 / 4; object-fit: cover; background: var(--dsw-alias-bg-layer-2); }
.lgs-tile .lgs-cap { padding: 5px 7px; font-size: 11px; line-height: 1.4; color: var(--dsw-alias-label-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lgs-tile .lgs-angles { display: flex; gap: 2px; padding: 0 6px 6px; flex-wrap: wrap; }
.lgs-angle { font-size: 10px; padding: 1px 5px; border-radius: 5px; border: 1px solid var(--dsw-alias-border-l1); background: transparent; color: var(--dsw-alias-label-secondary); cursor: pointer; }
.lgs-angle[data-on="1"] { background: var(--dsw-alias-brand-primary); border-color: var(--dsw-alias-brand-primary); color: #fff; }
.lgs-empty { padding: 40px 0; text-align: center; color: var(--dsw-alias-label-secondary); font-size: 12.5px; }
.lgs-pill { font-size: 11px; padding: 2px 7px; border-radius: 999px; border: 1px solid var(--dsw-alias-border-l1); color: var(--dsw-alias-label-secondary); }
/* history */
.lgs-hist { display: flex; flex-direction: column; gap: 10px; }
.lgs-hist-item { display: flex; gap: 12px; border: 1px solid var(--dsw-alias-border-l1); border-radius: 10px; padding: 10px; background: var(--dsw-alias-bg-layer-1); cursor: pointer; align-items: flex-start; }
.lgs-hist-item:hover { border-color: var(--dsw-alias-brand-primary); }
.lgs-hist-thumb { width: 64px; height: 64px; border-radius: 8px; object-fit: cover; background: var(--dsw-alias-bg-layer-2); border: 1px solid var(--dsw-alias-border-l1); flex: none; }
.lgs-hist-thumb.empty { display: flex; align-items: center; justify-content: center; color: var(--dsw-alias-label-secondary); font-size: 10px; text-align: center; }
.lgs-hist-main { flex: 1; min-width: 0; }
.lgs-hist-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.lgs-hist-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.lgs-hist-wf { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.lgs-hist-time { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.lgs-hist-prompt { font-size: 12px; margin-top: 6px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.lgs-hist-actions { display: flex; gap: 6px; flex: none; flex-wrap: wrap; align-items: flex-start; }
.lgs-detail-row { display: flex; gap: 10px; padding: 4px 0; font-size: 12px; border-bottom: 1px dashed var(--dsw-alias-border-l1); }
.lgs-detail-row .k { flex: none; width: 110px; color: var(--dsw-alias-label-secondary); }
.lgs-detail-row .v { flex: 1; min-width: 0; word-break: break-word; }
.lgs-detail-ref { margin-top: 10px; }
.lgs-detail-ref img { max-width: 160px; max-height: 160px; border-radius: 8px; border: 1px solid var(--dsw-alias-border-l1); }
/* settings */
.lgs-settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
@media (max-width: 720px) { .lgs-settings-grid { grid-template-columns: minmax(0, 1fr); } }
.lgs-field { display: flex; flex-direction: column; gap: 4px; }
.lgs-field label { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.lgs-field .hint { font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.lgs-req-restart { color: var(--dsw-alias-state-warn-primary); }
.lgs-ok-line { font-size: 11.5px; color: var(--dsw-alias-state-success-primary); }
.lgs-status { margin: 0 18px 10px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--dsw-alias-border-l1); background: var(--dsw-alias-bg-layer-1); font-size: 12px; display: flex; align-items: center; gap: 8px; }
.lgs-status-active { border-color: var(--dsw-alias-brand-primary); }
.lgs-status-idle { color: var(--dsw-alias-label-secondary); }
`;

    // ── sidebar glyph ───────────────────────────────────────────────────────

    function StudioGlyph({ size = 18 } = {}) {
      const s = typeof size === 'number' && size > 0 ? size : 18;
      return h('svg', {
        width: s, height: s, viewBox: '0 0 24 24', fill: 'none',
        stroke: 'currentColor', strokeWidth: 1.6,
        strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true,
      },
      h('rect', { x: 3, y: 3, width: 18, height: 18, rx: 4 }),
      h('circle', { cx: 12, cy: 8.4, r: 2.1 }),
      h('path', { d: 'M12 10.5v4.2M12 14.7l-2.6 3.4M12 14.7l2.6 3.4M8.2 12.2h7.6' })
      );
    }

    // ── picker modal ────────────────────────────────────────────────────────

    function groupPoses(items, previous) {
      const map = new Map();
      for (const group of previous) {
        map.set(group.code, { ...group, angles: group.angles.slice() });
      }
      for (const item of items) {
        const code = item.code || item.id;
        let group = map.get(code);
        if (!group) {
          group = {
            code,
            title: item.title || item.id,
            collection: item.collection || '',
            category: item.category || '',
            angles: [],
          };
          map.set(code, group);
        }
        if (!group.angles.some((angle) => angle.id === item.id)) {
          group.angles.push({
            id: item.id,
            angle: item.angle || '',
            thumb: item.thumb || item.image,
            image: item.image,
          });
        }
      }
      return Array.from(map.values());
    }

    function Picker({ kind, selectedId, onPick, onClose }) {
      const [query, setQuery] = useState('');
      const [category, setCategory] = useState('');
      const [collection, setCollection] = useState('');
      const [groups, setGroups] = useState([]);
      const [flat, setFlat] = useState([]);
      const [categories, setCategories] = useState([]);
      const [collections, setCollections] = useState([]);
      const [total, setTotal] = useState(0);
      const [offset, setOffset] = useState(0);
      const [loading, setLoading] = useState(false);
      const [error, setError] = useState('');
      const [angleOf, setAngleOf] = useState({});
      const scrollRef = useRef(null);
      const generation = useRef(0);

      const load = useCallback(async (nextOffset, replace) => {
        const token = ++generation.current;
        setLoading(true);
        setError('');
        try {
          const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(nextOffset) });
          if (query.trim()) params.set('query', query.trim());
          if (category) params.set('category', category);
          if (collection && kind === 'poses') params.set('collection', collection);
          const data = await callApi(`/api/v1/libraries/${kind}?${params.toString()}`);
          if (token !== generation.current) return;
          const items = Array.isArray(data.items) ? data.items : [];
          setTotal(data.total || 0);
          setCategories(Array.isArray(data.categories) ? data.categories : []);
          setCollections(Array.isArray(data.collections) ? data.collections : []);
          setOffset(nextOffset + items.length);
          if (kind === 'poses') {
            setGroups((previous) => groupPoses(items, replace ? [] : previous));
          } else {
            setFlat((previous) => (replace ? items : previous.concat(items)));
          }
        } catch (failure) {
          if (token === generation.current) setError(failure.message || String(failure));
        } finally {
          if (token === generation.current) setLoading(false);
        }
      }, [kind, query, category, collection]);

      useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = 0;
        load(0, true);
      }, [load]);

      const onScroll = () => {
        const node = scrollRef.current;
        if (!node || loading) return;
        const shown = kind === 'poses' ? groups.length : flat.length;
        if (node.scrollTop + node.clientHeight >= node.scrollHeight - 320 && shown < total) {
          load(offset, false);
        }
      };

      const title = kind === 'poses' ? '选择姿势' : '选择服装';

      return h('div', { className: 'lgs-overlay', role: 'dialog', 'aria-label': title, onMouseDown: (event) => { if (event.target === event.currentTarget) onClose(); } },
        h('div', { className: 'lgs-modal' },
          h('div', { className: 'lgs-modal-head' },
            h('h3', null, title),
            h('span', { className: 'lgs-pill' }, `${total} 条`),
            h('span', { className: 'lgs-spacer', style: { flex: 1 } }),
            h('button', { className: 'lgs-btn', onClick: onClose }, '关闭')
          ),
          h('div', { className: 'lgs-filters' },
            h('input', {
              className: 'lgs-input',
              placeholder: kind === 'poses' ? '搜索姿势名 / 编号 / 分类…' : '搜索服装描述 / 分类…',
              value: query,
              onChange: (event) => setQuery(event.target.value),
            }),
            h('select', { className: 'lgs-select', value: category, onChange: (event) => setCategory(event.target.value) },
              h('option', { value: '' }, '全部分类'),
              categories.map((item) => h('option', { key: item, value: item }, item))
            ),
            kind === 'poses' && collections.length > 0
              ? h('select', { className: 'lgs-select', value: collection, onChange: (event) => setCollection(event.target.value) },
                h('option', { value: '' }, '全部体系'),
                collections.map((item) => h('option', { key: item, value: item }, item)))
              : null
          ),
          h('div', { className: 'lgs-scroll', ref: scrollRef, onScroll },
            error ? h('div', { className: 'lgs-error', style: { padding: '18px 0' } }, error) : null,
            kind === 'poses'
              ? h('div', { className: 'lgs-cards' }, groups.map((group) => {
                const activeAngle = angleOf[group.code] || (group.angles.find((angle) => angle.id === selectedId) || group.angles[0] || {}).id;
                const chosen = group.angles.find((angle) => angle.id === activeAngle) || group.angles[0];
                if (!chosen) return null;
                return h('div', {
                  key: group.code,
                  className: 'lgs-tile',
                  'data-active': selectedId === chosen.id ? '1' : '0',
                  onClick: () => onPick({ id: chosen.id, title: group.title, angle: chosen.angle, category: group.category, collection: group.collection, thumb: chosen.thumb }),
                },
                h('img', { loading: 'lazy', alt: group.title, src: rawUrl(`/api/v1/libraries/poses/${encodeURIComponent(chosen.id)}/thumb`) }),
                h('div', { className: 'lgs-cap', title: `${group.title} · ${chosen.angle}` }, `${group.title}${chosen.angle ? ` · ${chosen.angle}` : ''}`),
                group.angles.length > 1
                  ? h('div', { className: 'lgs-angles' }, group.angles.map((angle) => h('button', {
                    key: angle.id,
                    className: 'lgs-angle',
                    'data-on': angle.id === chosen.id ? '1' : '0',
                    onClick: (event) => {
                      event.stopPropagation();
                      setAngleOf((previous) => ({ ...previous, [group.code]: angle.id }));
                    },
                  }, angle.angle || angle.id)))
                  : null
                );
              }))
              : h('div', { className: 'lgs-cards' }, flat.map((item) => h('div', {
                key: item.id,
                className: 'lgs-tile',
                'data-active': selectedId === item.id ? '1' : '0',
                onClick: () => onPick({ id: item.id, description: item.description || item.id, category: item.category_cn || item.category || '', image: item.image }),
              },
              h('img', { loading: 'lazy', alt: item.description || item.id, src: rawUrl(`/api/v1/libraries/garments/${encodeURIComponent(item.id)}/thumb`) }),
              h('div', { className: 'lgs-cap', title: item.description }, `${item.category_cn ? `${item.category_cn} · ` : ''}${item.description || item.id}`)
              ))),
            !error && !loading && ((kind === 'poses' && groups.length === 0) || (kind === 'garments' && flat.length === 0))
              ? h('div', { className: 'lgs-empty' }, '没有匹配的素材')
              : null,
            loading ? h('div', { className: 'lgs-empty' }, '加载中…') : null
          )
        )
      );
    }

    // ── shared hook: connection state ───────────────────────────────────────

    function useConnection() {
      const [state, setState] = useState(null);
      const refresh = useCallback(async () => {
        try {
          const response = await fetch(ROUTE_STATE, { cache: 'no-store' });
          const data = await response.json();
          setState(data);
          return data;
        } catch (failure) {
          const fallback = { tokenConfigured: false, baseUrl: '', error: String(failure && failure.message ? failure.message : failure) };
          setState(fallback);
          return fallback;
        }
      }, []);
      useEffect(() => { refresh(); }, [refresh]);
      return { state, refresh };
    }

    // ── generate panel ──────────────────────────────────────────────────────

    /**
     * Controlled generate form. Parent (Workbench / StudioPage) owns the
     * generation state so the history panel can back-fill it via `fillRef`.
     */
    function GeneratePanel({ state, fillRef, onJobsChanged, pendingFill, onFillConsumed, onSubmitted }) {
      const [workflows, setWorkflows] = useState([]);
      const [workflow, setWorkflow] = useState('');
      const [workflowError, setWorkflowError] = useState('');
      const [workflowLoading, setWorkflowLoading] = useState(false);

      const loadWorkflows = useCallback(async () => {
        if (!state || !state.tokenConfigured) return;
        setWorkflowLoading(true);
        setWorkflowError('');
        try {
          const data = await callApi('/api/v1/workflows');
          const items = Array.isArray(data.items) ? data.items : [];
          setWorkflows(items);
          const ids = items.map((item) => item.workflow_id);
          if (!ids.length) {
            setWorkflowError('服务返回了空的工作流列表');
            return;
          }
          setWorkflow((current) => {
            if (ids.includes(current)) return current;
            return ids.includes('qwen-image-21') ? 'qwen-image-21' : ids[0];
          });
        } catch (failure) {
          const code = failure.code ? `（${failure.code}）` : '';
          setWorkflowError(`${failure.message || failure}${code}`);
        } finally {
          setWorkflowLoading(false);
        }
      }, [state]);

      useEffect(() => {
        loadWorkflows();
      }, [loadWorkflows]);
      const [size, setSize] = useState('');
      const [seed, setSeed] = useState('');
      const [negative, setNegative] = useState('');
      const [steps, setSteps] = useState('');
      const [cfg, setCfg] = useState('');
      const [prompt, setPrompt] = useState('');

      const [pose, setPose] = useState(null);
      const [garment, setGarment] = useState(null);
      const [picker, setPicker] = useState(null);
      const [reference, setReference] = useState(null);
      const [strength, setStrength] = useState('');
      const [uploading, setUploading] = useState(false);

      const [submitting, setSubmitting] = useState(false);
      const [jobId, setJobId] = useState(null);
      const [job, setJob] = useState(null);
      const [error, setError] = useState('');
      const [startedAt, setStartedAt] = useState(null);
      const fileRef = useRef(null);

      const currentWorkflow = workflows.find((item) => item.workflow_id === workflow) || null;
      const sizes = currentWorkflow ? (currentWorkflow.verified_sizes || []).map(([w, h]) => [w, h]) : [];
      const defaultSize = currentWorkflow ? currentWorkflow.default_size : null;
      const engineParams = currentWorkflow ? currentWorkflow.engine_parameters || {} : {};
      const templateSteps = engineParams.steps;
      const templateCfg = engineParams.cfg_scale;
      const supports = currentWorkflow ? currentWorkflow.supported_parameters || [] : [];
      const required = currentWorkflow ? currentWorkflow.required_parameters || [] : [];
      const supportsImage = supports.includes('image');
      const supportsPose = supports.includes('pose_id');
      const supportsGarment = supports.includes('garment_id');
      const supportsNegative = supports.includes('negative_prompt');
      const supportsStrength = supports.includes('strength');
      // A workflow that REQUIRES a reference image (kontext-dev). "Supports"
      // image (moss/qwen) does NOT make it mandatory.
      const requiredImage = required.includes('image');

      // when workflow changes, pick its default size if the current one is invalid
      useEffect(() => {
        if (!currentWorkflow) return;
        const validKeys = sizes.map(([w, h]) => sizeKey(w, h));
        if (!validKeys.includes(size)) {
          const [dw, dh] = currentWorkflow.default_size || sizes[0] || [0, 0];
          setSize(sizeKey(dw, dh));
        }
      }, [workflow]);

      // back-fill from history (externally driven)
      const applyFill = useCallback((fill) => {
        if (!fill) return;
        if (fill.workflow_id) {
          setWorkflow(fill.workflow_id);
        }
        if (fill.prompt != null) setPrompt(String(fill.prompt));
        if (fill.width && fill.height) setSize(sizeKey(fill.width, fill.height));
        if (fill.seed != null) setSeed(String(fill.seed));
        if (fill.negative_prompt != null) setNegative(String(fill.negative_prompt || ''));
        if (fill.steps != null) setSteps(String(fill.steps));
        if (fill.cfg_scale != null) setCfg(String(fill.cfg_scale));
        if (fill.strength != null) setStrength(String(fill.strength));
        if (fill.pose) setPose(fill.pose);
        if (fill.garment) setGarment(fill.garment);
        if (fill.imageToken != null) {
          setReference({ name: fill.imageToken, label: fill.imageToken, preview: null });
        }
      }, []);

      useEffect(() => {
        if (fillRef) fillRef.current = applyFill;
      }, [applyFill, fillRef]);

      // Consume a pending fill passed from the parent (history reuse) once,
      // after workflows are loaded so workflow_id/size validation can run.
      useEffect(() => {
        if (!pendingFill) return;
        if (!workflows.length) return;
        const { __rerun, ...fill } = pendingFill;
        applyFill(fill);
        if (onFillConsumed) onFillConsumed();
        if (__rerun) {
          const wfInfo = workflows.find((item) => item.workflow_id === fill.workflow_id) || null;
          doSubmit(buildBodyFromFill(fill, wfInfo));
        }
      }, [pendingFill, workflows.length]);

      // poll active job
      useEffect(() => {
        if (!jobId) return undefined;
        let cancelled = false;
        let timer = null;
        const tick = async () => {
          try {
            const data = await callApi(`/api/v1/jobs/${jobId}`);
            if (cancelled) return;
            setJob(data);
            if (onJobsChanged) onJobsChanged();
            if (TERMINAL.includes(data.status)) return;
            timer = setTimeout(tick, POLL_MS);
          } catch (failure) {
            if (!cancelled) {
              setError(String(failure && failure.message ? failure.message : failure));
              timer = setTimeout(tick, POLL_MS * 2);
            }
          }
        };
        tick();
        return () => { cancelled = true; if (timer) clearTimeout(timer); };
      }, [jobId]);

      const onPickFile = async (event) => {
        const file = event.target.files && event.target.files[0];
        event.target.value = '';
        if (!file) return;
        setUploading(true);
        setError('');
        try {
          const data = await uploadReference(file);
          const preview = URL.createObjectURL(file);
          setReference({ name: data.name, label: file.name || data.name, preview });
        } catch (failure) {
          setError(String(failure && failure.message ? failure.message : failure));
        } finally {
          setUploading(false);
        }
      };

      const clearReference = () => {
        if (reference && reference.preview) URL.revokeObjectURL(reference.preview);
        setReference(null);
      };

      // client-side validation of steps / cfg
      const stepsError = useMemo(() => {
        if (steps === '') return '';
        const value = Number(steps);
        if (!Number.isInteger(value)) return '步数必须是整数';
        if (value < 1 || value > 25) return '步数范围 1–25';
        return '';
      }, [steps]);
      const cfgError = useMemo(() => {
        if (cfg === '') return '';
        const value = Number(cfg);
        if (!Number.isFinite(value)) return 'CFG 必须是数字';
        if (value < 1 || value > 15) return 'CFG 范围 1–15';
        return '';
      }, [cfg]);

      // Force a reference only when the workflow REQUIRES image (kontext-dev),
      // or when it has pose/garment inputs (pose-swap) which need at least one
      // of image/pose/garment. Pure text-to-image (moss/qwen/sd15/…) needs none.
      const needsRefInput = requiredImage || supportsPose || supportsGarment;
      const canGenerate = Boolean(prompt.trim())
        && Boolean(workflow)
        && (!needsRefInput || Boolean(reference || pose || garment))
        && !submitting && state && state.tokenConfigured
        && !stepsError && !cfgError;

      const disabledReason = useMemo(() => {
        if (!state) return '连接状态未加载';
        if (!state.tokenConfigured) return '未配置 LocalGen 访问令牌，请到「设置」填写';
        if (!workflow) return '工作流未加载（下拉为空？点重试）';
        if (!prompt.trim()) return '请输入提示词';
        if (needsRefInput && !(reference || pose || garment)) {
          return requiredImage ? '此工作流必须上传参考图' : '此工作流需要参考图 / 姿势 / 服装中的至少一项';
        }
        if (stepsError) return stepsError;
        if (cfgError) return cfgError;
        if (submitting) return '正在提交';
        return '';
      }, [state, workflow, prompt, reference, pose, garment, needsRefInput, stepsError, cfgError, submitting]);

      const doSubmit = async (body) => {
        setError('');
        setJob(null);
        setJobId(null);
        setSubmitting(true);
        try {
          const job0 = await callApi('/api/v1/jobs', 'POST', body);
          setJob(job0);
          setStartedAt(Date.now());
          setJobId(job0.job_id);
          if (onJobsChanged) onJobsChanged();
          if (onSubmitted) onSubmitted(job0);
        } catch (failure) {
          let suffix = '';
          if (failure.code === 'INPUT_IMAGE_MISSING') {
            suffix = '（参考图已过期，请重新上传）';
          } else if (failure.code === 'UNSUPPORTED_PARAMETER') {
            suffix = '（该工作流不支持此参数，请切换工作流或清除对应项）';
          }
          setError(`${failure.message || failure}${suffix}`);
        } finally {
          setSubmitting(false);
        }
      };

      const generate = async () => {
        const parsedSize = parseSizeKey(size);
        const body = { workflow_id: workflow, prompt: prompt.trim() };
        if (parsedSize) {
          body.width = parsedSize[0];
          body.height = parsedSize[1];
        }
        if (seed.trim() !== '') {
          const value = Number(seed);
          if (Number.isInteger(value) && value >= 0 && value <= 4294967295) body.seed = value;
        }
        if (negative.trim() !== '' && supportsNegative) body.negative_prompt = negative.trim();
        if (reference) body.image = reference.name;
        if (pose) body.pose_id = pose.id;
        if (garment) body.garment_id = garment.id;
        if (strength.trim() !== '' && supportsStrength) {
          const value = Number(strength);
          if (Number.isFinite(value) && value > 0 && value <= 1) body.strength = value;
        }
        if (steps.trim() !== '') body.steps = Number(steps);
        if (cfg.trim() !== '') body.cfg_scale = Number(cfg);
        await doSubmit(body);
      };

      // Build a submit body directly from a history fill (for rerun), without
      // waiting on React state to settle after back-fill.
      const buildBodyFromFill = (fill, workflowInfo) => {
        const supports = workflowInfo ? (workflowInfo.supported_parameters || []) : [];
        const body = { workflow_id: fill.workflow_id, prompt: String(fill.prompt || '') };
        if (fill.width && fill.height) {
          body.width = fill.width;
          body.height = fill.height;
        }
        if (fill.negative_prompt && supports.includes('negative_prompt')) body.negative_prompt = fill.negative_prompt;
        if (fill.imageToken) body.image = fill.imageToken;
        if (fill.pose && fill.pose.id) body.pose_id = fill.pose.id;
        if (fill.garment && fill.garment.id) body.garment_id = fill.garment.id;
        if (fill.strength && supports.includes('strength')) {
          const value = Number(fill.strength);
          if (Number.isFinite(value) && value > 0 && value <= 1) body.strength = value;
        }
        if (fill.steps != null && String(fill.steps) !== '') body.steps = Number(fill.steps);
        if (fill.cfg_scale != null && String(fill.cfg_scale) !== '') body.cfg_scale = Number(fill.cfg_scale);
        // rerun = fresh random seed: omit seed so the service generates one
        return body;
      };

      const progress = job && job.progress ? job.progress : null;
      const status = job ? job.status : null;
      const artifact = job && Array.isArray(job.artifacts) && job.artifacts.length ? job.artifacts[0] : null;
      const running = status && !TERMINAL.includes(status);
      const elapsed = useMemo(() => {
        if (!startedAt) return null;
        const end = job && TERMINAL.includes(job.status) && job.finished_at ? Date.parse(job.finished_at) : Date.now();
        const from = job && job.started_at ? Date.parse(job.started_at) : startedAt;
        return fmtElapsed((end - from) / 1000);
      }, [startedAt, job]);
      const stageLabel = !progress
        ? (running ? '排队 / 准备中' : '')
        : ({ loading: '加载模型', sampling: '采样', decoding: '解码' }[progress.stage] || progress.stage);
      const barWidth = progress && progress.total
        ? Math.min(100, Math.round((100 * (progress.current || 0)) / progress.total))
        : (running ? 8 : 0);

      const stepsPlaceholder = templateSteps != null ? `默认 ${templateSteps}` : '留空用模板默认';
      const cfgPlaceholder = templateCfg != null ? `默认 ${templateCfg}` : '留空用模板默认';

      return h('div', { className: 'lgs-grid2' },
        h('div', null,
          // ── model & params ──
          h('div', { className: 'lgs-card' },
            h('h2', null, '模型与参数'),
            h('label', { className: 'lgs-label' }, '工作流'),
            workflowLoading
              ? h('div', { className: 'lgs-note', style: { padding: '7px 0' } }, '加载工作流…')
              : h('select', { className: 'lgs-select', value: workflow, onChange: (event) => setWorkflow(event.target.value) },
                workflows.map((item) => h('option', { key: item.workflow_id, value: item.workflow_id },
                  `${item.title}${item.available ? '' : '（不可用）'}`))
              ),
            workflowError
              ? h('div', { className: 'lgs-row', style: { marginTop: 6, alignItems: 'center' } },
                h('div', { className: 'lgs-error', style: { flex: 1 } }, workflowError),
                h('button', { className: 'lgs-btn', onClick: loadWorkflows }, '重试'))
              : null,
            currentWorkflow && !currentWorkflow.available
              ? h('div', { className: 'lgs-note lgs-warn', style: { marginTop: 6 } },
                `不可用：${currentWorkflow.diagnostic_code || '缺少依赖'}`)
              : null,
            h('label', { className: 'lgs-label' }, '尺寸'),
            sizes.length
              ? h('select', { className: 'lgs-select', value: size, onChange: (event) => setSize(event.target.value) },
                sizes.map(([w, hh]) => h('option', { key: sizeKey(w, hh), value: sizeKey(w, hh) }, `${w} × ${hh}`))
              )
              : h('div', { className: 'lgs-note', style: { padding: '7px 0' } }, '该工作流没有可用尺寸'),
            h('div', { className: 'lgs-row', style: { marginTop: 10 } },
              h('div', { style: { flex: 1 } },
                h('label', { className: 'lgs-label' }, '种子'),
                h('input', { className: 'lgs-input', value: seed, onChange: (event) => setSeed(event.target.value), placeholder: '留空随机' })
              ),
              h('div', { style: { flex: 1 } },
                h('label', { className: 'lgs-label' }, '步数'),
                h('input', { className: 'lgs-input', value: steps, onChange: (event) => setSteps(event.target.value), placeholder: stepsPlaceholder })
              ),
              h('div', { style: { flex: 1 } },
                h('label', { className: 'lgs-label' }, 'CFG'),
                h('input', { className: 'lgs-input', value: cfg, onChange: (event) => setCfg(event.target.value), placeholder: cfgPlaceholder })
              )
            ),
            (stepsError || cfgError)
              ? h('div', { className: 'lgs-error', style: { marginTop: 6 } }, stepsError || cfgError)
              : null,
            h('div', { className: 'lgs-note', style: { marginTop: 8 } },
              '留空使用模型原始参数；步数超过 8 时 turbo 加速档自动停用。'),
            supportsNegative
              ? h('div', null,
                h('label', { className: 'lgs-label' }, '负面提示词'),
                h('input', { className: 'lgs-input', value: negative, onChange: (event) => setNegative(event.target.value), placeholder: '可选' })
              )
              : null
          ),

          // ── reference / pose / garment ──
          h('div', { className: 'lgs-card' },
            h('h2', null, '参考与图库'),
            supportsImage
              ? h('div', null,
                reference
                  ? h('div', { className: 'lgs-slot' },
                    reference.preview
                      ? h('img', { className: 'lgs-thumb', alt: '参考图', src: reference.preview })
                      : h('div', { className: 'lgs-thumb', style: { display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 9, textAlign: 'center', color: 'var(--dsw-alias-label-secondary)' } }, reference.name),
                    h('div', { className: 'lgs-meta' },
                      h('div', { className: 'lgs-name' }, reference.preview ? reference.label : '已回填参考图 token'),
                      h('div', { className: 'lgs-hint' }, reference.preview ? '已上传，将作为人物参考' : reference.name),
                      h('div', { className: 'lgs-row', style: { marginTop: 6 } },
                        h('button', { className: 'lgs-btn', onClick: () => fileRef.current && fileRef.current.click() }, '更换'),
                        h('button', { className: 'lgs-btn', onClick: clearReference }, '清除')
                      )
                    )
                  )
                  : h('div', null,
                    h('div', { className: 'lgs-note' }, '上传要换姿势 / 换装的人物图；也可只用图库素材。'),
                    h('button', { className: 'lgs-btn', style: { marginTop: 8 }, disabled: uploading, onClick: () => fileRef.current && fileRef.current.click() },
                      uploading ? '上传中…' : '选择图片')
                  ),
                h('input', { ref: fileRef, type: 'file', accept: 'image/png,image/jpeg,image/webp', style: { display: 'none' }, onChange: onPickFile }),
                supportsStrength
                  ? h('div', { style: { marginTop: 12 } },
                    h('label', { className: 'lgs-label' }, `重绘强度${strength.trim() !== '' ? `：${strength.trim()}` : '（留空用默认）'}`),
                    h('input', { type: 'range', min: '0.01', max: '1', step: '0.01', value: strength === '' ? '0.5' : strength, onChange: (event) => setStrength(event.target.value), style: { width: '100%' } }),
                    h('div', { className: 'lgs-row', style: { marginTop: 4, alignItems: 'center' } },
                      h('div', { className: 'lgs-note', style: { flex: 1 } }, '0–1，仅图生图有效；留空用工作流默认值。'),
                      strength !== ''
                        ? h('button', { className: 'lgs-btn', onClick: () => setStrength('') }, '清除')
                        : null
                    )
                  )
                  : null
              )
              : null,
            supportsPose
              ? h('div', { style: { marginTop: supportsImage ? 14 : 0 } },
                h('label', { className: 'lgs-label' }, '姿势'),
                pose
                  ? h('div', { className: 'lgs-slot' },
                    h('img', { className: 'lgs-thumb', alt: pose.title, src: rawUrl(`/api/v1/libraries/poses/${encodeURIComponent(pose.id)}/thumb`) }),
                    h('div', { className: 'lgs-meta' },
                      h('div', { className: 'lgs-name', title: pose.title }, pose.title),
                      h('div', { className: 'lgs-hint' }, [pose.angle, pose.category].filter(Boolean).join(' · ')),
                      h('div', { className: 'lgs-row', style: { marginTop: 6 } },
                        h('button', { className: 'lgs-btn', onClick: () => setPicker('poses') }, '更换'),
                        h('button', { className: 'lgs-btn', onClick: () => setPose(null) }, '清除')
                      )
                    )
                  )
                  : h('button', { className: 'lgs-btn', onClick: () => setPicker('poses') }, '从图库选择姿势')
              )
              : null,
            supportsGarment
              ? h('div', { style: { marginTop: 14 } },
                h('label', { className: 'lgs-label' }, '服装'),
                garment
                  ? h('div', { className: 'lgs-slot' },
                    h('img', { className: 'lgs-thumb', alt: garment.description, src: rawUrl(`/api/v1/libraries/garments/${encodeURIComponent(garment.id)}/thumb`) }),
                    h('div', { className: 'lgs-meta' },
                      h('div', { className: 'lgs-name', title: garment.description }, garment.description),
                      h('div', { className: 'lgs-hint' }, garment.category || ''),
                      h('div', { className: 'lgs-row', style: { marginTop: 6 } },
                        h('button', { className: 'lgs-btn', onClick: () => setPicker('garments') }, '更换'),
                        h('button', { className: 'lgs-btn', onClick: () => setGarment(null) }, '清除')
                      )
                    )
                  )
                  : h('button', { className: 'lgs-btn', onClick: () => setPicker('garments') }, '从图库选择服装')
              )
              : null
          ),

          // ── prompt & submit ──
          h('div', { className: 'lgs-card' },
            h('h2', null, '提示词'),
            h('textarea', {
              className: 'lgs-textarea',
              value: prompt,
              onChange: (event) => setPrompt(event.target.value),
              placeholder: '例如：保持人物身份与画风，按参考姿势自然站立，画面干净',
            }),
            h('button', {
              className: 'lgs-btn lgs-btn-primary',
              style: { marginTop: 12 },
              disabled: !canGenerate,
              onClick: generate,
            }, submitting ? '提交中…' : '生成一张'),
            !canGenerate && disabledReason
              ? h('div', { className: 'lgs-note', style: { marginTop: 8, color: 'var(--dsw-alias-state-warn-primary)' } },
                '为何不能生成：' + disabledReason)
              : null,
            h('div', { className: 'lgs-note lgs-warn', style: { marginTop: 8, textAlign: 'center' } },
              '编辑类单张预计 5–8 分钟，请勿重复点击')
          ),

          // ── progress / result ──
          h('div', { className: 'lgs-card' },
            h('h2', null, '当前任务'),
            !job
              ? h('div', { className: 'lgs-note' }, '还没有提交任务。')
              : h('div', null,
                h('div', { className: 'lgs-row', style: { alignItems: 'center' } },
                  h('span', { className: 'lgs-pill' }, STATUS_TEXT[status] || status),
                  elapsed ? h('span', { className: 'lgs-note' }, `已用 ${elapsed}`) : null
                ),
                h('div', { className: 'lgs-progress' }, h('i', { style: { width: `${barWidth}%` } })),
                h('div', { className: 'lgs-note' },
                  running
                    ? [stageLabel, progress && progress.total ? `${progress.current || 0} / ${progress.total} 步` : '', progress && progress.detail ? progress.detail : ''].filter(Boolean).join(' · ')
                    : (job.error ? `${job.error.code}：${job.error.message}` : '结束')),
                artifact
                  ? h('div', { className: 'lgs-result' },
                    h('img', { alt: '生成结果', src: rawUrl(artifact.download_path) }),
                    h('div', { className: 'lgs-note', style: { padding: '6px 10px' } },
                      `${artifact.width || '?'} × ${artifact.height || '?'} · ${Math.round((artifact.size_bytes || 0) / 1024)} KB`))
                  : null
              )
          ),

          error ? h('div', { className: 'lgs-card' }, h('div', { className: 'lgs-error' }, error)) : null
        ),

        // ── right rail: info ──
        h('div', { className: 'lgs-card' },
          h('h2', null, '关于'),
          h('div', { className: 'lgs-note' },
            '生成走 POST /api/v1/jobs（显式工作流）。选工作流后，参数控件按该工作流的 supported_parameters 显隐。'),
          currentWorkflow
            ? h('div', { className: 'lgs-note', style: { marginTop: 10 } },
              h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '模板步数'), h('span', { className: 'v' }, templateSteps != null ? String(templateSteps) : '—')),
              h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '模板 CFG'), h('span', { className: 'v' }, templateCfg != null ? String(templateCfg) : '—')),
              h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '默认尺寸'), h('span', { className: 'v' }, defaultSize ? `${defaultSize[0]} × ${defaultSize[1]}` : '—'))
            )
            : null
        ),

        picker
          ? h(Picker, {
            kind: picker,
            selectedId: picker === 'poses' ? (pose && pose.id) : (garment && garment.id),
            onClose: () => setPicker(null),
            onPick: (value) => {
              if (picker === 'poses') setPose(value);
              else setGarment(value);
              setPicker(null);
            },
          })
          : null
      );
    }

    // ── history panel ───────────────────────────────────────────────────────

    function HistoryPanel({ state, onReuse }) {
      const [items, setItems] = useState([]);
      const [cursor, setCursor] = useState(null);
      const [loading, setLoading] = useState(false);
      const [error, setError] = useState('');
      const [expanded, setExpanded] = useState(null);
      const [notice, setNotice] = useState('');

      const loadPage = useCallback(async (nextCursor, replace) => {
        if (!state || !state.tokenConfigured) return;
        setLoading(true);
        setError('');
        try {
          const params = new URLSearchParams({ limit: '50' });
          if (nextCursor) params.set('cursor', nextCursor);
          const data = await callApi(`/api/v1/jobs?${params.toString()}`);
          const pageItems = Array.isArray(data.items) ? data.items : [];
          setItems((previous) => (replace ? pageItems : previous.concat(pageItems)));
          setCursor(data.next_cursor || null);
        } catch (failure) {
          setError(failure.message || String(failure));
        } finally {
          setLoading(false);
        }
      }, [state]);

      useEffect(() => { loadPage(null, true); }, [loadPage]);

      const loadMore = () => { if (cursor && !loading) loadPage(cursor, false); };

      const removeItem = (jobId) => setItems((previous) => previous.filter((item) => item.job_id !== jobId));

      const doCancel = async (jobId, event) => {
        event.stopPropagation();
        try {
          await callApi(`/api/v1/jobs/${jobId}/cancel`, 'POST');
          setNotice('已请求取消');
          loadPage(null, true);
        } catch (failure) {
          setError(failure.message || String(failure));
        }
      };

      const doDelete = async (jobId, event) => {
        event.stopPropagation();
        if (!window.confirm('删除该任务及其成品图片？此操作不可撤销。')) return;
        try {
          await callApi(`/api/v1/jobs/${jobId}`, 'DELETE');
          removeItem(jobId);
          if (expanded === jobId) setExpanded(null);
          setNotice('已删除');
        } catch (failure) {
          setError(failure.message || String(failure));
        }
      };

      const doSave = (job, event) => {
        event.stopPropagation();
        const artifact = job.artifacts && job.artifacts[0];
        if (!artifact) return;
        const anchor = document.createElement('a');
        anchor.href = rawUrl(artifact.download_path);
        anchor.download = `LocalGen-${job.job_id.slice(0, 8)}.png`;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      };

      // resolve pose/garment display info for a job's parameters (async, best effort)
      const resolveLibraryItem = useCallback(async (kind, id) => {
        if (!id) return null;
        try {
          const entry = await callApi(`/api/v1/libraries/${kind}/${encodeURIComponent(id)}`);
          if (kind === 'poses') {
            return { id: entry.id, title: entry.title || entry.id, angle: entry.angle || '', category: entry.category || '', thumb: entry.thumb || entry.image };
          }
          return { id: entry.id, description: entry.description || entry.id, category: entry.category_cn || entry.category || '', image: entry.image };
        } catch {
          return { id, title: id, angle: '', category: '' };
        }
      }, []);

      const applyReuse = useCallback(async (job, rerun) => {
        const params = job.parameters || {};
        const fill = {
          workflow_id: job.workflow_id,
          prompt: params.prompt,
          width: params.width,
          height: params.height,
          seed: rerun ? '' : (params.seed != null ? String(params.seed) : ''),
          negative_prompt: params.negative_prompt || '',
          steps: params.steps != null ? String(params.steps) : '',
          cfg_scale: params.cfg_scale != null ? String(params.cfg_scale) : '',
          strength: params.strength != null ? String(params.strength) : '',
          imageToken: params.image || null,
          pose: null,
          garment: null,
        };
        setNotice(rerun ? '已回填并清空种子，请点生成' : '已回填参数');
        if (params.pose_id) {
          const poseInfo = await resolveLibraryItem('poses', params.pose_id);
          if (poseInfo) fill.pose = poseInfo;
        }
        if (params.garment_id) {
          const garmentInfo = await resolveLibraryItem('garments', params.garment_id);
          if (garmentInfo) fill.garment = garmentInfo;
        }
        if (onReuse) onReuse(fill, rerun);
      }, [onReuse, resolveLibraryItem]);

      const Detail = ({ job }) => {
        const params = job.parameters || {};
        const artifact = job.artifacts && job.artifacts[0];
        return h('div', { style: { marginTop: 10, padding: '10px 12px', border: '1px solid var(--dsw-alias-border-l1)', borderRadius: 10, background: 'var(--dsw-alias-bg-layer-2)' } },
          h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '状态'), h('span', { className: 'v' }, STATUS_TEXT[job.status] || job.status)),
          h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '工作流'), h('span', { className: 'v' }, `${job.workflow_id}@${job.workflow_revision}`)),
          h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '创建'), h('span', { className: 'v' }, fmtTime(job.created_at))),
          params.seed != null ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '种子'), h('span', { className: 'v' }, String(params.seed))) : null,
          params.steps != null ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '步数'), h('span', { className: 'v' }, String(params.steps))) : null,
          params.cfg_scale != null ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, 'CFG'), h('span', { className: 'v' }, String(params.cfg_scale))) : null,
          params.strength != null ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '强度'), h('span', { className: 'v' }, String(params.strength))) : null,
          params.width ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '尺寸'), h('span', { className: 'v' }, `${params.width} × ${params.height}`)) : null,
          h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '提示词'), h('span', { className: 'v' }, params.prompt || '—')),
          params.negative_prompt
            ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '负面'), h('span', { className: 'v' }, params.negative_prompt))
            : null,
          params.image
            ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '参考图'), h('span', { className: 'v' }, params.image))
            : null,
          params.pose_id
            ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '姿势'), h('span', { className: 'v' }, params.pose_id))
            : null,
          params.garment_id
            ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '服装'), h('span', { className: 'v' }, params.garment_id))
            : null,
          job.error
            ? h('div', { className: 'lgs-detail-row' }, h('span', { className: 'k' }, '错误'), h('span', { className: 'v lgs-error' }, `${job.error.code}：${job.error.message}`))
            : null,
          artifact
            ? h('div', { className: 'lgs-detail-ref' },
              h('img', { alt: '成品', src: rawUrl(artifact.download_path) }))
            : null
        );
      };

      return h('div', null,
        h('div', { className: 'lgs-row', style: { alignItems: 'center', marginBottom: 10 } },
          h('h2', { style: { margin: 0, fontSize: 13, fontWeight: 600 } }, '历史任务'),
          h('span', { className: 'lgs-spacer', style: { flex: 1 } }),
          notice ? h('span', { className: 'lgs-note lgs-ok' }, notice) : null,
          h('button', { className: 'lgs-btn', onClick: () => loadPage(null, true), disabled: loading }, '刷新')
        ),
        error ? h('div', { className: 'lgs-error', style: { marginBottom: 10 } }, error) : null,
        items.length === 0 && !loading
          ? h('div', { className: 'lgs-empty' }, '暂无历史任务')
          : h('div', { className: 'lgs-hist' },
            items.map((job) => {
              const artifact = job.artifacts && job.artifacts[0];
              const isActive = ACTIVE.includes(job.status);
              return h('div', { key: job.job_id },
                h('div', {
                  className: 'lgs-hist-item',
                  onClick: () => setExpanded(expanded === job.job_id ? null : job.job_id),
                },
                  artifact
                    ? h('img', { className: 'lgs-hist-thumb', alt: '', src: rawUrl(artifact.download_path) })
                    : h('div', { className: 'lgs-hist-thumb empty' }, STATUS_TEXT[job.status] || job.status),
                  h('div', { className: 'lgs-hist-main' },
                    h('div', { className: 'lgs-hist-top' },
                      h('span', { className: 'lgs-hist-dot', style: { background: STATUS_COLOR[job.status] || 'var(--dsw-alias-label-secondary)' } }),
                      h('span', { className: 'lgs-pill' }, STATUS_TEXT[job.status] || job.status),
                      h('span', { className: 'lgs-hist-wf' }, job.workflow_id),
                      h('span', { className: 'lgs-hist-time' }, fmtTime(job.created_at))
                    ),
                    h('div', { className: 'lgs-hist-prompt' }, (job.parameters && job.parameters.prompt) || '（无提示词）')
                  ),
                  h('div', { className: 'lgs-hist-actions', onClick: (event) => event.stopPropagation() },
                    isActive
                      ? h('button', { className: 'lgs-btn', onClick: (event) => doCancel(job.job_id, event) }, '取消')
                      : null,
                    artifact
                      ? h('button', { className: 'lgs-btn', onClick: (event) => doSave(job, event) }, '另存 PNG')
                      : null,
                    h('button', { className: 'lgs-btn', onClick: () => applyReuse(job, false) }, '复用参数'),
                    h('button', { className: 'lgs-btn', onClick: () => applyReuse(job, true) }, '同参重跑'),
                    h('button', { className: 'lgs-btn lgs-btn-danger', onClick: (event) => doDelete(job.job_id, event) }, '删除')
                  )
                ),
                expanded === job.job_id ? h(Detail, { job }) : null
              );
            })
          ),
        cursor
          ? h('button', { className: 'lgs-btn', style: { width: '100%', marginTop: 12 }, onClick: loadMore, disabled: loading },
            loading ? '加载中…' : '加载更多')
          : null
      );
    }

    // ── settings panel ──────────────────────────────────────────────────────

    const SERVER_SETTINGS_FIELDS = [
      { key: 'job_timeout', label: '任务超时（秒）', hint: '60–86400，改后立即生效' },
      { key: 'queue_limit', label: '队列上限', hint: '1–64，立即生效' },
      { key: 'max_pixels', label: '最大像素数', hint: '262144–16777216，立即生效' },
      { key: 'max_image_mib', label: '单图上限（MiB）', hint: '1–64，立即生效' },
      { key: 'max_request_bytes', label: '请求体上限（字节）', hint: '4096–10485760，立即生效' },
      { key: 'cancel_timeout', label: '取消超时（秒）', hint: '5–120，立即生效' },
      { key: 'request_timeout', label: '请求超时（秒）', hint: '5–120，立即生效' },
      { key: 'api_port', label: 'API 端口', hint: '1024–65535，需重启', restart: true },
      { key: 'engine_port', label: '引擎端口', hint: '1024–65535，需重启', restart: true },
      { key: 'engine_device', label: '引擎设备号', hint: '0–8，需重启', restart: true },
      { key: 'startup_timeout', label: '启动超时（秒）', hint: '30–600，需重启', restart: true },
      { key: 'engine_root', label: '引擎根目录', hint: '绝对路径，需重启', restart: true, text: true },
      { key: 'engine_python', label: '引擎 Python', hint: '绝对路径，需重启', restart: true, text: true },
    ];

    function SettingsPanel({ state, refresh }) {
      const [pluginBase, setPluginBase] = useState('');
      const [pluginToken, setPluginToken] = useState('');
      const [pollMs, setPollMs] = useState(String(POLL_MS));
      const [exePath, setExePath] = useState('');
      const [pluginNote, setPluginNote] = useState('');

      const [serverSettings, setServerSettings] = useState(null);
      const [serverNote, setServerNote] = useState('');
      const [serverError, setServerError] = useState('');
      const [saving, setSaving] = useState(false);

      useEffect(() => {
        if (state) {
          setPluginBase(state.baseUrl || '');
          if (state.pollMs) setPollMs(String(state.pollMs));
          if (state.exePath) setExePath(state.exePath);
        }
      }, [state]);

      const loadServer = useCallback(async () => {
        if (!state || !state.tokenConfigured) return;
        setServerError('');
        try {
          const data = await callApi('/api/v1/settings');
          setServerSettings(data.settings || {});
        } catch (failure) {
          setServerError(failure.message || String(failure));
        }
      }, [state]);

      useEffect(() => { loadServer(); }, [loadServer]);

      const savePlugin = async () => {
        setPluginNote('');
        try {
          const response = await fetch(ROUTE_CONFIG, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ baseUrl: pluginBase, token: pluginToken, pollMs: Number(pollMs) || POLL_MS, exePath }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data && data.error ? data.error.message : `HTTP ${response.status}`);
          setPluginToken('');
          setPluginNote('已保存');
          if (refresh) refresh();
        } catch (failure) {
          setPluginNote('保存失败：' + (failure.message || failure));
        }
      };

      const saveServerField = async (key, value) => {
        if (!serverSettings) return;
        setSaving(true);
        setServerError('');
        setServerNote('');
        try {
          const parsed = key === 'api_port' || key === 'engine_port' || key === 'engine_device'
            || key === 'startup_timeout' || key === 'job_timeout' || key === 'queue_limit'
            || key === 'max_pixels' || key === 'max_image_mib' || key === 'max_request_bytes'
            || key === 'cancel_timeout' || key === 'request_timeout'
            ? Number(value)
            : value;
          const data = await callApi('/api/v1/settings', 'PUT', { [key]: parsed });
          setServerSettings(data.settings || {});
          if (data.restart_required) {
            setServerNote(`⚠ ${data.note}`);
          } else {
            setServerNote(data.note || '已保存');
          }
          if (key === 'api_port') {
            setServerNote(`⚠ 已改为 ${data.settings.api_port}，需重启 LocalGen 生效；重启后请同步修改上方插件连接地址。`);
          }
        } catch (failure) {
          setServerError(failure.message || String(failure));
        } finally {
          setSaving(false);
        }
      };

      return h('div', null,
        h('div', { className: 'lgs-card' },
          h('h2', null, '插件连接'),
          h('label', { className: 'lgs-label' }, 'LocalGen 服务地址'),
          h('input', { className: 'lgs-input', value: pluginBase, onChange: (event) => setPluginBase(event.target.value), placeholder: 'http://127.0.0.1:12138' }),
          h('label', { className: 'lgs-label' }, 'API 令牌'),
          h('input', { className: 'lgs-input', type: 'password', value: pluginToken, onChange: (event) => setPluginToken(event.target.value), placeholder: state && state.tokenConfigured ? '已保存，留空则不修改' : '粘贴 LocalGen 的 Bearer 令牌' }),
          h('label', { className: 'lgs-label' }, '进度轮询间隔（毫秒）'),
          h('input', { className: 'lgs-input', value: pollMs, onChange: (event) => setPollMs(event.target.value) }),
          h('label', { className: 'lgs-label' }, 'LocalGen 程序路径'),
          h('input', { className: 'lgs-input', value: exePath, onChange: (event) => setExePath(event.target.value), placeholder: 'D:\\garbage pile\\local_image\\dist\\LocalGen\\LocalGen.exe' }),
          h('div', { className: 'lgs-row', style: { marginTop: 10 } },
            h('button', { className: 'lgs-btn', onClick: savePlugin }, '保存插件设置'),
            pluginNote ? h('span', { className: 'lgs-note', style: { alignSelf: 'center', color: pluginNote.startsWith('保存失败') ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-state-success-primary)' } }, pluginNote) : null
          )
        ),

        h('div', { className: 'lgs-card' },
          h('h2', null, 'LocalGen 服务设置'),
          serverError ? h('div', { className: 'lgs-error', style: { marginBottom: 10 } }, serverError) : null,
          serverNote ? h('div', { className: 'lgs-note', style: { marginBottom: 10, color: serverNote.startsWith('⚠') ? 'var(--dsw-alias-state-warn-primary)' : 'var(--dsw-alias-state-success-primary)' } }, serverNote) : null,
          !serverSettings
            ? h('div', { className: 'lgs-note' }, state && state.tokenConfigured ? '加载中…' : '先配置插件连接，才能读取服务设置。')
            : h('div', { className: 'lgs-settings-grid' },
              SERVER_SETTINGS_FIELDS.map((field) => {
                const value = serverSettings[field.key];
                const defaultValue = value != null ? String(value) : '';
                return h('div', { key: field.key, className: 'lgs-field' },
                  h('label', null, field.label + (field.restart ? ' ⚠重启' : '')),
                  h('input', {
                    className: 'lgs-input',
                    defaultValue,
                    key: `${field.key}:${defaultValue}`,
                    onBlur: (event) => {
                      const next = event.target.value;
                      if (next !== defaultValue) saveServerField(field.key, next);
                    },
                    disabled: saving,
                  }),
                  h('span', { className: 'hint' }, field.hint)
                );
              })
            )
        )
      );
    }

    // ── status card (always visible) ────────────────────────────────────────

    function StatusCard({ state }) {
      const [snap, setSnap] = useState(null);
      const [error, setError] = useState('');

      useEffect(() => {
        if (!state || !state.tokenConfigured) return undefined;
        let cancelled = false;
        let timer = null;
        const tick = async () => {
          try {
            const data = await callApi('/api/v1/progress');
            if (cancelled) return;
            setSnap(data);
            setError('');
            timer = setTimeout(tick, POLL_MS);
          } catch (failure) {
            if (!cancelled) {
              setError(failure.message || String(failure));
              timer = setTimeout(tick, POLL_MS * 2);
            }
          }
        };
        tick();
        return () => { cancelled = true; if (timer) clearTimeout(timer); };
      }, [state]);

      const job = snap && snap.job ? snap.job : null;
      const queued = snap ? snap.queued || 0 : 0;
      const active = job && ACTIVE.includes(job.status);
      const progress = job && job.progress ? job.progress : null;

      let elapsed = '';
      if (job && job.started_at) {
        const from = Date.parse(job.started_at);
        if (Number.isFinite(from)) {
          elapsed = fmtElapsed((Date.now() - from) / 1000);
        }
      }

      const stageText = !progress ? '准备中' : ({ loading: '加载模型', sampling: '采样', decoding: '解码' }[progress.stage] || progress.stage);
      const stepText = progress && progress.total ? (progress.current || 0) + '/' + progress.total : '';

      let line = '';
      if (active) {
        line = '正在跑 ' + (job.workflow_id || '') + ' · ' + stageText + (stepText ? ' · ' + stepText : '') + (elapsed ? ' · 已用 ' + elapsed : '');
      } else if (queued > 0) {
        line = '队列 ' + queued + ' 条 · 等待引擎';
      }

      if (!line && !error) {
        // idle: render a slim empty strip so layout is stable
        return h('div', { className: 'lgs-status lgs-status-idle' }, h('span', null, '空闲'));
      }

      return h('div', { className: 'lgs-status' + (active ? ' lgs-status-active' : '') },
        error ? h('span', { style: { color: 'var(--dsw-alias-state-error-primary)' } }, error) : h('span', null, line)
      );
    }

    // ── workbench (top conversation.view) ───────────────────────────────────

    function Workbench({ state, refresh, onJobsChanged }) {
      const [tab, setTab] = useState('generate');
      const [pendingFill, setPendingFill] = useState(null);
      const [openNote, setOpenNote] = useState('');
      const [toast, setToast] = useState('');

      const showToast = useCallback((message) => {
        setToast(message);
        setTimeout(() => setToast((current) => (current === message ? '' : current)), 5000);
      }, []);

      const doOpenLocalGen = async () => {
        setOpenNote('');
        try {
          const data = await openLocalGen();
          setOpenNote(data.message || (data.status === 'running' ? 'LocalGen 已在运行' : '已启动'));
        } catch (failure) {
          setOpenNote('打开失败：' + (failure.message || failure));
        }
      };

      // History reuse: stash the fill and switch to the generate tab. The
      // GeneratePanel consumes it on mount — no reliance on a stale ref to an
      // unmounted component.
      const requestFill = useCallback((fill, rerun) => {
        setPendingFill({ ...fill, __rerun: Boolean(rerun) });
        setTab('generate');
      }, []);

      const clearPendingFill = useCallback(() => setPendingFill(null), []);

      const tabs = [
        { id: 'generate', label: '生成' },
        { id: 'history', label: '历史' },
        { id: 'settings', label: '设置' },
      ];

      return h('div', { className: 'lgs-root' },
        h('style', null, CSS),
        h('div', { className: 'lgs-head' },
          h(StudioGlyph, { size: 20 }),
          h('h1', null, 'LocalGen'),
          h('span', { className: 'lgs-sub' }, state && state.baseUrl ? state.baseUrl : '未连接'),
          h('span', { className: 'lgs-spacer' }),
          state && state.tokenConfigured
            ? h('span', { className: 'lgs-pill' }, '已连接')
            : h('span', { className: 'lgs-pill lgs-warn' }, '未配置令牌'),
          h('button', { className: 'lgs-btn', onClick: doOpenLocalGen }, '打开 LocalGen'),
          h('button', { className: 'lgs-btn', onClick: () => setTab('settings') }, '设置')
        ),
        openNote
          ? h('div', { style: { padding: '6px 18px 0', fontSize: '12px', color: openNote.startsWith('打开失败') ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-label-secondary)' } }, openNote)
          : null,
        h('div', { className: 'lgs-tabs' },
          tabs.map((item) => h('button', {
            key: item.id,
            className: 'lgs-tab',
            'data-on': tab === item.id ? '1' : '0',
            onClick: () => setTab(item.id),
          }, item.label))
        ),
        toast
          ? h('div', { style: { position: 'fixed', right: 18, bottom: 18, zIndex: 950, padding: '10px 16px', borderRadius: 8, background: 'var(--dsw-alias-bg-overlay)', border: '1px solid var(--dsw-alias-brand-primary)', boxShadow: '0 8px 24px rgba(0,0,0,.3)', fontSize: 12.5, color: 'var(--dsw-alias-label-primary)' } }, toast)
          : null,
        h(StatusCard, { state }),
        h('div', { className: 'lgs-body' },
          !state || !state.tokenConfigured
            ? h('div', { className: 'lgs-card' },
              h('h2', null, '先配置访问令牌'),
              h('div', { className: 'lgs-note' }, '面板通过本机 DSH 转发通道访问 LocalGen，需要一次 Bearer 令牌。'),
              h('button', { className: 'lgs-btn', style: { marginTop: 10 }, onClick: () => setTab('settings') }, '打开设置')
            )
            : h('div', null,
              // All three panels stay mounted; only the active one is visible.
              // This preserves every input across tab switches (no unmount).
              h('div', { style: { display: tab === 'generate' ? 'block' : 'none' } },
                h(GeneratePanel, { state, onJobsChanged, pendingFill, onFillConsumed: clearPendingFill, onSubmitted: (job0) => showToast('已提交任务 ' + String(job0.job_id || '').slice(0, 8) + '，可在「历史」查看') })),
              h('div', { style: { display: tab === 'history' ? 'block' : 'none' } },
                h(HistoryPanel, { state, onReuse: requestFill })),
              h('div', { style: { display: tab === 'settings' ? 'block' : 'none' } },
                h(SettingsPanel, { state, refresh }))
            )
        )
      );
    }

    // ── sidebar panel (kept) ────────────────────────────────────────────────

    function StudioPage() {
      const { state, refresh } = useConnection();
      const fillRef = useRef(null);
      return h('div', { className: 'lgs-root' },
        h('style', null, CSS),
        h('div', { className: 'lgs-head' },
          h(StudioGlyph, { size: 20 }),
          h('h1', null, 'LocalGen'),
          h('span', { className: 'lgs-sub' }, state && state.baseUrl ? state.baseUrl : '未连接'),
          h('span', { className: 'lgs-spacer' }),
          state && state.tokenConfigured
            ? h('span', { className: 'lgs-pill' }, '已连接')
            : h('span', { className: 'lgs-pill lgs-warn' }, '未配置令牌')
        ),
        h('div', { className: 'lgs-body' },
          !state || !state.tokenConfigured
            ? h('div', { className: 'lgs-card' },
              h('h2', null, '先配置访问令牌'),
              h('div', { className: 'lgs-note' }, '在顶部「LocalGen」工作台 → 设置里配置令牌，或直接在此生成。'),
              h('button', { className: 'lgs-btn', style: { marginTop: 10 }, onClick: refresh }, '重新检测')
            )
            : h(GeneratePanel, { state, fillRef })
        )
      );
    }

    return {
      name: 'localgen-studio',
      inject: ['slots'],
      apply(ctx) {
        // top conversation view entry (new primary surface)
        ctx.slots.inject('conversation.view', () => ctx.slots.register({
          name: 'conversation.view',
          id: 'localgen',
          order: 30,
          label: () => 'LocalGen',
          inject: () => ({}),
        }, (props) => {
          const { state, refresh } = useConnection();
          return h(Workbench, { state, refresh, onJobsChanged: props && props.onJobsChanged });
        }));
        // sidebar panel (kept)
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: 'localgen-studio',
          order: 40,
          label: 'LocalGen',
        }, StudioGlyph));
        ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: 'localgen-studio',
        }, StudioPage));
      },
    };
  },
});
