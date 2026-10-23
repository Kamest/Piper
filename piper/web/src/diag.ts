// Diagnostics of the last build: piperd parses the compiler /
// test output of the session's latest Bash tool result (mvn, javac, tsc, eslint, gradle,
// python, junit) from the transcript. Grouped by file, a click opens the line in Files;
// "Send to Claude" types only references + messages (they came from Claude's own output).
// Inspector section: persistent element, fetched on the first show and after a turn ends.

import { api } from './api.js';
import { clear, fi, fold, h } from './dom.js';
import { cutText, plural, showBidi, markBidi } from './text.js';
import type { Diagnostic, Diagnostics } from './types.js';

export interface DiagDeps {
  toast(text: string): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  /** the session's project key */
  project(id: string): string;
  /** open a project-relative file at a line (Files) */
  openFile(project: string, path: string, line: number): void;
  /** type a message into the session; false when it did not go out */
  send(id: string, text: string): Promise<boolean>;
}

export interface DiagGroup {
  path: string;
  errors: number;
  items: Diagnostic[];
}

/** by file: files with errors first, then by path; items by line and column */
export function groupDiagnostics(items: Diagnostic[]): DiagGroup[] {
  const by = new Map<string, Diagnostic[]>();
  for (const d of items) {
    const list = by.get(d.path) ?? [];
    list.push(d);
    by.set(d.path, list);
  }
  return [...by].map(([path, list]) => ({
    path,
    errors: list.filter((d) => d.severity === 'error').length,
    items: list.sort((a, b) => a.line - b.line || (a.col ?? 0) - (b.col ?? 0)),
  })).sort((a, b) => Number(b.errors > 0) - Number(a.errors > 0) || a.path.localeCompare(b.path));
}

/** the grouped list (Git-panel section and the Files tool panel, K9): a row opens its line in Files,
 *  a row without a project-relative path (absolute, outside the project, none) is not a link */
export function diagGroupsEl(items: Diagnostic[], project: string, openFile: (project: string, path: string, line: number) => void): HTMLElement[] {
  return groupDiagnostics(items).map((g) => {
    const inside = !!g.path && !g.path.startsWith('/') && !!project;
    const name = g.path.slice(g.path.lastIndexOf('/') + 1);
    return h('div', { class: 'dgroup' },
      h('div', { class: 'dfile', title: g.path }, h('span', { class: 'fn' }, name || '(no file)'),
        g.path.includes('/') ? h('span', { class: 'dir' }, ' ' + g.path.slice(0, g.path.lastIndexOf('/'))) : null),
      h('ul', { class: 'plain' }, ...g.items.map((x) => h('li', {},
        h('button', {
          class: `link ditem ${x.severity}`, disabled: !inside, title: showBidi(`${x.tool} · ${x.path}:${x.line}${x.col !== null ? ':' + x.col : ''}`),
          onclick: () => openFile(project, x.path, x.line),
        }, h('span', { class: 'no' }, `${x.line}${x.col !== null ? ':' + x.col : ''}`), showBidi(` ${x.message}`))))));
  });
}

/** "Send to Claude": the message for confirmation, then into the session */
export function confirmDiagSend(deps: Pick<DiagDeps, 'toast' | 'modal' | 'closeModal'>, send: (text: string) => Promise<boolean>,
  items: Diagnostic[], source: string): void {
  if (!items.length) return;
  const text = diagMessage(items, source);
  deps.modal('Send the errors to the session?', h('pre', { class: 'confirm' }, text),
    h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'),
      h('button', {
        class: 'primary', onclick: () => {
          deps.closeModal();
          void send(text).then((ok) => { if (ok) deps.toast('Errors sent to the session'); });
        },
      }, 'Send')));
}

const MAX_SEND = 60;

/** one line, bounded: a message never carries more than the diagnostic itself */
function oneLine(s: string, max = 300): string {
  const t = s.replace(/\s+/g, ' ').trim();
  return t.length > max ? cutText(t, max - 1) + '…' : t;
}

/** the message for Claude: `path:line[:col]: message` per item, errors first, bounded */
export function diagMessage(items: Diagnostic[], source = ''): string {
  const flat = groupDiagnostics(items).flatMap((g) => g.items);
  const ordered = [...flat.filter((d) => d.severity === 'error'), ...flat.filter((d) => d.severity !== 'error')];
  const shown = ordered.slice(0, MAX_SEND);
  const head = `Please fix the errors from the last build${source ? ` (${oneLine(source, 120)})` : ''}, then briefly write what you did:`;
  const lines = shown.map((d) => `- ${d.path}:${d.line}${d.col !== null ? ':' + d.col : ''}: ${d.severity === 'warning' ? 'warning: ' : ''}${oneLine(d.message)}`);
  if (ordered.length > shown.length) lines.push(`- … and ${ordered.length - shown.length} more`);
  return [head, ...lines].join('\n');
}

export function createDiag(deps: DiagDeps) {
  const body = h('div', { class: 'diagbody' });
  const reload = h('button', { class: 'mini icon', title: 'refresh', 'aria-label': 'refresh the diagnostics', onclick: () => void load(current) }, fi('refresh'));
  const sendBtn = h('button', { class: 'mini hidden', title: 'send the error list to the session', onclick: () => confirmSend() }, 'Send to Claude');
  const sec = fold('diag', 'Diagnostics');
  sec.el.classList.add('diag');
  sec.actions.append(reload, sendBtn);
  sec.body.append(body);
  const el = sec.el;
  const cache = new Map<string, { data: Diagnostics | null; error: string; stale: boolean; loading: boolean }>();
  let current = '';
  let sig = '';

  async function load(id: string): Promise<void> {
    let c = cache.get(id);
    if (!c) {
      c = { data: null, error: '', stale: true, loading: false };
      cache.set(id, c);
    }
    if (c.loading) return;
    c.loading = true;
    c.stale = false;
    reload.textContent = '…';
    try {
      c.data = (await api.diagnostics(id)).data;
      c.error = '';
    } catch (e) {
      c.error = String(e instanceof Error ? e.message : e);
    }
    c.loading = false;
    reload.replaceChildren(fi('refresh'));
    if (id === current) draw();
  }

  function draw(): void {
    const c = cache.get(current);
    const d = c?.data ?? null;
    const next = JSON.stringify([current, c?.error, d]);
    if (next === sig) return;
    sig = next;
    clear(body);
    sendBtn.classList.toggle('hidden', !d?.items.length);
    // the header says the state; the body stays folded until the user opens it
    const errs = d?.items.filter((x) => x.severity === 'error').length ?? 0;
    const warns = (d?.items.length ?? 0) - errs;
    // no items with a note = no build found in the transcript: nothing to call clean
    const none = !!d && !d.items.length && !!d.note;
    sec.status.className = 'sec-s ' + (errs ? 'bad' : warns ? 'warn' : none || c?.error ? 'faint' : d ? 'ok' : '');
    sec.status.textContent = markBidi(c?.error ? 'unavailable' : !d ? '' : errs ? `${errs} ${plural(errs, 'error', 'errors', 'errors')}`
      : warns ? `${warns} ${plural(warns, 'warning', 'warnings', 'warnings')}` : none ? 'no build' : 'clean');
    sec.el.toggleAttribute('data-empty', none && !c?.error);   // F13: "Not in use" at the panel's end
    sec.auto(false);
    if (c?.error) {
      body.append(h('p', { class: 'mut small' }, c.error));
      return;
    }
    if (!d) {
      body.append(h('p', { class: 'mut small' }, 'loading…'));
      return;
    }
    if (!d.items.length) {
      body.append(h('p', { class: 'mut small' }, d.note || 'The last build had no errors (or there was none).'));
      return;
    }
    body.append(...diagGroupsEl(d.items, deps.project(current), deps.openFile));
    if (d.note) body.append(h('p', { class: 'mut small' }, d.note));
    body.append(h('p', { class: 'dsrc', title: d.source }, `${errs && warns ? `${warns} ${plural(warns, 'warning', 'warnings', 'warnings')} · ` : ''}from `,
      h('code', {}, d.source || '?'), d.at ? ` · ${new Date(d.at).toLocaleTimeString()}` : ''));
  }

  function confirmSend(): void {
    const id = current;
    const d = cache.get(id)?.data;
    if (!d?.items.length) return;
    confirmDiagSend(deps, (text) => deps.send(id, text), d.items, d.source);
  }

  return {
    el,
    /** switch to a session; fetches on the first show and after invalidate() */
    show(id: string): void {
      if (id !== current) {
        current = id;
        sig = '';
        draw();
      }
      if (cache.get(id)?.stale ?? true) void load(id);
    },
    /** a turn ended: the next show (or now, when shown) fetches again */
    invalidate(id: string): void {
      const c = cache.get(id);
      if (c) c.stale = true;
      if (id === current && el.isConnected) void load(id);
    },
  };
}
