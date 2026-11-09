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

    const PAGE_SIZE = 60;
    const POLL_MS = 2500;
    const TERMINAL = ['succeeded', 'failed', 'cancelled', 'interrupted'];
    const SIZE = [768, 768];

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

    // ── styles ──────────────────────────────────────────────────────────────

    const CSS = `
.lgs-root { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--dsw-alias-bg-base); color: var(--dsw-alias-label-primary); font-size: 13px; }
.lgs-head { display: flex; align-items: center; gap: 10px; padding: 12px 18px; border-bottom: 1px solid var(--dsw-alias-border-l1); flex: none; }
.lgs-head h1 { font-size: 15px; font-weight: 600; margin: 0; }
.lgs-head .lgs-sub { color: var(--dsw-alias-label-secondary); font-size: 12px; }
.lgs-head .lgs-spacer { flex: 1; }
.lgs-body { flex: 1; min-height: 0; overflow: auto; padding: 16px 18px 40px; }
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

    // ── main panel ──────────────────────────────────────────────────────────

    function StudioPage() {
      const [state, setState] = useState(null);
      const [setupOpen, setSetupOpen] = useState(false);
      const [baseInput, setBaseInput] = useState('');
      const [tokenInput, setTokenInput] = useState('');
      const [saveNote, setSaveNote] = useState('');

      const [pose, setPose] = useState(null);
      const [garment, setGarment] = useState(null);
      const [picker, setPicker] = useState(null);
      const [reference, setReference] = useState(null);
      const [uploading, setUploading] = useState(false);
      const [prompt, setPrompt] = useState('');
      const [submitting, setSubmitting] = useState(false);
      const [jobId, setJobId] = useState(null);
      const [job, setJob] = useState(null);
      const [error, setError] = useState('');
      const [startedAt, setStartedAt] = useState(null);
      const fileRef = useRef(null);

      const refreshState = useCallback(async () => {
        try {
          const response = await fetch(ROUTE_STATE, { cache: 'no-store' });
          const data = await response.json();
          setState(data);
          setBaseInput(data.baseUrl || '');
          if (!data.tokenConfigured) setSetupOpen(true);
        } catch (failure) {
          setState({ tokenConfigured: false, baseUrl: '', error: String(failure && failure.message ? failure.message : failure) });
          setSetupOpen(true);
        }
      }, []);

      useEffect(() => { refreshState(); }, [refreshState]);

      const saveSetup = async () => {
        setSaveNote('');
        setError('');
        try {
          const response = await fetch(ROUTE_CONFIG, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ baseUrl: baseInput, token: tokenInput }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data && data.error ? data.error.message : `HTTP ${response.status}`);
          setState({ baseUrl: data.baseUrl, tokenConfigured: data.tokenConfigured, tokenSource: data.tokenSource });
          setTokenInput('');
          setSaveNote('已保存');
          setSetupOpen(false);
        } catch (failure) {
          setError(String(failure && failure.message ? failure.message : failure));
        }
      };

      useEffect(() => {
        if (!jobId) return undefined;
        let cancelled = false;
        let timer = null;
        const tick = async () => {
          try {
            const data = await callApi(`/api/v1/jobs/${jobId}`);
            if (cancelled) return;
            setJob(data);
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
          // Local preview is a local object URL; the server only returns the token.
          const preview = URL.createObjectURL(file);
          setReference({ name: data.name, label: file.name || data.name, preview });
        } catch (failure) {
          setError(String(failure && failure.message ? failure.message : failure));
        } finally {
          setUploading(false);
        }
      };

      const canGenerate = Boolean(prompt.trim()) && Boolean(reference || pose || garment) && !submitting && state && state.tokenConfigured;

      const generate = async () => {
        setError('');
        setJob(null);
        setJobId(null);
        setSubmitting(true);
        try {
          const body = { prompt: prompt.trim(), size: SIZE };
          if (reference) body.image = reference.name;
          if (pose) body.pose = pose.id;
          if (garment) body.garment = garment.id;
          const job0 = await callApi('/api/v1/edit', 'POST', body);
          setJob(job0);
          setStartedAt(Date.now());
          setJobId(job0.job_id);
        } catch (failure) {
          const suffix = failure.routedTo ? `（已路由到 ${failure.routedTo}）` : '';
          setError(`${failure.message || failure}${suffix}`);
        } finally {
          setSubmitting(false);
        }
      };

      const progress = job && job.progress ? job.progress : null;
      const status = job ? job.status : null;
      const artifact = job && Array.isArray(job.artifacts) && job.artifacts.length ? job.artifacts[0] : null;
      const running = status && !TERMINAL.includes(status);
      const elapsed = useMemo(() => {
        if (!startedAt) return null;
        const end = job && TERMINAL.includes(job.status) && job.finished_at
          ? Date.parse(job.finished_at)
          : Date.now();
        const from = job && job.started_at ? Date.parse(job.started_at) : startedAt;
        const seconds = Math.max(0, Math.round((end - from) / 1000));
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`;
      }, [startedAt, job]);

      const stageLabel = !progress
        ? (running ? '排队 / 准备中' : '')
        : ({
          loading: '加载模型',
          sampling: '采样',
          decoding: '解码',
        }[progress.stage] || progress.stage);
      const barWidth = progress && progress.total
        ? Math.min(100, Math.round((100 * (progress.current || 0)) / progress.total))
        : (running ? 8 : 0);

      return h('div', { className: 'lgs-root' },
        h('style', null, CSS),
        h('div', { className: 'lgs-head' },
          h(StudioGlyph, { size: 20 }),
          h('h1', null, 'LocalGen Studio'),
          h('span', { className: 'lgs-sub' }, state && state.baseUrl ? state.baseUrl : '未连接'),
          h('span', { className: 'lgs-spacer' }),
          state && state.tokenConfigured
            ? h('span', { className: 'lgs-pill' }, '已连接')
            : h('span', { className: 'lgs-pill lgs-warn' }, '未配置令牌'),
          h('button', { className: 'lgs-btn', onClick: () => setSetupOpen((open) => !open) }, setupOpen ? '收起设置' : '设置')
        ),
        h('div', { className: 'lgs-body' },
          setupOpen
            ? h('div', { className: 'lgs-card' },
              h('h2', null, '连接设置'),
              h('label', { className: 'lgs-label' }, '服务地址'),
              h('input', { className: 'lgs-input', value: baseInput, onChange: (event) => setBaseInput(event.target.value), placeholder: 'http://127.0.0.1:12138' }),
              h('label', { className: 'lgs-label' }, '访问令牌'),
              h('input', { className: 'lgs-input', type: 'password', value: tokenInput, onChange: (event) => setTokenInput(event.target.value), placeholder: state && state.tokenConfigured ? '已保存，留空则不修改' : '粘贴 LocalGen 的 Bearer 令牌' }),
              h('div', { className: 'lgs-note', style: { marginTop: 8 } },
                '令牌在 LocalGen 的「设置 → 复制 API 令牌」，或运行 ',
                h('code', null, 'python -m localgen --show-token'),
                '。它经 DPAPI 加密存放，JS 读不到，所以需要你贴一次。'),
              h('div', { className: 'lgs-row', style: { marginTop: 10 } },
                h('button', { className: 'lgs-btn', onClick: saveSetup }, '保存'),
                h('button', { className: 'lgs-btn', onClick: refreshState }, '重新检测'),
                saveNote ? h('span', { className: 'lgs-note lgs-ok', style: { alignSelf: 'center' } }, saveNote) : null
              )
            )
            : null,

          !state || state.tokenConfigured
            ? h('div', { className: 'lgs-grid2', style: { marginTop: setupOpen ? 14 : 0 } },
              h('div', null,
                h('div', { className: 'lgs-card' },
                  h('h2', null, '参考图（人物）'),
                  reference
                    ? h('div', { className: 'lgs-slot' },
                      h('img', { className: 'lgs-thumb', alt: '参考图', src: reference.preview }),
                      h('div', { className: 'lgs-meta' },
                        h('div', { className: 'lgs-name' }, reference.label),
                        h('div', { className: 'lgs-hint' }, '已上传，将作为人物参考'),
                        h('div', { className: 'lgs-row', style: { marginTop: 6 } },
                          h('button', { className: 'lgs-btn', onClick: () => fileRef.current && fileRef.current.click() }, '更换'),
                          h('button', {
                            className: 'lgs-btn',
                            onClick: () => {
                              if (reference && reference.preview) URL.revokeObjectURL(reference.preview);
                              setReference(null);
                            },
                          }, '清除')
                        )
                      )
                    )
                    : h('div', null,
                      h('div', { className: 'lgs-note' }, '上传要换姿势 / 换装的人物图；也可以只用图库素材生成。'),
                      h('button', { className: 'lgs-btn', style: { marginTop: 8 }, disabled: uploading, onClick: () => fileRef.current && fileRef.current.click() },
                        uploading ? '上传中…' : '选择图片')
                    ),
                  h('input', { ref: fileRef, type: 'file', accept: 'image/png,image/jpeg,image/webp', style: { display: 'none' }, onChange: onPickFile })
                ),

                h('div', { className: 'lgs-card' },
                  h('h2', null, '姿势'),
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
                ),

                h('div', { className: 'lgs-card' },
                  h('h2', null, '服装'),
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
                ),

                h('div', { className: 'lgs-card' },
                  h('h2', null, '提示词'),
                  h('textarea', {
                    className: 'lgs-textarea',
                    value: prompt,
                    onChange: (event) => setPrompt(event.target.value),
                    placeholder: '例如：保持人物身份与画风，按参考姿势自然站立，画面干净',
                  }),
                  h('div', { className: 'lgs-note', style: { marginTop: 8 } },
                    '尺寸固定 768×768；服装描述由服务端自动拼进提示词。'),
                  h('button', {
                    className: 'lgs-btn lgs-btn-primary',
                    style: { marginTop: 12 },
                    disabled: !canGenerate,
                    onClick: generate,
                  }, submitting ? '提交中…' : '生成一张'),
                  h('div', { className: 'lgs-note lgs-warn', style: { marginTop: 8, textAlign: 'center' } },
                    '单张预计 5–8 分钟（Qwen-Image-2.1 · 8 步 turbo），请勿重复点击')
                ),

                h('div', { className: 'lgs-card' },
                  h('h2', null, '任务'),
                  !job
                    ? h('div', { className: 'lgs-note' }, '还没有提交任务。')
                    : h('div', null,
                      h('div', { className: 'lgs-row', style: { alignItems: 'center' } },
                        h('span', { className: 'lgs-pill' }, status),
                        job.routed_to ? h('span', { className: 'lgs-pill' }, job.routed_to) : null,
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
                            `${artifact.width || SIZE[0]} × ${artifact.height || SIZE[1]} · ${Math.round((artifact.size_bytes || 0) / 1024)} KB`))
                        : null
                    )
                ),

                error ? h('div', { className: 'lgs-card' }, h('div', { className: 'lgs-error' }, error)) : null
              ),

              h('div', { className: 'lgs-card' },
                h('h2', null, '图库'),
                h('div', { className: 'lgs-note' },
                  '姿势 1439 条（4 体系 / 26 分类，同姿势多角度）；服装 1407 条（8 分类）。' +
                  '点开缩略图网格后，滚动会自动加载更多。'),
                h('div', { className: 'lgs-note', style: { marginTop: 10 } },
                  '姿势决定动作与构图，服装决定穿着；两者可同时选。参考图提供人物身份。')
              )
            )
            : h('div', { className: 'lgs-card', style: { marginTop: setupOpen ? 14 : 0 } },
              h('h2', null, '先配置访问令牌'),
              h('div', { className: 'lgs-note' },
                '这个面板通过本机 DSH 的转发通道访问 LocalGen，需要一次 Bearer 令牌。'),
              h('button', { className: 'lgs-btn', style: { marginTop: 10 }, onClick: () => setSetupOpen(true) }, '打开设置')
            ),

          error && (!state || state.tokenConfigured) ? null : null
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

    return {
      name: 'localgen-studio',
      inject: ['slots'],
      apply(ctx) {
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
