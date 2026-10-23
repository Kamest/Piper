// .http files in the file browser (httpclient.py): an environment picker and "Run all" above
// the code, ▶ on every request line, the results below. piperd runs the file on the host with
// JetBrains' ijhttp and the real environment - the click is the approval; a production run is
// confirmed once more here (and once more for requests that change data). The same runs are
// what a session reads with http_last_result, so "run it in IDEA and paste the response" ends.

import { ApiError, call } from './api.js';
import { ago, clear, h } from './dom.js';
import { highlightLines, type Lang } from './highlight.js';
import { cutText, showBidi, markBidi } from './text.js';

/** Validate the whole document before offering a save; do not echo malformed secret text. */
export function parseEnvDocument(text: string): Record<string, object> {
  let doc: unknown;
  try { doc = JSON.parse(text); } catch { throw new Error('Invalid JSON'); }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc) ||
    Object.values(doc).some(v => !v || typeof v !== 'object' || Array.isArray(v)))
    throw new Error('Each environment must be a JSON object');
  return doc as Record<string, object>;
}

export type HttpClass = 'dev' | 'test' | 'prod';

export interface HttpEnv { name: string; cls: HttpClass }

export interface HttpReqBrief {
  index: number; name: string; method: string; url: string; line: number; start: number;
  /** as written in the file ({{variables}} unresolved - never their values) */
  headers?: [string, string][];
  body?: string;
  files?: string[];
  /** it has a response handler / pre-request script */
  script?: boolean;
}

export interface HttpEnvs {
  file: string;
  /** sha256 of the text the GUI got: a run of a file rewritten meanwhile is refused (409) */
  sha: string;
  env_dir: string | null;
  env_dirs: string[];
  envs: HttpEnv[];
  variables: Record<string, string[]>;
  requests: HttpReqBrief[];
  /** request URLs per environment, resolved as far as known before a run (secrets masked) */
  urls: Record<string, string[]>;
  ijhttp: string | null;
  installing: boolean;
  install_error: string;
}

export interface HttpTest { name: string; ok: boolean; message: string }

export interface HttpResult {
  name: string;
  method: string;
  url: string;
  status: number | null;
  status_text: string;
  headers: [string, string][];
  req_headers: [string, string][];
  body: string;
  body_size: number;
  body_cut?: boolean;
  time_ms: number | null;
  error: string;
  tests: HttpTest[];
  ok: boolean;
  /** ijhttp printed "Binary response body is ignored": no body here - save it to a file */
  binary?: boolean;
  /** save_to: the project file the whole response went to */
  saved?: string;
  saved_bytes?: number;
  saved_note?: string;
}

export type TunnelKind = 'auto' | 'none' | 'ssh' | 'stack';
export interface EnvCfg {
  tunnel?: { kind: TunnelKind; ssh?: string; forward?: string; target?: string; port?: number };
  /** null/absent = the default (class policy, the env file's SSLConfiguration) */
  verify_tls?: boolean;
}
export interface EnvTunnel {
  label?: string; source?: string; kind?: string; local?: number;
  state?: string; port?: number | null; url?: string; error?: string; test?: string; test_ok?: boolean;
  /** the route: localhost:<local> -> via (user@server / dev stack) -> to (host:port as the server sees it / env/target) */
  via?: string; to?: string;
}
export interface EnvCfgEnv extends HttpEnv {
  cfg: EnvCfg; tunnel: EnvTunnel; env_file_tls: boolean | null; insecure_by_class: boolean;
}
export interface EnvCfgInfo {
  env_dir: string | null;
  envs: EnvCfgEnv[];
  files: Record<'public' | 'private', { path: string; exists: boolean; hidden: boolean }>;
  targets: { env: string; target: string; kind?: string; available?: boolean; why?: string }[];
  targets_loaded?: boolean;
  env_files?: { path: string; exists: boolean; hidden: boolean }[];
}

export interface HttpRunBrief {
  id: string; file: string; env: string; cls: HttpClass; index: number | null; by: string; at: number; ms: number;
  ok: boolean; summary: string; share: boolean;
}

export interface HttpRun extends HttpRunBrief {
  project: string; rc: number; requests: HttpResult[]; output: string; changed: boolean;
  /** the run went through Piper's tunnel (its label) */
  tunnel?: string;
}

export interface HttpFileItem {
  file: string;
  requests: { index: number; name: string; method: string; url: string }[];
  last: HttpRunBrief | null;
  error?: string;
  /** hidden from Claude (isolation policy / exclude): the GUI runs it like any other */
  hidden?: boolean;
}

export interface HttpFiles { files: HttpFileItem[]; truncated: boolean; ijhttp: string | null }

/** per-tab state (Tab.http in files.ts) */
export interface HttpTab {
  info: HttpEnvs | null;
  env: string;
  running: number | 'all' | null;
  run: HttpRun | null;
  runs: HttpRunBrief[];
  error: string;
  /** results shown expanded (request positions in the run) */
  open: Set<number>;
  /** secret headers shown in clear */
  reveal: boolean;
  loaded: boolean;
  /** Piper's settings and the tunnels of the file's environments (GET /api/http/envcfg) */
  envcfg?: EnvCfgInfo | null;
  /** the redraw of the view showing this state now (a tab switch during a run builds a new view) */
  redraw?: () => void;
}

export interface HttpCtx {
  project: string;
  path: string;
  toast(text: string): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  /** the request lines are known (or changed): decorate the rendered rows */
  linesChanged(): void;
  /** after every redraw (environment picked, run finished): the HTTP tab redraws its request list */
  changed?(): void;
  /** the HTTP tab: results live in the request rows - the panel below shows only the run's summary line */
  compact?: boolean;
  /** a 409 "changed since" reloads by itself (the HTTP tab shows no code that would be stale) */
  autoReload?: boolean;
  /** open a project file in Files (a file hidden from Claude opens like any other, marked with the eye) */
  openFile?(path: string): void;
}

const q = (o: Record<string, string>) => new URLSearchParams(o).toString();
export const httpApi = {
  envs: (project: string, path: string) => call<HttpEnvs>('GET', `/api/http/envs?${q({ project, path })}`),
  files: (project: string) => call<HttpFiles>('GET', `/api/http/files?${q({ project })}`),
  runs: (project: string, path: string) => call<{ runs: HttpRunBrief[] }>('GET', `/api/http/runs?${q({ project, path })}`),
  run: (id: string) => call<HttpRun>('GET', `/api/http/run?${q({ id })}`),
  exec: (project: string, path: string, env: string, index: number | null, sha: string, saveTo = '') =>
    call<HttpRun>('POST', '/api/http/run', { project, path, env, sha, ...(index === null ? {} : { index }), ...(saveTo ? { save_to: saveTo } : {}) }),
  /** targets: also the dev stack's tunnel targets (slow when the stack scans: only for the dialog) */
  envcfg: (project: string, path: string, targets = false) =>
    call<EnvCfgInfo>('GET', `/api/http/envcfg?${q({ project, path, ...(targets ? { targets: '1' } : {}) })}`),
  saveEnvcfg: (project: string, path: string, env: string, cfg: EnvCfg) =>
    call<{ ok: boolean; cfg: EnvCfg }>('POST', '/api/http/envcfg', { project, path, env, cfg }),
  tunnel: (project: string, path: string, env: string, op: 'open' | 'close' | 'test') =>
    call<EnvTunnel>('POST', '/api/http/tunnel', { project, path, env, op }),
  share: (id: string, on: boolean) => call<{ id: string; share: boolean }>('POST', '/api/http/share', { id, on }),
  install: () => call<{ ijhttp: string | null; installing: boolean }>('POST', '/api/http/install', {}),
};

export const isHttpFile = (path: string) => /\.(http|rest)$/i.test(path);

/** an environment name short enough for a button ("test with tunel `ssh -L …`" -> "test with tunel");
 * the full name stays in the picker's title */
export function shortEnv(name: string): string {
  const s = name.split(/[`(]/)[0]!.trim() || name;
  // an environment's name comes from the project's env file: a bidi control is a mark
  return showBidi(s.length > 24 ? cutText(s, 23) + '…' : s);
}

export function newHttpTab(): HttpTab {
  return { info: null, env: '', running: null, run: null, runs: [], error: '', open: new Set(), reveal: false, loaded: false };
}

const ENV_KEY = 'piper.http.env.';
function savedEnv(project: string): string {
  try { return window.localStorage.getItem(ENV_KEY + project) ?? ''; } catch { return ''; }
}
function saveEnv(project: string, env: string, cls: HttpClass): void {
  if (cls === 'prod') return;   // never opens armed on production next time
  try { window.localStorage.setItem(ENV_KEY + project, env); } catch { /* private window: not remembered */ }
}

/** the environment to preselect: the remembered one when the file offers it (never a prod one), else the
 * first dev, else the first that is not prod, else the first */
export function pickEnv(envs: HttpEnv[], saved: string): string {
  if (envs.some((e) => e.name === saved && e.cls !== 'prod')) return saved;
  return (envs.find((e) => e.cls === 'dev') ?? envs.find((e) => e.cls !== 'prod') ?? envs[0])?.name ?? '';
}

const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
/** requests of a run that change data (anything but GET/HEAD/OPTIONS) */
export function writes(reqs: HttpReqBrief[]): HttpReqBrief[] {
  return reqs.filter((r) => !READ_METHODS.has(r.method.toUpperCase()));
}

export const SECRET_HEADER = /^(authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|api-key)$/i;

/** a body as the result panel shows it: JSON pretty-printed, XML/HTML highlighted, the rest plain */
export function prettyBody(body: string, ctype: string): { text: string; lang: Lang } {
  const t = body.trim();
  if (/json/i.test(ctype) || /^[[{]/.test(t)) {
    try { return { text: JSON.stringify(JSON.parse(t), null, 2), lang: 'json' }; } catch { /* not JSON after all */ }
  }
  if (/xml|html/i.test(ctype) || t.startsWith('<')) return { text: body, lang: 'xml' };
  return { text: body, lang: 'plain' };
}

const EXT: Record<string, string> = {
  'application/pdf': 'pdf', 'application/zip': 'zip', 'application/gzip': 'gz', 'image/png': 'png', 'image/jpeg': 'jpg',
  'image/gif': 'gif', 'image/webp': 'webp', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
  'application/vnd.ms-excel': 'xls', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'application/msword': 'doc', 'application/octet-stream': 'bin', 'text/csv': 'csv', 'application/json': 'json',
  'application/xml': 'xml', 'text/xml': 'xml', 'text/html': 'html', 'text/plain': 'txt',
};

/** a file name for a saved response: Content-Disposition's, else the request's name + the type's extension */
export function saveName(r: Pick<HttpResult, 'name' | 'headers'>): string {
  const hv = (k: string) => r.headers.find(([x]) => x.toLowerCase() === k)?.[1] ?? '';
  const cd = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(hv('content-disposition'));
  let name = '';
  if (cd) { try { name = decodeURIComponent(cd[1]!.trim()); } catch { name = cd[1]!.trim(); } }
  name = name.split(/[\\/]/).pop()!.replace(/^\.+/, '').replace(/[^\w.\- ]+/g, '_').trim();
  if (!name) {
    const ext = EXT[hv('content-type').split(';')[0]!.trim().toLowerCase()] ?? 'bin';
    name = `${(r.name || 'response').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'response'}.${ext}`;
  }
  return name.slice(0, 120);
}

/** what the certificate check does for an environment when Piper's setting is "default" */
export function tlsDefault(e: Pick<EnvCfgEnv, 'cls' | 'env_file_tls' | 'insecure_by_class'> & { tunnel?: EnvTunnel }): string {
  if (e.tunnel?.label) return 'not checked - through a tunnel the certificate is not for localhost';
  if (e.insecure_by_class) return `not checked - ${e.cls} (policy http.insecure)`;
  if (e.env_file_tls === false) return 'not checked - SSLConfiguration in the env file';
  return 'checked';
}

const CIRCLE = 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z';
const FILE = 'M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z';
const ICONS: Record<string, string[]> = {
  down: ['M6 9l6 6l6-6'], circle: [CIRCLE], dot: [CIRCLE, 'M12 11a1 1 0 1 0 0 2a1 1 0 1 0 0-2z'],
  check: [CIRCLE, 'M8.5 12l2.5 2.5l4.5-5'], alert: ['M12 4l9 16H3z', 'M12 10v4', 'M12 17h.01'],
  sliders: ['M20 7h-9', 'M14 17H5', 'M17 14a3 3 0 1 0 0 6a3 3 0 1 0 0-6z', 'M7 4a3 3 0 1 0 0 6a3 3 0 1 0 0-6z'],
  play: ['M7 4l13 8l-13 8z'], laptop: ['M4 5h16v11H4z', 'M2 20h20'],
  server: ['M3 3h18v8H3z', 'M3 13h18v8H3z', 'M7 7h.01', 'M7 17h.01'],
  target: [CIRCLE, 'M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10z', 'M12 11a1 1 0 1 0 0 2a1 1 0 1 0 0-2z'],
  arrow: ['M12 5v14', 'M18 13l-6 6l-6-6'], plug: ['M12 22v-5', 'M9 8V2', 'M15 8V2', 'M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z'],
  unplug: ['M19 5l3-3', 'M2 22l3-3', 'M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6l-2.3 2.3a2.4 2.4 0 0 0 0 3.4z', 'M7.5 13.5L10 11', 'M10.5 16.5L13 14', 'M12 6l6 6l2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0z'],
  pulse: ['M22 12h-4l-3 9L9 3l-3 9H2'], retry: ['M21 12a9 9 0 1 1-2.6-6.4L21 8', 'M21 3v5h-5'],
  x: ['M18 6L6 18', 'M6 6l12 12'], lock: ['M6 11h12v9H6z', 'M8 11V8a4 4 0 0 1 8 0v3'],
  file: [FILE, 'M14 3v5h5', 'M10 12l-2 2l2 2', 'M14 16l2-2l-2-2'], newfile: [FILE, 'M14 3v5h5', 'M12 12v6', 'M9 15h6'],
  eyeoff: ['M3 3l18 18', 'M10.6 6.1A10 10 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-3 3.6', 'M6.6 6.6C3.8 8.4 2 12 2 12s3.5 6 10 6a9.7 9.7 0 0 0 5.4-1.6', 'M9.9 9.9a3 3 0 0 0 4.2 4.2'],
};

function hic(name: string): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'hic');
  for (const d of ICONS[name] ?? []) {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

/** the chip's short name of a tunnel: "ssh test-k.example" / "stack test/app" */
export function tunnelName(t: EnvTunnel): string {
  if (t.kind === 'stack') return `stack ${t.to ?? ''}`.trim();
  const host = (t.via ?? '').replace(/^[^@]*@/, '').replace(/:\d+$/, '');
  return host ? `ssh ${host}` : 'tunnel';
}

/** the request index whose request line is line number n (1-based), or -1 */
export function requestAt(info: HttpEnvs | null, n: number): number {
  if (!info) return -1;
  const r = info.requests.find((x) => x.line + 1 === n);
  return r ? r.index : -1;
}

export function httpView(ctx: HttpCtx, st: HttpTab) {
  const bar = h('div', { class: 'httpbar' });
  const res = h('div', { class: 'httpres' });
  let installPoll = 0;
  st.redraw = () => draw();   // this view is the one on screen now
  const redraw = () => (st.redraw ?? draw)();

  async function load(): Promise<void> {
    try {
      const [{ data }, runs, cfg] = await Promise.all([httpApi.envs(ctx.project, ctx.path), httpApi.runs(ctx.project, ctx.path),
        httpApi.envcfg(ctx.project, ctx.path).catch(() => ({ data: null }))]);
      st.info = data;
      st.envcfg = cfg.data;
      st.runs = runs.data?.runs ?? [];
      st.error = '';
      if (data && (!st.env || !data.envs.some((e) => e.name === st.env))) st.env = pickEnv(data.envs, savedEnv(ctx.project));
      if (!st.run && st.runs[0]) {
        const r = await httpApi.run(st.runs[0].id);
        st.run = r.data;
      }
    } catch (e) {
      st.error = e instanceof Error ? e.message : String(e);
    }
    st.loaded = true;
    draw();
    ctx.linesChanged();
    resume();
  }

  /** Resume installation status polling when this retained view becomes visible again. */
  function resume(): void {
    if (st.info?.installing && !installPoll && bar.isConnected && bar.offsetParent !== null) {
      installPoll = window.setTimeout(() => { installPoll = 0; if (bar.isConnected && bar.offsetParent !== null) void load(); }, 3000);
    }
  }

  function cls(): HttpClass {
    if (!st.env) return 'dev';   // nothing to run: no reason to look like production
    return st.info?.envs.find((e) => e.name === st.env)?.cls ?? 'prod';
  }

  async function run(index: number | null, saveTo = ''): Promise<void> {
    const info = st.info;
    if (!info || st.running !== null) return;
    if (!info.ijhttp && !saveTo) return void ctx.toast('ijhttp is not installed - click Install');
    if (!st.env) return void ctx.toast('This file has no environment (http-client.env.json)');
    const reqs = index === null ? info.requests : info.requests.filter((r) => r.index === index);
    if (cls() === 'prod') return confirmProd(index, reqs, saveTo);
    await go(index, saveTo);
  }

  function confirmProd(index: number | null, reqs: HttpReqBrief[], saveTo = ''): void {
    const w = writes(reqs);
    const ack = h('input', { type: 'checkbox' }) as HTMLInputElement;
    const goBtn = h('button', { class: 'danger', disabled: w.length > 0, title: w.length ? 'tick "I know" first' : '',
      onclick: () => { ctx.closeModal(); void go(index, saveTo); } }, `Run on ${shortEnv(st.env)}`) as HTMLButtonElement;
    ack.addEventListener('change', () => { goBtn.disabled = !ack.checked; goBtn.title = ack.checked ? '' : 'tick "I know" first'; });
    const cancel = h('button', { onclick: () => ctx.closeModal() }, 'Cancel');
    ctx.modal(`Production · ${shortEnv(st.env)}`,
      h('p', {}, `${reqs.length === 1 ? 'This request goes' : `These ${reqs.length} requests go`} to `, h('strong', {}, 'production'), '.'),
      h('ul', { class: 'httpconfirm mono' }, ...reqs.map((r) => {
        const write = !READ_METHODS.has(r.method.toUpperCase());
        return h('li', { class: write ? 'w' : '' }, showBidi(`${r.method} ${st.info?.urls?.[st.env]?.[r.index] ?? r.url}`),
          write ? h('strong', {}, '  ← changes data') : null);
      })),
      w.length ? h('label', { class: 'httpack' }, ack, ` ${w.length} of them change data (${[...new Set(w.map((r) => r.method))].join(', ')}) - I know`) : null,
      h('div', { class: 'row end' }, cancel, goBtn));
    queueMicrotask(() => cancel.focus());   // Enter must not run it
  }

  let tick = 0;
  let startedAt = 0;
  async function go(index: number | null, saveTo = ''): Promise<void> {
    st.running = index === null ? 'all' : index;
    st.error = '';
    startedAt = Date.now();
    tick = window.setInterval(() => redraw(), 1000);   // the elapsed seconds
    draw();
    try {
      const { data } = await httpApi.exec(ctx.project, ctx.path, st.env, index, st.info?.sha ?? '', saveTo);
      st.run = data;
      const saved = data?.requests[0]?.saved;
      if (saveTo) ctx.toast(saved ? `Saved ${saved}` : `Not saved: ${data?.requests[0]?.error || 'see the result'}`);
      if (data?.tunnel) void refreshCfg();
      st.open = new Set(data && data.requests.length === 1 ? [0] : (data?.requests ?? []).map((r, i) => (r.ok ? -1 : i)).filter((i) => i >= 0));
      const runs = await httpApi.runs(ctx.project, ctx.path);
      st.runs = runs.data?.runs ?? st.runs;
    } catch (e) {
      st.error = e instanceof Error ? e.message : String(e);
      if (e instanceof ApiError && e.status === 409 && /changed since/.test(st.error)) {
        if (ctx.autoReload) {
          st.error = '';
          ctx.toast('The file changed (a session?) - reloaded, look at it and run again');
          void load();
        } else ctx.toast('The file changed (a session?) - reopen the file and look at it again');
      }
    }
    window.clearInterval(tick);
    st.running = null;
    redraw();
  }

  async function install(): Promise<void> {
    try {
      await httpApi.install();
      ctx.toast('Downloading ijhttp (~190 MB) from JetBrains…');
    } catch (e) {
      ctx.toast(e instanceof Error ? e.message : String(e));
    }
    void load();
  }

  async function showRun(id: string): Promise<void> {
    try {
      const { data } = await httpApi.run(id);
      st.run = data;
      st.open = new Set();
      draw();
    } catch (e) {
      ctx.toast(e instanceof Error ? e.message : String(e));
    }
  }

  async function share(on: boolean): Promise<void> {
    if (!st.run) return;
    try {
      await httpApi.share(st.run.id, on);
      st.run.share = on;
      const b = st.runs.find((r) => r.id === st.run?.id);
      if (b) b.share = on;
      draw();
    } catch (e) {
      ctx.toast(e instanceof Error ? e.message : String(e));
    }
  }

  function drawBar(): void {
    clear(bar);
    const info = st.info;
    if (!st.loaded) return void bar.append(h('span', { class: 'mut small' }, 'HTTP client…'));
    if (!info) return void bar.append(h('span', { class: 'bad small' }, st.error || 'not available'));
    if (!info.ijhttp) {
      bar.append(h('span', { class: 'small' }, info.installing ? 'Installing ijhttp (JetBrains HTTP Client CLI)…'
        : 'Running needs JetBrains\' ijhttp on the host.'));
      if (!info.installing) bar.append(h('button', { onclick: () => void install(), title: 'downloads https://jb.gg/ijhttp/latest into ijhttp in Piper\'s home (needs Java 17+)' }, 'Install ijhttp (~190 MB)'));
      if (info.install_error) bar.append(h('span', { class: 'bad small' }, info.install_error));
      return;
    }
    if (!info.envs.length) {
      bar.append(h('span', { class: 'mut small' }, `No environments: ${info.env_dir ?? 'no http-client.env.json in this project'}`));
    } else {
      const sel = h('select', { class: `httpenv c-${cls()}`, title: `environments of ${info.env_dir || '.'}/http-client.env.json` },
        ...info.envs.map((e) => h('option', { value: e.name, selected: e.name === st.env, title: showBidi(e.name) },
          e.name.toLowerCase() === e.cls ? e.name : `${shortEnv(e.name)} · ${e.cls}`))) as HTMLSelectElement;
      sel.title = markBidi(`${st.env} - environments of ${info.env_dir || '.'}/http-client.env.json`);
      sel.addEventListener('change', () => { st.env = sel.value; saveEnv(ctx.project, st.env, cls()); draw(); });
      bar.append(sel);
      const ec = envOf(st.env);
      const t = ec?.tunnel;
      if (t?.label || t?.error) {
        const open = t.state === 'open';
        const chip: HTMLButtonElement = h('button', { class: `httptun${t.error ? ' bad' : open ? ' ok' : ''}`, 'aria-haspopup': 'dialog',
          title: t.error ? `the tunnel: ${t.error}` : `${t.label} - open, close or test it`, onclick: () => tunnelPop(chip) },
        hic(t.error ? 'alert' : open ? 'dot' : 'circle'),
        t.error ? h('span', {}, 'tunnel') : h('span', { class: 'mono' }, tunnelName(t)),
        h('span', { class: 'httptun-st' }, t.error ? 'error' : open ? `:${t.local} open` : 'closed'), hic('down'));
        bar.append(chip);
      }
      if (st.envcfg) bar.append(h('button', { class: 'httpcfg', title: 'the environment\'s settings: tunnel, certificate check, env files',
        onclick: () => void envDialog() }, hic('sliders'), 'Environment'));
    }
    const prod = cls() === 'prod';
    bar.dataset.cls = cls();
    bar.append(h('button', { class: 'httprun' + (prod ? ' danger' : ''), disabled: st.running !== null || !st.env, onclick: () => void run(null),
      title: st.running !== null ? 'a run is in progress' : st.env ? `run every request of the file on ${st.env}` : 'no environment to run on: add one to http-client.env.json' },
    ...(st.running === 'all' ? ['Running…'] : [hic('play'), st.env ? `Run all on ${shortEnv(st.env)} (${info.requests.length})` : `Run all (${info.requests.length}) - no environment`])));
    if (st.running !== null) {
      const what = st.running === 'all' ? 'all requests' : showBidi(info.requests[st.running]?.name ?? '');
      bar.append(h('span', { class: 'httpbusy small' }, h('span', { class: 'spinner' }),
        ` ${what} · ${Math.round((Date.now() - startedAt) / 1000)} s`));
    }
    if (st.runs.length) {
      const hist = h('select', { class: 'httphist', 'aria-label': 'Run history', title: 'earlier runs of this file (yours and the sessions\')' },
        ...st.runs.map((r) => h('option', { value: r.id, selected: r.id === st.run?.id },
          `${r.ok ? '✓' : '✗'} ${shortEnv(r.env)} · ${r.index === null ? 'all' : '#' + (r.index + 1)} · ${r.by === 'gui' ? 'you' : 'session'} · ${ago(r.at)}`))) as HTMLSelectElement;
      hist.addEventListener('change', () => void showRun(hist.value));
      bar.append(hist);
    }
    if (st.error) bar.append(h('span', { class: 'bad small httperr' }, st.error));
  }

  function headerRows(list: [string, string][]): HTMLElement {
    return h('table', { class: 'httphdr mono' }, ...list.map(([k, v]) => h('tr', {},
      h('th', {}, showBidi(k)), h('td', {}, SECRET_HEADER.test(k) && !st.reveal ? '••••' : showBidi(v)))));
  }

  function bodyBox(r: HttpResult): HTMLElement {
    const ctype = r.headers.find(([k]) => k.toLowerCase() === 'content-type')?.[1] ?? '';
    const { text, lang } = prettyBody(r.body, ctype);
    const lines = text.split('\n');
    const toks = highlightLines(lines, lang);
    const pre = h('pre', { class: 'httpbody mono' });
    toks.forEach((row, i) => {
      for (const tk of row) pre.append(tk.c ? h('span', { class: 'hl-' + tk.c }, showBidi(tk.t)) : showBidi(tk.t));   // copy: the raw body
      if (i < toks.length - 1) pre.append('\n');
    });
    const copy = h('button', { class: 'link small', onclick: () => void navigator.clipboard?.writeText(r.body).then(() => ctx.toast('Copied')) }, 'copy');
    return h('div', { class: 'httpbodybox' },
      h('div', { class: 'mut small row' }, `${r.body_size} B${r.body_cut ? ' (cut for the GUI)' : ''}`, copy), pre);
  }

  function result(r: HttpResult, i: number): HTMLElement {
    const open = st.open.has(i);
    const failed = r.tests.filter((t) => !t.ok).length;
    const status = r.error ? 'error' : r.status === null ? '-' : String(r.status);
    const head = h('button', { class: 'httpreq-h', 'aria-expanded': String(open), onclick: () => {
      if (st.open.has(i)) st.open.delete(i); else st.open.add(i);
      draw();
    } },
    h('span', { class: `httpst ${r.ok ? 'ok' : 'bad'}` }, r.ok ? '✓' : '✗'),
    h('strong', {}, showBidi(r.name)),
    h('span', { class: `httpcode s${status[0]}` }, status),
    h('span', { class: 'mono small httpurl' }, showBidi(`${r.method} ${r.url}`)),
    h('span', { class: 'mut small' }, r.time_ms !== null ? `${r.time_ms} ms` : ''),
    r.tests.length ? h('span', { class: failed ? 'bad small' : 'mut small' }, `${r.tests.length - failed}/${r.tests.length} ${r.tests.length === 1 ? 'test' : 'tests'}`) : null);
    const box = h('div', { class: 'httpreq' + (open ? ' open' : '') }, head);
    if (!open) return box;
    box.append(...resultParts(r));
    return box;
  }

  /** what an expanded result shows: error, tests, headers, the body */
  function resultParts(r: HttpResult): HTMLElement[] {
    const out: HTMLElement[] = [];
    if (r.error) out.push(h('div', { class: 'bad small mono' }, showBidi(r.error)));
    if (r.tests.length) out.push(h('ul', { class: 'httptests' }, ...r.tests.map((t) => h('li', { class: t.ok ? 'ok' : 'bad' },
      `${t.ok ? '✓' : '✗'} ${showBidi(t.name)}`, t.message ? h('div', { class: 'mono small' }, showBidi(t.message)) : null))));
    if (r.headers.length) out.push(h('details', {}, h('summary', { class: 'small' }, `Response headers (${r.headers.length})`), headerRows(r.headers)));
    if (r.req_headers.length) out.push(h('details', {}, h('summary', { class: 'small' }, 'Request headers (as sent)'), headerRows(r.req_headers)));
    if (r.saved) {
      out.push(h('div', { class: 'httpsaved small' }, `Saved to ${r.saved} (${r.saved_bytes ?? 0} B)`,
        r.saved_note ? h('span', { class: 'mut' }, ` - ${r.saved_note}`) : null,
        h('button', { class: 'link small', onclick: () => void navigator.clipboard?.writeText(r.saved!).then(() => ctx.toast('Copied')) }, 'copy path')));
    } else if (r.status !== null && (r.binary || isBinaryResult(r))) {
      const ctype = r.headers.find(([k]) => k.toLowerCase() === 'content-type')?.[1] ?? '';
      const len = r.headers.find(([k]) => k.toLowerCase() === 'content-length')?.[1];
      const i = st.run ? indexOf(r) : -1;
      out.push(h('div', { class: 'httpbin small' },
        `Binary response (${ctype.split(';')[0] || 'unknown type'}${len ? `, ${len} B` : ''}) - ijhttp does not hand it over.`,
        i >= 0 ? h('button', { onclick: () => askSave(i, r) }, 'Save to file…') : null));
    } else if (r.status !== null) out.push(bodyBox(r));
    return out;
  }

  const isBinaryResult = (r: HttpResult) => !r.body && r.status !== null && /^(application\/(pdf|zip|gzip|octet-stream|vnd\.)|image\/)/i
    .test(r.headers.find(([k]) => k.toLowerCase() === 'content-type')?.[1] ?? '');

  /** the index in the file of a result of the shown run (a whole-file run: its position) */
  function indexOf(r: HttpResult): number {
    const run = st.run;
    if (!run) return -1;
    if (run.index !== null) return run.index;
    return run.requests.indexOf(r);
  }

  function askSave(index: number, r: HttpResult): void {
    const input = h('input', { type: 'text', class: 'mono', value: `development/${saveName(r)}`, style: 'width:100%' }) as HTMLInputElement;
    const ok = h('button', { class: 'primary', onclick: () => { const v = input.value.trim(); if (!v) return; ctx.closeModal(); void run(index, v); } }, 'Run and save');
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
    ctx.modal(`Save the response of ${showBidi(r.name)}`,
      h('p', { class: 'small' }, 'Piper runs this request once more (with the globals of the last run) and writes the whole response into the project. ',
        'Never over a file git tracks; a secret of the environment in a text response is masked.'),
      input,
      h('div', { class: 'row end' }, h('button', { onclick: () => ctx.closeModal() }, 'Cancel'), ok));
    queueMicrotask(() => { input.focus(); input.setSelectionRange(input.value.lastIndexOf('/') + 1, input.value.lastIndexOf('.') > 0 ? input.value.lastIndexOf('.') : input.value.length); });
  }

  // ---------------------------------------------------------------- environment settings (tunnel, certificate, env files)
  const envOf = (name: string) => st.envcfg?.envs.find((e) => e.name === name) ?? null;

  async function refreshCfg(): Promise<void> {
    try {
      st.envcfg = (await httpApi.envcfg(ctx.project, ctx.path)).data;
    } catch { /* an older piperd: no settings */ }
    redraw();
  }

  // ---- the tunnel chip's popover: the live tunnel (route, state) and what to do with it
  function tunnelPop(anchor: HTMLElement): void {
    const name = st.env;
    const pop = h('div', { class: 'httppop', role: 'dialog', 'aria-label': 'Tunnel', tabindex: '-1' });
    const shade = h('div', { class: 'httppop-shade', onclick: () => close() });
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); } };
    function close(): void { shade.remove(); pop.remove(); document.removeEventListener('keydown', onKey, true); }
    let busy: 'open' | 'close' | 'test' | null = null;
    let err = '';   // the last action failed (ssh refused, the dev stack down): shown until the next one
    let failed: 'open' | 'close' | 'test' = 'open';
    const paint = () => {
      const t = envOf(name)?.tunnel ?? {};
      const open = t.state === 'open';
      clear(pop);
      const [stIc, stCls, stTxt] = err ? ['alert', 'bad', failed === 'close' ? 'cannot close' : failed === 'test' ? 'test failed' : 'cannot open'] : t.error ? ['alert', 'bad', 'not usable']
        : open ? ['dot', 'ok', `open on 127.0.0.1:${t.port}`] : ['circle', 'mut', 'closed - the next run opens it'];
      pop.append(h('div', { class: 'httppop-h' }, h('span', { class: 'httppop-k' }, 'Tunnel'), h('span', { class: 'grow' }),
        h('span', { class: `httppop-st ${stCls}` }, hic(stIc), stTxt)));
      if (t.via) {
        const hop = (icon: string, text: string, note: string) => h('div', { class: 'hop' }, hic(icon), h('span', { class: 'mono' }, text), h('span', { class: 'grow' }), h('span', { class: 'hop-n' }, note));
        pop.append(h('div', { class: 'httproute' },
          hop('laptop', `localhost:${t.local ?? '?'}`, 'the URLs of the env'), h('div', { class: 'hop-a' }, hic('arrow')),
          hop('server', t.via, t.kind === 'stack' ? 'port-forward' : 'ssh'), h('div', { class: 'hop-a' }, hic('arrow')),
          hop('target', t.to ?? '?', t.kind === 'stack' ? 'stack target' : 'as the server sees it')));
        const from = t.source === 'Piper settings' ? 'From Piper\'s settings' : `From the ${t.source || 'env file'}`;
        pop.append(h('div', { class: 'httppop-note' }, `${from} · Piper opens it for every run (yours and the sessions')`));
      }
      if (t.error || err) pop.append(h('div', { class: 'httppop-err' }, hic('alert'), h('span', {}, err || t.error || '')));
      if (!t.error) {
        const b = (op: 'open' | 'close' | 'test', icon: string, label: string, primary = false) => h('button', {
          class: primary ? 'primary' : '', disabled: busy !== null, onclick: () => void act(op) },
        busy === op ? h('span', { class: 'spinner' }) : hic(icon), label);
        pop.append(h('div', { class: 'httppop-a' },
          err ? b(failed, 'retry', 'Try again') : open ? b('close', 'unplug', 'Close tunnel') : b('open', 'plug', 'Open tunnel', true),
          err ? null : b('test', 'pulse', 'Test connection')));
        if (t.test && !err) pop.append(h('div', { class: `httppop-test ${t.test_ok ? 'ok' : 'bad'}` }, hic(t.test_ok ? 'check' : 'alert'), h('span', {}, t.test)));
      }
      pop.append(h('div', { class: 'httppop-sep' }),
        h('button', { class: 'link httppop-f', onclick: () => { close(); void envDialog(); } }, hic('sliders'), 'Tunnel settings…'));
    };
    async function act(op: 'open' | 'close' | 'test'): Promise<void> {
      busy = op;
      err = '';
      paint();
      try {
        const r = (await httpApi.tunnel(ctx.project, ctx.path, name, op)).data;
        const e = envOf(name);
        if (r && e) {
          const { test: _old, test_ok: _ok, ...rest } = e.tunnel;   // a test result belongs to the state it tested
          e.tunnel = { ...rest, ...r };
        }
      } catch (x) {
        err = x instanceof Error ? x.message : String(x);
        failed = op;
      }
      busy = null;
      if (pop.isConnected) paint();
      redraw();   // the chip shows the new state
    }
    paint();
    const r = anchor.getBoundingClientRect();
    pop.style.top = `${r.bottom + 6}px`;
    pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 428, r.left))}px`;
    document.addEventListener('keydown', onKey, true);
    document.body.append(shade, pop);
    pop.focus({ preventScroll: true });   // Tab goes on to its buttons
  }

  async function envFileEditor(path: string): Promise<void> {
    try {
      const r = (await call<{ text: string; sha: string; exists: boolean }>('GET',
        `/api/http/envfile?${q({ project: ctx.project, path })}`)).data;
      if (!r) throw new Error('Could not load environment file');
      const editor = h('textarea', { class: 'mono', 'aria-label': 'Environment JSON', spellcheck: 'false',
        style: 'width:100%;min-height:260px;resize:vertical' }) as HTMLTextAreaElement;
      editor.value = r.text;
      const message = h('div', { class: 'small bad', role: 'alert' });
      const validate = () => parseEnvDocument(editor.value);
      const save = h('button', { class: 'primary', onclick: async () => {
        message.textContent = '';
        save.disabled = true;
        try {
          validate();
          await call('POST', '/api/http/envfile', { project: ctx.project, path, text: editor.value, sha: r.sha });
          ctx.closeModal();
          ctx.toast('Environment file saved');
          await load();
        } catch (x) { message.textContent = markBidi(x instanceof Error ? x.message : String(x)); save.disabled = false; }
      } }, 'Save') as HTMLButtonElement;
      ctx.modal('Edit environments', h('div', { class: 'httpenvdlg' },
        h('div', { class: 'hed-head' }, h('h3', {}, 'Edit environments'), h('div', { class: 'mono' }, showBidi(path))),
        h('div', { class: 'hed-body' }, h('p', {}, 'Edit JSON environments. Agent writes require approval.'), editor, message),
        h('div', { class: 'hed-foot' }, h('button', { onclick: () => {
          try { editor.value = JSON.stringify(validate(), null, 2) + '\n'; message.textContent = ''; }
          catch (x) { message.textContent = markBidi(x instanceof Error ? x.message : String(x)); }
        } }, 'Format JSON'), h('span', { class: 'grow' }),
        h('button', { onclick: () => ctx.closeModal() }, 'Cancel'), save)));
      editor.focus();
    } catch (x) { ctx.toast(x instanceof Error ? x.message : String(x)); }
  }

  // ---- ⚙ Environment: the settings of the picked environment (configuration only - the chip acts on the tunnel)
  async function envDialog(): Promise<void> {
    if (!st.envcfg) return void ctx.toast('Environment settings need a newer piperd (restart it)');
    let info: EnvCfgInfo = st.envcfg;
    try {   // with the dev stack's tunnel targets (the bar's load skips them: it can take long)
      const r = (await httpApi.envcfg(ctx.project, ctx.path, true)).data;
      if (r) info = st.envcfg = r;
    } catch { /* the dialog still works without the targets */ }
    const e = envOf(st.env);
    if (!e) {   // 9.37: Settings › Edit environments… lands here also in a project that has none yet
      if (!info.envs.length) return envFileEditor(info.files.public.path);
      return void ctx.toast('Pick an environment first');
    }
    const t0 = e.cfg.tunnel ?? { kind: 'auto' as TunnelKind };
    let kind: TunnelKind = t0.kind;
    let vt: '' | 'true' | 'false' = e.cfg.verify_tls === true ? 'true' : e.cfg.verify_tls === false ? 'false' : '';
    const input = (value: string, placeholder: string, cls = '') => h('input', { type: 'text', class: `mono ${cls}`, value, placeholder, spellcheck: 'false' }) as HTMLInputElement;
    const sshIn = input(t0.ssh ?? '', 'user@server');
    const fwdIn = input(t0.forward ?? '', '192.0.2.1:443');
    const portIn = input(t0.port ? String(t0.port) : '', 'from the URLs', 'short');
    portIn.inputMode = 'numeric';
    const tgts = info.targets.filter((x) => x.env === e.cls);
    if (t0.target && !tgts.some((x) => `${x.env}/${x.target}` === t0.target)) {   // stored, but the stack does not list it now
      const [te, ...tt] = t0.target.split('/');
      tgts.unshift({ env: te ?? e.cls, target: tt.join('/'), why: '' });
    }
    const tgtSel = h('select', {}, ...(tgts.length ? tgts : [{ env: e.cls, target: '', why: 'no dev stack tunnel targets for this class (is the stack running?)' }]).map((x) =>
      h('option', { value: x.target ? `${x.env}/${x.target}` : '', selected: t0.target === `${x.env}/${x.target}`, disabled: !x.target },
        x.target ? `${x.env}/${x.target}${x.kind ? ` (${x.kind})` : ''}` : x.why ?? ''))) as HTMLSelectElement;
    const field = (label: string, control: HTMLElement, hint: string) => h('div', { class: 'hed-field' },
      h('label', {}, label), h('div', { class: 'hed-ctl' }, control, h('div', { class: 'hed-hint' }, hint)));
    const portField = field('Local port', portIn, `the port in the URLs (localhost:${e.tunnel.local ?? '<port>'}) - empty = from the URLs`);
    const seg = h('div', { class: 'hed-seg', role: 'radiogroup', 'aria-label': 'Tunnel' });
    const modeEl = h('div', { class: 'hed-mode' });
    const paintMode = () => {
      const had = seg.contains(document.activeElement);   // N10-C-9: a redraw keeps the keyboard on the choice
      clear(seg);
      for (const [k, l] of [['auto', 'Automatic'], ['none', 'None'], ['ssh', 'SSH'], ['stack', 'Dev stack']] as [TunnelKind, string][]) {
        seg.append(h('button', { class: kind === k ? 'on' : '', role: 'radio', 'aria-checked': String(kind === k), onclick: () => { kind = k; paintMode(); dirty(); } }, l));
      }
      if (had) seg.querySelector<HTMLElement>('[aria-checked=true]')?.focus();
      clear(modeEl);
      const t = e.tunnel;
      if (kind === 'auto') {
        modeEl.append(h('p', {}, 'Piper takes the tunnel from the env file ("piper": {…}) or from the environment\'s name (ssh -L …).'));
        if (t0.kind !== 'auto') modeEl.append(h('div', { class: 'hed-found' }, hic('circle'), h('div', {}, h('div', {}, 'Save to see what Piper finds.'))));
        else if (t.error) modeEl.append(h('div', { class: 'hed-found bad' }, hic('alert'), h('div', {}, h('div', {}, t.error))));
        else if (t.label) {
          modeEl.append(h('div', { class: 'hed-found' }, h('span', { class: 'ok' }, hic('check')), h('div', {},
            h('div', { class: 'mono' }, `Found: ${t.label.replace(' -> ', ' → ')}`),
            h('div', { class: 'hed-hint' }, `from the ${t.source || 'env file'} · the URLs use localhost:${t.local}`))));
        } else modeEl.append(h('div', { class: 'hed-found' }, hic('circle'), h('div', {}, h('div', {}, 'Nothing found - the requests go straight to the environment\'s URLs.'))));
      } else if (kind === 'none') {
        modeEl.append(h('p', {}, 'Piper opens no tunnel - the requests go straight to the environment\'s URLs. Use it when you open the tunnel yourself.'));
      } else if (kind === 'ssh') {
        modeEl.append(h('p', {}, 'Piper runs ssh -L itself (BatchMode: your key must work without a prompt).'),
          h('div', { class: 'hed-fields' }, field('Server', sshIn, 'user@server, optionally :port'),
            field('Forward to', fwdIn, 'host:port as the server sees it'), portField));
      } else {
        modeEl.append(h('p', {}, `Piper opens a tunnel of the dev stack in the ${e.cls} class.`),
          h('div', { class: 'hed-fields' }, field('Target', tgtSel, info.targets_loaded ? 'the tunnel targets the dev stack offers' : 'the dev stack did not answer'), portField));
      }
    };
    const certEl = h('div', { class: 'hed-radios', role: 'radiogroup', 'aria-label': 'Certificate check' });
    const paintCert = () => {
      const had = certEl.contains(document.activeElement);
      clear(certEl);
      const opts: ['' | 'true' | 'false', string, string][] = [['', 'Default', tlsDefault(e)],
        ['true', 'Check the certificate', e.tunnel.label ? 'fails through a tunnel' : 'it must be valid for the host'],
        ['false', 'Don\'t check', 'self-signed or issued for another name']];
      for (const [v, l, d] of opts) {
        certEl.append(h('button', { class: vt === v ? 'on' : '', role: 'radio', 'aria-checked': String(vt === v), onclick: () => { vt = v; paintCert(); dirty(); } },
          hic(vt === v ? 'dot' : 'circle'), h('strong', {}, l), h('span', {}, d)));
      }
      if (had) certEl.querySelector<HTMLElement>('[aria-checked=true]')?.focus();
    };
    const fileRow = (f: EnvCfgInfo['files']['public']) => {
      return h('div', { class: 'hed-file' + (f.exists ? '' : ' none') }, hic(f.exists ? 'file' : 'newfile'), h('span', { class: 'mono' }, showBidi(f.path)),
        f.hidden ? h('span', { class: 'hed-tag' }, hic('eyeoff'), 'hidden from Claude') : null, h('span', { class: 'grow' }),
        h('button', { class: 'link', onclick: () => void envFileEditor(f.path) }, f.exists ? 'Edit JSON' : 'Create JSON'));
    };
    const msgEl = h('div', { class: 'small bad' });
    const cfgOf = (): EnvCfg => {
      const out: EnvCfg = {};
      if (kind !== 'auto') {
        out.tunnel = { kind };
        if (kind === 'ssh') { out.tunnel.ssh = sshIn.value.trim(); out.tunnel.forward = fwdIn.value.trim(); }
        if (kind === 'stack') out.tunnel.target = tgtSel.value;
        if ((kind === 'ssh' || kind === 'stack') && portIn.value.trim()) out.tunnel.port = Number(portIn.value.trim());
      }
      if (vt) out.verify_tls = vt === 'true';
      return out;
    };
    const save = h('button', { class: 'primary', onclick: async () => {
      clear(msgEl);
      save.disabled = true;
      try {
        await httpApi.saveEnvcfg(ctx.project, ctx.path, e.name, cfgOf());
        ctx.closeModal();
        ctx.toast(`Saved the settings of ${shortEnv(e.name)}`);
        await refreshCfg();
      } catch (x) {
        msgEl.textContent = markBidi(x instanceof Error ? x.message : String(x));
        dirty();
      }
    } }, 'Save') as HTMLButtonElement;
    const initial = JSON.stringify(cfgOf());
    function dirty(): void { save.disabled = JSON.stringify(cfgOf()) === initial; }
    for (const x of [sshIn, fwdIn, portIn]) x.addEventListener('input', dirty);
    tgtSel.addEventListener('change', dirty);
    paintMode();
    paintCert();
    dirty();
    ctx.modal(`Environment settings · ${shortEnv(e.name)}`, h('div', { class: 'httpenvdlg' },
      h('div', { class: 'hed-head' },
        h('div', { class: 'hed-title' }, h('h3', {}, 'Environment settings'), h('span', { class: 'grow' }),
          h('button', { class: 'hed-x', 'aria-label': 'Close', onclick: () => ctx.closeModal() }, hic('x'))),
        h('div', { class: 'hed-env' }, h('span', { class: `hed-cls c-${e.cls}` }, e.cls), h('span', { class: 'mono', title: showBidi(e.name) }, showBidi(e.name)))),
      h('div', { class: 'hed-body' },
        h('section', {}, h('h4', {}, 'Tunnel'), h('p', { class: 'hed-d' }, 'How the requests reach a server that is not directly reachable.'), seg, modeEl),
        h('section', {}, h('h4', {}, 'Certificate check'), certEl),
        h('section', {}, h('h4', {}, 'Env files'),
          h('p', { class: 'hed-d' }, 'Edit environment variables here. Agents can propose changes only through an approval card.'),
          h('div', { class: 'hed-files' }, ...(info.env_files?.length ? info.env_files : Object.values(info.files)).map(fileRow))),
        msgEl),
      h('div', { class: 'hed-foot' }, hic('lock'), h('span', { class: 'grow' }, 'Stored in Piper on this machine - never in git, never seen by a session.'),
        h('button', { onclick: () => ctx.closeModal() }, 'Cancel'), save)));
    queueMicrotask(() => seg.querySelector<HTMLElement>('[aria-checked=true]')?.focus());   // N10-C-9: the focus was on body
  }

  /** the shown run is of another environment than the one picked: its results are not about these URLs */
  const stale = () => !!st.run && !!st.env && st.run.env !== st.env;

  function drawRes(): void {
    clear(res);
    res.classList.toggle('busy', st.running !== null);
    res.classList.toggle('stale', stale());
    const r = st.run;
    if (!r) {
      res.classList.add('idle');   // not `.empty`: that global class centres with margin:auto
      if (st.info?.ijhttp && st.info.requests.length) res.append(h('div', { class: 'mut small' }, ctx.compact
        ? 'Pick a request to see its details; ▶ runs it alone (with the globals of the last run).'
        : '▶ next to a request runs it alone (with the globals of the last run), ▶ Run all runs the file.'));
      return;
    }
    res.classList.remove('idle');
    const who = r.by === 'gui' ? 'you' : 'a session';
    const shareBox = h('input', { type: 'checkbox', checked: r.share }) as HTMLInputElement;
    shareBox.addEventListener('change', () => void share(shareBox.checked));
    const secrets = r.requests.some((q) => [...q.headers, ...q.req_headers].some(([k]) => SECRET_HEADER.test(k)));
    res.append(
      h('div', { class: 'httpres-h' },
        h('span', { class: `httpst ${r.ok ? 'ok' : 'bad'}` }, r.ok ? '✓' : '✗'),
        h('strong', {}, r.summary),
        h('span', { class: `httpcls c-${r.cls}`, title: r.env }, shortEnv(r.env)),
        stale() ? h('span', { class: 'httpstale small', title: `the picked environment is ${st.env}` }, `results are from ${shortEnv(r.env)}, not ${shortEnv(st.env)}`) : null,
        h('span', { class: 'mut small' }, `${r.index === null ? 'whole file' : 'request #' + (r.index + 1)} · ${who} · ${ago(r.at)} · ${r.ms} ms`),
        h('span', { class: 'grow' }),
        secrets ? h('button', { class: 'link small', onclick: () => { st.reveal = !st.reveal; draw(); } }, st.reveal ? 'hide secret headers' : 'show secret headers') : null,
        h('label', { class: 'small httpshare', title: 'a session that asks for the last result gets the (scrubbed) response bodies too' },
          shareBox, ' Sessions can read the bodies')));
    if (!ctx.compact) res.append(...r.requests.map(result));
    if (!r.requests.length && r.output) res.append(h('pre', { class: 'httpbody mono bad' }, r.output));
  }

  function draw(): void {
    drawBar();
    drawRes();
    ctx.changed?.();
  }

  /** expand the result of request `name` of the shown run and bring it into view */
  function reveal(name: string): void {
    const i = st.run?.requests.findIndex((r) => r.name === name) ?? -1;
    if (i < 0) return;
    st.open.add(i);
    draw();
    res.querySelectorAll('.httpreq')[i]?.scrollIntoView({ block: 'nearest' });
  }

  return { bar, res, load, resume, run, draw, reveal, resultParts, stale, cls, envDialog, running: () => st.running };
}
