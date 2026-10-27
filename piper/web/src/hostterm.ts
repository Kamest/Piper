// W2 host terminal: IntelliJ's ⌥F12 next to the sessions -
// login shells on the Mac itself, no sandbox, in a project's folder. piperd runs them in ptyhost (hostterm.py)
// and lists them in /api/state `host_terms` for the GUI's own token only; the view is tabs over one terminal
// each, the red HOST strip above them never scrolls away. Off (the host module, D10) = the view says how to
// turn it on and the rail has no icon. Closing a tab HUPs the shell; an exited one stays grey with its rc.
//
// No xterm import here: main.ts hands in the terminal factory, so the pure helpers load in node --test.

import { call } from './api.js';
import { clear, h } from './dom.js';
import { kbd } from './clipkeys.js';
import { openMenu } from './menu.js';
import { markBidi } from './text.js';
import type { HostTerm, Project, State } from './types.js';

/** what the view needs of a TermView */
export interface HostTermLike {
  readonly el: HTMLElement;
  /** the pane bar (a grid pane's title, W26) */
  readonly label: HTMLElement;
  mount(parent: HTMLElement): void;
  focus(): void;
  refit(): void;
  dispose(): void;
  /** Touch keys follow process lifetime, including when an ended shell keeps its replay. */
  setInputState?(alive: boolean, active: boolean, label: string, visible?: boolean): void;
}

export interface HostDeps {
  state(): State;
  /** a TermView on /ws/<id> (main.ts: images refused, file links to Files / IntelliJ) */
  makeTerm(id: string, cwd: string): HostTermLike;
  /** the project a new shell opens in without asking: the active session's (or '') */
  currentProject(): string;
  openSettings(): void;
  toast(msg: string): void;
  /** the list changed here before the next poll (a tab opened / closed): redraw the rail and header */
  changed(): void;
  /** W26: the grid's "Add to grid" / "Remove from grid" for a shell (a tab's context menu); none = no grid */
  grid?: { has(id: string): boolean; toggle(id: string): void };
}

export interface HostView {
  readonly el: HTMLElement;
  /** the host module is on (state.host_terms.on) */
  on(): boolean;
  /** into the view: the active tab gets the size and the keyboard */
  show(): void;
  /** out of the view (the shells keep running) */
  hide(): void;
  /** every /api/state poll */
  sync(): void;
  /** a new shell in `project` ('' = the picker unless the current project is known); `at` anchors the picker;
   *  `then` gets the new shell's id (W26: ⌥⇧T in the grid puts it into the grid) */
  open(project?: string, at?: HTMLElement, then?: (id: string) => void, session?: string): Promise<void>;
  /** close the active tab (⌥⇧W) */
  closeActive(): void;
  /** W26: the terminal of a listed shell for a grid pane (made on first use unless `make` is false, shared with
   *  this view - one ws per shell); null = not listed (closed elsewhere, the policy off) or not made yet */
  term(id: string, make?: boolean): HostTermLike | null;
  /** W26: close one shell (a grid pane's Close) */
  close(id: string): void;
  /** W26: the tab the view shows next (a grid pane's ⤢) */
  pick(id: string): void;
  /** D5: close the ended shells outside the grid that ended ENDED_CLOSE_S ago (main.ts' minute timer) */
  closeStale(): void;
}

/** the shells to show, oldest first (piperd's order); none while the policy is off */
export function hostItems(s: Pick<State, 'host_terms'>): HostTerm[] {
  return s.host_terms?.on ? (s.host_terms.items ?? []) : [];
}

/** the tab that stays active: the wanted one while listed, else the last running one, else the last */
export function pickActive(items: HostTerm[], want: string): string {
  if (items.some((x) => x.id === want)) return want;
  const alive = items.filter((x) => x.alive);
  return (alive[alive.length - 1] ?? items[items.length - 1])?.id ?? '';
}

/** the projects a shell can open in (their checkout exists), the current one first */
export function shellProjects(projects: Project[], current: string): Project[] {
  const ok = projects.filter((p) => p.exists);
  return [...ok.filter((p) => p.key === current), ...ok.filter((p) => p.key !== current)];
}

/** "exit 1" on a grey tab; a shell ended without a known code (ptyhost restarted) says "ended" */
export function exitText(x: Pick<HostTerm, 'alive' | 'rc'>): string {
  if (x.alive) return '';
  return x.rc === null || x.rc === undefined ? 'ended' : `exit ${x.rc}`;
}

/** D5: an ended shell outside the grid closes itself this long after it ended (s) */
export const ENDED_CLOSE_S = 600;

/** D5: the grid chooser's host rows - running ones and those already chosen up front, the rest in "Ended (n)" */
export function splitHosts(items: HostTerm[], chosen: string[]): { live: HostTerm[]; ended: HostTerm[] } {
  const up = (x: HostTerm) => x.alive || chosen.includes(x.id);
  return { live: items.filter(up), ended: items.filter((x) => !up(x)) };
}

/** D5: the ended shells "Close ended" closes (all of them, `now` unset) or the timer does (ended ENDED_CLOSE_S ago);
 *  a grid pane keeps its shell - the grid is the GUI's own state, so the GUI closes them, not piperd */
export function endedToClose(items: HostTerm[], inGrid: (id: string) => boolean, now?: number): string[] {
  return items.filter((x) => !x.alive && !inGrid(x.id) && (now === undefined || (!!x.ended && now - x.ended >= ENDED_CLOSE_S)))
    .map((x) => x.id);
}

/** ~ for the home folder in the strip's cwd */
export function tildePath(path: string, user: string): string {
  if (!user) return path;
  for (const home of [`/Users/${user}`, `/home/${user}`]) {
    if (path === home) return '~';
    if (path.startsWith(home + '/')) return '~' + path.slice(home.length);
  }
  return path;
}

export function createHostView(d: HostDeps): HostView {
  const views = new Map<string, HostTermLike>();
  let active = '';
  let shown = false;
  let busy = false;
  let sig = '';

  const tabsEl = h('div', { class: 'ht-tabs', role: 'tablist', 'aria-label': 'host shells' });
  const addBtn = h('button', { class: 'ht-add', title: `New host shell in this project (${kbd('⌥⇧T')})`, 'aria-label': 'New host shell', onclick: () => void open() }, '+');
  const pickBtn = h('button', { class: 'ht-pick', title: 'New host shell in another project…', 'aria-label': 'Choose a project', onclick: () => void open('', pickBtn, true) }, h('i', { class: 'fi fi-chev-d', 'aria-hidden': 'true' }));
  // D5: the grey tabs in one go (a grid pane keeps its shell); they also close themselves 10 minutes after the end
  const endedBtn = h('button', { class: 'link small ht-ended', title: 'close every ended shell that is not a grid pane - they also close by themselves 10 minutes after they end',
    onclick: () => { for (const id of endedToClose(items(), inGrid)) close(id); } });
  // N16: the session bar's pattern (F03, 9.102) - title, underline tabs, + ▾ as one control; the keys live in
  // the tooltips and the Keys sheet instead of a line of text
  const bar = h('div', { class: 'ht-bar', title: '' },
    h('span', { class: 'ht-title', title: `${kbd('⌥F12')} shows / hides this view` }, 'Host shells'), tabsEl,
    h('span', { class: 'ht-new' }, addBtn, pickBtn), h('span', { class: 'grow' }), endedBtn);
  const who = h('span', { class: 'ht-who' });
  const cwd = h('span', { class: 'ht-cwd mono' });
  const banner = h('div', { class: 'ht-banner', role: 'note' }, h('span', { class: 'ht-pill' }, 'HOST · no sandbox'), who, cwd);
  const body = h('div', { class: 'ht-body' });
  const empty = h('div', { class: 'ht-empty' });
  const off = h('div', { class: 'ht-off' },
    h('h2', {}, 'The host terminal is off'),
    h('p', { class: 'mut' }, 'Turn it on in Settings → Integrations → Host shells.'),
    h('p', { class: 'mut' }, 'A shell there runs on this Mac without any sandbox - only you can open one, never a session.'),
    h('button', { class: 'primary', onclick: () => d.openSettings() }, 'Open Settings'));
  const el = h('div', { class: 'hostterm' }, bar, banner, body, empty, off);

  const items = () => hostItems(d.state());
  const inGrid = (id: string) => !!d.grid?.has(id);
  const user = () => d.state().host_terms?.user ?? '';

  function draw(): void {
    const list = items();
    const isOn = !!d.state().host_terms?.on;
    active = pickActive(list, active);
    // tabs of shells piperd no longer lists (closed elsewhere, trimmed): their terminals go
    for (const [id, t] of views) if (!list.some((x) => x.id === id)) { t.dispose(); views.delete(id); }
    off.classList.toggle('hidden', isOn);
    bar.classList.toggle('hidden', !isOn);
    banner.classList.toggle('hidden', !isOn || !active);
    empty.classList.toggle('hidden', !isOn || !!active);
    body.classList.toggle('hidden', !isOn || !active);
    const cur = list.find((x) => x.id === active);
    const nEnded = endedToClose(list, inGrid).length;
    endedBtn.textContent = markBidi(`Close ended (${nEnded})`);
    endedBtn.classList.toggle('hidden', !nEnded);
    const next = JSON.stringify([isOn, active, user(), list.map((x) => [x.id, x.title, x.alive, x.rc])]);
    if (next !== sig) {
      sig = next;
      clear(tabsEl);
      for (const x of list) {
        const ex = exitText(x);
        tabsEl.append(h('div', { class: 'ht-tab' + (x.id === active ? ' on' : '') + (x.alive ? '' : ' dead'), role: 'tab', 'aria-selected': String(x.id === active),
          title: `${x.path}${ex ? ` · ${ex}` : ''}`, onclick: () => select(x.id),
          oncontextmenu: (e: Event) => { e.preventDefault(); const m = e as MouseEvent; tabMenu(x.id, m.clientX, m.clientY); } },
          h('span', { class: 'ht-dot' }), h('span', { class: 'ht-name' }, x.title), ex ? h('span', { class: 'ht-rc' }, ex) : null,
          h('button', { class: 'ht-x', title: kbd(x.alive ? 'Close (HUP the shell) · ⌥⇧W' : 'Close · ⌥⇧W'), 'aria-label': `Close ${x.title}`,
            onclick: (e: Event) => { e.stopPropagation(); close(x.id); } }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }))));
      }
      who.textContent = markBidi(`This shell runs directly on your Mac${user() ? ` as ${user()}` : ''} - real ~/.ssh, ~/.m2, docker. Nothing here is the session's box.`);
      clear(empty);
      empty.append(h('h2', {}, 'No host shell open'),
        h('p', { class: 'mut' }, 'A login shell on this Mac, in a project\'s folder - no sandbox.'),
        h('button', { class: 'primary', onclick: () => void open() }, `Open a host shell (${kbd('⌥⇧T')})`));
    }
    // the path keeps its end in view (rtl box), its own text left to right - a bare rtl box moved the leading "/" to the end
    cwd.replaceChildren(h('span', { dir: 'ltr' }, cur ? markBidi(tildePath(cur.path, user())) : ''));
    cwd.title = markBidi(cur?.path ?? '');
    if (!shown || !isOn) return;
    // one terminal per listed shell, created when first shown; only the active one is visible
    if (cur && !views.has(cur.id)) views.set(cur.id, d.makeTerm(cur.id, cur.path));
    for (const [id, t] of views) {
      const me = id === active;
      const shell = list.find(x => x.id === id);
      t.setInputState?.(!!shell?.alive, me, shell?.title ?? id);
      t.el.classList.toggle('hidden', !me);
      if (me) {
        if (t.el.parentElement !== body) body.append(t.el);   // W26: it may come back from a grid pane
        t.mount(body);
        t.refit();
      }
    }
  }

  /** a tab's right click: into the grid (W26), close */
  function tabMenu(id: string, x: number, y: number): void {
    const g = d.grid;
    openMenu({ x, y }, [
      g ? { label: g.has(id) ? 'Remove from grid' : 'Add to grid', run: () => g.toggle(id), title: 'the shell as a grid pane (⌘G) - one terminal, shown in one place at a time' } : null,
      { label: 'Close', run: () => close(id), danger: true },
    ], { label: 'host shell' });
  }

  function select(id: string): void {
    active = id;
    draw();
    views.get(id)?.focus();
  }

  async function open(project = '', at?: HTMLElement, ask = false, then?: (id: string) => void, owner = ''): Promise<void> {
    if (busy) return;
    const s = d.state();
    if (!s.host_terms?.on) { d.openSettings(); return; }
    const fromTab = items().find((x) => x.id === active)?.project ?? '';
    const context = owner ? s.sessions.find((x) => x.id === owner && x.kind === 'project') : null;
    if (owner && !context) return void d.toast('The session is no longer available.');
    const key = context?.project || project || (ask ? '' : fromTab || d.currentProject());
    const ok = shellProjects(s.projects, key);
    if (!context && (!key || !ok.some((p) => p.key === key))) {
      if (!ok.length) { d.toast('No project with a checkout on this Mac to open a shell in'); return; }
      openMenu(at ?? pickBtn, [{ group: 'Open a host shell in…' }, ...ok.map((p) => ({ label: p.title || p.key, hint: tildePath(p.path, user()), search: `${p.title} ${p.key} ${p.path}`, run: () => void open(p.key, undefined, false, then) }))],
        { label: 'Open a host shell in', search: true, foot: 'no sandbox: the shell runs as you' });
      return;
    }
    busy = true;
    try {
      const r = await call<HostTerm>('POST', '/api/host-terminals', { project: key, cols: 120, rows: 32, ...(owner ? { session: owner } : {}) });
      if (r.data) {
        // shown at once: the next poll brings the same row
        const ht = s.host_terms;
        if (!(ht.items ?? []).some((x) => x.id === r.data!.id)) ht.items = [...(ht.items ?? []), r.data];
        active = r.data.id;
        sig = '';
        d.changed();
        draw();
        if (then) then(r.data.id);
        else views.get(active)?.focus();
      }
    } catch (e) {
      d.toast(`Host shell: ${(e as Error).message}`);
    } finally {
      busy = false;
    }
  }

  function close(id: string): void {
    if (!id) return;
    call('POST', `/api/host-terminals/${encodeURIComponent(id)}/close`).catch((e: Error) => d.toast(`Close: ${e.message}`));
    const ht = d.state().host_terms;
    if (ht?.items) ht.items = ht.items.filter((x) => x.id !== id);
    if (active === id) active = '';
    sig = '';
    d.changed();
    draw();
    if (shown) views.get(active)?.focus();   // closed from a grid pane: the keyboard stays where it is
  }

  return {
    el,
    on: () => !!d.state().host_terms?.on,
    show() {
      shown = true;
      sig = '';
      draw();
      // setView renders (puts el into the page) right after show: focus once el is there, or the keys go nowhere
      queueMicrotask(() => { if (shown) views.get(active)?.focus(); });
    },
    hide() {
      shown = false;
      for (const t of views.values()) {
        t.el.classList.add('hidden');
        t.setInputState?.(false, false, '');
      }
    },
    sync: draw,
    open: (project = '', at?: HTMLElement, then?: (id: string) => void, owner = '') => open(project, at, false, then, owner),
    closeActive: () => close(active),
    term(id, make = true) {
      const x = items().find((i) => i.id === id);
      if (!x) return null;
      let t = views.get(id);
      if (!t) {
        if (!make) return null;
        t = d.makeTerm(id, x.path);
        views.set(id, t);
      }
      return t;
    },
    close,
    pick(id) {
      if (!items().some((x) => x.id === id)) return;
      active = id;
      sig = '';
    },
    closeStale() {
      if (!d.state().host_terms?.on) return;
      for (const id of endedToClose(items(), inGrid, Date.now() / 1000)) close(id);
    },
  };
}
