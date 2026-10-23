// The HTTP tab of a session (httpclient.py): every .http file of the project in one list - grouped
// by directory, with its requests and how its last run ended - and, for the picked file, the same
// runner as in the file browser (environment, ▶ Run all, results) with the requests as rows: ▶ runs
// one, a click on its name opens the line in Files. Hidden files never appear (piperd lists only
// what the session's isolation rule shows).

import { api } from './api.js';
import { ago, clear, fi, h } from './dom.js';
import { showBidi, markBidi } from './text.js';
import { httpApi, httpView, newHttpTab, shortEnv, type HttpFileItem, type HttpReqBrief, type HttpResult, type HttpTab } from './httpview.js';

export interface HttpPanelDeps {
  toast(text: string): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  /** open a file at a line in the Files tab */
  openFile(project: string, path: string, line: number): void;
}

/** files grouped by directory, in path order ('' = the project root) */
export function groupByDir(files: HttpFileItem[]): [string, HttpFileItem[]][] {
  const out = new Map<string, HttpFileItem[]>();
  for (const f of [...files].sort((a, b) => a.file.localeCompare(b.file))) {
    const i = f.file.lastIndexOf('/');
    const dir = i < 0 ? '' : f.file.slice(0, i);
    if (!out.has(dir)) out.set(dir, []);
    out.get(dir)!.push(f);
  }
  return [...out.entries()];
}

/** does a file match the filter (its path or one of its requests' names / URLs)? */
export function matchesFilter(f: HttpFileItem, q: string): boolean {
  const s = q.trim().toLowerCase();
  if (!s) return true;
  return f.file.toLowerCase().includes(s) || f.requests.some((r) => r.name.toLowerCase().includes(s) || r.url.toLowerCase().includes(s));
}

/** the file to open when nothing is picked: the one run last, else the only one */
export function newestRun(files: HttpFileItem[]): string {
  const ran = files.filter((f) => f.last).sort((a, b) => (b.last!.at - a.last!.at));
  return ran[0]?.file ?? (files.length === 1 ? files[0]!.file : '');
}

/** F09: the header shows the HTTP tab only when it makes sense - the module is on and the project has a .http file,
 *  or the tab is the one shown now (it never disappears under the user) */
export function httpTabWanted(hasHttpFiles: boolean, moduleOn: boolean, shown = false): boolean {
  return moduleOn && (hasHttpFiles || shown);
}

/** F09: what "New request file…" creates - development/<project>.http with one request and, beside it, an
 *  http-client.env.json with an empty dev.host (only when there is none; fsCreate never replaces a file) */
export function newRequestFiles(project: string): { dir: string; file: string; text: string; env: string; envText: string } {
  const base = (project.split('/').filter(Boolean).pop() ?? '').replace(/[^\w.-]+/g, '-').replace(/^[.-]+/, '') || 'requests';
  return { dir: 'development', file: `development/${base}.http`, text: 'GET {{host}}/\n',
    env: 'development/http-client.env.json', envText: JSON.stringify({ dev: { host: '' } }, null, 2) + '\n' };
}

/** Source lines in a request header/body still select that request, not only the METHOD line. */
export function requestForLine<T extends { start: number }>(requests: T[], line: number): T | undefined {
  return [...requests].reverse().find(r => r.start + 1 <= line) ?? requests[0];
}

export function createHttpPanel(deps: HttpPanelDeps) {
  const listEl = h('div', { class: 'hplist' });
  const filterEl = h('input', { type: 'search', placeholder: 'Filter requests', title: 'filter files, requests, URLs', class: 'hpfilter' }) as HTMLInputElement;
  const countEl = h('span', { class: 'mut small' });
  const right = h('div', { class: 'hpright' });
  const el = h('div', { class: 'httppanel' },
    h('div', { class: 'hpleft' },
      h('div', { class: 'hphead' }, filterEl,
        h('button', { title: 'look for .http files again', 'aria-label': 'look for .http files again', onclick: () => void load(true) }, fi('refresh', 'gi')), countEl),
      listEl),
    right);

  let project = '';
  let files: HttpFileItem[] = [];
  let loadedFor = '';
  let loadedAt = 0;
  let error = '';
  let truncated = false;
  const picked = new Map<string, string>();        // project -> file
  const states = new Map<string, HttpTab>();       // project + file -> runner state (env, last run, results)
  let current: ReturnType<typeof httpView> | null = null;
  let currentLoad: Promise<void> = Promise.resolve();   // the shown runner's first load (envs, tunnel settings)
  let reqsEl: HTMLElement | null = null;

  filterEl.addEventListener('input', () => drawList());

  async function load(force = false): Promise<void> {
    if (!project || (!force && loadedFor === project)) return;
    const want = project;
    countEl.textContent = 'looking…';
    try {
      const { data } = await httpApi.files(want);
      if (want !== project) return;
      files = data?.files ?? [];
      truncated = !!data?.truncated;
      error = '';
      loadedFor = want;
      loadedAt = Date.now();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      files = [];
    }
    drawList();
    const sel = picked.get(project);
    const newest = newestRun(files);
    if (!sel && newest) pick(newest);
    else if (!current || !files.some((f) => f.file === sel)) drawRight();   // the runner shown stays (its results)
  }

  function badge(f: HttpFileItem): HTMLElement | null {
    const r = f.last;
    if (!r) return null;
    return h('span', { class: `hpbadge ${r.ok ? 'ok' : 'bad'}`, title: `${r.summary} · ${r.env} · ${r.by === 'gui' ? 'you' : 'a session'}` },
      `${r.ok ? '✓' : '✗'} ${shortEnv(r.env)} · ${ago(r.at)}`);
  }

  function drawList(): void {
    clear(listEl);
    if (!project) return void listEl.append(h('p', { class: 'mut small' }, 'Pick a project session.'));
    if (error) return void listEl.append(h('p', { class: 'bad small' }, error));
    const shown = files.filter((f) => matchesFilter(f, filterEl.value));
    countEl.textContent = markBidi(`${shown.length}${shown.length !== files.length ? ' of ' + files.length : ''} file${files.length === 1 ? '' : 's'}${truncated ? ' (first 400)' : ''}`);
    if (!files.length && loadedFor === project) return;   // F09: one empty state on the right, the list is hidden
    // N10-C-10: a filter without a match said nothing in the list
    if (!shown.length && files.length) return void listEl.append(h('p', { class: 'mut small' }, showBidi(`No file, request or URL matches "${filterEl.value.trim()}".`)));
    const sel = picked.get(project);
    for (const [dir, group] of groupByDir(shown)) {
      listEl.append(h('div', { class: 'hpdir mut small' }, dir || '(project root)'));
      for (const f of group) {
        const name = showBidi(f.file.slice(f.file.lastIndexOf('/') + 1));
        listEl.append(h('button', { class: 'hpfile' + (f.file === sel ? ' on' : ''), title: showBidi(f.file), onclick: () => pick(f.file) },
          h('span', { class: 'hpname' }, name),
          f.hidden ? h('span', { class: 'fi fi-hidden hid-mark', title: 'hidden from Claude (isolation policy / exclude): sessions never see it' }) : null,
          h('span', { class: 'mut small' }, f.error ? 'unreadable' : `${f.requests.length} req`),
          badge(f)));
      }
    }
  }

  function pick(file: string): void {
    picked.set(project, file);
    drawList();
    drawRight();
  }

  function drawRight(): void {
    clear(right);
    current = null;
    reqsEl = null;
    const file = picked.get(project);
    const item = files.find((f) => f.file === file);
    // F09: no .http file - one empty state with one action instead of two empty planes
    const none = !files.length && loadedFor === project && !!project && !error;
    el.classList.toggle('hp-none', none);
    if (none) {
      right.append(h('div', { class: 'fempty hp-empty' },
        h('h3', {}, showBidi(`No .http files in ${project}`)),
        h('p', { class: 'mut' }, 'Requests for the IntelliJ HTTP Client - run by ijhttp, variables from http-client.env.json.'),
        h('div', { class: 'hp-empty-acts' }, h('button', { class: 'primary', title: showBidi(`creates ${newRequestFiles(project).file} with GET {{host}}/ and an http-client.env.json with an empty dev.host`),
          onclick: (e: Event) => void createFirst(e.currentTarget as HTMLButtonElement) }, 'New request file…'))));
      return;
    }
    if (!file || !item) {
      right.append(h('div', { class: 'fempty' }, h('p', { class: 'mut' },
        files.length ? 'Pick a .http file on the left - ▶ runs a request, Open in Files edits it.' : error ? 'The .http files could not be listed.' : 'Looking for .http files…')));
      return;
    }
    const key = project + '\0' + file;
    const st = states.get(key) ?? newHttpTab();
    states.set(key, st);
    const proj = project;
    const hv = httpView({
      project: proj, path: file, toast: deps.toast, modal: deps.modal, closeModal: deps.closeModal,
      openFile: (p) => deps.openFile(proj, p, 1),
      linesChanged: () => drawReqs(st, item),
      changed: () => { drawReqs(st, item); syncBadge(item, st); },
      compact: true, autoReload: true,
    }, st);
    current = hv;
    reqsEl = h('div', { class: 'hpreqs', 'aria-label': 'requests' });
    reqsEl.addEventListener('keydown', (e) => onReqKey(e, st, item));
    right.append(
      h('div', { class: 'fhead' }, h('strong', { class: 'fpath' }, showBidi(file)),
        h('div', { class: 'actions' }, h('button', { title: 'open the file in Files (edit it there)', onclick: () => deps.openFile(proj, file, 1) }, 'Open in Files'))),
      hv.bar, reqsEl, hv.res);
    hv.draw();
    currentLoad = hv.load();
  }

  /** F09: create development/<project>.http (+ the env file when missing), then show it here */
  async function createFirst(btn: HTMLButtonElement): Promise<void> {
    const p = project;
    const n = newRequestFiles(p);
    btn.disabled = true;
    const exists = (e: unknown) => e instanceof Error && /already exists/.test(e.message);
    try {
      await api.fsCreate(p, n.dir, 'dir').catch((e) => { if (!exists(e)) throw e; });
      await api.fsCreate(p, n.file, 'file', n.text);
      await api.fsCreate(p, n.env, 'file', n.envText).catch((e) => { if (!exists(e)) throw e; });
    } catch (e) {
      btn.disabled = false;
      return deps.toast(e instanceof Error ? e.message : String(e));
    }
    if (p !== project) return;
    picked.set(p, n.file);
    await load(true);
    deps.toast(`Created ${n.file} - set dev.host in ${n.env}`);
  }

  /** the left list shows how the last run ended - also right after one here */
  function syncBadge(old: HttpFileItem, st: HttpTab): void {
    const item = files.find((f) => f.file === old.file) ?? old;   // the list may have been reloaded meanwhile
    const r = st.run;
    if (!r || item.last?.id === r.id) return;
    item.last = { id: r.id, file: r.file, env: r.env, cls: r.cls, index: r.index, by: r.by, at: r.at, ms: r.ms, ok: r.ok,
      summary: r.summary, share: r.share };
    drawList();
  }

  function drawReqs(st: HttpTab, item: HttpFileItem): void {
    if (!reqsEl) return;
    const focused = (document.activeElement as HTMLElement | null)?.dataset?.req;
    clear(reqsEl);
    const info = st.info;
    const reqs: HttpReqBrief[] = info?.requests ?? item.requests.map((r) => ({ ...r, line: -1, start: -1 }));
    const urls = info?.urls?.[st.env];
    const results = new Map<string, HttpResult>();
    for (const r of st.run?.requests ?? []) results.set(r.name, r);
    const open = expandedOf(item.file);
    const stale = current?.stale() ?? false;
    const cls = current?.cls() ?? 'dev';
    right.classList.remove('c-dev', 'c-test', 'c-prod');
    right.classList.add('c-' + cls);
    reqsEl.classList.toggle('stale', stale);
    for (const r of reqs) {
      const res = results.get(r.name);
      const isOpen = open.has(r.index);
      const running = st.running === r.index || st.running === 'all';
      const why = !info?.ijhttp ? 'install ijhttp first (the bar above)' : st.running !== null ? 'a run is in progress'
        : !st.env ? 'no environment to run on: add one to http-client.env.json'   // as Run all: never "alone on  ("
        : `run ${showBidi(r.name)} alone on ${st.env} (with the globals of the last run)`;
      const runBtn = (klass: string, label: string) => h('button', { class: klass, title: why, 'aria-label': st.env ? `Run ${showBidi(r.name)} on ${st.env}` : `Run ${showBidi(r.name)}`,
        disabled: st.running !== null || !info?.ijhttp || !st.env, onclick: (e: Event) => { e.stopPropagation(); void current?.run(r.index); } },
      running && klass === 'hrun' ? h('span', { class: 'spinner' }) : label);
      const status = res ? (res.error ? 'error' : String(res.status ?? '-')) : '';
      // the row: a click anywhere folds/unfolds it; the real controls are the toggle button and ▶
      const head = h('div', { class: 'hpreq-h', onclick: () => toggle(item.file, r.index),
        ondblclick: () => deps.openFile(project, item.file, r.line >= 0 ? r.line + 1 : 1) },
      runBtn('hrun', '▶'),
      h('button', { class: 'hpreq-toggle', 'aria-expanded': String(isOpen), 'data-req': String(r.index),
        title: isOpen ? 'fold (Enter)' : 'the last result and the request (Enter) · double-click: open in Files',
        onclick: (e: Event) => { e.stopPropagation(); toggle(item.file, r.index); } },
      h('span', { class: 'hpchev', 'aria-hidden': 'true' }),
      h('span', { class: 'hpmethod mono' }, r.method),
      h('span', { class: 'hpreqname' }, showBidi(r.name))),
      h('span', { class: 'mono small mut hpurl' }, showBidi(urls?.[r.index] ?? r.url)),
      h('span', { class: res ? `httpcode s${status[0] === 'e' ? 'e' : status[0]}` : '', title: stale && res ? `from the ${st.run?.env} run` : '' }, status),
      h('span', { class: res ? (res.ok ? 'ok' : 'bad') : '' }, res ? (res.ok ? '✓' : '✗') : ''));
      const row = h('div', { class: 'hpreq' + (isOpen ? ' open' : '') }, head);
      if (isOpen) row.append(details(st, item, r, urls?.[r.index] ?? r.url, res, runBtn));
      reqsEl.append(row);
    }
    if (focused) (reqsEl.querySelector(`[data-req="${focused}"]`) as HTMLElement | null)?.focus();
  }

  function details(st: HttpTab, item: HttpFileItem, r: HttpReqBrief, url: string, res: HttpResult | undefined,
    runBtn: (klass: string, label: string) => HTMLElement): HTMLElement {
    const box = h('div', { class: 'hpdetail' });
    // the result first - it is what a run is for; the request as written below it
    if (res && current) {
      box.append(h('div', { class: 'hplast mut small' },
        `${current.stale() ? 'Result from the ' + st.run?.env + ' run (not ' + st.env + ')' : 'Last run'} · ${ago(st.run?.at ?? 0)}` +
        `${res.time_ms !== null ? ' · ' + res.time_ms + ' ms' : ''}`));
      box.append(...current.resultParts(res));
    } else box.append(h('div', { class: 'mut small' }, 'Not run yet in this view.'));
    const req = h('details', { class: 'hpdef' }, h('summary', { class: 'small' }, 'Request as written'),
      h('div', { class: 'mono small hpfull' }, showBidi(`${r.method} ${url}`)));
    if (r.headers?.length) req.append(h('table', { class: 'httphdr mono' }, ...r.headers.map(([k, v]) => h('tr', {}, h('th', {}, showBidi(k)), h('td', {}, showBidi(v))))));
    if (r.body) req.append(h('pre', { class: 'httpbody mono hpbody' }, showBidi(r.body)));
    const notes = [r.script ? 'has a script (tests / globals)' : '', r.files?.length ? `uses ${r.files.join(', ')}` : ''].filter(Boolean);
    if (notes.length) req.append(h('div', { class: 'mut small' }, showBidi(notes.join(' · '))));
    if (!res) req.open = true;
    box.append(req, h('div', { class: 'hpactions' },
      runBtn(current?.cls() === 'prod' ? 'danger' : '', st.running === r.index ? 'Running…' : `▶ Run on ${shortEnv(st.env) || '-'}`),
      h('button', { title: 'open the file at this request in Files (edit it there) · o',
        onclick: () => deps.openFile(project, item.file, r.line >= 0 ? r.line + 1 : 1) }, 'Open in Files')));
    return box;
  }

  /** ↑↓ move between requests, Enter/Space fold (the button), r or ⌘/Ctrl+Enter run, o open in Files */
  function onReqKey(e: KeyboardEvent, st: HttpTab, item: HttpFileItem): void {
    const t = e.target as HTMLElement;
    if (!t.classList.contains('hpreq-toggle') || !reqsEl) return;
    const all = [...reqsEl.querySelectorAll<HTMLElement>('.hpreq-toggle')];
    const at = all.indexOf(t);
    const idx = Number(t.dataset.req);
    const r = (st.info?.requests ?? []).find((x) => x.index === idx);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      all[Math.max(0, Math.min(all.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1)))]?.focus();
    } else if (e.key === 'r' || (e.key === 'Enter' && (e.metaKey || e.ctrlKey))) {
      void current?.run(idx);
    } else if (e.key === 'o') {
      deps.openFile(project, item.file, r && r.line >= 0 ? r.line + 1 : 1);
    } else return;
    e.preventDefault();
  }

  const expanded = new Map<string, Set<number>>();   // project + file -> expanded request indexes
  function expandedOf(file: string): Set<number> {
    const k = project + '\0' + file;
    if (!expanded.has(k)) expanded.set(k, new Set());
    return expanded.get(k)!;
  }
  function toggle(file: string, index: number): void {
    const set = expandedOf(file);
    if (set.has(index)) set.delete(index); else set.add(index);
    const item = files.find((f) => f.file === file);
    const st = states.get(project + '\0' + file);
    if (item && st) drawReqs(st, item);
  }

  return {
    el,
    /** Select a request by its source line, without running it. */
    async openFile(p: string, path: string, line = 0): Promise<void> {
      project = p; filterEl.value = ''; picked.set(p, path);
      await load(true);
      if (project !== p || picked.get(p) !== path) return;
      if (!files.some(f => f.file === path)) { deps.toast('This HTTP file is not available in the project'); return; }
      pick(path);
      await currentLoad;
      if (project !== p || picked.get(p) !== path) return;
      const st = states.get(p + '\0' + path);
      const reqs = st?.info?.requests ?? [];
      const request = requestForLine(reqs, line);
      if (request) {
        expandedOf(path).add(request.index);
        const item = files.find(f => f.file === path)!;
        if (st) drawReqs(st, item);
        const row = reqsEl?.querySelector<HTMLElement>(`[data-req="${request.index}"]`);
        row?.focus(); row?.scrollIntoView({ block: 'nearest' });
      }
    },
    resume: () => current?.resume(),
    project: () => project,
    show(p: string): void {
      if (p !== project) {
        project = p;
        filterEl.value = '';
      }
      drawList();
      drawRight();
      void load(loadedFor !== project || Date.now() - loadedAt > 20000);   // new files, runs of the sessions
    },
    /** D10: Settings › HTTP requests › Edit environments… - the environment dialog of the file shown here, else of
     *  the newest run's file, else of the first one (environments belong to the .http files' folder) */
    async openEnvironments(p: string): Promise<void> {
      if (!p) return deps.toast('Open a project session first - the environments belong to its .http files');
      if (p !== project) {
        project = p;
        filterEl.value = '';
      }
      await load(loadedFor !== project);
      const sel = picked.get(project);
      const file = sel && files.some((f) => f.file === sel) ? sel : newestRun(files) || files[0]?.file;
      if (!file) return deps.toast('No .http files in this project - the environments live beside them (http-client.env.json)');
      if (file !== sel || !current) pick(file);
      const hv = current as ReturnType<typeof httpView> | null;
      if (!hv) return;
      await currentLoad;
      if (current === hv) await hv.envDialog();
    },
  };
}
