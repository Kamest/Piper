// The session context panel's "Database" section: one header
// line (class of the session's dev connection, Claude's queries, a waiting card), opened: the
// connections of this checkout with their state, Claude's last queries (a waiting write first); one action in the
// header (F10): Open beside (D8: the Database pane next to the session), Open ↗ (the view) or Add…. Persistent element like the Stack section: show() on a redraw, its own 1 s timer refetches
// (connections / history every 30 s, running statements every 3 s while one runs, else every 10 s).

import { clear, fold, h } from './dom.js';
import { markBidi } from './text.js';
import { classDot, dbApi, needsPassword, rowsText, type DbClass, type DbConn, type DbConnList, type DbHistoryRow, type DbRunning } from './dbview.js';

export interface DbSectionDeps {
  /** the approval cards waiting now */
  pending(): { kind: string; session: string; summary: string }[];
  /** open the Database view (on this connection) */
  open(connection?: string): void;
  /** D8/D1: the project's Database pane next to the session (on this connection) */
  openPane?(connection: string): void;
  /** the grid module is on - without it there is no pane, only the view */
  paneOn?(): boolean;
}

/** F10: the section's header - one connection (the dev one first) or "none for this project", and one action:
 *  Open beside (the Database pane next to the session), Open ↗ (the view, without the pane), Add… (none yet) */
export function sectionHead(conns: Pick<DbConn, 'name' | 'class' | 'label'>[] | null, project: string, paneOn: boolean):
  { cls: DbClass | null; text: string; action: { label: string; kind: 'pane' | 'view' | 'add'; connection: string } | null } {
  if (!conns) return { cls: null, text: '…', action: null };
  if (!conns.length) return { cls: null, text: 'none for this project', action: { label: 'Add…', kind: 'add', connection: '' } };
  const c = conns.find((x) => x.class === 'dev') ?? conns[0]!;
  const name = c.label || c.name.replace(project + '/', '');
  return { cls: c.class, text: name, action: paneOn ? { label: 'Open beside', kind: 'pane', connection: c.name } : { label: 'Open ↗', kind: 'view', connection: c.name } };
}

const REFRESH_MS = 30000;   // a poll redraws the inspector every few seconds; the DB section needs far less
const RUNNING_MS = 3000;    // while this session's statement runs: its elapsed time, its end
const IDLE_RUN_MS = 10000;  // nothing runs: how soon a new statement of the session shows up

export function createDbSection(deps: DbSectionDeps) {
  const sec = fold('db', 'Database');
  let sid = '';
  let at = 0;
  let list: DbConnList | null = null;
  let hist: DbHistoryRow[] = [];
  let err = '';
  let sig = '';
  let running: DbRunning[] = [];   // this session's statements running now (L7)
  let rat = 0;
  let cancelling = false;

  // F10: one action (D8: the pane next to the session when there is one, else the view; Add… without a connection)
  const paneOn = () => !!deps.openPane && (deps.paneOn?.() ?? true);
  const head = () => sectionHead(err ? null : list?.connections ?? null, list?.project ?? '', paneOn());
  const actBtn = h('button', { class: 'mini', onclick: (e: Event) => {
    e.stopPropagation();
    const a = head().action;
    if (!a) return;
    if (a.kind === 'pane') deps.openPane?.(a.connection);
    else if (a.kind === 'view') deps.open(a.connection);
    else deps.open();
  } });
  sec.actions.append(actBtn);
  function actions(): void {
    const a = head().action;
    actBtn.hidden = !a;
    actBtn.textContent = markBidi(a?.label ?? '');
    actBtn.title = !a ? '' : a.kind === 'pane' ? 'this project\'s Database pane next to the session (⌘D)' : a.kind === 'view' ? 'the Database view of this session\'s checkout' : 'add a connection in the Database view';
  }

  async function fetchData(id: string): Promise<void> {
    at = Date.now();
    try {
      const [c, hh] = await Promise.all([dbApi.connections('', id), dbApi.history('', id, 20)]);
      if (id !== sid) return;
      list = c.data;
      hist = hh.data?.queries ?? [];
      err = '';
    } catch (e) {
      err = String(e instanceof Error ? e.message : e);
    }
    draw();
    void fetchRunning(id);
  }

  async function fetchRunning(id: string): Promise<void> {
    rat = Date.now();
    try {
      const { data } = await dbApi.running('', '', id);
      if (id !== sid) return;
      const was = running.length;
      running = (data?.running ?? []).filter((r) => r.owner === id);
      if (was && !running.length) at = 0;   // it ended: its history row is there now
    } catch {
      running = [];   // an older piperd without the route: no running row, nothing else changes
    }
    draw();
  }

  async function cancel(r: DbRunning): Promise<void> {
    cancelling = true;
    draw();
    try {
      await dbApi.cancel('', r.connection, r.owner);
    } catch {
      /* the next poll shows whether it still runs */
    }
    cancelling = false;
    void fetchRunning(sid);
  }

  function draw(): void {
    const waiting = deps.pending().filter((p) => p.session === sid && p.kind.startsWith('db_'));
    const claude = hist.filter((r) => r.origin === 'mcp');
    const now = Date.now() / 1000;
    const s2 = JSON.stringify([list?.connections.map((c) => [c.name, c.class, c.tunnel?.state, c.has_password]), claude.map((r) => r.id), waiting.map((w) => w.summary), err,
      running.map((r) => [r.connection, r.op, r.since, Math.floor(now - r.since)]), cancelling]);
    if (s2 === sig) return;
    sig = s2;
    clear(sec.status);
    const hd = head();
    actions();
    if (err) sec.status.append('unavailable');
    else if (hd.cls) {
      sec.status.append(classDot(hd.cls, hd.text));
      const more = [claude.length ? `${claude.length} ${claude.length === 1 ? 'query' : 'queries'}` : '', running.length ? `${running.length} running` : '',
        waiting.length ? `${waiting.length} waiting` : ''].filter(Boolean).join(' · ');
      if (more) sec.status.append(markBidi(` · ${more}`));
    } else sec.status.append(hd.text);
    // F13: no connection, nothing Claude ran or waits for = "Not in use" at the panel's end
    sec.el.toggleAttribute('data-empty', !err && !!list && !list.connections.length && !claude.length && !waiting.length);
    clear(sec.body);
    if (err) {
      sec.body.append(h('p', { class: 'mut small' }, err));
      sec.auto(false);
      return;
    }
    if (!list) return;
    if (!list.connections.length) {
      sec.body.append(h('p', { class: 'mut small' }, 'No database connection for this checkout: no dev stack connection here and nothing registered.'));
      sec.auto(false);
      return;
    }
    const rows = h('div', { class: 'dbs-conns' });
    for (const c of list.connections) {
      const n = claude.filter((r) => r.connection === c.name).length;
      const state = c.reach === 'stack' ? `stack${n ? ` · ${n} ${n === 1 ? 'query' : 'queries'}` : ''}`
        : needsPassword(c) ? 'no password'
          : c.reach === 'tunnel' || c.reach === 'ssh' ? `${c.reach} ${c.tunnel?.state ?? 'closed'}`
            : c.class !== 'dev' ? 'first read asks' : c.reach;
      rows.append(h('div', { class: 'dbs-conn' }, classDot(c.class), h('span', { class: 'dbs-name' }, c.label || c.name.replace(list.project + '/', '').replace(/\//g, ' / ')),
        h('span', { class: 'dbs-st' + (state === 'no password' || c.tunnel?.state === 'signin' ? ' warn' : '') }, c.tunnel?.state === 'signin' ? 'sign in' : state)));
    }
    sec.body.append(rows);
    sec.body.append(h('div', { class: 'dbs-h' }, `Claude queries · ${claude.length + waiting.length}`));
    for (const r of running) {
      const name = r.connection.replace(list.project + '/', '').replace(/\//g, ' / ');
      sec.body.append(h('div', { class: 'dbs-q' }, h('div', { class: 'dbs-ql' }, h('span', { class: 'dbs-t' }, 'now'), h('code', {}, `${r.op} · ${name}`)),
        h('div', { class: 'dbs-m warn' }, `running ${elapsed(now - r.since)} `,
          r.cancellable ? h('button', { class: 'mini', disabled: cancelling, title: 'stop this read (a write the card approved runs to its end)', onclick: () => void cancel(r) }, cancelling ? 'Cancelling…' : 'Cancel') : null)));
    }
    for (const w of waiting) {
      const sql = w.summary.split('\n').find((l) => /^\s*(UPDATE|INSERT|DELETE|REPLACE|CREATE|ALTER|DROP|SELECT|WITH|TRUNCATE)\b/i.test(l)) ?? w.summary.split('\n')[0] ?? '';
      const n = /affects ([\d, ]+) row/.exec(w.summary)?.[1];
      sec.body.append(h('div', { class: 'dbs-q' }, h('div', { class: 'dbs-ql' }, h('span', { class: 'dbs-t' }, 'now'), h('code', {}, sql.slice(0, 140))),
        h('div', { class: 'dbs-m warn' }, `waiting for your card${n ? ` · ${rowsText(n)}` : ''}`)));
    }
    for (const r of claude.slice(0, 4)) {
      const meta = r.error ? r.error.slice(0, 80) : r.approved === 'idempotent-replay' ? 'same statements again - answered from the first run, nothing ran' : r.rowcount ? `${rowsText(r.rowcount.reduce((a, b) => a + Math.max(0, b), 0))} changed` : r.rows !== null && r.rows !== undefined
        ? `${rowsText(r.rows)}${r.elapsed_ms !== null ? ` · ${r.elapsed_ms} ms` : ''}` : r.tool;
      sec.body.append(h('div', { class: 'dbs-q' }, h('div', { class: 'dbs-ql' }, h('span', { class: 'dbs-t' }, ago(r.ts)), h('code', {}, (r.sql ?? r.tool).replace(/\s+/g, ' ').slice(0, 140))),
        h('div', { class: 'dbs-m' + (r.error ? ' bad' : '') }, meta)));
    }
    if (!claude.length && !waiting.length && !running.length) sec.body.append(h('p', { class: 'mut small' }, 'none yet'));
    sec.auto(waiting.length > 0 || running.some((r) => r.cancellable));
  }

  function elapsed(s: number): string {
    const n = Math.max(0, Math.floor(s));
    return n < 60 ? `${n} s` : `${Math.floor(n / 60)} min ${n % 60} s`;
  }

  function ago(ts: number): string {
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 60 ? 'now' : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`;
  }

  // its own timer (e2e 2026-10-08): the inspector redraws only when /api/state changes, and a running
  // query does not change it - so neither a new statement nor its elapsed time would show up
  window.setInterval(() => {
    if (!sid || !sec.el.isConnected || document.hidden) return;
    if (Date.now() - at > REFRESH_MS) void fetchData(sid);
    else if (Date.now() - rat > (running.length ? RUNNING_MS : IDLE_RUN_MS)) void fetchRunning(sid);
  }, 1000);

  function show(id: string): void {
    if (id !== sid) {
      sid = id;
      list = null;
      hist = [];
      sig = '';
      at = 0;
      running = [];
      rat = 0;
    }
    if (Date.now() - at > REFRESH_MS) void fetchData(id);
    actions();
    draw();
  }

  return { el: sec.el, show };
}
