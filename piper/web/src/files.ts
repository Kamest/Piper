import { machineStorage, rawProjectKey, refHost } from './environment.js';
// File browser: the whole project for the human -
// a file hidden from Claude (isolation policy / exclude) is opened, searched and edited like any other,
// marked with the eye icon (2026-10-07); only .git is never shown. "Insert" types `path:line` into the
// prompt (never file content).
// Layout: tree (scope All / Changed / Turn / Open, type-to-filter, tooling noise folded) | tabs,
// one-line header (breadcrumbs + symbol scope, icon actions), the code view, the bottom tool panel
// (Usages · Search · Problems · Diagnostics · Turns) and a status bar.
// The code view is CodeMirror 6 (cmeditor.ts): a file is always an editor, also while the
// project's session works; ⌘S writes through POST /api/fs/write with the sha lock (9.7 #6).

import { api, ApiError, call, fsGrep, fsIndex, projectDiff, rawImage, turnDiff } from './api.js';
import { appMod, isMac, kbd } from './clipkeys.js';
import { ago, clear, fi, h, sideSplitter } from './dom.js';
import { langOf } from './highlight.js';
import { closeMenu, openMenu, type MenuEntry } from './menu.js';
import { score, type Command } from './palette.js';
import {
  dirsOf, fileKind, leftCut, matchSpans, outline, parseLineCol, quickQuery, rankFiles, resolveRel, scopeAt, treeNav, usagePattern,
  type OutlineItem,
} from './codenav.js';
import { parseMarkdown, renderMarkdown } from './markdown.js';
import { pairDiff, renderSideDiff } from './sidediff.js';
import { fileContent } from './editor.js';
import { createEditor, loadCM, type Caret, type Editor } from './cmeditor.js';
import { httpView, isHttpFile, newHttpTab, requestAt, type HttpTab } from './httpview.js';
import { HIDDEN_TITLE, hiddenMark, isJunk, type ChangeMark } from './gitpanel.js';
import { confirmDiagSend, diagGroupsEl } from './diag.js';
import { cutText, showBidi, markBidi } from './text.js';
import type {
  BlameLine, DirEntry, DirListing, FileBlob, FileText, FileTurns, GrepHit, GrepResult, IdeaProblem, LineMarks, Project, SymbolHit,
} from './types.js';

/** An explicit navigation during asynchronous restoration takes priority. */
export function restoredFileFocus(current: string, preferred: string, paths: string[]): string {
  return paths.includes(current) ? current : paths.includes(preferred) ? preferred : paths.at(-1) ?? '';
}

/** Metadata survives even when Git emits no text hunks. */
export function diffMetadata(patch: string, left: FileBlob): string[] {
  const lines = patch.split('\n').filter((line) => /^(old mode |new mode |new file mode |deleted file mode |Binary files |GIT binary patch|\\ No newline)/.test(line));
  if (left.mode === '120000') lines.push('HEAD: symlink (target text)');
  if (left.state === 'binary') lines.push('HEAD: binary file');
  if (left.state === 'too_large') lines.push('HEAD: too large to review');
  if (left.state === 'missing') lines.push('New file (absent from HEAD)');
  if (left.state === 'text' && left.text && !left.final_newline) lines.push('HEAD: no final newline');
  return lines;
}

export interface FilesDeps {
  reviewTarget?(project: string, target: import('./types.js').ReviewTarget): void;
  toast(text: string): void;
  /** W13: a line in the Notifications list (the Undo toast is its own, its text is kept there too) */
  notice?: (text: string) => void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  showFiles?(project: string): void;
  projects(): Project[];
  /** types text into the active session's prompt; false when no live terminal takes it */
  insert(text: string): boolean;
  /** project-relative paths with uncommitted changes (from the Git panel) */
  changed(project: string): Set<string>;
  /** change marks by path (M A U R D !) - the tree's letters */
  marks?(project: string): Map<string, ChangeMark>;
  /** the turn picked in the project session's Git panel (the newest by default) and its files (null = not loaded); null = no turn (K7) */
  turnFiles?(project: string): { n: number; files: Set<string> | null } | null;
  /** the review notes of the project's session (the Git panel holds them): the gutter marks (K6) */
  notes?(project: string): { path: string; line: number; old: boolean; text: string }[];
  /** type a message into the project's session (Diagnostics "Send to Claude", K9); false = it did not go out */
  send?(project: string, text: string): Promise<boolean>;
  /** module diag on: the Diagnostics tab of the tool panel (D14) */
  diagOn?(): boolean;
  /** the checkout's branch */
  branch?(project: string): string;
  /** the project's session id (diagnostics, turns of a file) */
  sessionOf?(project: string): string;
  /** the Git panel's review diff of a file */
  diff(project: string, path: string): void;
  stageFile?(project: string, path: string): void;
  reviewPaths?(project: string): string[];
  reviewLeave?(project: string, path: string, sha: string | null, leftOid: string | null, leftMode: string | null): Promise<void>;
  /** a review note on a line (9.7 #3): lands in the Git panel's notes of the project's session; false = no session */
  addNote(project: string, path: string, line: number, text: string, excerpt?: string): boolean;
  /** a session of the project is working: GUI edits wait (9.7 #6) */
  working(project: string): boolean;
  /** git history and blame of a file (9.7 #7) */
  history?(project: string, path: string): void;
  /** files changed outside a turn (GUI save, file operations, a session edit): the Git panel refreshes */
  gitChanged?(project: string): void;
  /** the command palette's commands (">" in ⌘P) */
  commands?(): Command[];
  /** a file the user opened (not a quiet refresh): a stop of the GUI's back / forward (policy nav_history) */
  opened?(project: string, path: string, line: number): void;
  /** ⌘[ / ⌘] with no jump of Files' own left: the GUI's back / forward (policy nav_history) */
  nav?(dir: -1 | 1): void;
}

type Mode = 'code' | 'preview' | 'diff';

/** Claude board 04: Markdown editing must be discoverable without guessing an icon. */
export function markdownModeControl(mode: Mode, choose: (mode: 'code' | 'preview') => void): HTMLElement {
  return h('span', { class: 'db-segs file-md-mode', role: 'group', 'aria-label': 'Markdown mode' },
    ...([['preview', 'eye', 'Preview'], ['code', 'pen', 'Edit']] as const).map(([value, icon, label]) =>
      h('button', { class: 'db-seg' + (mode === value ? ' on' : ''), 'aria-pressed': String(mode === value),
        onclick: () => choose(value) }, fi(icon), label)));
}
type Scope = 'all' | 'changed' | 'turn' | 'open' | 'scratch';
type PanelTab = 'usages' | 'search' | 'problems' | 'diag' | 'turns';

interface Tab {
  path: string;
  file: FileText | null;
  /** the whole text as last read / saved (the editor's baseline) */
  text: string;
  /** blob: URL of an image tab (revoked on close) */
  image: string;
  mode: Mode;
  marks: LineMarks | null;
  problems: IdeaProblem[] | null;
  ed: Editor | null;
  edLoading: boolean;
  dirty: boolean;
  caret: Caret | null;
  used: number;
  scroll: number;
  /** lines the session changed in its latest edit of this file */
  claude: number[];
  /** the session changed the file while the user had unsaved changes: the text on disk */
  remote: FileText | null;
  /** a note bar for the edit outcome (save conflict, Claude changed it) */
  banner: { kind: 'claude' | 'conflict' | 'error'; text: string } | null;
  blame: BlameLine[] | null;
  blameLoading: boolean;
  md: HTMLElement | null;
  /** .http files: environments, the last run, the results panel (httpview.ts) */
  http: HttpTab | null;
  /** the file is hidden from Claude (isolation policy / exclude): a banner says so, the rest works as usual */
  hidden: boolean;
  /** 9.41: the pending autosave (0 = none) */
  autoTimer?: number;
}

interface Hit { path: string; line: number; text: string }

interface ProjectView {
  dirs: Map<string, DirListing>;
  expanded: Set<string>;
  tabs: Tab[];
  active: string;
  scope: Scope;
  filter: string;
  /** the ⌘P index; `all` = with the excluded tooling folders (K1, asked for after an empty result) */
  index: { at: number; files: string[]; all: boolean } | null;
  /** paths known to be hidden from Claude (the index, listings, reads): the eye in flat lists, ⌘P, tabs (K10) */
  hidden: Set<string>;
  /** S2: the isolation policy is unreadable - one banner instead of an eye on every file */
  policyUnreadable: boolean;
  policyError: string;
  /** the view was restored from localStorage and its tabs are not opened yet (K14) */
  restore: Persisted | null;
  recent: string[];
  /** F06: when each recent file was opened in this page's life (not persisted; no time = none shown) */
  recentAt: Map<string, number>;
  /** recent locations (⌘P with an empty query): file + line + the line's text */
  locs: Hit[];
  back: { path: string; line: number }[];
  fwd: { path: string; line: number }[];
  panel: { open: boolean; tab: PanelTab; height: number };
  usages: { title: string; hits: Hit[]; cur: number; busy: boolean; note: string } | null;
  search: { q: string; glob: string; cs: boolean; result: GrepResult | null; busy: boolean; cur: number; all: boolean };
  turns: { path: string; rows: FileTurns['turns'] | null; diffPath: string; note: string } | null;
  /** a pending inline create / rename in the tree */
  edit: { kind: 'file' | 'dir' | 'rename'; dir: string; from?: string; at?: string | undefined; value: string } | null;   // at: the row ⌥N started on (G-N11-10)
  /** the last fsedit seq seen per session (each session counts its own) */
  fsSeq: Map<string, number>;
}

/** what a project's Files view keeps across a reload (K14): paths and layout only, never content */
export interface Persisted {
  tabs: string[];
  active: string;
  expanded: string[];
  scope: Scope;
  panel: { open: boolean; tab: PanelTab; height: number };
  recent: string[];
}

const SCOPES: Scope[] = ['all', 'changed', 'open', 'scratch'];
const PANEL_TABS: PanelTab[] = ['usages', 'search', 'problems', 'diag', 'turns'];

/** the stored JSON -> a Persisted (anything malformed falls back to the defaults, never throws) */
export function decodeView(raw: string | null): Persisted | null {
  if (!raw) return null;
  let o: unknown;
  try { o = JSON.parse(raw); } catch { return null; }
  if (!o || typeof o !== 'object') return null;
  const x = o as Record<string, unknown>;
  const strs = (v: unknown, max: number) => (Array.isArray(v) ? v.filter((p): p is string => typeof p === 'string' && !!p).slice(0, max) : []);
  const pn = (x.panel && typeof x.panel === 'object' ? x.panel : {}) as Record<string, unknown>;
  const height = typeof pn.height === 'number' && Number.isFinite(pn.height) ? Math.max(120, Math.min(2000, pn.height)) : 240;
  return {
    tabs: strs(x.tabs, 15),
    active: typeof x.active === 'string' ? x.active : '',
    expanded: strs(x.expanded, 500),
    scope: SCOPES.includes(x.scope as Scope) ? x.scope as Scope : 'all',
    panel: { open: pn.open === true, tab: PANEL_TABS.includes(pn.tab as PanelTab) ? pn.tab as PanelTab : 'usages', height },
    recent: strs(x.recent, 12),
  };
}

/** `src/A.java:12` or `src/A.java:12-20` (the session's cwd is the project root) */
export function reference(path: string, sel: [number, number] | null): string {
  if (!sel) return path;
  const [a, b] = sel[0] <= sel[1] ? sel : [sel[1], sel[0]];
  return a === b ? `${path}:${a}` : `${path}:${a}-${b}`;
}

/** a path's git mark: git (core.precomposeUnicode) reports a name in NFC, the listing has it as stored -
 *  decomposed (NFD) from macOS tools / archives (N6: such a new file showed no U) */
export function markAt<T>(marks: Map<string, T> | undefined, p: string): T | undefined {
  return marks?.get(p) ?? marks?.get(p.normalize('NFC'));
}

/** label of a compacted directory: Java-style dots below a source root, slashes elsewhere */
export function chainLabel(parent: string, name: string, chain: string): string {
  if (!chain) return name;
  const javaRoot = /(^|\/)(java|kotlin|groovy|scala)$/.test(parent);
  return javaRoot ? `${name}.${chain.replace(/\//g, '.')}` : `${name}/${chain}`;
}

/** tab labels: the file name, with the parent directory when two tabs share a name */
export function tabLabels(paths: string[]): string[] {
  const base = (p: string) => p.slice(p.lastIndexOf('/') + 1);
  return paths.map((p) => {
    const b = base(p);
    if (paths.filter((x) => base(x) === b).length < 2) return b;
    const parts = p.split('/');
    return parts.length > 1 ? `${b} · ${parts[parts.length - 2]}` : b;
  });
}

/** File scopes: All, Changed and Open. The legacy turn count is ignored. */
export function scopePills(changed: number, _turn: number | null, open: number, scratch: number | null = null): [Scope, string, number | null][] {
  const n = (x: number | null) => (x !== null && x > 0 ? x : null);
  return [['all', 'All', null], ['changed', 'Changed', n(changed)], ['open', 'Open', n(open)], ['scratch', 'Scratches', n(scratch)]];
}

/** Files > New scratch file (IDEA Scratches): the checkout's unversioned folder piperd creates them in (workbench.SCRATCH_DIR) */
export const SCRATCH_DIR = 'development/scratch';
/** the types the menu offers: label, extension (piperd's SCRATCH_TYPES has a starter for each) */
export const SCRATCH_TYPES: [string, string][] = [['SQL', 'sql'], ['HTTP request', 'http'], ['JSON', 'json'], ['YAML', 'yaml'], ['XML', 'xml'],
  ['Markdown', 'md'], ['Java', 'java'], ['Shell script', 'sh'], ['Properties', 'properties'], ['Plain text', 'txt']];
export const isScratch = (path: string): boolean => path.startsWith(SCRATCH_DIR + '/') && !path.slice(SCRATCH_DIR.length + 1).includes('/');
/** the scratch files of a development/scratch listing, by name (files only; a missing folder = none) */
export function scratchPaths(listing: { entries: { name: string; type: string }[] } | undefined): string[] {
  return (listing?.entries ?? []).filter((e) => e.type === 'file').map((e) => `${SCRATCH_DIR}/${e.name}`).sort((a, b) => a.localeCompare(b));
}

/** F06: the empty editor's hub - up to `max` changed files (and how many more), the recently opened ones */
export function emptyHub(changed: string[], recent: string[], max = 8, maxRecent = 5): { changed: string[]; more: number; recent: string[] } {
  return { changed: changed.slice(0, max), more: Math.max(0, changed.length - max), recent: recent.slice(0, maxRecent) };
}

/** N10-B-2: the tree row the keyboard goes to when `gone` (and what was below it) left the tree - the row now in
 *  its place, else the last one; null = no row (the keyboard fell to <body> after a delete before) */
export function rowAfter(rows: string[], gone: string): string | null {
  const i = rows.indexOf(gone);
  if (i < 0) return null;
  const rest = rows.filter((p) => p !== gone && !p.startsWith(gone + '/'));
  return rest[Math.min(i, rest.length - 1)] ?? null;
}

/** lines of `next` that differ from `prev` (1-based, in `next`): the purple marks after a session edit */
export function changedLines(prev: string[], next: string[], max = 2000): number[] {
  let a = 0;
  while (a < prev.length && a < next.length && prev[a] === next[a]) a++;
  let b = 0;
  while (b < prev.length - a && b < next.length - a && prev[prev.length - 1 - b] === next[next.length - 1 - b]) b++;
  const out: number[] = [];
  for (let i = a; i < next.length - b && out.length < max; i++) out.push(i + 1);
  // a pure deletion: mark the line after it
  if (!out.length && prev.length > next.length && a < next.length) out.push(a + 1);
  return out;
}

/** "Name · 3 d ago · summary" of a blame line (inline blame) */
export function blameText(b: BlameLine | undefined, now = Date.now() / 1000): string | null {
  if (!b) return null;
  if (!b.commit || /^0+$/.test(b.commit)) return 'not committed yet';
  const d = Math.max(0, now - b.time);
  const age = d < 3600 ? `${Math.max(1, Math.round(d / 60))} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago`
    : d < 86400 * 60 ? `${Math.round(d / 86400)} d ago` : `${Math.round(d / (86400 * 30))} mo ago`;
  const sum = b.summary.length > 60 ? cutText(b.summary, 59) + '…' : b.summary;
  return `${b.author} · ${age} · ${sum}`;
}

/** usages grouped like IntelliJ: Main / Test, then by file */
export function groupHits(hits: Hit[]): { group: 'Main' | 'Test'; files: { path: string; hits: Hit[] }[] }[] {
  const isTest = (p: string) => /(^|\/)(src\/)?test(s)?\/|Test\.[a-z]+$|\.test\.|\.spec\./i.test(p);
  const out: { group: 'Main' | 'Test'; files: { path: string; hits: Hit[] }[] }[] = [];
  for (const group of ['Main', 'Test'] as const) {
    const byFile = new Map<string, Hit[]>();
    for (const x of hits) if (isTest(x.path) === (group === 'Test')) byFile.set(x.path, [...(byFile.get(x.path) ?? []), x]);
    if (byFile.size) out.push({ group, files: [...byFile].map(([path, hs]) => ({ path, hits: hs })) });
  }
  return out;
}

export const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg|ico|bmp)$/i;
const MAC = isMac(navigator.platform || navigator.userAgent);
const SOURCE_ROOTS = /^(java|kotlin|groovy|scala)$/;
const RECENT_MAX = 12;
const LOCS_MAX = 30;
const TYPE_KINDS = new Set(['class', 'interface', 'enum', 'record', 'annotation', 'object', 'trait', 'type']);   // symbols.py
const TABS_MAX = 15;
/** 9.41: an edited file saves itself after this much quiet (the same write as ⌘S) */
export const AUTOSAVE_MS = 2000;
const EDITORS_MAX = 8;   // live editors kept for quick tab switches (each holds its document)

const store = {
  get(k: string, d: boolean): boolean {
    try { const v = machineStorage(window.localStorage).getItem(k); return v === null ? d : v === '1'; } catch { return d; }
  },
  set(k: string, v: boolean): void {
    try { machineStorage(window.localStorage).setItem(k, v ? '1' : '0'); } catch { /* private window */ }
  },
  /** a project's view (K14): open tabs, the active file, expanded folders, scope, panel, recent files */
  view(project: string): Persisted | null {
    try { return decodeView(machineStorage(window.localStorage).getItem('piper.files.view.' + project)); } catch { return null; }
  },
  saveView(project: string, p: Persisted): void {
    try { machineStorage(window.localStorage).setItem('piper.files.view.' + project, JSON.stringify(p)); } catch { /* private window, quota */ }
  },
};

export function createFiles(deps: FilesDeps) {
  const el = h('div', { class: 'files' });
  const leftEl = h('div', { class: 'fleft' });
  const rightEl = h('div', { class: 'fright' });
  el.append(leftEl, rightEl);
  sideSplitter(el, leftEl, '--tree-w', 'piper.w.tree', 160, 520, 264);
  const views = new Map<string, ProjectView>();
  let project = '';
  let narrow = false;
  let treeWide = store.get('piper.files.tree', true);
  let treeNarrow = store.get('piper.files.treen', false);
  let showNoise = store.get('piper.files.noise', false);
  let blameOn = store.get('piper.files.blame', true);
  let useClock = 0;
  let refreshTimer = 0;
  let httpCur: ReturnType<typeof httpView> | null = null;
  // persistent parts of the right side, redrawn on their own
  const tabsEl = h('div', { class: 'ftabs', role: 'tablist' });
  const headEl = h('div', { class: 'fhead' });
  const bannerEl = h('div', { class: 'fbanners' });
  const bodyEl = h('div', { class: 'fbody' });
  const panelEl = h('div', { class: 'fpanel' });
  const statusEl = h('div', { class: 'fstatus' });
  const noteEl = h('div', { class: 'fnotebar hidden' });
  rightEl.append(tabsEl, headEl, bannerEl, bodyEl, noteEl, panelEl, statusEl);
  const idea = { at: 0, ok: false };

  const view = (): ProjectView => {
    let v = views.get(project);
    if (!v) {
      const saved = project ? store.view(project) : null;
      v = { dirs: new Map(), expanded: new Set(['.', ...(saved?.expanded ?? [])]), tabs: [], active: '', scope: saved?.scope ?? 'all', filter: '',
        index: null, hidden: new Set(), policyUnreadable: false, policyError: '', restore: saved?.tabs.length ? saved : null, recent: saved?.recent ?? [], recentAt: new Map(), locs: [],
        back: [], fwd: [], panel: saved?.panel ?? { open: false, tab: 'usages', height: 240 }, usages: null,
        search: { q: '', glob: '', cs: false, result: null, busy: false, cur: -1, all: false }, turns: null, edit: null, fsSeq: new Map() };
      views.set(project, v);
    }
    return v;
  };
  /** the view of another project (created empty): sessionEdited records seqs of projects not shown */
  const view0 = (key: string): void => {
    const was = project;
    project = key;
    view();
    project = was;
  };
  const cur = (): Tab | null => { const v = view(); return v.tabs.find((t) => t.path === v.active) ?? null; };
  const msg = (e: unknown) => String(e instanceof Error ? e.message : e);
  const err = (e: unknown) => deps.toast(msg(e));
  const info = () => deps.projects().find((p) => p.key === project);
  const treeOn = () => (narrow ? treeNarrow : treeWide);
  // hidden only while a file is shown: without one, the tree is the only way in
  const applyTree = () => el.classList.toggle('tree-off', !treeOn() && !!project && !!cur());
  const textOf = (f: FileText) => f.lines.map((raw) => raw.slice(raw.indexOf('\t') + 1)).join('\n');
  // editable also while the project's session works (2026-10-07): the save's sha lock catches an edit of
  // Claude's (409: Reload / Overwrite), Claude Code refuses to Edit a file changed since its Read, and a file
  // hidden from Claude cannot be touched by it at all
  const writable = (t: Tab) => !!t.file?.sha;
  const roWhy = (t: Tab): string => (!t.file?.sha ? 'piperd returns no file hash - cannot write' : '');
  /** a path known to be hidden from Claude (K10, S5) */
  const isHidden = (p: string) => view().hidden.has(p);
  const noteHidden = (t: Tab | null, p: string, hidden: boolean) => {
    const v = view();
    if (hidden) v.hidden.add(p);
    else v.hidden.delete(p);
    if (t) t.hidden = hidden;
  };
  /** S5: an @reference or a note on a file Claude cannot read still goes out, with a warning */
  const warnHidden = (p: string, what: string) => {
    if (isHidden(p)) deps.toast(`${what} - but Claude cannot read ${p.split('/').pop()}: it is hidden from Claude (isolation policy / exclude)`);
    return isHidden(p);
  };
  /** the gutter's note lines of a file: the Git panel's notes of the project's session (K6) */
  const notesOf = (path: string) => (deps.notes?.(project) ?? []).filter((n) => n.path === path && !n.old);
  /** K14: the project's tabs, folders, scope and panel survive a reload (paths only, try/catch inside) */
  const persist = () => {
    const v = views.get(project);
    if (!project || !v || v.restore) return;
    store.saveView(project, { tabs: v.tabs.map((t) => t.path), active: v.active, expanded: [...v.expanded].filter((d) => d !== '.'),
      scope: v.scope, panel: { ...v.panel }, recent: v.recent });
  };
  const changedSet = () => deps.changed(project);
  const markOf = (p: string) => markAt(deps.marks?.(project), p);

  function setTree(on: boolean): void {
    if (narrow) { treeNarrow = on; store.set('piper.files.treen', on); }
    else { treeWide = on; store.set('piper.files.tree', on); }
    applyTree();
    drawTabs();
    if (on) (leftEl.querySelector('[data-nav]') as HTMLElement | null)?.focus({ preventScroll: true });
  }

  async function loadDir(path: string): Promise<void> {
    const v = view();
    const dirs = v.dirs;
    try {
      const r = await api.list(project, path);
      if (v.dirs !== dirs) return;   // a session edit invalidated this in-flight listing
      if (r.data) listed(v, path, r.data);
    } catch (e) {
      err(e);
    }
    drawLeft();
  }

  /** a directory listing arrived: keep it, learn its hidden entries and the policy state (K10, S2) */
  function listed(v: ProjectView, path: string, d: DirListing): void {
    v.dirs.set(path, d);
    v.policyUnreadable = !!d.policy_unreadable;
    v.policyError = d.policy_error ?? '';
    for (const e of d.entries) {
      const child = path === '.' ? e.name : `${path}/${e.name}`;
      if (e.type === 'file') {
        if (e.hidden) v.hidden.add(child);
        else v.hidden.delete(child);
      }
    }
  }

  function newTab(path: string): Tab {
    return { path, file: null, text: '', image: '', mode: 'code', marks: null, problems: null, ed: null, edLoading: false,
      dirty: false, caret: null, used: ++useClock, scroll: 0, claude: [], remote: null, banner: null, blame: null, blameLoading: false,
      md: null, http: null, hidden: false };
  }

  function dropEditor(t: Tab): void {
    if (t.ed) { t.scroll = t.ed.scrollTop(); t.ed.destroy(); t.ed = null; }
    t.md = null;
  }

  /** the whole file (pages of 5000 lines): the editor needs all of it */
  async function readAll(path: string): Promise<FileText | null> {
    const r = await api.read(project, path, 0);
    let f = r.data;
    while (f && f.lines.length < f.total_lines) {
      const more = (await api.read(project, path, f.offset + f.lines.length)).data;
      if (!more || !more.lines.length) break;
      f = { ...f, lines: [...f.lines, ...more.lines] };
    }
    return f;
  }

  async function open(path: string, line = 0, quiet = false): Promise<void> {
    const v = view();
    const key = project;
    let t = v.tabs.find((x) => x.path === path) ?? null;
    const fresh = !t;
    try {
      if (!t) {
        t = newTab(path);
        if (IMAGE_RE.test(path)) t.image = await rawImage(project, path);
        else {
          const f = await readAll(path);
          if (!f) return;
          t.file = f;
          t.hidden = !!f.hidden;   // the view of the project it was opened in (the user may have switched meanwhile)
          if (t.hidden) v.hidden.add(path);
          else v.hidden.delete(path);
          t.text = textOf(f);
          if (langOf(path) === 'md' && !line) t.mode = 'preview';
        }
      }
    } catch (e) {
      if (t && deps.changed(key).has(path)) t.mode = 'diff';
      else { if (!quiet) err(e); return; }
    }
    if (key !== project) return;
    if (v.active !== path) flush();   // 9.41: the tab left saves its pending edit now
    if (fresh) {
      const twin = v.tabs.find((x) => x.path === path);   // a second open of the path finished first
      if (twin) t = twin;
      else {
        v.tabs.push(t);
        trimTabs();
      }
    }
    if (quiet) return; // restore content without stealing a newer explicit navigation
    if (v.active !== path) rememberLoc();
    v.active = path;
    if (!quiet) deps.opened?.(project, path, line);
    t.used = ++useClock;
    if (line && t.mode !== 'code') t.mode = 'code';
    v.recent = [path, ...v.recent.filter((x) => x !== path)].slice(0, RECENT_MAX);
    v.recentAt.set(path, Date.now() / 1000);
    if (v.scope === 'all' && !v.filter) reveal(path, false);
    drawRight(line);
    drawLeft();
    if (t.file && fresh) {
      void loadMarks(t);
      void loadProblems(t);
      if (t.hidden) drawBanners();
    } else if (t.file && !t.dirty) void reread(t, false);
  }

  function trimTabs(): void {
    const v = view();
    while (v.tabs.length > TABS_MAX) {
      const old = v.tabs.filter((t) => t.path !== v.active && !t.dirty).sort((a, b) => a.used - b.used)[0];
      if (!old) break;
      forget(old);
    }
  }

  function forget(t: Tab): void {
    const v = view();
    unschedule(t);
    dropEditor(t);
    if (t.image) URL.revokeObjectURL(t.image);
    v.tabs = v.tabs.filter((x) => x !== t);
  }

  /** the app's own confirmation (not window.confirm: it blocks the page and looks foreign); Esc / Cancel = no */
  function ask(title: string, text: string, ok: string, then: () => void): void {
    const yes = h('button', { class: 'danger', onclick: () => { deps.closeModal(); then(); } }, ok);
    deps.modal(title, h('p', {}, showBidi(text)), h('div', { class: 'row' }, h('button', { onclick: () => deps.closeModal() }, 'Cancel'), yes));
    yes.focus();
  }

  function closeTab(path?: string, sure = false): void {
    const v = view();
    const t = path ? v.tabs.find((x) => x.path === path) : cur();
    if (!t) return;
    if (t.dirty && !sure) return ask('Discard unsaved changes?', `${t.path} has changes that are not saved.`, 'Discard', () => closeTab(t.path, true));
    const i = v.tabs.indexOf(t);
    forget(t);
    if (v.active === t.path) {
      const next = v.tabs[Math.min(i, v.tabs.length - 1)];
      v.active = next?.path ?? '';
    }
    drawRight();
    drawLeft();
  }

  function activate(path: string): void {
    const v = view();
    if (v.active === path) return;
    flush();
    rememberLoc();
    v.active = path;
    const t = cur();
    if (t) t.used = ++useClock;
    drawRight();
    drawLeft();
    // a tab shown again: the file may have changed meanwhile
    if (t?.file && !t.dirty) void reread(t, false);
  }

  /** expand the tree down to a path (its parent directories), loading what is missing */
  function reveal(path: string, isDir: boolean): void {
    const v = view();
    const parts = path.split('/');
    for (let i = 1; i <= parts.length - (isDir ? 0 : 1); i++) {
      const d = parts.slice(0, i).join('/');
      v.expanded.add(d);
      if (!v.dirs.has(d)) void loadDir(d);
    }
  }

  /** the ⌘P index; all = with the excluded tooling folders (K1), kept until it expires */
  async function index(all = false): Promise<string[]> {
    const v = view();
    if (v.index && Date.now() - v.index.at < 30000 && (v.index.all || !all)) return v.index.files;
    const r = await fsIndex(project, all);
    v.index = { at: Date.now(), files: r.data?.files ?? [], all };
    const hid = new Set(r.data?.hidden ?? []);
    for (const p of v.index.files) if (hid.has(p)) v.hidden.add(p); else v.hidden.delete(p);
    if (r.data?.policy_unreadable !== undefined) v.policyUnreadable = r.data.policy_unreadable;
    if (r.data?.truncated) deps.toast('The project has more files than the index holds - find the rest by search');
    return v.index.files;
  }

  // ---------------------------------------------------------------- locations (⌘[ ⌘] and recent locations)
  function here(): { path: string; line: number } | null {
    const t = cur();
    if (!t) return null;
    return { path: t.path, line: t.caret?.line ?? 0 };
  }

  function rememberLoc(): void {
    const t = cur();
    if (!t?.ed || !t.caret) return;
    const v = view();
    const text = t.ed.text().split('\n', t.caret.line)[t.caret.line - 1]?.trim() ?? '';
    v.locs = [{ path: t.path, line: t.caret.line, text }, ...v.locs.filter((l) => !(l.path === t.path && Math.abs(l.line - t.caret!.line) < 3))].slice(0, LOCS_MAX);
  }

  /** push the current place, then open the target (go to definition, results) */
  function jump(path: string, line: number): void {
    const v = view();
    const at = here();
    if (at) {
      v.back.push(at);
      if (v.back.length > 50) v.back.shift();
      v.fwd = [];
    }
    void open(path, line);
  }

  /** false: no jump of its own to go back to (the GUI's history takes over, policy nav_history) */
  function back(): boolean {
    const v = view();
    const b = v.back.pop();
    if (!b) return false;
    const at = here();
    if (at) v.fwd.push(at);
    void open(b.path, b.line);
    return true;
  }

  function forward(): boolean {
    const v = view();
    const f = v.fwd.pop();
    if (!f) return false;
    const at = here();
    if (at) v.back.push(at);
    void open(f.path, f.line);
    return true;
  }

  // ---------------------------------------------------------------- pickers (outline, definitions, projects)

  interface Pick { name: string; sub: string; tag?: string; depth?: number; spans?: [number, number][]; badge?: string; hidden?: boolean; run(event?: MouseEvent | KeyboardEvent): void; alt?(): void }

  /** a filterable list in the modal: ↑↓ + Enter (⌥Enter = the row's alternative), the rows recomputed by `items` */
  function picker(title: string, placeholder: string, items: (q: string) => Pick[] | Promise<Pick[]>, empty: (q: string) => string,
    opts: { foot?: string; sections?: (rows: Pick[]) => (string | Pick)[] } = {}): { refresh(): void; value(): string; input: HTMLInputElement } {
    const input = h('input', { type: 'search', placeholder, autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement;
    const list = h('div', { class: 'plist tall' });
    let rows: Pick[] = [];
    let sel = 0;
    let seq = 0;
    const name = (p: Pick) => {
      if (!p.spans?.length) return showBidi(p.name);
      const out: (string | HTMLElement)[] = [];
      let at = 0;
      for (const [a, b] of p.spans) {
        if (a > at) out.push(showBidi(p.name.slice(at, a)));
        out.push(h('b', { class: 'hit' }, showBidi(p.name.slice(a, b))));
        at = b;
      }
      if (at < p.name.length) out.push(showBidi(p.name.slice(at)));
      return out;
    };
    const draw = () => {
      clear(list);
      const shown = opts.sections ? opts.sections(rows) : rows;
      for (const r of shown) {
        if (typeof r === 'string') { list.append(h('div', { class: 'psec' }, r)); continue; }
        const i = rows.indexOf(r);
        list.append(h('button', {
          class: 'pitem' + (i === sel ? ' active' : ''), style: r.depth ? `padding-left:${10 + r.depth * 14}px` : undefined,
          onclick: (event: Event) => { deps.closeModal(); r.run(event as MouseEvent); },
        }, h('span', { class: 'name' }, r.tag ? h('span', { class: 'kind' }, r.tag) : null, ...[name(r)].flat(), r.hidden ? hiddenMark() : null),
        h('span', { class: 'mut mono' }, showBidi(r.sub)), r.badge ? h('span', { class: 'pbadge' }, r.badge) : null));
      }
      list.querySelector('.pitem.active')?.scrollIntoView({ block: 'nearest' });
      if (!rows.length) list.append(h('p', { class: 'mut' }, empty(input.value)));
    };
    const update = async () => {
      const my = ++seq;
      const r = await Promise.resolve(items(input.value)).catch((e: unknown) => { err(e); return [] as Pick[]; });
      if (my !== seq) return;
      rows = r;
      sel = Math.min(sel, Math.max(0, rows.length - 1));
      draw();
    };
    input.addEventListener('input', () => { sel = 0; void update(); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, rows.length - 1); draw(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); }
      else if (e.key === 'PageDown') { sel = Math.min(sel + 10, rows.length - 1); draw(); e.preventDefault(); }
      else if (e.key === 'PageUp') { sel = Math.max(sel - 10, 0); draw(); e.preventDefault(); }
      else if (e.key === 'Enter') {
        const p = rows[sel];
        if (p) { deps.closeModal(); if (e.altKey && p.alt) p.alt(); else p.run(e); }
        e.preventDefault();
      }
    });
    deps.modal(title, input, list, opts.foot ? h('div', { class: 'pfoot mut small' }, opts.foot) : null);
    input.focus();
    void update();
    return { refresh: () => void update(), value: () => input.value, input };
  }

  function pickProject(): void {
    const ps = deps.projects().filter((x) => x.exists && refHost(x.key) === refHost(project));
    picker('Project', 'project…', (q) => ps.map((p) => ({ p, s: q.trim() ? score(q, p.key) : 0 }))
      .filter((x): x is { p: Project; s: number } => x.s !== null).sort((a, b) => b.s - a.s)
      .map((x) => ({ name: rawProjectKey(x.p.key), sub: x.p.path, run: () => show(x.p.key) })), () => 'no project');
  }

  // ---------------------------------------------------------------- Search Everywhere ⌘P
  /** all = the index and text search include the excluded tooling folders (K1, offered after an empty result) */
  function quickOpen(initial = '', all = false): void {
    if (!project) return pickProject();
    const v = view();
    let files: string[] = [];
    let symTimer = 0;
    let textTimer = 0;
    let syms: SymbolHit[] = [];
    let symsFor = '';
    let text: GrepResult | null = null;
    let textFor = '';
    const ref = (p: string, line: number) => reference(p, line ? [line, line] : null) + ' ';
    const insertRef = (p: string, line: number) => {
      if (!deps.insert(ref(p, line))) deps.toast('No live session of this project');
      else warnHidden(p, `Inserted ${p}`);
    };
    const wider = (raw: string): Pick[] => (all ? [] : [{ name: 'Search the excluded folders too', sub: 'node_modules, target, .venv, .idea, caches…',
      tag: 'all', run: () => quickOpen(raw, true) }]);
    const openPick = (path: string, line: number, event?: MouseEvent | KeyboardEvent) => {
      if (event && (MAC ? event.metaKey : event.ctrlKey)) deps.showFiles?.(project);
      jump(path, line);
    };
    const fileRow = (path: string, line: number, spans: [number, number][] = []): Pick => {
      const slash = path.lastIndexOf('/');
      const name = path.slice(slash + 1);
      const ns = spans.filter(([a]) => a > slash).map(([a, b]) => [a - slash - 1, b - slash - 1] as [number, number]);
      const m = markOf(path);
      return { name, sub: slash > 0 ? leftCut(path.slice(0, slash), 54) : '', spans: ns, badge: m?.letter ?? (/(^|\/)test|Test\./.test(path) ? 'test' : ''),
        hidden: isHidden(path), run: (event) => openPick(path, line, event), alt: () => insertRef(path, line) };
    };
    const symRow = (s: SymbolHit, q: string): Pick => ({ name: s.name, tag: s.kind, sub: `${leftCut(s.path, 48)}:${s.line}`,
      spans: matchSpans(q, s.name) ?? [], hidden: isHidden(s.path), run: (event) => openPick(s.path, s.line, event), alt: () => insertRef(s.path, s.line) });
    const cmdRows = (q: string): Pick[] => (deps.commands?.() ?? [])
      .map((c) => ({ c, s: score(q, c.label) })).filter((x): x is { c: Command; s: number } => x.s !== null).sort((a, b) => b.s - a.s)
      .slice(0, 8).map(({ c }) => ({ name: c.label, sub: c.hint ?? '', tag: 'action', spans: matchSpans(q, c.label) ?? [], run: () => c.run() }));
    const textRows = (r: GrepResult | null): Pick[] => (r?.matches ?? []).slice(0, 20).map((m: GrepHit) => ({
      name: m.text.trim().slice(0, 120), sub: `${leftCut(m.path, 48)}:${m.line}`, tag: 'text', hidden: !!m.hidden || isHidden(m.path),
      run: (event) => openPick(m.path, m.line, event), alt: () => insertRef(m.path, m.line) }));
    const kinds = new Map<Pick, string>();
    const pk = picker(kbd('Search everywhere ⌘P'), 'file, #symbol, >action, /text · Foo.java:120', (input) => {
      kinds.clear();
      const qq = quickQuery(input);
      const raw = input.trim();
      const tag = (rows: Pick[], k: string) => { for (const r of rows) kinds.set(r, k); return rows; };
      if (raw.startsWith('>')) return tag(cmdRows(raw.slice(1).trim()), 'ACTIONS');
      if (raw.startsWith('/')) {
        const q = raw.slice(1);
        if (q.length < 2) return [];
        if (q !== textFor) {
          window.clearTimeout(textTimer);
          textTimer = window.setTimeout(() => {
            textFor = q;
            fsGrep(project, usagePattern(q).replace(/\\b/g, ''), '', { all }).then((r) => { text = r.data; pk.refresh(); }).catch(err);
          }, 250);
          return tag(textRows(text), 'TEXT');
        }
        return text && !text.matches.length ? tag(wider(input), 'NOTHING FOUND') : tag(textRows(text), 'TEXT');
      }
      if (qq.kind === 'symbol') {
        if (!qq.q) return [];
        if (qq.q !== symsFor) {
          window.clearTimeout(symTimer);
          symTimer = window.setTimeout(() => {
            symsFor = qq.q;
            api.symbols(project, '', qq.q).then((r) => { syms = r.data?.symbols ?? []; pk.refresh(); }).catch(err);
          }, 150);
        }
        return tag(syms.slice(0, 60).map((s) => symRow(s, qq.q)), 'SYMBOLS');
      }
      if (!qq.q) {
        // recent locations first (file + line + text), then recent files
        const locs = v.locs.slice(0, 12).map((l): Pick => ({ name: l.path.split('/').pop() ?? l.path, sub: `${l.line} · ${l.text.slice(0, 80)}`,
          run: (event) => openPick(l.path, l.line, event), alt: () => insertRef(l.path, l.line) }));
        const seen = new Set(v.locs.map((l) => l.path));
        return [...tag(locs, 'RECENT LOCATIONS'), ...tag(v.recent.filter((f) => !seen.has(f)).map((f) => fileRow(f, qq.line)), 'RECENT FILES')];
      }
      const ranked = rankFiles(qq.q, files, { changed: changedSet(), open: new Set(v.tabs.map((t) => t.path)), recent: v.recent }, 40);
      const fileRows = ranked.length || !files.length ? tag(ranked.map((r) => fileRow(r.path, qq.line, r.spans)), 'FILES') : tag(wider(input), 'NO FILE');
      // symbols too when the query looks like a name (debounced, the index ranks exact/prefix first)
      let symRows: Pick[] = [];
      if (qq.q.length >= 3 && !qq.line && !qq.q.includes('/') && !qq.q.includes('.')) {
        if (qq.q !== symsFor) {
          window.clearTimeout(symTimer);
          symTimer = window.setTimeout(() => {
            symsFor = qq.q;
            api.symbols(project, '', qq.q).then((r) => { syms = r.data?.symbols ?? []; pk.refresh(); }).catch(() => { /* files still show */ });
          }, 200);
        } else symRows = tag(syms.filter((s) => TYPE_KINDS.has(s.kind) || s.kind === 'method' || s.kind === 'function').slice(0, 6).map((s) => symRow(s, qq.q)), 'SYMBOLS');
      }
      const acts = qq.q.length >= 3 ? tag(cmdRows(qq.q).slice(0, 3), 'ACTIONS') : [];
      const find: Pick[] = qq.q.length >= 3 ? tag([{ name: `"${qq.q}" in the content of the project`, sub: kbd('⌘⇧F'), tag: 'text',
        run: () => openSearch(qq.q) }], 'TEXT') : [];
      return [...fileRows, ...symRows, ...acts, ...find];
    }, (input) => {
      const raw = input.trim();
      if (raw.startsWith('/')) return raw.length > 2 ? (text ? 'nothing found' : 'searching…') : 'type text after /';
      if (raw.startsWith('>')) return 'no action';
      const qq = quickQuery(input);
      if (qq.kind === 'symbol') return qq.q ? 'no symbol (index of classes, methods, beans, keys…)' : 'type a symbol name after #';
      return qq.q ? (files.length ? 'nothing found' : 'loading the index…') : 'type part of a name or path · #Symbol · >action · /text';
    }, {
      foot: kbd(`↑↓ select · ⏎ open · ${MAC ? '⌘' : 'Ctrl'}+click / ${MAC ? '⌘' : 'Ctrl+'}⏎ open in Files · ⌥⏎ insert @reference into the prompt · Foo.java:120 line · empty = recent locations`),
      sections: (rows) => {
        const out: (string | Pick)[] = [];
        let last = '';
        for (const r of rows) {
          const k = kinds.get(r) ?? '';
          if (k && k !== last) { out.push(k); last = k; }
          out.push(r);
        }
        return out;
      },
    });
    if (initial) { pk.input.value = initial; pk.refresh(); }
    index(all).then((f) => { files = f; pk.refresh(); }).catch(err);
  }

  function outlineOf(t: Tab): OutlineItem[] {
    return outline((t.ed ? t.ed.text() : t.text).split('\n'), t.path);
  }

  /** ⌘⇧O: classes, methods, headings of the current file */
  function showOutline(): void {
    const t = cur();
    if (!t?.file) return deps.toast('Open a text file');
    const all = outlineOf(t);
    picker(`Outline ${t.path.split('/').pop()}`, 'filter…', (q) => {
      const rows = q.trim()
        ? all.map((o) => ({ o, s: score(q, o.name) })).filter((x): x is { o: OutlineItem; s: number } => x.s !== null)
          .sort((a, b) => b.s - a.s).map((x) => x.o)
        : all;
      return rows.slice(0, 400).map((o) => ({ name: o.name, tag: o.kind, sub: String(o.line), depth: q.trim() ? 0 : o.depth,
        spans: matchSpans(q, o.name) ?? [], run: () => gotoLineNum(o.line) }));
    }, () => (all.length ? 'nothing found' : 'nothing to outline in this file'));
  }

  function gotoLine(): void {
    const t = cur();
    if (!t?.file) return deps.toast('Open a text file');
    const n = (t.ed ? t.ed.text() : t.text).split('\n').length;
    const input = h('input', { type: 'text', placeholder: `line[:column] (1-${n})`, autocomplete: 'off', inputmode: 'numeric' }) as HTMLInputElement;
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const lc = parseLineCol(input.value);
      if (!lc) { input.classList.add('notfound'); return; }
      deps.closeModal();
      gotoLineNum(Math.max(1, Math.min(lc.line, n)));
    });
    deps.modal(kbd('Go to line ⌘L', 'Ctrl'), input);
    input.focus();
  }

  function gotoLineNum(n: number): void {
    const t = cur();
    if (!t?.file) return;
    if (t.mode === 'preview' && t.md) {
      let best: HTMLElement | null = null;
      t.md.querySelectorAll<HTMLElement>('[data-line]').forEach((b) => { if (Number(b.dataset.line) <= n) best = b; });
      (best as HTMLElement | null)?.scrollIntoView({ block: 'start' });
      return;
    }
    if (t.mode !== 'code') { t.mode = 'code'; drawRight(n); return; }
    if (t.ed) { t.ed.reveal(n); t.ed.focus(); }
    else drawRight(n);
  }

  async function gotoDefinition(name: string, fromLine: number): Promise<void> {
    const t = cur();
    let hits: SymbolHit[] = [];
    try {
      hits = (await api.symbols(project, name)).data?.symbols ?? [];
    } catch (e) {
      return err(e);
    }
    // a Java/Kotlin constructor is named like its class: in a file that defines the type, the type wins
    const typeFiles = new Set(hits.filter((s) => TYPE_KINDS.has(s.kind)).map((s) => s.path));
    const elsewhere = hits.filter((s) => !(t && s.path === t.path && s.line === fromLine)
      && !(s.kind === 'method' && typeFiles.has(s.path)));
    // clicked the definition itself (or nothing is indexed): its usages, like IntelliJ
    if (!elsewhere.length) {
      if (!hits.length) deps.toast(`Definition "${name}" is not in the index - searching for usages`);
      return findUsages(name);
    }
    if (elsewhere.length === 1) return jump(elsewhere[0]!.path, elsewhere[0]!.line);
    const here_ = t?.path ?? '';
    const sorted = [...elsewhere].sort((a, b) => Number(b.path === here_) - Number(a.path === here_));
    picker(`Definitions of ${name} (${sorted.length})`, 'filter…', (q) => sorted
      .filter((s) => !q.trim() || score(q, `${s.path} ${s.text}`) !== null)
      .map((s) => ({ name: s.path.split('/').pop() ?? s.path, tag: s.kind, sub: `${s.path}:${s.line} · ${s.text}`, run: () => jump(s.path, s.line) })),
    () => 'Nothing matches.');
  }

  // ---------------------------------------------------------------- usages (⌥F7) - the bottom panel
  function findUsages(name?: string): void {
    const t = cur();
    const n = name ?? (t?.caret?.selText && /^[\w$.-]{1,120}$/.test(t.caret.selText) ? t.caret.selText : t?.caret?.word) ?? '';
    if (!n) return deps.toast('Put the caret on an identifier (or select it)');
    const v = view();
    const mine = { title: n, hits: [] as Hit[], cur: -1, busy: true, note: '' };
    v.usages = mine;
    openPanel('usages');
    const key = project;
    void (async () => {
      // IntelliJ's MCP has no references tool: the whole word, case-sensitive
      try {
        const r = (await api.grep(key, usagePattern(n), '', { cs: true, limit: 1000 })).data;
        mine.hits = r?.matches ?? [];
        if (r?.truncated) mine.note = 'truncated - the first 1000';
      } catch (e) {
        mine.note = msg(e);
      }
      mine.busy = false;
      // a newer ⌥F7 (or another project) owns the panel now: this answer stays in its own object
      if (key === project && v.usages === mine) drawPanel();
    })();
  }

  // ---------------------------------------------------------------- left: tree
  function icon(name: string, dir: boolean, open_ = false): HTMLElement {
    return h('span', { class: 'fi ' + (dir ? (open_ ? 'fi-diro' : 'fi-dir') : 'fi-' + fileKind(name)), 'aria-hidden': 'true' });
  }

  function badge(m: ChangeMark | undefined): HTMLElement | null {
    return m ? h('span', { class: 'gm gm-' + m.cls, title: m.word }, m.letter) : null;
  }

  function fileRow(child: string, label: string, depth: number, extra: HTMLElement | null = null, extra2: HTMLElement | null = null, cls = ''): HTMLElement {
    const v = view();
    const m = markOf(child);
    const open_ = v.tabs.some((t) => t.path === child);
    return h('button', {
      class: 'tnode file' + (child === v.active ? ' active' : '') + (open_ ? ' opened' : '') + (m ? ' gs-' + m.cls : changedSet().has(child) ? ' gs-mod' : '') + cls,
      style: `padding-left:${depth * 12 + 8}px`, title: showBidi(child), 'data-nav': child, 'data-depth': depth, 'data-dir': '',
      onclick: () => { if (narrow && matchMedia('(max-width: 640px)').matches) setTree(false); void open(child); }, oncontextmenu: (e: Event) => treeMenu(e as MouseEvent, child, false),
    }, h('span', { class: 'chev' }), icon(label, false), h('span', { class: 'nm' }, showBidi(label)), extra, extra2, badge(m));
  }

  function entryNode(path: string, e: DirEntry, depth: number, changed: Set<string>): HTMLElement[] {
    const v = view();
    const pad = `padding-left:${depth * 12 + 8}px`;
    const child = path === '.' ? e.name : `${path}/${e.name}`;
    // hidden from Claude (isolation policy / exclude): an ordinary entry here, marked - nothing in Files
    // sends file content to a session, only @references; an unreadable policy marks everything: one banner instead (S2)
    const hid = e.hidden && !v.policyUnreadable ? hiddenMark() : null;
    // a symbolic link: shown and opened, but ⌘P and search skip it (C5)
    const link = e.link ? h('span', { class: 'mut small', title: kbd('symbolic link - ⌘P and search skip it') }, '↗') : null;
    if (e.type !== 'dir') return [fileRow(child, e.name, depth, hid, link)];
    // compact packages: one node for the chain - but a source root (java/, kotlin/) stays its
    // own node, so the package below it reads com.acme.shop like in IntelliJ
    const chain = SOURCE_ROOTS.test(e.name) ? '' : e.chain ?? '';
    const full = chain ? `${child}/${chain}` : child;
    const isOpen = v.expanded.has(full);
    const n = [...changed].filter((c) => c.startsWith(full + '/')).length;
    const out: HTMLElement[] = [h('button', {
      class: 'tnode dir' + (n ? ' has-chg' : ''), style: pad, title: showBidi(full),
      'data-nav': full, 'data-depth': depth, 'data-dir': '1', 'data-open': isOpen ? '1' : '',
      onclick: () => {
        if (isOpen) v.expanded.delete(full);
        else {
          v.expanded.add(full);
          if (!v.dirs.has(full)) void loadDir(full);
        }
        drawLeft();
      },
      oncontextmenu: (ev: Event) => treeMenu(ev as MouseEvent, full, true),
    }, h('span', { class: 'chev' + (isOpen ? ' open' : '') }), icon(e.name, true, isOpen), h('span', { class: 'nm' }, showBidi(chainLabel(path, e.name, chain))), hid, link,
    n ? h('span', { class: 'cnt', title: `${n} changed` }, String(n)) : null)];   // F06: the count in grey, the name carries the colour
    if (isOpen) {
      if (v.edit && v.edit.kind !== 'rename' && v.edit.dir === full) out.push(editRow(depth + 1));
      out.push(...tree(full, depth + 1, changed));
    }
    return out;
  }

  function tree(path: string, depth: number, changed: Set<string>): HTMLElement[] {
    const v = view();
    const d = v.dirs.get(path);
    if (!d) return [h('div', { class: 'tnode mut', style: `padding-left:${depth * 12 + 8}px` }, 'loading…')];
    const all = [...d.entries].sort((a, b) => (a.type === b.type ? 0 : a.type === 'dir' ? -1 : 1));
    // tooling noise: the server's one list (projectfs.NOISE, K1)
    const noise = all.filter((e) => e.noise);
    const entries = showNoise ? all : all.filter((e) => !e.noise);
    const out: HTMLElement[] = [];
    for (const e of entries) {
      const child = path === '.' ? e.name : `${path}/${e.name}`;
      const chain = e.type === 'dir' && !SOURCE_ROOTS.test(e.name) ? e.chain ?? '' : '';   // a compacted node renames its last directory
      if (v.edit?.kind === 'rename' && v.edit.from === (chain ? `${child}/${chain}` : child)) out.push(editRow(depth));
      else out.push(...entryNode(path, e, depth, changed));
    }
    if (noise.length) {
      out.push(h('button', { class: 'tnode noise', style: `padding-left:${depth * 12 + 26}px`,
        title: kbd('tooling folders and files (IDE, OS, caches, build output) - Claude sees them as before; ⌘P and search skip them unless asked; click shows / hides them'),
        onclick: () => { showNoise = !showNoise; store.set('piper.files.noise', showNoise); drawLeft(); },
      }, showNoise ? `hide ${noise.length} system` : `+ ${noise.length} system  ${noise.map((x) => x.name).slice(0, 3).join(' ')}${noise.length > 3 ? ' …' : ''}`));
    }
    if (d.truncated) out.push(h('div', { class: 'tnode mut' }, '… (truncated)'));
    return out;
  }

  /** a flat list grouped by directory: the Changed / Turn / Open scopes and the filter; `scratch` = development/ and
   *  OS droppings go last, dimmed, like the Git panel's "scratch & OS" (K8) */
  function flat(paths: string[], spans: Map<string, [number, number][]> | null = null, scratch = false): HTMLElement[] {
    if (scratch) {
      const junk = paths.filter(isJunk);
      if (junk.length) {
        return [...flat(paths.filter((p) => !isJunk(p)), spans),
          h('div', { class: 'tdir mono scratch', title: 'development/ scratch and OS files - the Git panel leaves them unchecked' }, `scratch & OS (${junk.length})`),
          ...flat(junk, spans).map((x) => { x.classList.add('scratch'); return x; })];
      }
    }
    const byDir = new Map<string, string[]>();
    for (const p of paths) {
      const d = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '.';
      byDir.set(d, [...(byDir.get(d) ?? []), p]);
    }
    const out: HTMLElement[] = [];
    const dirs = spans ? [...byDir.keys()] : [...byDir.keys()].sort();
    for (const d of dirs) {
      out.push(h('div', { class: 'tdir mono', title: showBidi(d) }, d === '.' ? '(root)' : showBidi(leftCut(d, 44))));
      for (const p of byDir.get(d)!) {
        const name = p.slice(p.lastIndexOf('/') + 1);
        const sp = spans?.get(p);
        const row = fileRow(p, name, 1, isHidden(p) && !view().policyUnreadable ? hiddenMark() : null);
        if (sp?.length) {
          const nm = row.querySelector('.nm')!;
          clear(nm as HTMLElement);
          const off = p.length - name.length;
          let at = 0;
          for (const [a0, b0] of sp) {
            const a = a0 - off;
            const b = b0 - off;
            if (b <= 0) continue;
            if (a > at) nm.append(showBidi(name.slice(at, a)));
            nm.append(h('b', { class: 'hit' }, showBidi(name.slice(Math.max(a, 0), b))));
            at = b;
          }
          nm.append(showBidi(name.slice(at)));
        }
        out.push(row);
      }
    }
    return out;
  }

  /** the keyboard to the first of these tree rows that is shown, else the first row (never <body>: N10-B-2) */
  function focusRow(...navs: (string | null | undefined)[]): void {
    const rows = [...leftEl.querySelectorAll<HTMLElement>('.tree [data-nav]')];
    const r = navs.map((n) => rows.find((x) => n != null && x.dataset.nav === n)).find(Boolean) ?? rows[0];
    r?.focus({ preventScroll: true });
    r?.scrollIntoView({ block: 'nearest' });
  }

  function editRow(depth: number): HTMLElement {
    const v = view();
    const ed = v.edit!;
    const input = h('input', { type: 'text', class: 'tedit', value: ed.value, spellcheck: 'false', autocomplete: 'off',
      placeholder: ed.kind === 'dir' ? 'folder name' : ed.kind === 'rename' ? 'new name or path' : 'file name (a/b.txt makes folders)' }) as HTMLInputElement;
    input.addEventListener('input', () => { ed.value = input.value; });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); void commitEdit(); }
      else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        v.edit = null;
        drawLeft();
        // the keyboard stays in the tree: back on the row the edit started from (⇧F6 / ⌥N), not on <body>
        focusRow(ed.from ?? ed.at ?? ed.dir);
      }
    });
    input.addEventListener('blur', () => { window.setTimeout(() => { if (v.edit === ed && document.activeElement !== input) { v.edit = null; drawLeft(); } }, 150); });
    window.requestAnimationFrame(() => {
      input.focus();
      const dot = ed.kind === 'rename' ? input.value.lastIndexOf('.') : -1;
      input.setSelectionRange(dot > 0 ? input.value.lastIndexOf('/') + 1 : input.value.length, dot > 0 ? dot : input.value.length);
    });
    return h('div', { class: 'tnode tedit-row', style: `padding-left:${depth * 12 + 8}px` },
      h('span', { class: 'fi ' + (ed.kind === 'dir' ? 'fi-dir' : 'fi-new') }), input,
      h('span', { class: 'mut small' }, '⏎ ' + (ed.kind === 'rename' ? 'rename' : 'create') + ' · Esc'));
  }

  function drawLeft(): void {
    const v = view();
    const tscroll = leftEl.querySelector('.tree')?.scrollTop ?? 0;
    const active = document.activeElement as HTMLElement | null;
    const filterFocus = active?.classList.contains('tfilter') ?? false;
    const caret = filterFocus ? [(active as HTMLInputElement).selectionStart, (active as HTMLInputElement).selectionEnd] as const : null;
    const navFocus = active && leftEl.contains(active) ? active.dataset.nav ?? '' : '';
    const editing = active?.classList.contains('tedit') ?? false;
    clear(leftEl);
    const top = h('div', { class: 'ftop' },
      h('button', { class: 'pchip', title: project ? `${rawProjectKey(project)} · switch project` : 'pick a project', 'aria-label': project ? `Project ${rawProjectKey(project)} – switch project` : 'pick a project', onclick: pickProject }, h('span', { class: 'fi fi-proj' }), h('span', { class: 'pl' }, project ? rawProjectKey(project) : 'pick a project'), h('span', { class: 'mut' }, ' ▾')),
      project ? h('button', { class: 'icon scratch-new', title: 'New scratch file', 'aria-label': 'New scratch file', onclick: (e: Event) => scratchMenu(e.currentTarget as HTMLElement) }, h('span', { class: 'fi fi-new' })) : null,
      h('button', { class: 'icon', title: 'collapse all', 'aria-label': 'collapse all', onclick: () => { v.expanded = new Set(['.']); drawLeft(); } }, '⊟'),
      h('button', { class: 'icon tree-hide', title: 'hide tree', 'aria-label': 'hide tree', onclick: () => setTree(false) }, '«'));
    leftEl.append(top);
    if (!project) return;
    const fi = h('input', { type: 'search', class: 'tfilter', placeholder: 'Filter', title: 'Filter - or just type in the tree', value: v.filter, autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement;
    let ft = 0;
    fi.addEventListener('input', () => { v.filter = fi.value; window.clearTimeout(ft); ft = window.setTimeout(drawLeft, 80); });
    fi.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'Enter') {
        const first = leftEl.querySelector<HTMLElement>('.tree [data-nav]');
        if (e.key === 'Enter' && first && v.filter) first.click();
        else first?.focus();
        e.preventDefault();
      } else if (e.key === 'Escape' && v.filter) { v.filter = ''; e.preventDefault(); e.stopPropagation(); drawLeft(); }
    });
    const ch = changedSet();
    // the turn picked in the Git panel (the newest unless one is chosen there, K7)
    const picked = deps.turnFiles?.(project) ?? null;
    const turn = picked?.files ?? null;
    const scratchList = v.dirs.get(SCRATCH_DIR);
    if (!scratchList) void loadScratch();
    const scratches = scratchPaths(scratchList);
    const scopes = scopePills(ch.size, turn ? turn.size : null, v.tabs.length, scratches.length);
    const seg = h('div', { class: 'scope', role: 'tablist' }, ...scopes.map(([s, l, n]) => h('button', {
      class: v.scope === s ? 'on' : '', role: 'tab', 'aria-selected': v.scope === s ? 'true' : 'false',
      title: s === 'turn' ? `files of the turn picked in the Git panel (the newest by default)${picked ? ` - turn ${picked.n}` : ''}` : s === 'changed' ? 'uncommitted changes' : s === 'open' ? 'open tabs' : s === 'scratch' ? `scratch files in ${SCRATCH_DIR} - never committed` : 'the whole project',
      onclick: () => { v.scope = s; drawLeft(); if (s === 'changed' || s === 'turn') deps.gitChanged?.(project); },   // fresh git status, not the last one the panel fetched
    }, l, n !== null ? h('span', { class: 'n' }, String(n)) : null)));
    leftEl.append(fi, seg);
    // S2: the isolation policy is unreadable - every file would carry the eye; one banner says it instead
    if (v.policyUnreadable) {
      leftEl.append(h('div', { class: 'fbanner b-hidden', title: v.policyError }, h('span', { class: 'fi fi-hidden' }),
        h('span', { class: 'grow' }, 'The isolation policy cannot be read - which files are hidden from Claude is unknown, so the eye marks are off and files are not moved until it can.')));
    }
    const box = h('div', { class: 'tree' });
    const q = v.filter.trim();
    if (q) {
      const pool = v.scope === 'changed' ? [...ch] : v.scope === 'turn' ? [...(turn ?? [])] : v.scope === 'open' ? v.tabs.map((t) => t.path) : v.scope === 'scratch' ? scratches : (v.index?.files ?? null);
      if (!pool) {
        box.append(h('p', { class: 'mut small' }, 'loading the file index…'));
        void index().then(() => drawLeft()).catch(err);
      } else {
        const r = rankFiles(q, pool, { changed: ch, open: new Set(v.tabs.map((t) => t.path)), recent: v.recent }, 200);
        if (!r.length) {
          box.append(h('p', { class: 'mut small' }, 'nothing matches'));
          // the index leaves the tooling folders out (K1): a file seen in the tree must still be findable
          if (v.scope === 'all' && !v.index?.all) {
            box.append(h('button', { class: 'link small', onclick: () => { void index(true).then(() => drawLeft()).catch(err); } },
              'Search the excluded folders too (node_modules, target, .venv, .idea…)'));
          }
        }
        box.append(...flat(r.map((x) => x.path), new Map(r.map((x) => [x.path, x.spans]))));
      }
    } else if (v.scope === 'all') {
      if (v.edit && v.edit.kind !== 'rename' && v.edit.dir === '.') box.append(editRow(0));
      box.append(...tree('.', 0, ch));
    } else if (v.scope === 'scratch') {
      if (!scratchList) box.append(h('p', { class: 'mut small' }, 'loading the scratch files…'));
      else if (!scratches.length) {
        box.append(h('p', { class: 'mut small' }, `No scratch files yet - they live in ${SCRATCH_DIR} of this checkout and are never committed.`),
          h('button', { class: 'mini scratch-empty', onclick: (e: Event) => scratchMenu(e.currentTarget as HTMLElement) }, 'New scratch file…'));
      }
      box.append(...flat(scratches));
    } else {
      const paths = v.scope === 'changed' ? [...ch] : v.scope === 'turn' ? [...(turn ?? [])] : v.tabs.map((t) => t.path);
      if (v.scope === 'turn' && !turn) box.append(h('p', { class: 'mut small' }, picked ? 'loading the turn\'s files…' : 'no turn of this project\'s session is known yet'));
      else if (!paths.length) box.append(h('p', { class: 'mut small' }, v.scope === 'open' ? 'no open file' : v.scope === 'turn' ? 'the turn changed nothing' : 'nothing changed'));
      box.append(...flat(paths, null, v.scope === 'changed'));
    }
    leftEl.append(box);   // F06: no key strip under the tree - the keys are in the empty editor and ⌘K
    box.scrollTop = tscroll;
    if (filterFocus) {
      fi.focus();
      if (caret && caret[0] !== null) fi.setSelectionRange(caret[0], caret[1]);
    } else if (navFocus && !editing) {
      const b = [...leftEl.querySelectorAll<HTMLElement>('[data-nav]')].find((x) => x.dataset.nav === navFocus);
      b?.focus({ preventScroll: true });
    }
    persist();
  }

  // tree keyboard: ↑↓ move, → expand, ← collapse / parent, Enter open; typing filters; ⌥N new, ⇧F6 rename,
  // F6 move, ⌫ delete, ⌥⏎ insert the path into the prompt, ⌥⇧C copy the path (focus in the tree only)
  leftEl.addEventListener('keydown', (e) => {
    const target = e.target as HTMLElement;
    if (target instanceof HTMLInputElement) return;
    const b = target.closest?.('[data-nav]') as HTMLElement | null;
    const path = b?.dataset.nav ?? '';
    const isDir = b?.dataset.dir === '1';
    if (path && e.altKey && !e.metaKey && e.code === 'KeyN') { e.preventDefault(); return startCreate(isDir ? path : parentOf(path), e.shiftKey ? 'dir' : 'file', path); }
    if (path && e.key === 'F6' && e.shiftKey) { e.preventDefault(); return startRename(path); }
    if (path && e.key === 'F6' && !e.shiftKey) { e.preventDefault(); return moveTo(path); }
    if (path && (e.key === 'Backspace' || e.key === 'Delete') && !e.altKey && !e.ctrlKey) { e.preventDefault(); return void del(path, isDir); }
    if (path && e.altKey && e.key === 'Enter') { e.preventDefault(); insertPath(path); return; }
    if (path && e.altKey && e.shiftKey && e.code === 'KeyC') { e.preventDefault(); void navigator.clipboard?.writeText(path).then(() => deps.toast('Copied')); return; }
    if (!b || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key.length === 1 && /\S/.test(e.key)) {
      // speed search: typing goes to the filter
      const fi = leftEl.querySelector<HTMLInputElement>('.tfilter');
      if (fi) { view().filter += e.key; fi.value = view().filter; fi.focus(); drawLeft(); e.preventDefault(); }
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Enter', 'Home', 'End', 'PageUp', 'PageDown'].includes(e.key)) return;
    const rows = [...leftEl.querySelectorAll<HTMLElement>('.tree [data-nav]')];
    const i = rows.indexOf(b);
    const r = treeNav(rows.map((x) => ({ depth: Number(x.dataset.depth ?? 0), dir: x.dataset.dir === '1', open: x.dataset.open === '1' })), i, e.key);
    e.preventDefault();
    const tgt = rows[r.i];
    if (!tgt) return;
    if (r.act !== 'none') tgt.click();
    else {
      tgt.focus();
      tgt.scrollIntoView({ block: 'nearest' });
    }
  });

  /** a tree path into the prompt (S5: a hidden file is inserted, with a warning) */
  function insertPath(path: string): void {
    if (!deps.insert(path + ' ')) deps.toast('No live session of this project');
    else warnHidden(path, `Inserted ${path}`);
  }

  // ---------------------------------------------------------------- file operations
  const parentOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '.');
  // file operations run also while the project's session works (2026-10-08): piperd refuses only one that
  // touches a file Claude edited in this turn, or that would show it a file hidden from it

  function startCreate(dir: string, kind: 'file' | 'dir', at?: string): void {
    const v = view();
    v.scope = 'all';
    v.filter = '';
    if (dir !== '.') reveal(dir, true);
    v.edit = { kind, dir, at, value: '' };
    drawLeft();
  }

  function startRename(path: string): void {
    const v = view();
    v.scope = 'all';
    v.filter = '';
    reveal(path, false);
    v.edit = { kind: 'rename', dir: parentOf(path), from: path, value: path.slice(path.lastIndexOf('/') + 1) };
    drawLeft();
  }

  async function commitEdit(): Promise<void> {
    const v = view();
    const ed = v.edit;
    if (!ed) return;
    const name = ed.value.trim().replace(/^\/+/, '');
    if (!name) { v.edit = null; drawLeft(); return focusRow(ed.from ?? ed.at ?? ed.dir); }
    const key = project;
    // N10-B-2: Enter left the keyboard on <body> (only Esc put it back in the tree): the renamed / created row,
    // else the row the edit started from
    let target: string | null = null;
    let typing = false;   // a new file opens in the editor: the caret is there, not in the tree
    try {
      if (ed.kind === 'rename') {
        const to = name.includes('/') && !name.startsWith('./') ? name : (ed.dir === '.' ? '' : ed.dir + '/') + name.replace(/^\.\//, '');
        if (to !== ed.from) await renamePath(ed.from!, to);
        target = to;
      } else {
        const path = (ed.dir === '.' ? '' : ed.dir + '/') + name;
        // a/b/c.txt: the folders first
        const parts = path.split('/');
        for (let i = ed.dir === '.' ? 1 : ed.dir.split('/').length + 1; i < parts.length + (ed.kind === 'dir' ? 1 : 0); i++) {
          const d = parts.slice(0, i).join('/');
          if (i === parts.length && ed.kind === 'dir') await api.fsCreate(key, d, 'dir');
          else if (i < parts.length) await api.fsCreate(key, d, 'dir').catch((e: unknown) => { if (!(e instanceof ApiError && e.status === 409)) throw e; });
        }
        if (ed.kind === 'file') await api.fsCreate(key, path, 'file', '');
        await refreshDir(parentOf(path));
        if (ed.kind === 'file') { typing = true; void open(path, 1); }   // G-N11-9: line 1 = the caret in the editor (it fell to <body>)
        target = path;
        deps.toast(`Created ${path}`);
      }
    } catch (e) {
      deps.toast(`Not done: ${msg(e)}`);
    }
    deps.gitChanged?.(key);
    if (key !== project) return;
    v.edit = null;
    v.index = null;
    drawLeft();
    if (!typing) focusRow(target, target ? parentOf(target) : null, ed.from ?? ed.at ?? ed.dir);
  }

  async function renamePath(from: string, to: string): Promise<void> {
    const v = view();
    const dirty = v.tabs.find((x) => (x.path === from || x.path.startsWith(from + '/')) && x.dirty);
    if (dirty) throw new Error(`${dirty.path} has unsaved changes - save or discard them first`);
    await api.fsRename(project, from, to);
    for (const x of v.tabs) {
      if (x.path === from || x.path.startsWith(from + '/')) {
        const np = to + x.path.slice(from.length);
        if (v.active === x.path) v.active = np;
        x.path = np;
        dropEditor(x);
        // the new name may be hidden from Claude, or no longer (A5): the next read says
        if (x.file) void reread(x, false);
      }
    }
    v.recent = v.recent.map((p) => (p === from ? to : p));
    await refreshDir(parentOf(from));
    if (parentOf(to) !== parentOf(from)) await refreshDir(parentOf(to));
    v.index = null;
    deps.toast(`Renamed to ${to}`);
    drawRight();
  }

  function moveTo(path: string): void {
    const v = view();
    void index().then((files) => {
      // every real folder: the ancestors of indexed files and the folders listed in the tree (empty ones too, C4)
      const listedDirs = [...v.dirs.entries()].flatMap(([d, l]) => l.entries.filter((e) => e.type === 'dir' && !e.link).map((e) => (d === '.' ? e.name : `${d}/${e.name}`)));
      const dirs = dirsOf(files, [...v.dirs.keys(), ...listedDirs]).filter((d) => d !== parentOf(path) && d !== path && !d.startsWith(path + '/'));
      const move = (d: string) => {
        const to = `${d === '.' ? '' : d + '/'}${path.split('/').pop()}`;
        void renamePath(path, to).then(() => { drawLeft(); focusRow(to, d, parentOf(path)); }).catch((e: unknown) => deps.toast(`Not moved: ${msg(e)}`));
      };
      picker(`Move ${path.split('/').pop()} to…`, 'target folder…', (q) => {
        const r = q.trim() ? rankFiles(q, dirs, {}, 60).map((x) => x.path) : dirs.slice(0, 60);
        return r.map((d) => ({ name: d === '.' ? '(project root)' : d, sub: '', run: () => move(d) }));
      }, () => 'no such folder - create it first (⌥⇧N in the tree)');
    }).catch(err);
  }

  async function duplicate(path: string): Promise<void> {
    try {
      const f = await readAll(path);
      if (!f) return;
      const dot = path.lastIndexOf('.');
      const base = dot > path.lastIndexOf('/') ? path.slice(0, dot) : path;
      const ext = dot > path.lastIndexOf('/') ? path.slice(dot) : '';
      let to = `${base} copy${ext}`;
      for (let i = 2; view().index?.files.includes(to) && i < 50; i++) to = `${base} copy ${i}${ext}`;
      await api.fsCreate(project, to, 'file', fileContent(textOf(f), f.eol ?? '\n', f.final_newline ?? true));
      await refreshDir(parentOf(to));
      view().index = null;
      startRename(to);
    } catch (e) {
      deps.toast(`Not duplicated: ${msg(e)}`);
    }
  }

  async function del(path: string, isDir: boolean, sure = false): Promise<void> {
    const v = view();
    const open_ = v.tabs.filter((x) => x.path === path || x.path.startsWith(path + '/'));
    if (open_.some((x) => x.dirty)) return deps.toast('An open file there has unsaved changes - save or discard them first');
    if (!sure) return ask(`Delete ${isDir ? 'the folder ' : ''}${path}?`, 'It goes to Piper\'s trash (Undo in the next message).', 'Delete', () => void del(path, isDir, true));
    const key = project;
    const rows = [...leftEl.querySelectorAll<HTMLElement>('.tree [data-nav]')].map((x) => x.dataset.nav ?? '');
    try {
      const r = (await api.fsDelete(key, path)).data;
      for (const x of open_) forget(x);
      if (open_.some((x) => x.path === v.active)) v.active = v.tabs[0]?.path ?? '';
      await refreshDir(parentOf(path));
      v.index = null;
      deps.gitChanged?.(key);
      drawRight();
      // N10-B-2: the keyboard was on the deleted row - the row in its place (it fell to <body>)
      if (key === project) focusRow(rowAfter(rows, path), parentOf(path));
      const trash = r?.trash ?? '';
      undoToast(`Deleted ${path} → Piper's trash`, trash ? async () => {
        await api.fsRestore(key, trash);
        await refreshDir(parentOf(path));
        deps.gitChanged?.(key);
        deps.toast(`Restored ${path}`);
      } : null);
    } catch (e) {
      deps.toast(`Not deleted: ${msg(e)}`);
    }
  }

  function undoToast(text: string, undo: (() => Promise<void>) | null): void {
    const box = h('div', { class: 'ftoast' }, h('span', { class: 'ok' }, '✓ '), text);
    if (undo) box.append(h('button', { class: 'mini', onclick: () => { box.remove(); undo().catch((e: unknown) => deps.toast(`Not restored: ${msg(e)}`)); } }, 'Undo'));
    document.body.append(box);
    deps.notice?.(text);
    // 30 s to change your mind, and never while the pointer is on it (9 s was gone before you found Undo)
    let left = undo ? 30000 : 9000;
    const tick = window.setInterval(() => {
      if (!box.isConnected) return window.clearInterval(tick);
      if (box.matches(':hover')) return;
      left -= 500;
      if (left <= 0) { box.remove(); window.clearInterval(tick); }
    }, 500);
  }

  // ---------------------------------------------------------------- scratch files (IDEA Scratches)
  let scratchLoading = '';
  /** the development/scratch listing; a missing folder is no scratch file, not an error */
  async function loadScratch(): Promise<void> {
    const v = view();
    const key = project;
    if (!key || scratchLoading === key) return;
    scratchLoading = key;
    const dirs = v.dirs;
    try {
      const r = await api.list(key, SCRATCH_DIR);
      if (v.dirs === dirs) listed(v, SCRATCH_DIR, r.data ?? ({ entries: [] } as unknown as DirListing));
    } catch {
      if (v.dirs === dirs) v.dirs.set(SCRATCH_DIR, { entries: [] } as unknown as DirListing);
    } finally {
      scratchLoading = '';
    }
    if (project === key) drawLeft();
  }

  const SCRATCH_ICONS: Record<string, string> = { sql: 'fi-db', http: 'fi-http', json: 'fi-conf', yaml: 'fi-conf', xml: 'fi-code',
    md: 'fi-doc', java: 'fi-code', sh: 'fi-term', properties: 'fi-conf', txt: 'fi-file' };

  /** Files > New scratch file: the type menu */
  function scratchMenu(at: HTMLElement | 'center'): void {
    if (!project) return;
    openMenu(at === 'center' ? { x: 0, y: 0 } : at, [{ group: 'NEW SCRATCH FILE' }, ...SCRATCH_TYPES.map(([label, ext]): MenuEntry => ({
      label, keys: '.' + ext, icon: SCRATCH_ICONS[ext], run: () => void createScratch(ext),
    }))], { label: 'new scratch file', center: at === 'center', foot: `In ${SCRATCH_DIR} of this checkout · never committed · the agent can read it` });
  }

  async function createScratch(ext: string): Promise<void> {
    const key = project;
    try {
      const r = await api.fsScratch(key, ext);
      if (project !== key || !r.data) return;
      const v = view();
      v.scope = 'scratch'; v.filter = '';
      await refreshDir(SCRATCH_DIR);
      await open(r.data.path, 1);
      deps.toast(`Created ${r.data.path.split('/').pop()}`);
      deps.gitChanged?.(key);
    } catch (e) {
      err(e);
    }
  }

  async function refreshDir(dir: string): Promise<void> {
    const v = view();
    try {
      const r = await api.list(project, dir);
      if (r.data) listed(v, dir, r.data);
      // a compacted chain may have changed shape: reload the parent too
      if (dir !== '.') {
        const p = await api.list(project, parentOf(dir));
        if (p.data) listed(v, parentOf(dir), p.data);
      }
    } catch { /* the next expand loads it */ }
    drawLeft();
  }

  function treeMenu(e: MouseEvent, path: string, isDir: boolean): void {
    e.preventDefault();
    const dir = isDir ? path : parentOf(path);
    const abs = `${info()?.path ?? ''}/${path}`;
    menu({ x: e.clientX, y: e.clientY }, [
      [isDir ? `${path.split('/').pop()}/` : path.split('/').pop() ?? path],
      ['New file', '⌥N', () => startCreate(dir, 'file')],
      ['New folder', '⌥⇧N', () => startCreate(dir, 'dir')],
      null,
      ['Rename', '⇧F6', () => startRename(path)],
      !isDir ? ['Duplicate', '', () => void duplicate(path)] : undefined,
      ['Move…', 'F6', () => moveTo(path)],
      ['Delete', '⌫', () => void del(path, isDir), 'danger'],
      null,
      [`Insert @${isDir ? 'folder' : 'file'} into the prompt`, '⌥⏎', () => insertPath(path)],
      isDir ? ['Search in this folder', kbd('⌘⇧F'), () => openSearch('', `${path}/**`)] : undefined,
      ['Copy path', '⌥⇧C', () => void navigator.clipboard?.writeText(path).then(() => deps.toast('Copied'))],
      ['Open in IntelliJ', '', () => { window.location.href = `idea://open?file=${encodeURIComponent(abs)}`; }],
    ], 'file operations wait while the session works · audit gui-*');
  }

  // ---------------------------------------------------------------- right: tabs, header, body
  function drawTabs(): void {
    const v = view();
    clear(tabsEl);
    tabsEl.append(h('button', {
      class: 'ftree' + (treeOn() ? ' on' : ''), title: treeOn() ? 'hide tree' : 'show tree', 'aria-label': 'tree',
      onclick: () => setTree(!treeOn()),
    }, fi('tree', 'gi')));
    const labels = tabLabels(v.tabs.map((t) => t.path));
    v.tabs.forEach((t, i) => {
      const m = markOf(t.path);
      const tab = h('div', {
        class: 'ftab' + (t.path === v.active ? ' on' : '') + (t.dirty ? ' dirty' : '') + (t.claude.length ? ' claude' : ''),
        title: showBidi(t.path) + (t.dirty ? ' (unsaved)' : ''), role: 'tab', 'aria-selected': t.path === v.active ? 'true' : 'false',
      },
      h('button', { class: 'fname', onclick: () => activate(t.path) }, icon(t.path.split('/').pop() ?? '', false), h('span', { class: 'tl' }, showBidi(labels[i]!)),
        t.hidden && !v.policyUnreadable ? hiddenMark() : null, m ? h('span', { class: 'gdot gm-' + m.cls, title: m.word }) : null),
      h('button', { class: 'fclose', title: 'close (⌥W, middle button)', onclick: (e: Event) => { e.stopPropagation(); closeTab(t.path); } }, t.dirty ? '●' : h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })));
      tab.addEventListener('auxclick', (e) => { if ((e as MouseEvent).button === 1) { e.preventDefault(); closeTab(t.path); } });
      tab.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); });
      tabsEl.append(tab);
    });
    tabsEl.querySelector('.ftab.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  function crumbs(t: Tab): HTMLElement {
    const box = h('div', { class: 'crumbs' });
    if (isScratch(t.path)) {
      box.append(h('button', { class: 'link', title: 'all scratch files', onclick: () => { const v = view(); v.scope = 'scratch'; v.filter = ''; if (!treeOn()) setTree(true); drawLeft(); } }, 'Scratches'),
        h('span', { class: 'sep' }, '›'), h('strong', {}, showBidi(t.path.slice(SCRATCH_DIR.length + 1))));
      return box;
    }
    const parts = t.path.split('/');
    // long paths: the first part, …, the last two directories; beside the terminal only the file name
    const shown = narrow ? [parts.length - 1] : parts.length > 5 ? [0, -1, parts.length - 3, parts.length - 2, parts.length - 1] : parts.map((_, i) => i);
    shown.forEach((i, k) => {
      if (k) box.append(h('span', { class: 'sep' }, '›'));
      if (i < 0) { box.append(h('span', { class: 'mut' }, '…')); return; }
      const p = parts[i]!;
      if (i === parts.length - 1) { box.append(h('strong', {}, showBidi(p))); return; }
      box.append(h('button', {
        class: 'link', title: 'show in tree',
        onclick: () => { const v = view(); v.scope = 'all'; v.filter = ''; if (!treeOn()) setTree(true); reveal(parts.slice(0, i + 1).join('/'), true); drawLeft(); },
      }, showBidi(p)));
    });
    // the symbol scope of the caret (class › method)
    if (t.caret && t.mode === 'code') {
      for (const o of scopeAt(outlineOf(t), t.caret.line).slice(narrow ? -1 : -2)) {
        box.append(h('span', { class: 'sep' }, '›'), h('button', { class: 'link sym sym-' + (TYPE_KINDS.has(o.kind) ? 'type' : 'fn'),
          title: `${o.kind} · line ${o.line}`, onclick: () => gotoLineNum(o.line) }, TYPE_KINDS.has(o.kind) ? o.name : `${o.name}()`));
      }
    }
    return box;
  }

  const isChanged = (t: Tab) => !!t.marks && (t.marks.untracked || t.marks.added.length + t.marks.changed.length + t.marks.deleted.length > 0)
    || changedSet().has(t.path);

  function selOf(t: Tab): [number, number] | null {
    return t.caret ? t.caret.sel : null;
  }

  function insertRef(): void {
    const t = cur();
    if (!t) return;
    if (!deps.insert(reference(t.path, selOf(t)) + ' ')) deps.toast('No live session of this project');
    else if (!warnHidden(t.path, `Inserted ${reference(t.path, selOf(t))}`)) deps.toast(`Inserted ${reference(t.path, selOf(t))}`);
  }

  function drawHead(): void {
    const t = cur();
    clear(headEl);
    headEl.classList.toggle('hidden', !t);
    if (!t) return;
    const v = view();
    const f = t.file;
    const abs = `${info()?.path ?? ''}/${t.path}`;
    const ib = (glyph: string | Node, title: string, run: () => void, cls = '', disabled = false) =>
      h('button', { class: 'ib ' + cls, title, 'aria-label': title, disabled, onclick: run }, glyph);
    const acts = h('div', { class: 'actions' },
      isScratch(t.path) && !narrow ? h('span', { class: 'scratch-chip', title: `${SCRATCH_DIR} is kept out of git (info/exclude)` }, 'SCRATCH · NOT IN GIT') : null,
      f && langOf(t.path) === 'md' ? markdownModeControl(t.mode, (mode) => setMode(t, mode)) : null,
      deps.marks?.(project).get(t.path)?.letter === 'U' ? h('button', { class: 'mini', title: 'Add the current file to Git', onclick: () => deps.stageFile?.(project, t.path) }, '+ Git') : null,
      f && !t.hidden ? h('button', { class: 'mini', title: kbd('Actions for the selected code · ⌘⌥A', 'Ctrl'), onclick: () => reviewSelection(t) }, '+ Actions') : null,
      v.back.length ? ib(tbIcon('back'), kbd('back ⌘[', 'Ctrl'), back) : null,
      v.fwd.length ? ib(tbIcon('fwd'), kbd('forward ⌘]', 'Ctrl'), forward) : null,
      ib(tbIcon('at'), `insert ${showBidi(reference(t.path, selOf(t)))} into the prompt (⌥⏎) - the reference, never the content`, insertRef, 'at'),
      isChanged(t) || t.mode === 'diff' ? ib(tbIcon('diff'), kbd('diff against HEAD (⌘⇧D)', 'Ctrl'), () => toggleDiff(), t.mode === 'diff' ? 'on' : '') : null,
      f ? ib(tbIcon('blame'), blameOn ? 'inline blame: on (click hides)' : 'inline blame: off', () => { blameOn = !blameOn; store.set('piper.files.blame', blameOn); t.ed?.refreshBlame(); drawHead(); if (blameOn) void loadBlame(t); }, (blameOn ? 'on' : '') + ' wide') : null,
      deps.history && f ? ib(tbIcon('history'), 'history and blame of the file', () => deps.history?.(project, t.path), 'wide') : null,
      f ? ib(tbIcon('outline'), kbd('outline (⌘⇧O)'), showOutline, 'wide') : null,
      ib('IJ', 'open in IntelliJ', () => { window.location.href = `idea://open?file=${encodeURIComponent(abs)}&line=${t.caret?.line ?? 1}`; }, 'ij wide'),
      ib(tbIcon('more'), 'more', () => {
        menu(headEl.querySelector<HTMLElement>('.ib.more')!, [
          ['Copy reference', '', () => void navigator.clipboard?.writeText(reference(t.path, selOf(t))).then(() => deps.toast('Copied'))],
          ['Copy path', '', () => void navigator.clipboard?.writeText(t.path).then(() => deps.toast('Copied'))],
          f ? ['Find usages', '⌥F7', () => findUsages()] : undefined,
          f ? ['Note on the line for the review', '', () => { if (t.caret) noteAt(t, t.caret.line); }] : undefined,
          f ? ['Fold all', kbd('⌘⌥[', 'Ctrl'), () => t.ed?.foldAll()] : undefined,
          f ? ['Unfold all', kbd('⌘⌥]', 'Ctrl'), () => t.ed?.unfoldAll()] : undefined,
          f ? ['Reload from disk', '', () => void reread(t, true)] : undefined,
          idea.ok && f ? ['IntelliJ: info on the symbol', '', () => void symbolInfo()] : undefined,
          t.problems ? [`Problems (${t.problems.length})`, '', () => openPanel('problems')] : undefined,
        ]);
      }, 'more'));
    headEl.append(crumbs(t), acts);
  }

  function reviewSelection(t: Tab): void {
    const file = t.mode === 'diff' ? diffFile : t.file;
    if (!file?.sha) return;
    if (t.hidden) return void deps.toast('Not for a session: ' + HIDDEN_TITLE);
    if (t.dirty || t.ed?.dirty()) return void deps.toast('Save or discard your edits before collecting a versioned review target.');
    const caret = (t.mode === 'diff' ? diffEd : t.ed)?.caret() ?? t.caret;
    const [from, to] = caret?.sel ?? [1, file.total_lines];
    const text = (t.mode === 'diff' ? diffEd?.text() : t.ed?.text()) ?? t.text;
    deps.reviewTarget?.(project, { path: t.path, from, to, old: false, code: text.split('\n').slice(from - 1, to).join('\n'), sha: file.sha });
  }

  function setMode(t: Tab, m: Mode): void {
    if (t.ed) t.scroll = t.ed.scrollTop();
    if (m === 'preview') t.md = null;   // the editor may have changed since the previous render
    t.mode = m;
    drawRight();
  }

  function toggleDiff(): void {
    const t = cur();
    if (t) setMode(t, t.mode === 'diff' ? 'code' : 'diff');
  }

  function drawBanners(): void {
    const t = cur();
    clear(bannerEl);
    if (!t?.file) return;
    if (t.banner) {
      const b = t.banner;
      const box = h('div', { class: 'fbanner b-' + b.kind }, h('span', { class: 'grow' }, b.text));
      if (b.kind === 'claude' && t.remote) {
        box.append(h('button', { class: 'mini', onclick: () => { void takeRemote(t); } }, 'Discard mine, load theirs'),
          h('button', { class: 'mini', onclick: () => { t.remote = null; t.banner = { kind: 'claude', text: kbd('Keeping your text - ⌘S will ask before it overwrites the session\'s change', 'Ctrl') }; drawBanners(); } }, 'Keep mine'));
      } else if (b.kind === 'claude') {
        if (isChanged(t)) box.append(h('button', { class: 'mini', onclick: () => setMode(t, 'diff') }, kbd('Diff ⌘⇧D', 'Ctrl')));
        box.append(h('button', { class: 'mini', onclick: () => { t.banner = null; t.claude = []; t.ed?.setClaude([], false); drawBanners(); drawTabs(); } }, 'Seen'));
      } else if (b.kind === 'conflict') {
        box.append(h('button', { class: 'mini', onclick: () => void reread(t, true) }, 'Reload (drop mine)'),
          h('button', { class: 'mini danger', onclick: () => void save(true) }, 'Overwrite'));
      } else box.append(h('button', { class: 'mini icon', 'aria-label': 'dismiss', title: 'dismiss', onclick: () => { t.banner = null; drawBanners(); } }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })));
      bannerEl.append(box);
    }
    if (t.hidden && !view().policyUnreadable) {
      bannerEl.append(h('div', { class: 'fbanner b-hidden', title: HIDDEN_TITLE }, h('span', { class: 'fi fi-hidden' }),
        h('span', { class: 'grow' }, 'Excluded from AI by policy - sandbox coverage may vary. Manual Files operations remain available.')));
    }
    const why = roWhy(t);
    if (why && t.mode === 'code') {
      bannerEl.append(h('div', { class: 'fbanner b-ro' }, h('span', { class: 'lock' }, '🔒'), h('span', { class: 'grow' }, why),
        t.dirty ? h('button', { class: 'mini', onclick: () => void reread(t, true) }, 'Discard mine') : null));
    } else if (deps.working(project) && !t.hidden && t.mode === 'code') {
      bannerEl.append(h('div', { class: 'fbanner b-work' }, h('span', { class: 'grow' },
        kbd('Claude is working in this project - you can still edit; if it changes this file first, ⌘S asks before overwriting.', 'Ctrl'))));
    }
  }

  function drawStatus(): void {
    const t = cur();
    clear(statusEl);
    statusEl.classList.toggle('hidden', !t);
    if (!t) return;
    const c = t.caret;
    const f = t.file;
    const bits: (HTMLElement | string | null)[] = [];
    if (t.claude.length) bits.push(h('span', { class: 'legend' }, h('i', { class: 'lg-claude' }), 'Claude, last edit'));
    bits.push(h('span', { class: 'grow' }));
    if (c && f) {
      bits.push(h('span', {}, c.cursors > 1 ? `${c.cursors} cursors · ${kbd('⌘D', 'Ctrl')} next · Esc one` : `Ln ${c.line}, Col ${c.col}`));
      if (c.sel[1] > c.sel[0]) bits.push(h('span', {}, `${c.sel[1] - c.sel[0] + 1} lines`));
    }
    if (f) {
      bits.push(h('span', {}, f.eol === '\r\n' ? 'CRLF' : 'LF'), h('span', {}, langOf(t.path)));
    }
    const br = deps.branch?.(project) ?? '';
    if (br) bits.push(h('span', { class: 'br', title: 'branch' }, fi('branch', 'gi'), showBidi(br)));
    if (idea.ok) bits.push(h('span', { class: 'idea', title: 'IntelliJ is running: Problems and symbol info come from it (usages are a whole-word text search)' }, h('i', {}), 'IDEA'));
    if (t.mode === 'diff') {
      bits.push(h('span', { class: 'wstate ro' }, '🔒 read-only review'));
    } else if (f) {
      const why = roWhy(t);
      bits.push(h('span', { class: 'wstate ' + (t.dirty ? 'dirty' : why ? 'ro' : 'rw'), title: why || kbd(`edits save themselves ${AUTOSAVE_MS / 1000} s after the last keystroke, or now with ⌘S (sha lock, audit gui-write); not while a banner asks`, 'Ctrl') },
        t.dirty ? (why ? '● unsaved · read-only now' : kbd(autoOk(t) ? `● unsaved · saves in ${AUTOSAVE_MS / 1000} s · ⌘S now` : '● unsaved · ⌘S save', 'Ctrl')) : why ? '🔒 read-only' : '✎ editable · saved automatically'));
    } else if (t.image) bits.push(h('span', {}, 'image'));
    statusEl.append(...bits.filter((x): x is HTMLElement | string => x !== null));
  }

  /** F06: the empty editor is a hub - Go to file / Search, the files changed in this session, the recent ones */
  function emptyEl(): HTMLElement {
    const v = view();
    const hub = emptyHub([...changedSet()], v.recent);
    const row = (p: string, right: HTMLElement | null, cls = '') => {
      const name = p.slice(p.lastIndexOf('/') + 1);
      const dir = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
      return h('button', { class: 'fe-row' + cls, title: showBidi(p), onclick: () => void open(p) },
        h('span', { class: 'nm' }, showBidi(name)), h('span', { class: 'dir' }, showBidi(leftCut(dir, 40))), right);
    };
    const sec = (title: string, rows: HTMLElement[]) => h('section', { class: 'fe-sec' }, h('div', { class: 'sec-h' }, title), ...rows);
    const changed = hub.changed.map((p) => {
      const m = markOf(p);
      return row(p, h('span', { class: 'gm', title: m?.word ?? 'changed' }, m?.letter ?? 'M'));
    });
    if (hub.more) changed.push(h('button', { class: 'fe-more link small', onclick: () => { v.scope = 'changed'; v.filter = ''; if (!treeOn()) setTree(true); drawLeft(); } }, `+ ${hub.more} more in Changed`));
    const recent = hub.recent.map((p) => { const at = v.recentAt.get(p); return row(p, at ? h('span', { class: 'when' }, ago(at)) : null); });
    return h('div', { class: 'fempty fe-hub' },
      h('div', { class: 'fe-in' },
        h('h3', { class: 'fe-title' }, showBidi(rawProjectKey(project))),
        h('div', { class: 'fe-acts' },
          h('button', { onclick: () => quickOpen() }, 'Go to file', h('kbd', {}, kbd('⌘P'))),
          h('button', { onclick: () => openSearch() }, 'Search', h('kbd', {}, kbd('⌘⇧F')))),
        changed.length ? sec('Changed in this session', changed) : null,
        recent.length ? sec('Recently opened', recent) : null,
        !changed.length && !recent.length ? h('p', { class: 'mut small' }, 'Pick a file in the tree') : null));
  }

  function drawRight(line = 0): void {
    const t = cur();
    closeMenu();
    drawTabs();
    applyTree();
    drawHead();
    drawBanners();
    drawStatus();
    drawPanel();
    for (const x of view().tabs) if (x.ed && x !== t) x.scroll = x.ed.scrollTop();
    diffEd?.destroy();
    diffEd = null;
    diffFile = null;
    diffStep = null;
    clear(bodyEl);
    httpCur = null;
    if (!t) {
      bodyEl.append(project ? emptyEl() : h('div', { class: 'fempty' }, h('p', {}, 'Pick a project')));
      return;
    }
    if (t.mode === 'diff') return void bodyEl.append(diffBox(t));
    if (t.image) {
      const img = h('img', { src: t.image, alt: t.path });
      const cap = h('div', { class: 'mut small' });
      img.addEventListener('load', () => { cap.textContent = markBidi(`${img.naturalWidth} × ${img.naturalHeight} px`); });
      bodyEl.append(h('div', { class: 'fimg' }, img, cap));
      return;
    }
    const f = t.file;
    if (!f) return;
    if (t.mode === 'preview') {
      if (!t.md) {
        const blocks = parseMarkdown((t.ed ? t.ed.text() : t.text).split('\n'));
        t.md = h('div', { class: 'fmd' }, renderMarkdown(blocks, {
          openRel: (href) => {
            const p = resolveRel(t.path, href);
            if (p) jump(p, 0);
            else deps.toast(`Link outside the project: ${href}`);
          },
        }));
      }
      bodyEl.append(t.md);
      return;
    }
    if (isHttpFile(t.path)) {
      const st = t.http ?? (t.http = newHttpTab());
      const tab = t;
      httpCur = httpView({ project, path: t.path, toast: deps.toast, modal: deps.modal, closeModal: deps.closeModal,
        openFile: (p, line) => void open(p, line ?? 0),
        linesChanged: () => httpLines(tab),
        changed: () => { if (cur() === tab) httpLines(tab); } }, st);
      bodyEl.append(httpCur.bar);
    }
    const host = h('div', { class: 'fcode' });
    bodyEl.append(host);
    if (httpCur) {
      bodyEl.append(httpCur.res);
      httpCur.draw();
      void httpCur.load();
    }
    void mountEditor(t, host, line);
  }

  async function mountEditor(t: Tab, host: HTMLElement, line: number): Promise<void> {
    if (!t.ed) {
      if (t.edLoading) return;
      t.edLoading = true;
      try {
        const key = project;
        const ed = await createEditor({
          path: t.path, text: t.text, readOnly: !writable(t),
          onDirty: (d) => { t.dirty = d; drawTabs(); drawStatus(); if (deps.working(key)) drawBanners(); },
          onCaret: (c) => {
            const prevLine = t.caret?.line;
            t.caret = c;
            if (cur() === t) { scheduleChrome(); if (blameOn && !t.blame && prevLine !== c.line) void loadBlame(t); }
          },
          onEdit: () => schedule(t),
          onSave: () => void save(),
          onDefinition: (w, ln) => { t.caret = t.ed?.caret() ?? t.caret; void gotoDefinition(w, ln); },
          onMenu: (e, c) => { t.caret = c; codeMenu(t, e, c); },
          onNote: (ln) => noteAt(t, ln),
          blame: (ln) => (blameOn && t.blame ? blameText(t.blame[ln - 1]) : null),
          httpRun: (ln) => { const idx = requestAt(t.http?.info ?? null, ln); if (idx >= 0 && cur() === t) void httpCur?.run(idx); },
          extraKeys: [
            { key: 'Mod-l', run: () => { gotoLine(); return true; } },
            { key: 'Mod-i', run: () => true },   // ⌘I is Piper's context panel, not selectParentSyntax
            { key: 'Mod-Alt-ArrowDown', run: () => { togglePanel(); return true; } },   // the tool panel, not addCursorBelow
            { key: 'Mod-[', run: () => { if (!back()) deps.nav?.(-1); return true; } },
            { key: 'Mod-]', run: () => { if (!forward()) deps.nav?.(1); return true; } },
            { key: 'Mod-Shift-d', preventDefault: true, run: () => { toggleDiff(); return true; } },
            { key: 'Alt-Enter', run: () => { insertRef(); return true; } },
            { key: 'Mod-Alt-[', run: () => { t.ed?.foldAll(); return true; } },
            { key: 'Mod-Alt-]', run: () => { t.ed?.unfoldAll(); return true; } },
          ],
        });
        t.ed = ed;
        ed.setMarks(t.marks);
        ed.setProblems(t.problems);
        ed.setNotes(notesOf(t.path).map((n) => n.line));
        if (t.claude.length) ed.setClaude(t.claude, false);
        // a few live editors stay for quick tab switches; the least recently used others are rebuilt on demand
        const live = view().tabs.filter((x) => x.ed && x !== t && !x.dirty).sort((a, b) => a.used - b.used);
        while (live.length >= EDITORS_MAX) dropEditor(live.shift()!);
      } catch (e) {
        host.append(h('p', { class: 'bad' }, `The editor did not load: ${msg(e)}`));
        return;
      } finally {
        t.edLoading = false;
      }
    }
    if (cur() !== t || !host.isConnected) return;
    host.append(t.ed!.dom);
    t.ed!.setReadOnly(!writable(t));
    if (line) { t.ed!.reveal(line); t.ed!.focus(); }
    else if (t.scroll) t.ed!.setScrollTop(t.scroll);
    if (t.http) httpLines(t);
    t.caret = t.ed!.caret();
    drawStatus();
    drawHead();
  }

  let chromeTimer = 0;
  /** caret moves: header crumbs + status bar, at most every 80 ms */
  function scheduleChrome(): void {
    window.clearTimeout(chromeTimer);
    chromeTimer = window.setTimeout(() => { drawHead(); drawStatus(); }, 80);
  }

  function httpLines(t: Tab): void {
    if (!t.ed || !t.http?.info) return;
    const set = new Set<number>();
    const n = t.ed.text().split('\n').length;
    let prev = -1;
    for (let i = 1; i <= n; i++) {
      const idx = requestAt(t.http.info, i);
      if (idx >= 0 && idx !== prev) set.add(i);
      prev = idx;
    }
    t.ed.setHttpLines(set);
  }

  // ---------------------------------------------------------------- context menu (W11: the one menu component)
  type Item = [string] | [string, string, () => void] | [string, string, () => void, string] | null | undefined;

  /** a context menu: [label] = section title, null = separator, [label, keys, run, cls?] */
  function menu(at: HTMLElement | { x: number; y: number }, items: Item[], foot = ''): void {
    openMenu(at, items.map((it): MenuEntry => {
      if (!it) return it;
      if (it.length === 1) return { group: showBidi(it[0]) };
      const [label, keys, run, cls] = it;
      return { label: showBidi(label), keys: keys || undefined, run, danger: cls === 'danger' };
    }), { label: 'editor', back: () => cur()?.ed?.focus(), ...(foot ? { foot } : {}) });
  }

  function codeMenu(t: Tab, e: MouseEvent, c: Caret): void {
    const name = c.word;
    const ref = reference(t.path, c.sel);
    menu({ x: e.clientX, y: e.clientY }, [
      name ? ['NAVIGATE'] : undefined,
      name ? [`Go to definition of ${name}`, kbd('⌘click'), () => void gotoDefinition(name, c.line)] : undefined,
      name ? [`Find usages of ${name}`, '⌥F7', () => findUsages(name)] : undefined,
      ['Outline', kbd('⌘⇧O'), showOutline],
      null,
      ['FOR CLAUDE'],
      [`Insert ${ref.length > 40 ? '…' + ref.slice(-38) : ref}`, '⌥⏎', insertRef],
      [`Note on line ${c.line} for the review`, 'gutter', () => noteAt(t, c.line)],
      null,
      ['GIT'],
      isChanged(t) ? ['Diff against HEAD', kbd('⌘⇧D', 'Ctrl'), toggleDiff] : undefined,
      deps.history ? ['History and blame', '', () => deps.history?.(project, t.path)] : undefined,
      null,
      ['Copy reference', '', () => void navigator.clipboard?.writeText(ref).then(() => deps.toast('Copied'))],
      idea.ok && name ? [`IntelliJ: info on ${name}`, '', () => void symbolInfo()] : undefined,
    ]);
  }

  // ---------------------------------------------------------------- notes on a line (9.7 #3)
  function noteAt(t: Tab, n: number): void {
    const file = t.mode === 'diff' ? diffFile : t.file;
    if (!file?.sha) return;
    if (t.hidden) return void deps.toast('Not for a session: ' + HIDDEN_TITLE);
    if (t.dirty || t.ed?.dirty()) return void deps.toast('Save or discard your edits before collecting a versioned review target.');
    const text = (t.mode === 'diff' ? diffEd?.text() : t.ed?.text()) ?? t.text;
    deps.reviewTarget?.(project, { path: t.path, from: n, to: n, old: false, code: text.split('\n')[n - 1] ?? '', sha: file.sha });
  }

  // ---------------------------------------------------------------- change markers, blame (9.7 #3, #7)
  async function loadMarks(t: Tab): Promise<void> {
    const key = project;
    try {
      t.marks = (await api.marks(key, t.path)).data;
    } catch {
      t.marks = null;   // not a repo, or an older piperd: no markers, no noise
    }
    if (key !== project) return;
    if (!t.dirty) t.ed?.setMarks(t.marks);
    if (t === cur()) drawHead();
  }

  async function loadBlame(t: Tab): Promise<void> {
    if (t.blame || t.blameLoading || !t.file) return;
    t.blameLoading = true;
    try {
      t.blame = (await api.blame(project, t.path)).data?.lines ?? [];
    } catch {
      t.blame = [];   // untracked or not a repo
    }
    t.blameLoading = false;
    t.ed?.refreshBlame();
  }

  let diffEd: Editor | null = null;
  let diffFile: FileText | null = null;
  let diffStep: ((dir: number) => void) | null = null;
  let diffEdge = 0;
  let diffAdvancing = false;

  function diffBox(t: Tab): HTMLElement {
    return mergeBox(t);
  }


  function mergeBox(t: Tab): HTMLElement {
    const box = h('div', { class: 'fdiff' }, h('p', { class: 'mut' }, 'Loading diff…'));
    const key = project;
    const query = new URLSearchParams({ project: key, path: t.path, tree: 'HEAD' });
    Promise.all([call<FileBlob>('GET', '/api/fs/blob?' + query), projectDiff(key, t.path), loadCM()]).then(async ([response, patch, cm]) => {
      if (!box.isConnected || key !== project || t.mode !== 'diff') return;
      const left = response.data;
      if (!left) return;
      clear(box);
      const metadata = diffMetadata(patch, left);
      const header = h('div', { class: 'sbs-head small' }, h('span', { class: 'mut' }, 'HEAD → working tree · read-only'),
        h('button', { class: 'mini', title: 'Previous change, continuing across files', onclick: () => diffStep?.(-1) }, '↑ ⇧F7'),
        h('button', { class: 'mini', title: 'Next change, continuing across files', onclick: () => diffStep?.(1) }, '↓ F7'),
        h('button', { class: 'mini', onclick: () => deps.diff(key, t.path) }, 'Review in the Git panel'));
      box.append(header);
      for (const line of metadata) box.append(h('p', { class: 'mut small' }, line));
      const workingSymlink = /^new (?:file )?mode 120000$/m.test(patch) ||
        (left.mode === '120000' && !/^new mode 100/m.test(patch) && !/^deleted file mode /m.test(patch));
      if (workingSymlink) {
        box.append(h('p', { class: 'mut small' }, 'Working tree: symlink (target text in the patch)'), h('pre', { class: 'mono' }, patch || left.text || ''));
        return;
      }
      if (/^Binary files |^GIT binary patch/m.test(patch)) {
        box.append(h('p', { class: 'mut small' }, 'Binary change; text diff unavailable'));
        return;
      }
      const deleted = /^deleted file mode /m.test(patch);
      if (deleted) box.append(h('p', { class: 'mut small' }, 'Deleted from the working tree'));
      if (left.state === 'binary' || left.state === 'too_large') return;
      let right: FileText | null;
      try {
        right = deleted ? { path: t.path, total_lines: 0, offset: 0, lines: [], final_newline: true }
          : (await api.read(key, t.path)).data;
        while (right && right.lines.length < right.total_lines) {
          const more = (await api.read(key, t.path, right.lines.length)).data;
          if (!more?.lines.length) throw new Error('Incomplete working file');
          if (more.sha !== right.sha) throw new Error('File changed while loading the diff; reopen it');
          right = { ...right, lines: [...right.lines, ...more.lines] };
        }
      } catch (e) { box.append(h('p', { class: 'bad' }, 'Working tree: ' + msg(e))); return; }
      if (!right || !box.isConnected || key !== project || t.mode !== 'diff') return;
      if (right.final_newline === false && right.total_lines) box.append(h('p', { class: 'mut small' }, 'Working tree: no final newline'));
      const original = left.text ?? '';
      const text = fileContent(textOf(right), right.eol, right.final_newline);
      if (original === text && !metadata.length) box.append(h('p', { class: 'mut small' }, 'No changes against HEAD.'));
      const editor = await createEditor({
        path: t.path, text, original, readOnly: true,
        onDirty: () => {}, onCaret: () => {}, onSave: () => {}, onDefinition: () => {}, onMenu: () => {},
        onNote: (line) => noteAt(t, line), blame: () => null,
        extraKeys: [
          { key: 'F7', run: () => { diffStep?.(1); return true; }, preventDefault: true },
          { key: 'Shift-F7', run: () => { diffStep?.(-1); return true; }, preventDefault: true },
          { key: 'Mod-Shift-d', run: () => { toggleDiff(); return true; }, preventDefault: true },
        ],
      });
      if (!box.isConnected || key !== project || t.mode !== 'diff') { editor.destroy(); return; }
      diffEd = editor;
      diffFile = right;
      diffStep = dir => {
        const view = cm.EditorView.findFromDOM(editor.dom);
        if (!view) return;
        const before = view.state.selection.main.head;
        (dir > 0 ? cm.goToNextChunk : cm.goToPreviousChunk)({ state: view.state, dispatch: transaction => {
          const after = transaction.state.selection.main.head;
          if (dir > 0 ? after > before : after < before) view.dispatch(transaction);
        } });
        if (view.state.selection.main.head !== before) { view.focus(); return; }
        if (diffAdvancing) return;
        void advance();
        async function advance(): Promise<void> {
          diffAdvancing = true;
          try {
            const paths = deps.reviewPaths?.(key) ?? [...deps.changed(key)].sort();
            const next = paths[paths.indexOf(t.path) + dir];
            if (dir > 0) await deps.reviewLeave?.(key, t.path, right?.sha ?? null, left?.oid ?? null, left?.mode ?? null);
            if (!next) { deps.toast(dir > 0 ? 'All changed files reviewed. Prepare the task in Review.' : 'First changed file.'); return; }
            diffEdge = dir;
            await open(next);
            const tab = cur(); if (tab?.path === next && key === project && tab.mode !== 'diff') setMode(tab, 'diff');
          } catch (error) { deps.toast(msg(error)); }
          finally { diffAdvancing = false; }
        }
      };
      editor.setMarks(t.marks);
      editor.setNotes(notesOf(t.path).map((x) => x.line));
      box.append(editor.dom);
      editor.focus();
      if (diffEdge) {
        const view = cm.EditorView.findFromDOM(editor.dom), dir = diffEdge; diffEdge = 0;
        if (view) { view.dispatch({ selection: { anchor: dir > 0 ? 0 : view.state.doc.length } });
          (dir > 0 ? cm.goToNextChunk : cm.goToPreviousChunk)(view); view.focus(); }
      }
    }).catch((e: unknown) => { clear(box); box.append(h('p', { class: 'bad' }, msg(e))); });
    return box;
  }

  // ---------------------------------------------------------------- find in file (CodeMirror's panel: Aa, W, .*, replace)
  function find(): void {
    const t = cur();
    if (!t?.file) return deps.toast('Open a text file');
    if (t.mode !== 'code') setMode(t, 'code');
    const sel = t.caret?.selText && !t.caret.selText.includes('\n') ? t.caret.selText : '';
    if (t.ed) t.ed.find(sel || undefined);
    else window.setTimeout(() => t.ed?.find(sel || undefined), 150);
  }

  // ---------------------------------------------------------------- reading again, saving (9.7 #6)
  /** read the file again: a changed text replaces the editor's when it has no unsaved change (force = drop it) */
  async function reread(t: Tab, force: boolean, bySession = false): Promise<void> {
    const key = project;
    let f: FileText | null;
    try {
      f = await readAll(t.path);
    } catch {
      return;   // deleted meanwhile: the tab stays as it was
    }
    if (!f || key !== project) return;
    const next = textOf(f);
    // the policy may have changed, or the tab was renamed (A5): the banner and the eye follow the read
    if (!!f.hidden !== t.hidden) {
      noteHidden(t, t.path, !!f.hidden);
      if (t.ed) t.ed.setReadOnly(!writable(t));
      if (cur() === t) { drawTabs(); drawBanners(); drawStatus(); }
    }
    if (t.file?.sha && f.sha && t.file.sha === f.sha && !force) return;
    if (t.dirty && !force) {
      if (next !== t.text) { t.remote = f; t.banner = { kind: 'claude', text: `${bySession ? 'Claude edited' : 'Changed on disk:'} ${t.path.split('/').pop()} while you have unsaved changes` }; drawBanners(); }
      return;
    }
    const prev = (t.ed ? t.ed.text() : t.text).split('\n');
    t.file = f;
    t.text = next;
    t.remote = null;
    if (force) t.banner = null;
    t.md = null;
    if (t.ed) { t.ed.replace(next); t.ed.setBaseline(next); }
    t.dirty = false;
    if (!force && prev.join('\n') !== next) {
      const lines = changedLines(prev, next.split('\n'));
      t.claude = lines;
      t.ed?.setClaude(lines, true);
      const where = lines.length === 1 ? `line ${lines[0]}` : `lines ${lines[0]}–${lines[lines.length - 1]}`;
      if (lines.length) t.banner = { kind: 'claude', text: bySession ? `Claude edited this file · ${where}` : `Changed on disk · ${where}` };
    }
    t.blame = null;
    if (blameOn && t.ed) void loadBlame(t);
    if (cur() === t) { drawTabs(); drawBanners(); drawStatus(); if (t.mode !== 'code') drawRight(); }
    void loadMarks(t);
    void loadProblems(t);
  }

  async function takeRemote(t: Tab): Promise<void> {
    t.dirty = false;
    await reread(t, true);
  }

  // ---------------------------------------------------------------- autosave (9.41)
  /** whether a quiet tab may save itself: never over a change on disk (`remote`, its banner asks), never after
   *  a failed save (conflict / error banner), never a read-only file or a diff review - ⌘S still does each of
   *  those on purpose. The sha lock stays: a write against a stale hash fails with the conflict banner. */
  const autoOk = (t: Tab): boolean => t.dirty && !!t.ed && writable(t) && t.mode === 'code' && !t.remote
    && t.banner?.kind !== 'conflict' && t.banner?.kind !== 'error';
  function unschedule(t: Tab): void {
    if (t.autoTimer) clearTimeout(t.autoTimer);
    t.autoTimer = 0;
  }
  /** every edit restarts the quiet timer; the save runs only while the tab is still the shown one */
  function schedule(t: Tab): void {
    unschedule(t);
    if (!autoOk(t)) return;
    const key = project;
    t.autoTimer = window.setTimeout(() => {
      t.autoTimer = 0;
      if (key === project && cur() === t && autoOk(t)) void save(false, false, t, true);
    }, AUTOSAVE_MS);
  }
  /** leaving the tab, the project or the Files view: the pending autosave runs now */
  function flush(): void {
    const t = cur();
    if (t?.autoTimer && autoOk(t)) void save(false, false, t, true);
    else if (t) unschedule(t);
  }

  async function save(force = false, sure = false, t = cur(), auto = false): Promise<void> {
    if (t) unschedule(t);
    if (!t?.file || !t.ed) return;
    const text = t.ed.text();
    if (!t.dirty && !force) return;
    const key = project;
    let sha = t.file.sha ?? '';
    if (force || t.remote) {
      if (!sure) return ask(`Overwrite ${t.path} on disk?`, 'Your text replaces the other change, which is lost (git still has the last commit).', 'Overwrite', () => void save(force, true, t));
      try { sha = (await api.read(key, t.path, 0)).data?.sha ?? sha; } catch { /* the write reports it */ }
    }
    let wrote: { sha: string; total_lines: number } | null = null;
    try {
      // lines joined by LF, no final newline: piperd restores the file's own eol + final newline
      wrote = (await api.write(key, t.path, fileContent(text, '\n', false), sha)).data;
    } catch (e) {
      t.banner = e instanceof ApiError && e.status === 409
        ? { kind: 'conflict', text: `Not saved: ${e.message}` }
        : { kind: 'error', text: `Not saved: ${msg(e)}` };
      if (key === project) { drawBanners(); drawStatus(); }   // the status bar no longer promises an autosave
      return;
    }
    if (!auto) deps.toast(`Saved ${t.path}`);   // an autosave says it in the status bar only
    deps.gitChanged?.(key);
    t.banner = null;
    t.remote = null;
    // the lock of the next save is the sha of exactly what was written (piperd's answer) - never a
    // re-read, which could pick up someone else's write; typing during the request stays unsaved
    const lines = text.split('\n');
    t.file = { ...t.file, sha: wrote?.sha ?? '', total_lines: wrote?.total_lines ?? lines.length, offset: 0,
      lines: lines.map((l, i) => `${i + 1}\t${l}`) };
    t.blame = null;
    t.text = text;
    t.ed.setBaseline(text);
    t.dirty = t.ed.dirty();
    if (key !== project) return;
    if (blameOn) void loadBlame(t);
    drawTabs();
    drawBanners();
    drawStatus();
    void loadMarks(t);
    void loadProblems(t);
  }

  // ---------------------------------------------------------------- IntelliJ bridge (9.7 #10)
  async function ideaOk(): Promise<boolean> {
    if (Date.now() - idea.at < 60000) return idea.ok;
    idea.at = Date.now();
    try {
      idea.ok = !!(await api.ideaStatus()).data?.available;
    } catch {
      idea.ok = false;
    }
    return idea.ok;
  }

  async function loadProblems(t: Tab): Promise<void> {
    if (!(await ideaOk())) {
      t.problems = null;
      return;
    }
    const key = project;
    try {
      const r = (await api.ideaProblems(key, t.path)).data;
      t.problems = r?.available ? r.problems : null;
    } catch {
      t.problems = null;
    }
    if (key !== project) return;
    t.ed?.setProblems(t.problems);
    if (t === cur()) { drawHead(); drawStatus(); if (view().panel.tab === 'problems') drawPanel(); }
  }

  async function symbolInfo(): Promise<void> {
    const t = cur();
    if (!t?.caret?.word) return deps.toast('Put the caret on an identifier');
    const c = t.caret;
    try {
      const r = (await api.ideaSymbol(project, t.path, c.line, c.col)).data;
      if (!r?.available) return deps.toast('IntelliJ does not respond');
      // N9: an empty answer was a modal with the single word "nothing"
      if (!r.info?.trim()) return deps.toast(`IntelliJ has no information on ${c.word}`);
      deps.modal(`${c.word} · IntelliJ`, h('pre', { class: 'idea-info' }, r.info));
    } catch (e) {
      err(e);
    }
  }

  // ---------------------------------------------------------------- the bottom tool panel
  function openPanel(tab: PanelTab): void {
    const v = view();
    v.panel.open = true;
    v.panel.tab = tab;
    drawPanel();
    if (tab === 'turns') void loadTurns();
  }

  function togglePanel(): void {
    const v = view();
    v.panel.open = !v.panel.open;
    drawPanel();
  }

  function openSearch(q = '', glob = ''): void {
    const v = view();
    if (q) v.search.q = q;
    if (glob) v.search.glob = glob;
    openPanel('search');
    window.requestAnimationFrame(() => {
      const i = panelEl.querySelector<HTMLInputElement>('input.sq');
      i?.focus();
      i?.select();
      if (q) void runSearch();
    });
  }

  async function runSearch(): Promise<void> {
    const v = view();
    const s = v.search;
    if (!s.q.trim()) return;
    s.busy = true;
    s.result = null;
    s.cur = -1;
    drawPanel();
    const key = project;
    try {
      const r = await fsGrep(key, s.q, s.glob, { cs: s.cs, limit: 2000, all: s.all });
      if (key === project) s.result = r.data;
    } catch (e) {
      s.result = { matches: [], truncated: false };
      err(e);
    }
    s.busy = false;
    drawPanel();
  }

  /** F4 / ⇧F4: the next / previous result of the panel's list */
  function step(d: number): void {
    const v = view();
    const list = v.panel.tab === 'search' ? v.search.result?.matches ?? [] : v.panel.tab === 'usages' ? v.usages?.hits ?? [] : [];
    if (!list.length) return;
    const st = v.panel.tab === 'search' ? v.search : v.usages!;
    st.cur = (st.cur + d + list.length) % list.length;
    const m = list[st.cur]!;
    jump(m.path, m.line);
    drawPanel();
  }

  async function loadTurns(): Promise<void> {
    const v = view();
    const t = cur();
    const sid = deps.sessionOf?.(project) ?? '';
    if (!t || !sid) { v.turns = { path: t?.path ?? '', rows: [], diffPath: '', note: sid ? '' : 'no session of this project' }; return drawPanel(); }
    v.turns = { path: t.path, rows: null, diffPath: '', note: '' };
    drawPanel();
    try {
      const r = (await api.fileTurns(sid, t.path)).data;
      v.turns = { path: t.path, rows: r?.turns ?? [], diffPath: r?.diff_path ?? t.path, note: '' };
    } catch (e) {
      v.turns = { path: t.path, rows: [], diffPath: '', note: msg(e) };
    }
    drawPanel();
  }

  function hitRow(m: Hit, active: boolean, run: () => void, rx: RegExp | null): HTMLElement {
    const text = m.text.trim().slice(0, 240);
    const tx = h('span', { class: 'tx mono' });
    const hit = rx ? rx.exec(text) : null;
    // a found line is the project's text: a bidi control in it is a mark, not a reordering (Trojan Source)
    if (hit && hit[0]) tx.append(showBidi(text.slice(0, hit.index)), h('mark', {}, showBidi(hit[0])), showBidi(text.slice(hit.index + hit[0].length)));
    else tx.append(showBidi(text));
    return h('button', { class: 'hit' + (active ? ' on' : ''), onclick: run, 'data-nav': `${m.path}:${m.line}` },
      h('span', { class: 'lno mono' }, String(m.line)), tx);
  }

  function hitList(hits: Hit[], curIdx: number, pattern: string, cs: boolean, onPick: (i: number) => void): HTMLElement {
    const box = h('div', { class: 'hits' });
    let rx: RegExp | null = null;
    try { rx = pattern ? new RegExp(pattern, cs ? '' : 'i') : null; } catch { /* PCRE-only syntax: no highlight */ }
    for (const g of groupHits(hits)) {
      box.append(h('div', { class: 'hgroup' }, g.group, h('span', { class: 'mut' }, ` ${g.files.reduce((n, f) => n + f.hits.length, 0)}`)));
      for (const f of g.files) {
        const slash = f.path.lastIndexOf('/');
        const hid = (f.hits as GrepHit[]).some((x) => x.hidden) || isHidden(f.path);
        box.append(h('div', { class: 'hfile', title: showBidi(f.path) }, icon(f.path.slice(slash + 1), false), h('span', { class: 'hn' }, showBidi(f.path.slice(slash + 1))),
          hid && !view().policyUnreadable ? hiddenMark() : null,
          h('span', { class: 'mut mono hd' }, ' ' + showBidi(leftCut(f.path.slice(0, Math.max(0, slash)), 50))), h('span', { class: 'mut hc' }, ` ${f.hits.length}`)));
        for (const m of f.hits) {
          const i = hits.indexOf(m);
          box.append(hitRow(m, i === curIdx, () => onPick(i), rx));
        }
      }
    }
    return box;
  }

  function drawPanel(): void {
    const v = view();
    clear(panelEl);
    panelEl.classList.toggle('open', v.panel.open);
    if (!project) return;
    persist();
    const t = cur();
    const probs = t?.problems?.length ?? 0;
    if (v.panel.tab === 'diag' && !deps.diagOn?.()) v.panel.tab = 'usages';
    const tabs: [PanelTab, string, string][] = ([
      ['usages', 'Usages', v.usages ? String(v.usages.hits.length) : ''],
      ['search', 'Search', v.search.result ? `${v.search.result.matches.length}${v.search.result.truncated ? '+' : ''}` : ''],
      ['problems', 'Problems', probs ? String(probs) : ''],
      ['diag', 'Diagnostics', ''],
      ['turns', 'Turns', ''],
    ] as [PanelTab, string, string][]).filter(([k]) => k !== 'diag' || !!deps.diagOn?.());
    const strip = h('div', { class: 'pstrip' }, ...tabs.map(([k, l, n]) => h('button', {
      class: 'ptab' + (v.panel.open && v.panel.tab === k ? ' on' : '') + (k === 'problems' && probs ? ' warn' : ''),
      onclick: () => { if (v.panel.open && v.panel.tab === k) togglePanel(); else openPanel(k); },
    }, l, n ? h('span', { class: 'n' }, n) : null)), h('span', { class: 'grow' }),
    h('span', { class: 'mut small' }, kbd(v.panel.open ? 'F4 next · ⇧F4 previous · ⌘⌥↓ close' : '⌘⌥↓ open', 'Ctrl')));
    panelEl.append(strip);
    if (!v.panel.open) return;
    const body = h('div', { class: 'pbody', style: `height:${v.panel.height}px` });
    panelEl.append(body);
    // drag the strip = height
    strip.addEventListener('mousedown', (e) => {
      if ((e.target as HTMLElement).closest('button')) return;
      const y0 = e.clientY;
      const h0 = v.panel.height;
      const mv = (ev: MouseEvent) => { v.panel.height = Math.max(120, Math.min(window.innerHeight * 0.7, h0 + (y0 - ev.clientY))); body.style.height = `${v.panel.height}px`; };
      const up = () => { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); persist(); };
      window.addEventListener('mousemove', mv);
      window.addEventListener('mouseup', up);
    });
    if (v.panel.tab === 'usages') {
      const u = v.usages;
      if (!u) return void body.append(h('p', { class: 'mut pad' }, 'Put the caret on an identifier and press ⌥F7 (or right click > Find usages).'));
      body.append(h('div', { class: 'phead' }, h('strong', { class: 'mono' }, u.title), h('span', { class: 'mut' }, u.busy ? ' searching…' : ` ${u.hits.length} usages`),
        h('span', { class: 'grow' }), u.busy ? null : h('span', { class: 'prec', title: 'whole-word, case-sensitive text search (IntelliJ\'s MCP has no references tool)' }, 'text match'),
        u.note ? h('span', { class: 'mut small' }, ' ' + u.note) : null));
      body.append(hitList(u.hits, u.cur, usagePattern(u.title), true, (i) => { u.cur = i; const m = u.hits[i]!; jump(m.path, m.line); drawPanel(); }));
    } else if (v.panel.tab === 'search') {
      const s = v.search;
      const qi = h('input', { type: 'search', class: 'sq mono', placeholder: 'regex (⏎ searches)', value: s.q, autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement;
      const gi = h('input', { type: 'search', class: 'sg mono', placeholder: 'glob, e.g. *.java or src/**', value: s.glob, autocomplete: 'off', spellcheck: 'false' }) as HTMLInputElement;
      qi.addEventListener('input', () => { s.q = qi.value; s.all = false; });
      gi.addEventListener('input', () => { s.glob = gi.value; });
      const go = (e: KeyboardEvent) => {
        if (e.key === 'Enter') { e.preventDefault(); void runSearch(); }
        else if (e.key === 'ArrowDown') { body.querySelector<HTMLElement>('.hit')?.focus(); e.preventDefault(); }
      };
      qi.addEventListener('keydown', go);
      gi.addEventListener('keydown', go);
      const cs = h('button', { class: 'mini' + (s.cs ? ' on' : ''), title: 'match case', onclick: () => { s.cs = !s.cs; cs.classList.toggle('on', s.cs); if (s.q) void runSearch(); } }, 'Aa');
      body.append(h('div', { class: 'phead' }, qi, gi, cs, h('button', { class: 'mini', onclick: () => void runSearch() }, 'Search')));
      if (s.busy) body.append(h('p', { class: 'mut pad' }, h('span', { class: 'spinner' }), ' searching…'));
      else if (s.result) {
        const nf = new Set(s.result.matches.map((m) => m.path)).size;
        body.append(h('div', { class: 'mut small pad' }, `${s.result.matches.length}${s.result.truncated ? '+' : ''} ${s.result.matches.length === 1 && !s.result.truncated ? 'match' : 'matches'} in ${nf} ${nf === 1 ? 'file' : 'files'}${s.result.truncated ? ' - truncated, narrow the query or glob' : ''}`,
          s.result.regex === 'ere' ? ' · this git has no PCRE: extended regex (no \\d, lookarounds)' : '',
          s.all ? ' · excluded folders included' : '',
          // nothing: the tooling folders are left out by default (K1)
          !s.result.matches.length && !s.all ? h('button', { class: 'link small', onclick: () => { s.all = true; void runSearch(); } }, ' Search the excluded folders too (node_modules, target, .venv, .idea…)') : null));
        body.append(hitList(s.result.matches, s.cur, s.q, s.cs, (i) => { s.cur = i; const m = s.result!.matches[i]!; jump(m.path, m.line); drawPanel(); }));
      }
    } else if (v.panel.tab === 'problems') {
      if (!t?.file) return void body.append(h('p', { class: 'mut pad' }, 'Open a file.'));
      if (!t.problems) return void body.append(h('p', { class: 'mut pad' }, idea.ok ? 'IntelliJ has not answered for this file.' : 'Problems come from IntelliJ - it is not running (or its MCP server is off).'));
      if (!t.problems.length) return void body.append(h('p', { class: 'mut pad' }, `IntelliJ reports nothing in ${showBidi(t.path.split('/').pop() ?? '')}.`));
      for (const p of [...t.problems].sort((a, b) => a.line - b.line)) {
        body.append(h('button', { class: 'hit', onclick: () => gotoLineNum(p.line) }, h('span', { class: 'lno mono' }, String(p.line)),
          h('span', { class: 'sev ' + (/err/i.test(p.severity) ? 'bad' : 'warn') }, p.severity.toLowerCase()), h('span', { class: 'tx' }, p.message)));
      }
    } else if (v.panel.tab === 'diag') {
      const sid = deps.sessionOf?.(project) ?? '';
      if (!sid) return void body.append(h('p', { class: 'mut pad' }, 'No session of this project - diagnostics come from its last build output.'));
      body.append(h('p', { class: 'mut pad' }, h('span', { class: 'spinner' }), ' reading the last build output…'));
      const key = project;
      api.diagnostics(sid).then((r) => {
        if (v.panel.tab !== 'diag' || key !== project) return;
        clear(body);
        const items = r.data?.items ?? [];
        if (!items.length) return void body.append(h('p', { class: 'mut pad' }, r.data?.note || 'No errors in the last build output of the session.'));
        // the Git panel's Diagnostics section, same groups and the same "Send to Claude" (K9)
        const send = deps.send;
        body.append(h('div', { class: 'phead' }, h('span', { class: 'mut small grow' }, r.data?.source ? `from ${r.data.source}` : ''),
          send ? h('button', { class: 'mini', title: 'send the error list to the session', onclick: () => confirmDiagSend(deps, (text) => send(key, text), items, r.data?.source ?? '') }, 'Send to Claude') : null),
        h('div', { class: 'diagbody pad' }, ...diagGroupsEl(items, key, (_p, path, line) => jump(path, line))));
      }).catch((e: unknown) => { clear(body); body.append(h('p', { class: 'bad pad' }, msg(e))); });
    } else if (v.panel.tab === 'turns') {
      const tt = v.turns;
      if (!t?.file) return void body.append(h('p', { class: 'mut pad' }, 'Open a file: its changes per turn of the session show here.'));
      if (!tt || tt.path !== t.path) { void loadTurns(); return; }
      if (tt.rows === null) return void body.append(h('p', { class: 'mut pad' }, h('span', { class: 'spinner' }), ' loading…'));
      if (tt.note) body.append(h('p', { class: 'mut pad' }, tt.note));
      if (!tt.rows.length && !tt.note) body.append(h('p', { class: 'mut pad' }, `No turn of the session changed ${showBidi(t.path.split('/').pop() ?? '')}.`));
      const sid = deps.sessionOf?.(project) ?? '';
      for (const r of tt.rows) {
        const first = (r.prompt.split('\n').find((l) => l.trim()) ?? '').trim();
        body.append(h('button', { class: 'hit', title: r.prompt, onclick: () => void showTurnDiff(sid, r, tt.diffPath, t.path) },
          h('span', { class: 'lno mono' }, `#${r.n}`),
          r.add === null ? h('span', { class: 'mut' }, 'binary') : h('span', { class: 'add' }, `+${r.add}`), r.del === null ? null : h('span', { class: 'del' }, `−${r.del}`),
          h('span', { class: 'mut small' }, `${new Date(r.at * 1000).toLocaleString()}${r.to === null ? ' · running' : ''}`),
          h('span', { class: 'tx' }, first.length > 90 ? cutText(first, 89) + '…' : first)));
      }
    }
  }

  async function showTurnDiff(sid: string, r: { n: number; from: string; to: string | null }, diffPath: string, path: string): Promise<void> {
    try {
      const text = await turnDiff(sid, r.from, r.to ?? '', diffPath);
      const rows = pairDiff(text);
      deps.modal(`Turn ${r.n} · ${path.split('/').pop()}`, rows.length ? renderSideDiff(rows, langOf(path), (n) => { deps.closeModal(); jump(path, n); }) : h('p', { class: 'mut' }, 'no change'));
    } catch (e) {
      err(e);
    }
  }

  /** ⌘⇧E: the keyboard to the tree (the active row, else the first) */
  function focusTree(): void {
    if (!treeOn()) setTree(true);
    (leftEl.querySelector('.tree [data-nav].active') as HTMLElement | null ?? leftEl.querySelector('.tree [data-nav]') as HTMLElement | null)?.focus();
  }

  // ---------------------------------------------------------------- keys inside Files (outside CodeMirror's own)
  el.addEventListener('keydown', (e) => {
    const typing = e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement;
    const inCode = (e.target as HTMLElement).closest?.('.cm-editor') !== null;
    if ((MAC ? e.metaKey : e.ctrlKey) && e.altKey && e.code === 'KeyA') {
      const t = cur(); if (t) reviewSelection(t); e.preventDefault(); e.stopPropagation(); return;
    }
    // ⌥W closes the tab - only outside the editor (there ⌥W writes a character)
    if (e.altKey && !e.metaKey && !e.ctrlKey && e.code === 'KeyW' && !typing && !inCode) { closeTab(); e.preventDefault(); }
    else if (e.key === 'F4') { step(e.shiftKey ? -1 : 1); e.preventDefault(); }
    // ⌘⌥↓ (Ctrl+Alt+↓ off a Mac: Alt+⇧↓ moves a session in the queue there)
    else if ((MAC ? e.metaKey && e.altKey : e.ctrlKey && e.altKey && !e.metaKey) && e.key === 'ArrowDown' && !inCode) { togglePanel(); e.preventDefault(); }   // the editor binds it itself
    else if (appMod(e, MAC) && e.shiftKey && e.code === 'KeyE' && !typing) {   // the keyboard to the tree
      focusTree();
      e.preventDefault();
      e.stopPropagation();   // not Piper's ⌘E as well
    } else if (e.key === 'Escape' && !typing && !inCode) cur()?.ed?.focus();
  });
  const navKey = (e: KeyboardEvent) => el.classList.toggle('navkey', e.metaKey || e.altKey);
  window.addEventListener('keydown', navKey);
  window.addEventListener('keyup', navKey);
  window.addEventListener('blur', () => el.classList.remove('navkey'));

  // ---------------------------------------------------------------- public
  function show(key: string): void {
    if (key !== project) {
      flush();
      for (const t of view().tabs) if (t.ed) t.scroll = t.ed.scrollTop();
      project = key;
      if (project && !view().dirs.has('.')) void loadDir('.');
      // folders expanded before a reload (K14)
      if (project) for (const d of view().expanded) if (d !== '.' && !view().dirs.has(d)) void loadDir(d);
      void ideaOk().then(() => drawStatus());
      drawRight();
      if (project && view().restore) void restoreTabs();
    }
    drawLeft();
  }

  /** K14: the tabs open before a reload, quietly (a file gone meanwhile is skipped), then the active one */
  async function restoreTabs(): Promise<void> {
    const key = project;
    const v = view();
    const r = v.restore;
    if (!r) return;
    for (const p of r.tabs) {
      if (key !== project) return;   // switched away: the rest opens on the next show
      if (!v.tabs.some((t) => t.path === p)) await open(p, 0, true);
      r.tabs = r.tabs.filter((x) => x !== p);
    }
    v.restore = null;
    v.recent = [...new Set([...r.recent, ...v.recent])].slice(0, RECENT_MAX);
    v.expanded = new Set(['.', ...r.expanded]);
    const focus = restoredFileFocus(v.active, r.active, v.tabs.map((t) => t.path));
    if (focus) activate(focus);
    drawLeft();
  }

  /** git status arrived: re-mark the tree, re-read open files that are not being edited */
  function refreshMarks(): void {
    if (!el.isConnected || !project) return;
    drawLeft();
    drawTabs();
    // F06: the empty editor lists the changed files - git status came after it was drawn
    if (!cur() && bodyEl.querySelector('.fe-hub')) { clear(bodyEl); bodyEl.append(emptyEl()); }
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      for (const t of view().tabs) {
        if (!t.file) continue;
        if (t.dirty) void loadMarks(t);
        else if (t === cur() || t.ed) void reread(t, false);
      }
    }, 250);
  }

  /** Session edits can create / remove files too: invalidate listings and the quick-open index. */
  function sessionEdited(key: string, sid: string, seq: number, paths: string[], unknown = 0): void {
    if (!views.has(key)) view0(key);
    const v = views.get(key)!;
    const seen = v.fsSeq.get(sid);
    v.fsSeq.set(sid, seq);
    // the first sight of a session only records where it is; later bumps re-read
    if (seen === undefined || seq <= seen) return;
    const all = unknown > seen;   // an edit without a known path (Codex apply_patch): every open tab
    v.dirs = new Map();   // collapsed folders reload on expand; inactive projects reload on show
    v.index = null;
    if (key !== project) return;
    for (const dir of new Set(['.', ...v.expanded])) void loadDir(dir);
    for (const t of v.tabs) if (t.file && (all || paths.includes(t.path))) void reread(t, false, true);
    deps.gitChanged?.(key);
    drawLeft();
  }

  /** the project's session started / stopped working: read-only on / off */
  function refreshWorking(): void {
    const t = cur();
    if (!t?.ed) return;
    t.ed.setReadOnly(!writable(t));
    drawBanners();
    drawStatus();
  }

  drawLeft();
  drawRight();

  return {
    el,
    show,
    async openDirectory(key: string, path: string): Promise<void> {
      show(key);
      view().scope = 'all'; view().filter = '';
      for (const dir of [...dirsOf([path + '/_']), path]) {
        view().expanded.add(dir);
        await loadDir(dir);
        if (project !== key) return;
      }
      setTree(true); drawLeft(); persist();
    },
    resumeHttp: () => httpCur?.resume(),
    project: () => project,
    open: (path: string, line = 0) => void open(path, line),
    /** open a file of any project (switches the project first) */
    openFile(key: string, path: string, line = 0): void {
      if (key && key !== project) show(key);
      void open(path, line);
    },
    /** the side pane beside the terminal: tree collapsible, tabs + code */
    setNarrow(on: boolean): void {
      if (narrow === on) return;
      narrow = on;
      el.classList.toggle('narrow', on);
      applyTree();
      drawTabs();
      drawHead();
    },
    toggleTree: () => setTree(!treeOn()),
    focusTree,
    quickOpen: () => quickOpen(),
    /** ⌥⇧S / ⌘K "New scratch file…": the type menu in the middle of the window, like the palette - under a button
     *  that just appeared (the Files view opened by the key) it sat in a corner nobody looks at */
    newScratch(): void {
      if (!treeOn()) setTree(true);
      scratchMenu('center');
    },
    searchMode(): void {
      const t = cur();
      const sel = t?.caret?.selText && !t.caret.selText.includes('\n') ? t.caret.selText : '';
      openSearch(sel ? sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') : '');
    },
    refreshMarks,
    sessionEdited,
    refreshWorking,
    /** the Git panel's review notes changed (added, sent, discarded): the gutter marks follow (K6) */
    refreshNotes(): void {
      for (const [key, v] of views) {
        const notes = deps.notes?.(key) ?? [];
        for (const t of v.tabs) t.ed?.setNotes(notes.filter((n) => n.path === t.path && !n.old).map((n) => n.line));
      }
    },
    closeTab: () => closeTab(),
    gotoLine,
    find,
    outline: showOutline,
    findUsages: () => findUsages(),
    back,
    forward,
    toggleDiff,
    togglePanel,
    showDiagnostics: () => openPanel('diag'),
    insertRef,
    save: () => void save(),
    /** 9.41: the shown tab's pending autosave runs now (the Files view is left, the page hides) */
    flush,
    /** a text file (not an image) is shown: ⌘F / ⌘L belong to the file pane */
    hasFile: () => !!cur()?.file,
    /** unsaved edits anywhere (the page asks before it unloads) */
    dirty: () => [...views.values()].some((v) => v.tabs.some((t) => t.dirty)),
  };
}

/** 9.114: the editor toolbar draws line icons like the rest of the app, not @ ◧ ⓑ ⏱ ☰ glyphs */
function tbIcon(name: string): HTMLElement {
  return h('i', { class: `fi fi-${name}`, 'aria-hidden': 'true' });
}
