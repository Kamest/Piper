// Git panel: git runs on the host (and, since 2026-10-01, in the
// sandbox too). The human sees every changed file (policy files are marked and start
// unchecked), reads diffs, commits the checked files and pushes after a confirmation.
// The element is persistent: the inspector re-renders on every poll, the commit
// message being typed and the selection must survive that.
// 9.7: "Last turn" (the diff of one turn from piperd's snapshots, #2), history / graph
// links, CI chip and "Create MR…" after a push (#7). Review notes work the same on a
// turn diff as on a file diff; the file preview adds its notes through addNote().

import { api } from './api.js';
import { isMac, kbd } from './clipkeys.js';
import { clear, fold, h } from './dom.js';
import { createMergeReview, type MergeReview } from './cmeditor.js';
import { actionMenu, reviewDigit, reviewCodeAppendix, reviewPrompt, reviewTargetKey, toggleReviewAction } from './reviewactions.js';
import { openHistory } from './history.js';
import { fileTree, openGitWindow, openPushDialog, setTreePref, shortDir, stat, treePref } from './gitwindow.js';
import { filesLabel, lastTurn, pickTurn, turnLabel, turnRange } from './turns.js';
import { plural, showBidi, markBidi } from './text.js';
import { firstOver } from './layers.js';
import { toggleMenu } from './menu.js';
import type { BlobVersion, FileText, Session, ReviewAction, ReviewItem, ReviewTarget, CiState, FileBlob, GitStatus, MrDraft, ReviewFile, ReviewFiles, TurnFiles, TurnSnap } from './types.js';

export interface GitDeps {
  session?(id: string): Session | undefined;
  sessionCreated?(id: string): void;
  /** ⌘/Ctrl+click in Changes / review: the session's file in the main Files view */
  openSourceFull?(id: string, path: string, line: number): void;
  conversation?(id: string, index: number): void;
  /** commit A: a toast may carry one action (Push… after a commit) */
  toast(text: string, action?: { label: string; run(): void }): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  /** W11: a dialog over the open one (closeModal goes back to it); absent = modal() */
  modalOver?(title: string, ...content: (Node | null)[]): void;
  /** W11: every dialog closes (leaving for a file / session) */
  closeAllModals?(): void;
  /** host path of the session's project (git paths are relative to the repository root) */
  projectPath(id: string): string;
  /** type the review notes into the session; false when it did not go out */
  send(id: string, text: string): Promise<boolean>;
  /** a status arrived (the file browser re-marks changed files) */
  onStatus?(id: string): void;
  /** other live sessions in the same working tree */
  sharedWith?(id: string): string[];
  /** commit A: the sessions of this working tree (this one too) that are working right now */
  workingIn?(id: string): string[];
  /** commit A: policy commit_requires_review - Commit only after every checked file was viewed */
  requireReview?(): boolean;
  /** 9.7: the session's project KEY (Session.project) - history, graph, CI and MR need it */
  project?(id: string): string;
  /** 9.7: every CI fetch (the lead notifies when a pipeline turns failed) */
  onCi?(project: string, ci: CiState): void;
  /** 9.7: the session has a GitLab project (no GitLab = no MR offer) */
  gitlab?(id: string): boolean;
  /** Feature switches gate automatic CI work and cached chips. */
  ciEnabled?(forge: 'gitlab' | 'github'): boolean;
  /** open a project-relative file at a line in Piper's editor (K3/K4: rows, review diff, history) */
  openFile?(project: string, path: string, line: number): void;
  /** the review notes of a session changed (added, sent, discarded): the editor's gutter marks follow (K6) */
  onNotes?(id: string): void;
  /** the Git window's worktree rows: a live session there, a new session in a project key */
  openSession?(id: string): void;
  newSession?(project: string): void;
}

/** the one "hidden from Claude" wording (K10): tree, tabs, lists, ⌘P, search, Git panel, turns */
export const HIDDEN_TITLE = 'hidden from Claude (isolation policy / exclude): sessions never see it';

/** the eye mark of a file hidden from Claude */
export function hiddenMark(): HTMLElement {
  return h('span', { class: 'fi fi-hidden hid-mark', title: HIDDEN_TITLE, 'aria-label': 'hidden from Claude' });
}

export interface Note {
  path: string;
  line: number;
  /** a removed line: `line` is the old file's number */
  old: boolean;
  text: string;
  state?: 'open' | 'sent' | 'resolved';
  excerpt?: string;
  id?: string;
}

export interface DiffLine {
  text: string;
  cls: '' | 'add' | 'del' | 'hunk' | 'meta';
  newNo: number | null;
  oldNo: number | null;
}

/** unified diff -> lines with their old/new file line numbers (from the @@ headers) */
export function numberDiff(text: string): DiffLine[] {
  const out: DiffLine[] = [];
  let o = 0;
  let n = 0;
  let inHunk = false;
  for (const line of text.split('\n')) {
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (m) {
      o = Number(m[1]);
      n = Number(m[2]);
      inHunk = true;
      out.push({ text: line, cls: 'hunk', newNo: null, oldNo: null });
    } else if (!inHunk || line.startsWith('diff ')) {   // file headers; inside a hunk '---' is a removed '--' line
      inHunk = false;
      out.push({ text: line, cls: 'meta', newNo: null, oldNo: null });
    } else if (line.startsWith('+')) {
      out.push({ text: line, cls: 'add', newNo: n++, oldNo: null });
    } else if (line.startsWith('-')) {
      out.push({ text: line, cls: 'del', newNo: null, oldNo: o++ });
    } else if (line.startsWith('\\')) {
      out.push({ text: line, cls: 'meta', newNo: null, oldNo: null });
    } else {
      out.push({ text: line, cls: '', newNo: n++, oldNo: o++ });
    }
  }
  return out;
}

/** git path (repository-relative) -> the path the session's Claude uses (project-relative) */
export function projectRel(path: string, root: string, project: string): string {
  const prefix = project.startsWith(root + '/') ? project.slice(root.length + 1) + '/' : '';
  if (!prefix) return path;
  return path.startsWith(prefix) ? path.slice(prefix.length) : `${root}/${path}`;
}

/** one message for the session: `path:line: note` per note, sorted by file and line */
export function notesMessage(notes: Note[]): string {
  const sorted = [...notes].sort((a, b) => a.path.localeCompare(b.path) || a.line - b.line);
  return ['Review notes on the changes (fix them, then briefly say what you did):',
    ...sorted.map((x) => `- ${x.path}:${x.line}${x.old ? ' (removed line, number in the old version)' : ''}: ${x.text}${x.excerpt ? `\n  Code: ${x.excerpt}` : ''}`)].join('\n');
}

/** CI chip tone of a GitLab pipeline status */
export function ciTone(status: string): 'ok' | 'bad' | 'run' | 'mut' {
  if (status === 'success') return 'ok';
  if (status === 'failed') return 'bad';
  if (['running', 'pending', 'created', 'preparing', 'waiting_for_resource', 'scheduled'].includes(status)) return 'run';
  return 'mut';
}

const CI_LABEL: Record<string, string> = {
  success: 'passed', failed: 'failed', running: 'running', pending: 'pending', created: 'pending', preparing: 'preparing',
  waiting_for_resource: 'waiting for a runner', scheduled: 'scheduled', canceled: 'canceled', skipped: 'skipped', manual: 'manual step',
};

export function ciLabel(status: string): string {
  return CI_LABEL[status] ?? status;
}

/** F27: offer the MR in the push dialog - canOfferMr without "already pushed": a named branch, not the main line, no MR yet */
export function canOfferMrWithPush(st: Pick<GitStatus, 'branch'> | null, ci: CiState | null): boolean {
  if (!st || !ci || !st.branch || ['master', 'main', 'develop'].includes(st.branch)) return false;
  return !ci.mr && (!ci.branch || ci.branch === st.branch);
}

/** offer "Create MR…": the branch is pushed, nothing waits to be pushed, no MR yet, not the main line */
export function canOfferMr(st: Pick<GitStatus, 'branch' | 'upstream' | 'ahead'> | null, ci: CiState | null): boolean {
  if (!st || !ci || !st.branch || !st.upstream || st.ahead) return false;
  if (['master', 'main', 'develop'].includes(st.branch)) return false;
  return !ci.mr && (!ci.branch || ci.branch === st.branch);
}

/** N10-B-7: the files ticked when Review opens - what the Changes panel ticked, a file hidden from Claude only when the
 *  human ticked it there (the panel ticks those one by one; its tick was dropped: "Commit (1)" opened "0 selected");
 *  before the panel knows the status, every committable change but the hidden ones. */
export function reviewPreselect(files: (Pick<ReviewFile, 'path' | 'withheld' | 'sensitive' | 'committable'> & { live_status?: string })[], panel: Set<string> | null): string[] {
  return files.filter((f) => f.committable && reviewGroup(f) === 'Changes' && (panel ? panel.has(f.path) : !f.sensitive)).map((f) => f.path);
}

/** Fold tooling and scratch separately so opening a review never marks them viewed. */
export function reviewGroup(file: Pick<ReviewFile, 'path' | 'withheld'> & { live_status?: string }): string {
  if (file.withheld) return 'Withheld';
  if (file.live_status === '??') return 'Unversioned Files';
  if (isJunk(file.path) || /(^|\/)(scratch|tmp|temp)(\/|$)/i.test(file.path)) return 'Scratch';
  if (/(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum|composer\.lock|Gemfile\.lock|Cargo\.lock)$|\.lock$/i.test(file.path)) return 'Lock files';
  if (/(^|\/)(dist|build|target|vendor|generated|node_modules)(\/|$)|\.(map|min\.js|min\.css)$|\.generated\./i.test(file.path)) return 'Generated';
  return 'Changes';
}

const reviewGroups = ['Changes', 'Unversioned Files', 'Lock files', 'Generated', 'Scratch', 'Withheld'];

/** Pair a test with its source without pulling it into another risk group. */
export function reviewOrder<T extends Pick<ReviewFile, 'path'>>(files: T[]): T[] {
  const key = (path: string) => path.toLowerCase().replace(/(^|\/)(src|main|tests?|java|kotlin)(?=\/)/g, '')
    .replace(/\/+/g, '/').replace(/^\/+/, '').replace(/(^|\/)test_/, '$1').replace(/(?:[._](test|spec)|test)(?=\.[^.]+$)/, '');
  const test = (path: string) => /(^|\/)(test_|tests?\/)|(?:[._](test|spec)|Test)\.[^.]+$/.test(path);
  return [...files].sort((a, b) => key(a.path).localeCompare(key(b.path)) || Number(test(a.path)) - Number(test(b.path)) || a.path.localeCompare(b.path));
}

/** Navigation follows the displayed groups, then their source/test ordering. */
export function reviewNavigationOrder<T extends Pick<ReviewFile, 'path' | 'withheld'> & { live_status?: string }>(files: T[]): T[] {
  return reviewGroups.flatMap(group => reviewOrder(files.filter(file => reviewGroup(file) === group)));
}

/** Both sides, the live bytes/mode and HEAD bind Viewed to this exact comparison. */
export function reviewKey(file: ReviewFile, head: string): string {
  return JSON.stringify([head, file.orig ?? '', file.before, file.after, file.orig_after, file.work_sha, file.work_mode]);
}

export function sourceClick(event: Event | { metaKey: boolean; ctrlKey: boolean }, mac = typeof navigator !== 'undefined' && isMac(navigator.platform)): boolean {
  return mac ? 'metaKey' in event && event.metaKey === true : 'ctrlKey' in event && event.ctrlKey === true;
}

interface PerSession {
  status: GitStatus | null;
  error: string;
  selected: Set<string>;
  message: string;
  loading: boolean;
  fetched: number;
  notes: Note[];
  viewed: Map<string, string>;
  accepted: Map<string, Set<number>>;
  // 9.7 #2: turns
  turns: TurnSnap[] | null;
  turnNote: string;
  /** the picked turn; null = follow the newest */
  turnN: number | null;
  /** from..to -> files */
  turnFiles: Map<string, TurnFiles>;
  /** a load was asked for while one ran: run once more at its end */
  turnAgain: boolean;
  /** the board's stamp (Session.since) of the last turn fetch */
  turnStamp: number;
  /** a push went through: highlight "Create MR…" */
  offerMr: boolean;
  /** W6: the next commit replaces HEAD */
  amend: boolean;
  /** commit A: the HEAD message Amend filled in - Amend off takes it back while untouched */
  amendFilled: string;
  /** W6: what Generate drafted from (hidden files left out, a cut diff) */
  genNote: string;
  /** the drafted message left hidden files out: the note is a warning (S2) */
  genHeld: boolean;
  generating: boolean;
  /** S2: Commit & Push - the Push dialog opens once the commit is in */
  pushAfter: boolean;
}

/** S2: a group's / directory's box over its files - the files hidden from Claude are only ever ticked one by one */
export function pickState(files: { path: string; sensitive: boolean }[], selected: Set<string>): { checked: boolean; indeterminate: boolean; disabled: boolean; bulk: string[] } {
  const bulk = files.filter((f) => !f.sensitive).map((f) => f.path);
  const on = files.filter((f) => selected.has(f.path)).length;
  return { checked: on > 0 && on === files.length, indeterminate: on > 0 && on < files.length, disabled: !bulk.length, bulk };
}

/** something to push: a branch with commits the upstream lacks, or one not on a remote yet */
export function canPush(st: Pick<GitStatus, 'branch' | 'upstream' | 'ahead'>): boolean {
  return !!st.branch && (st.ahead > 0 || !st.upstream);
}

/** W6: Amend is off once HEAD is on the upstream ('' = allowed) */
export function amendBlock(st: Pick<GitStatus, 'upstream' | 'ahead' | 'log'>): string {
  if (!st.log.length) return 'Amend off: no commit yet.';
  if (st.upstream && !st.ahead) return `Amend off: HEAD ${st.log[0]!.split(' ')[0]} is already pushed.`;
  return '';
}

/** W6: the note under a drafted message; `files` a count, or what it was drafted from (commit A: "the branch") */
export function generatedNote(files: number | string, withheld: string[], truncated: boolean): string {
  const base = (p: string) => p.slice(p.lastIndexOf('/') + 1);
  return [`Drafted by Haiku from ${typeof files === 'string' ? files : `${files} ${files === 1 ? 'file' : 'files'}`}.`,
    withheld.length ? `${withheld.map(base).join(', ')} ${withheld.length === 1 ? 'is' : 'are'} hidden and ${withheld.length === 1 ? 'was' : 'were'} not sent.` : '',
    truncated ? 'The diff was cut to fit.' : ''].filter(Boolean).join(' ');
}

/** commit A: what a Commit sends - the versions pinned at the click for the checked paths (both rename paths);
 *  a checked path the comparison lacks or cannot commit stops it, and the ones not viewed are counted */
export interface CommitPlan { versions: Record<string, BlobVersion>; missing: string[]; blocked: string[]; unviewed: string[] }
export function commitPlan(selected: Iterable<string>, review: Pick<ReviewFiles, 'files' | 'head'>, viewed: Map<string, string>): CommitPlan {
  const plan: CommitPlan = { versions: {}, missing: [], blocked: [], unviewed: [] };
  for (const path of selected) {
    const file = review.files.find((row) => row.path === path);
    if (!file) { plan.missing.push(path); continue; }
    if (file.withheld || !file.committable || !file.after || (file.orig && !file.orig_after)) { plan.blocked.push(path); continue; }
    plan.versions[file.path] = file.after;
    if (file.orig) plan.versions[file.orig] = file.orig_after!;
    if (viewed.get(file.path) !== reviewKey(file, review.head)) plan.unviewed.push(path);
  }
  return plan;
}

/** commit A: the pinned paths whose version the comparison no longer shows - what changed after the Commit click */
export function changedSince(pinned: Record<string, BlobVersion>, review: Pick<ReviewFiles, 'files'>): string[] {
  const now = new Map<string, BlobVersion | undefined>();
  for (const file of review.files) {
    now.set(file.path, file.after);
    if (file.orig) now.set(file.orig, file.orig_after);
  }
  return Object.keys(pinned).filter((path) => {
    const v = now.get(path);
    const p = pinned[path]!;
    return !v || v.oid !== p.oid || v.mode !== p.mode;
  });
}

/** commit A: Amend off takes back the HEAD message it filled in - one the user edited stays */
export function amendOffMessage(message: string, filled: string): string {
  return filled && message === filled ? '' : message;
}

/** the toast after a created MR / PR - a forge answer without a number says only that it was created */
export function mrCreatedText(mr: unknown, gh: boolean): string {
  const m = mr && typeof mr === 'object' && !Array.isArray(mr) ? mr as { iid?: unknown; web_url?: unknown } : null;
  if (!m || typeof m.iid !== 'number') return gh ? 'PR created' : 'MR created';
  return `${gh ? 'PR #' : 'MR !'}${m.iid} created${typeof m.web_url === 'string' && m.web_url ? `: ${m.web_url}` : ''}`;
}

/** commit A: the one primary button of the panel - Commit while files are checked, then Push…, then Create MR… */
export function primaryAction(st: Pick<GitStatus, 'branch' | 'upstream' | 'ahead'>, picked: boolean, offer: boolean): 'commit' | 'push' | 'mr' | '' {
  if (picked) return 'commit';
  if (canPush(st)) return 'push';
  return offer ? 'mr' : '';
}

/** commit A: the foot's words beside Commit - what it lacks, else its key. F07: no "N of M" - the count is on the
 *  button ("1 of 10" counted Unversioned files beside a "Changes 2 files" group) */
export function commitHint(picked: number, message: string, amend: boolean): { text: string; warn: boolean } {
  if (!picked && !amend) return { text: 'Check files to commit', warn: false };
  if (!message.trim()) return { text: 'Write a message', warn: true };
  return { text: '⌘⏎', warn: false };
}

/** F07: the Commit button says how many checked files go in */
export function commitLabel(picked: number, amend: boolean): string {
  const verb = amend ? 'Amend' : 'Commit';
  return picked ? `${verb} ${picked} ${picked === 1 ? 'file' : 'files'}` : verb;
}

/** F07: Review's one green button - Prepare task while review items are open, else Commit */
export function reviewPrimary(pending: number): 'send' | 'commit' {
  return pending ? 'send' : 'commit';
}

const REFRESH_MS = 15000;
const CI_MS = 60000;

export function createGitPanel(deps: GitDeps) {
  const sec = fold('git', 'Commit');
  sec.el.classList.add('git');
  const el = sec.el;
  el.addEventListener('keydown', (event) => {
    const button = (event.target as HTMLElement).closest<HTMLElement>('[data-git-path]');
    if (!button) {   // commit A: ⌘D in the panel = Review before commit (the checked files)
      if (sourceClick(event) && event.key.toLowerCase() === 'd' && of(current).status?.files.length) { event.preventDefault(); event.stopPropagation(); void showReview(current, 'head'); }
      return;
    }
    if (sourceClick(event) && event.key.toLowerCase() === 'd') {
      event.preventDefault(); event.stopPropagation(); void showReview(current, 'head', 0, button.dataset.gitPath);
    } else if (event.key === 'F4' || (sourceClick(event) && event.key === 'ArrowDown')) {
      event.preventDefault(); event.stopPropagation(); openSource(current, button.dataset.gitPath!);
    }
  });
  let junkOpen = false;
  let foreignOpen = false;
  let changesOpen = true;
  /** S2: the files as a directory tree (the Git window's choice, shared) */
  let tree = treePref();
  /** folded directories, `group:dir` */
  const foldedDirs = new Set<string>();
  const state = new Map<string, PerSession>();
  const turnRuns = new Map<string, Promise<void>>();
  const ciState = new Map<string, { ci: CiState | null; at: number; loading: boolean }>();
  let current = '';
  // persistent parts: redrawn on their own data, re-attached by draw()
  const msg = h('textarea', { rows: 3, placeholder: 'commit message (project convention, e.g. feat(#123): …)' }) as HTMLTextAreaElement;
  msg.addEventListener('input', () => { of(current).message = msg.value; drawFoot(); });
  msg.addEventListener('keydown', (event) => {
    if (sourceClick(event) && event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); startCommit(false); }
  });
  // commit A: Commit commits the checked files as they are at the click (pinned blobs); Review is the optional step before it
  const commitBtn = h('button', { class: 'primary', title: 'commit the checked files as they are now · ⌘⏎ in the message', onclick: () => startCommit(false) }, 'Commit');
  const moreBtn = h('button', { class: 'split-more', title: 'Commit & Push, Amend, Review before commit', 'aria-label': 'More commit actions', 'aria-haspopup': 'menu', onclick: () => commitMenu() }, h('i', { class: 'fi fi-chev-d' }));
  const commitPushBtn = h('button', { title: 'commit, then the Push dialog shows what goes out', onclick: () => startCommit(true) }, 'Commit & Push');
  const footCount = h('span', { class: 'small mut grow' });
  function startCommit(push: boolean): void {
    const id = current;
    const s = of(id);
    if (busy || ((!s.selected.size && !s.amend) || !s.message.trim())) return;
    s.pushAfter = push;
    const busyHere = deps.workingIn?.(id) ?? [];
    if (busyHere.length && s.selected.size) confirmWorking(busyHere, s.selected.size, () => void commit(), deps.modal, () => { s.pushAfter = false; });
    else void commit();
  }
  /** commit A: an agent working in this tree can change a file between your check and the click */
  function confirmWorking(names: string[], files: number, go: () => void, open: (title: string, ...content: (Node | null)[]) => void = deps.modal, wait?: () => void): void {
    open('Commit while the agent is working?',
      h('p', {}, `${names.join(', ')} ${names.length === 1 ? 'is' : 'are'} working in this tree. Piper commits the versions as they are at your click; what the agent writes later stays uncommitted.`),
      h('div', { class: 'row' }, h('button', { onclick: () => { wait?.(); deps.closeModal(); } }, 'Wait'),
        h('button', { class: 'primary', onclick: () => { deps.closeModal(); go(); } }, `Commit ${files} ${files === 1 ? 'file' : 'files'}`)));
  }
  function commitMenu(): void {
    const s = of(current);
    const st = s.status;
    const blocked = st ? amendBlock(st) : 'Amend off: no status yet.';
    toggleMenu(moreBtn, () => [
      { label: s.amend ? 'Amend' : 'Commit', keys: '⌘⏎', disabled: commitBtn.disabled && 'check files and write a message', run: () => startCommit(false) },
      { label: s.amend ? 'Amend & Push…' : 'Commit & Push…', disabled: commitPushBtn.disabled && 'check files and write a message on a branch', run: () => startCommit(true) },
      { label: 'Amend last commit', on: s.amend, disabled: !s.amend && blocked ? blocked.replace(/^Amend off: /, '') : false,
        title: 'replace the last commit (HEAD) with this message and the checked files', run: () => void toggleAmend(!s.amend) },
      null,
      { label: 'Review before commit…', keys: '⌘D', disabled: !s.selected.size && 'check files first', run: () => void showReview(current, 'head') },
    ], { label: 'commit actions' });
  }
  const genBtn = h('button', { class: 'mini', title: 'draft the message from the checked files\' diff (claude -p on this Mac, Haiku, no tools; files hidden from Claude are never sent)', onclick: () => void generate() }, h('i', { class: 'fi fi-sparkles' }), h('span', {}, 'Generate')) as HTMLButtonElement;
  const ciEl = h('div', { class: 'ci row left' });
  // the pipeline in the section header while it is folded (open, the branch line shows it): a failed one stays visible
  const ciHead = h('span', {});
  // the last turn lives in the panel's "Last turn" section (main.ts), next to the prompt and the answer
  const turnStatus = h('span', {});
  const turnBody = h('div', { class: 'tbody' });
  let turnSig = '';
  let ciSig = '';

  const notesKey = (id: string) => 'piper.review.notes.' + id;
  const itemsCache = new Map<string, ReviewItem[]>();
  function itemsOf(id: string): ReviewItem[] {
    if (!itemsCache.has(id)) {
      let items: ReviewItem[] = [];
      try {
        const saved: unknown = JSON.parse(localStorage.getItem('piper.review.items.' + id) ?? '[]');
        if (Array.isArray(saved)) items = saved.filter((x): x is ReviewItem => x && typeof x.id === 'string' && typeof x.path === 'string' &&
          typeof x.code === 'string' && typeof x.text === 'string' && Array.isArray(x.actions) && Number.isInteger(x.from) && Number.isInteger(x.to) &&
          ['open', 'sent', 'resolved'].includes(x.state) && (typeof x.sha === 'string' || x.sha === null) &&
          x.actions.every((a: ReviewAction) => a && typeof a.id === 'string' && typeof a.prompt === 'string' && ['read', 'edit'].includes(a.kind)));
      } catch { /* private browsing or an old draft */ }
      for (const [index, note] of readNotes(id).entries()) {
        const ident = note.id ?? `imported-${index}-${note.path}:${note.line}`;
        if (!items.some(x => x.id === ident)) items.push({ id: ident, path: note.path, from: note.line, to: note.line, old: note.old,
          code: note.excerpt ?? '', sha: null, text: note.text, state: note.state ?? 'open', needsReview: true,
          actions: [{ id: 'custom', key: 6, label: 'Custom instruction', kind: 'edit', prompt: '{{note}}' }] });
      }
      itemsCache.set(id, items);
      try { localStorage.setItem('piper.review.items.' + id, JSON.stringify(items)); localStorage.removeItem(notesKey(id)); } catch { /* retain the original if storage failed */ }
    }
    return itemsCache.get(id)!;
  }
  function saveItems(id: string, items = itemsOf(id)): void {
    itemsCache.set(id, items);
    try { localStorage.setItem('piper.review.items.' + id, JSON.stringify(items)); } catch { /* private browsing */ }
  }
  function clearItems(id: string): void {
    saveItems(id, []);
    try { localStorage.removeItem('piper.review.prompt.' + id); } catch { /* private browsing */ }
  }
  /** back: the review it came from, on its scope and file (N6: Back to review opened "This session's changes") */
  function prepareTask(id: string, only?: ReviewItem[], back?: () => void): void {
    const items = only ?? itemsOf(id).filter(x => x.state === 'open' && (x.actions.length || x.text.trim()));
    if (!items.length) return void deps.toast('Choose review actions or add an instruction first.');
    if (items.some(x => x.needsReview)) return void deps.toast('Verify imported annotations against their current code before preparing the task.');
    const draftKey = 'piper.review.prompt.' + id;
    const signature = JSON.stringify(items);
    let includeCode = false;
    let initial = reviewPrompt(items);
    try { const draft = JSON.parse(localStorage.getItem(draftKey) ?? 'null'); if (draft?.signature === signature) { initial = draft.text; includeCode = draft.includeCode === true; } } catch { /* private browsing */ }
    const prompt = h('textarea', { class: 'review-prompt mono', rows: 18, 'aria-label': 'Generated review prompt', spellcheck: 'false' }, initial) as HTMLTextAreaElement;
    const destination = h('select', { 'aria-label': 'Review task destination' },
      h('option', { value: 'new' }, `New ${deps.session?.(id)?.agent ?? 'agent'} review session`), h('option', { value: 'current' }, 'Current session')) as HTMLSelectElement;
    const status = h('span', { class: 'mut small grow', role: 'status' });
    const dispatch = h('button', { class: 'primary', onclick: () => void sendTask() }, 'Send task');
    const update = () => { status.textContent = markBidi(`${prompt.value.length.toLocaleString()}/20,000 characters · ${items.length} ${plural(items.length, 'target', 'targets', 'targets')}`); dispatch.disabled = !prompt.value.trim() || prompt.value.length > 20000; };
    const saveDraft = () => { update(); try { localStorage.setItem(draftKey, JSON.stringify({ signature, text: prompt.value, includeCode })); } catch { /* private browsing */ } };
    prompt.addEventListener('input', saveDraft); update();
    const attach = h('input', { type: 'checkbox', 'aria-label': 'Include reviewed working code' }) as HTMLInputElement;
    attach.checked = includeCode;
    attach.addEventListener('change', () => {
      const appendix = reviewCodeAppendix(items);
      if (attach.checked && !includeCode) prompt.value += appendix;
      else if (!attach.checked && includeCode) prompt.value = prompt.value.replace(appendix, '');
      includeCode = attach.checked; saveDraft();
    });
    const root = h('div', { class: 'review-task' }, h('div', { class: 'row' }, destination,
      h('label', { class: 'small' }, attach, 'Include code · original side is always included')), prompt, h('div', { class: 'row' }, status,
      // W11: from the review it is a layer over it - Back to review / Esc just goes back, the review stays as it was
      back ? null : h('button', { onclick: deps.closeModal }, 'Close'),
      h('button', { onclick: () => { deps.closeModal(); if (!back) void showReview(id); } }, 'Back to review'), dispatch));
    root.addEventListener('keydown', event => {
      if (sourceClick(event) && event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); if (!dispatch.disabled) void sendTask(); }
    });
    (back ? over() : deps.modal)('Review task · editable prompt', root);
    async function sendTask(): Promise<void> {
      dispatch.disabled = true;
      try {
        const result = (await api.reviewDispatch(id, prompt.value, destination.value as 'new' | 'current', items)).data;
        if (!result?.sent) throw new Error('Task was not sent.');
        const ids = new Set(items.map(x => x.id));
        saveItems(id, itemsOf(id).map(x => ids.has(x.id) ? { ...x, state: 'sent' } : x));
        try { localStorage.removeItem(draftKey); } catch { /* private browsing */ }
        if (back && deps.closeAllModals) deps.closeAllModals(); else deps.closeModal();
        deps.toast('Review task sent; items kept for verification.');
        if (result.session !== id) deps.sessionCreated?.(result.session);
      } catch (error) { status.textContent = markBidi(errText(error)); dispatch.disabled = false; }
    }
  }
  async function chooseActions(id: string, target: ReviewTarget): Promise<void> {
    try {
      const actions = (await api.reviewActions(id)).data?.actions ?? [];
      const box = h('div', { class: 'review-selection', tabindex: 0 });
      const text = h('textarea', { rows: 3, 'aria-label': 'Custom review instruction', placeholder: 'Your specific solution or instruction…' }) as HTMLTextAreaElement;
      const find = () => itemsOf(id).find(x => x.state === 'open' && reviewTargetKey(x) === reviewTargetKey(target));
      text.value = find()?.text ?? '';
      let previous: ReviewItem[] | null = null;
      const undo = h('button', { disabled: true, onclick: () => {
        if (!previous) return;
        saveItems(id, previous); previous = null; undo.disabled = true; text.value = find()?.text ?? ''; drawMenu();
      } }, h('kbd', {}, 'u'), 'Undo');
      const apply = (action: ReviewAction | null) => {
        if (action?.id === 'custom') { text.focus(); return; }
        previous = structuredClone(itemsOf(id)); undo.disabled = false;
        saveItems(id, toggleReviewAction(itemsOf(id), target, action));
        if (action === null) text.value = '';
        if (action?.id === 'custom') text.focus();
        drawMenu();
      };
      const menu = h('div');
      const drawMenu = () => { clear(menu); menu.append(actionMenu(actions, find()?.actions ?? [], apply)); };
      text.addEventListener('input', () => {
        let item = find();
        if (!item) { const custom = actions.find(x => x.id === 'custom') ?? { id: 'custom', key: 6, label: 'Custom instruction', kind: 'edit' as const, prompt: '{{note}}' };
          saveItems(id, toggleReviewAction(itemsOf(id), target, custom)); item = find()!; }
        item.text = text.value; saveItems(id);
      });
      box.addEventListener('keydown', event => {
        if (event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLInputElement) return;
        if (event.key === 'u' && !event.metaKey && !event.ctrlKey && !event.altKey) { event.preventDefault(); event.stopPropagation(); undo.click(); return; }
        const key = reviewDigit(event); if (key === null) return;
        const action = actions.find(x => x.key === key);
        if (key === 0 || action) { event.preventDefault(); event.stopPropagation(); apply(action ?? null); }
      });
      drawMenu(); box.append(h('p', { class: 'mut small' }, `${target.path}:${target.from}–${target.to}${target.old ? ' · original' : ''}`), h('details', {}, h('summary', {}, 'Selected code'), h('pre', { class: 'review-excerpt mono' }, target.code)), menu, text,
        h('div', { class: 'row' }, undo, h('button', { onclick: deps.closeModal }, 'Done'), h('button', { class: 'primary', onclick: () => prepareTask(id) }, 'Prepare task')));
      deps.modal('Code review actions', box); box.focus();
    } catch (error) { deps.toast(errText(error)); }
  }
  function readNotes(id: string): Note[] {
    try {
      const value: unknown = JSON.parse(localStorage.getItem(notesKey(id)) ?? '[]');
      return Array.isArray(value) ? value.filter((n): n is Note => n && typeof n.path === 'string' &&
        Number.isInteger(n.line) && typeof n.text === 'string' && typeof n.old === 'boolean') : [];
    } catch { return []; }
  }
  async function stageFile(id: string, path: string): Promise<void> {
    try {
      const review = (await api.reviewFiles(id, 'head')).data;
      if (!review) throw new Error('Review unavailable');
      await api.reviewStage(id, review.id, path);
      await refresh(id); deps.toast('Added to Git; no commit created.');
    } catch (error) { deps.toast(errText(error)); }
  }
  function openSource(id: string, path: string, line = 1): void {
    deps.openSourceFull?.(id, rel(id, path), line);
  }
  function fileAction(id: string, path: string, show: () => void): (event: Event) => void {
    return (event) => {
      if (sourceClick(event)) { event.preventDefault(); openSource(id, path); }
      else show();
    };
  }
  const of = (id: string): PerSession => {
    let s = state.get(id);
    if (!s) {
      s = { status: null, error: '', selected: new Set(), message: '', loading: false, fetched: 0, notes: readNotes(id), viewed: new Map(), accepted: new Map(),
        turns: null, turnNote: '', turnN: null, turnFiles: new Map(), turnAgain: false, turnStamp: 0, offerMr: false, amend: false, amendFilled: '', genNote: '', genHeld: false, generating: false, pushAfter: false };
      state.set(id, s);
    }
    return s;
  };
  const projectOf = (id: string) => deps.project?.(id) ?? '';
  const rel = (id: string, path: string) => {
    const st = of(id).status;
    return st ? projectRel(path, st.root, deps.projectPath(id)) : path;
  };
  const errText = (e: unknown) => String(e instanceof Error ? e.message : e);

  async function refresh(id = current): Promise<void> {
    const s = of(id);
    s.loading = true;
    if (id === current) drawHead();
    const turns = loadTurns(id);
    try {
      const r = await api.git(id);
      const prev = s.status;
      s.status = r.data;
      s.error = '';
      // keep the user's choices; new files start checked unless the policy marks them or they are OS junk
      const known = new Set(prev?.files.map((f) => f.path) ?? []);
      for (const f of s.status?.files ?? []) if (!known.has(f.path) && !f.sensitive && !isJunk(f.path) && f.x !== '?') s.selected.add(f.path);
      const now = new Set(s.status?.files.map((f) => f.path) ?? []);
      for (const p of [...s.selected]) if (!now.has(p)) s.selected.delete(p);
    } catch (e) {
      s.error = errText(e);
    }
    s.loading = false;
    s.fetched = Date.now();
    if (id === current) draw();
    deps.onStatus?.(id);
    await turns;
    deps.onStatus?.(id);   // the last turn's files (the file browser's Turn scope)
  }

  // ---- turns (9.7 #2)
  const keyOf = (t: TurnSnap) => `${t.from}..${t.to ?? ''}`;

  async function loadFiles(id: string, t: TurnSnap): Promise<void> {
    const s = of(id);
    const key = keyOf(t);
    // a closed turn never changes; an open one is diffed against "now"
    if (t.to !== null && s.turnFiles.has(key)) return;
    const r = turnRange(t);
    try {
      const d = (await api.turnFiles(id, r.from, r.to)).data;
      if (d) s.turnFiles.set(key, d);
    } catch {
      /* shown as "not loaded"; the next refresh retries */
    }
  }

  /** one fetch at a time per session; a call during a fetch runs it once more and waits for that */
  function loadTurns(id: string): Promise<void> {
    const s = of(id);
    const running = turnRuns.get(id);
    if (running) {
      s.turnAgain = true;
      return running;
    }
    const p = (async () => {
      do {
        s.turnAgain = false;
        await fetchTurns(id);
      } while (s.turnAgain);
    })().finally(() => turnRuns.delete(id));
    turnRuns.set(id, p);
    return p;
  }

  async function fetchTurns(id: string): Promise<void> {
    const s = of(id);
    try {
      const r = await api.turns(id);
      s.turns = r.data?.turns ?? [];
      s.turnNote = r.data?.note ?? '';
    } catch (e) {
      // an older piperd without snapshots: the section says so once, nothing else breaks
      if (s.turns === null) s.turnNote = errText(e);
      s.turns = s.turns ?? [];
    }
    const last = lastTurn(s.turns);
    const pick = pickTurn(s.turns, s.turnN);
    await Promise.all([last, pick !== last ? pick : null].filter((t): t is TurnSnap => t !== null).map((t) => loadFiles(id, t)));
    // keep only what can still be shown
    const keep = new Set([last, pick].filter((t): t is TurnSnap => t !== null).map(keyOf));
    for (const k of [...s.turnFiles.keys()]) if (!keep.has(k)) s.turnFiles.delete(k);
    if (id === current) drawTurn();
  }

  function drawTurn(): void {
    const id = current;
    const s = of(id);
    const turns = s.turns ?? [];
    const t = pickTurn(turns, s.turnN);
    const files = t ? s.turnFiles.get(keyOf(t)) : undefined;
    const sig = JSON.stringify([id, turns.map((x) => [x.n, x.to, x.prompt]), s.turnN, t ? keyOf(t) : '', files?.files, s.turnNote, s.status?.root]);
    if (sig === turnSig) return;
    turnSig = sig;
    turnStatus.textContent = markBidi(t ? `${t.n}${files ? ' · ' + (files.files.length ? filesLabel(files.files.length) : 'no files') : ''}${t.to === null ? ' · running' : ''}` : s.turns === null ? '' : 'no turn yet');
    clear(turnBody);
    if (s.turns === null) {
      turnBody.append(h('p', { class: 'mut small' }, 'loading…'));
      return;
    }
    if (!t) {
      turnBody.append(h('p', { class: 'mut small' }, 'No turn with a snapshot yet - one is taken at the session\'s next prompt.'));
      if (s.turnNote && !/^A snapshot is taken/.test(s.turnNote)) turnBody.append(h('p', { class: 'mut small' }, s.turnNote));
      return;
    }
    const pickRow = h('div', { class: 'row left trow' });
    if (turns.length > 1) {
      const sel = h('select', { title: 'older turns' }) as HTMLSelectElement;
      for (const x of [...turns].sort((a, b) => b.n - a.n)) {
        const o = h('option', { value: String(x.n) }, turnLabel(x, 48)) as HTMLOptionElement;
        o.selected = x.n === t.n;
        sel.append(o);
      }
      sel.addEventListener('change', () => {
        const n = Number(sel.value);
        s.turnN = n === lastTurn(turns)?.n ? null : n;
        drawTurn();
        // the file browser's Turn scope follows the picked turn (K7)
        void loadTurns(id).then(() => deps.onStatus?.(id));
      });
      pickRow.append(sel);
    } else pickRow.append(h('span', { class: 'mut small grow', title: t.prompt }, turnLabel(t, 48)));
    pickRow.append(h('button', { class: 'mini', title: 'the whole turn diff, with notes', onclick: () => showTurn(id, t.n) }, 'Full diff'));
    turnBody.append(pickRow);
    turnBody.append(h('p', { class: 'mut small' }, t.to === null ? 'running · diff against the current state' : `${new Date(t.started * 1000).toLocaleTimeString()}–${t.ended ? new Date(t.ended * 1000).toLocaleTimeString() : '?'}`));
    if (!files) {
      turnBody.append(h('p', { class: 'mut small' }, 'loading files…'));
      return;
    }
    if (!files.files.length) turnBody.append(h('p', { class: 'mut small' }, 'The turn changed nothing.'));
    const list = h('ul', { class: 'gitfiles turnfiles' });
    for (const f of files.files) {
      const m = changeMark(f.status);
      list.append(h('li', { class: `${f.sensitive ? 'sensitive' : ''} ch-${m.cls}` },
        markEl(m),
        fileButton(f.path, `${m.word} · ${f.orig ? f.orig + ' → ' : ''}${f.path}${f.sensitive ? ' - ' + HIDDEN_TITLE : ''} - turn diff`,
          fileAction(id, f.path, () => void showReview(id, 'turn', t.n, f.path)), true),
        f.sensitive ? hiddenMark() : h('span'),
        rowActions(id, f.path, m.letter !== 'D', false, null)));
    }
    turnBody.append(list);
  }

  async function showTurn(id: string, n?: number): Promise<void> {
    const s = of(id);
    if (!s.status && !s.loading) await refresh(id);   // the repository root for project-relative note paths
    if (s.turns === null || turnRuns.has(id)) await loadTurns(id);
    const t = pickTurn(s.turns ?? [], n ?? null);
    if (!t) {
      deps.toast(s.turnNote || 'The session has no turn with a snapshot yet.');
      return;
    }
    return void showReview(id, 'turn', t.n);
  }

  /** a git path of the session's repository opened in Piper's editor (closes the review) */
  function openAt(id: string, gitPath: string, line: number): void {
    const p = rel(id, gitPath);
    const project = projectOf(id);
    if (!deps.openFile || !project) return;
    if (p.startsWith('/')) return deps.toast(`${gitPath} is outside the project`);
    deps.closeModal();
    deps.openFile(project, p, line);
  }

  /** Open · Diff · History of a changed file (C6); `diff` null = the row's name is the diff already */
  function rowActions(id: string, gitPath: string, exists: boolean, tracked: boolean, diff: (() => void) | null): HTMLElement {
    const project = projectOf(id);
    return h('span', { class: 'gact' },
      exists && deps.openFile && project ? h('button', { class: 'mini', title: 'open in Piper\'s editor', onclick: () => openAt(id, gitPath, 0) }, 'Open') : null,
      diff ? h('button', { class: 'mini', title: 'review diff (click a line to add a note)', onclick: diff }, 'Diff') : null,
      tracked && project ? h('button', { class: 'mini', title: 'file history and blame', onclick: () => openHistory(deps, project, rel(id, gitPath)) }, 'History') : null);
  }

  function showDiff(path: string, id = current): void {
    void showReview(id, 'head', 0, path);
  }

  // ---- full pinned comparison; Viewed belongs to versions, never just a file name
  async function showReview(id: string, initialScope: 'session' | 'head' | 'branch' | 'turn' = 'session', _initialTurn = 0, initialPath = ''): Promise<void> {
    const state = of(id);
    let data: ReviewFiles | null = null;
    let scope: 'session' | 'head' | 'branch' = initialScope === 'turn' ? 'session' : initialScope;
    let path = initialPath;
    let generation = 0;
    let closed = false;
    let loading = false;
    let ready = false;
    let reviewing = false;
    let viewer: MergeReview | null = null;
    let loaded: [FileBlob, FileBlob] | null = null;
    let templates: ReviewAction[] = [];
    let wholeTarget = false;
    let advancing = false;
    let navigating = false;
    let navigationVersion = 0;
    const navigation: { dir: -1 | 1; change: boolean }[] = [];
    let checking = false;
    let atSummary = false;
    let viewedUndo: { path: string; key: string | undefined } | null = null;
    let writeUndo: { review: string; path: string; text: string | null; sha: string | null } | null = null;
    let itemsUndo: ReviewItem[] | null = null;
    const accepted = state.accepted;
    let acceptedUndo: { key: string; indices: Set<number>; path: string; viewed: string | undefined } | null = null;
    let undoKind: 'write' | 'items' | 'viewed' | 'accepted' | null = null;
    // commit A: one selection - the review ticks the panel's files (the Changes panel and the review agree)
    const selected = state.selected;
    const stale = new Set<string>();
    const expanded = new Set(['Changes']);
    const layout = { fullscreen: false, files: true, actions: false, working: true };
    try {
      const saved = JSON.parse(localStorage.getItem('piper.review.layout') ?? '{}');
      if (saved && typeof saved === 'object') for (const key of Object.keys(layout) as (keyof typeof layout)[]) if (typeof saved[key] === 'boolean') layout[key] = saved[key];
    } catch { /* A broken preference must not prevent reviewing code. */ }
    layout.actions = false;   // F23: the review items open from the foot's count, never by themselves
    let first = true;
    const scopePick = h('select', { 'aria-label': 'Review scope' },
      h('option', { value: 'branch' }, 'Entire branch / MR'),
      h('option', { value: 'head' }, 'Uncommitted changes'),
      h('option', { value: 'session' }, 'This session’s changes')) as HTMLSelectElement;
    scopePick.value = scope;
    const scopeHint = h('span', { class: 'review-scope-hint', hidden: true });   // F23: the explanation is the scope's tooltip
    function describeScope(): void {
      scopeHint.textContent = scope === 'branch' ? 'Branch base → current files, including uncommitted edits.' :
        scope === 'head' ? 'Last commit → current files.' : 'Session start → current files, including committed edits.';
      scopePick.title = markBidi([scopeHint.textContent, pinnedLine].filter(Boolean).join('\n'));
    }
    let pinnedLine = '';
    describeScope();
    const banner = h('div', { class: 'review-banner mut small', role: 'status' });
    const loadingText = h('span', {});
    const loadingStatus = h('div', { class: 'review-loading row left small mut hidden', role: 'status' },
      h('span', { class: 'spinner' }), loadingText,
      h('progress', { 'aria-label': 'Preparing review comparison' }));
    let loadingStarted = 0;
    let loadingPhase = 'Preparing comparison';
    const list = h('div', { class: 'review-list vcs', role: 'navigation', 'aria-label': 'Review files' });
    const code = h('div', { class: 'review-code' });
    const detail = h('div', { class: 'review-detail small' });
    const notes = h('aside', { class: 'review-notes', 'aria-label': 'Review actions and instructions' });
    list.id = `review-files-${id}`; notes.id = `review-actions-${id}`;
    const palette = h('div', { class: 'review-palette' });
    const editor = h('div', { class: 'review-note-edit hidden' });
    const mod = isMac(navigator.platform) ? '⌘' : 'Ctrl+';
    const action = (label: string | HTMLElement, keys: string) => h('span', { class: 'review-action' },
      typeof label === 'string' ? h('span', {}, label) : label, h('kbd', {}, keys));
    const viewedLabel = h('span', {}, 'Viewed');
    const viewed = h('button', { class: 'review-viewed', onclick: toggleViewed, 'aria-label': 'Mark viewed' }, action(viewedLabel, 'v'));
    const open = h('button', { onclick: jumpToSource, title: 'Open source at the caret' }, action('Open in Files', `${mod}↓ / F4`));
    // F23: the foot is one row - progress, the review items as a count, the message, Commit, Prepare task
    const count = h('span', { class: 'mut small' });
    const progress = h('progress', { class: 'review-progress', 'aria-label': 'Viewed files', max: 1, value: 0 }) as HTMLProgressElement;
    const itemsLink = h('button', { class: 'link small review-items-link', 'aria-controls': notes.id, title: `Show / hide the review items · ${mod}⌥2`,
      onclick: () => setLayout('actions', !layout.actions) });
    const commitMessage = h('input', { placeholder: 'Commit message', 'aria-label': 'Review commit message', value: state.message }) as HTMLInputElement;
    commitMessage.addEventListener('input', () => { state.message = commitMessage.value; drawControls(); });
    commitMessage.addEventListener('keydown', (event) => {
      if (sourceClick(event) && event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.stopPropagation(); void commitViewed(); }
    });
    const commit = h('button', { class: 'primary', onclick: () => void commitViewed() }, 'Commit');
    const commitNote = h('span', { class: 'small mut' });
    const stage = h('button', { onclick: () => void addToGit() }, 'Add to Git');
    const restore = h('button', { onclick: () => void restoreBlock(), title: 'Restore the active block or selected working rows from the left version' }, action('↶ Revert', `${mod}⌥Z`));
    const remove = h('button', { onclick: () => void restoreBlock('delete'), title: 'Delete the active block or selected working rows' }, action('× Delete code', `${mod}⌫`));
    const accept = h('button', { onclick: () => void acceptBlock(), title: 'Keep this change and mark the block reviewed' }, action('✓ Accept', 'a'));
    const undo = h('button', { onclick: () => void undoLast(), title: 'Undo the last restore or automatic Viewed' }, action('Undo', 'u'));
    const sendLabel = h('span', {}, 'Prepare task');
    const back = () => void showReview(id, scope, 0, path);
    const send = h('button', { class: 'primary', onclick: () => prepareTask(id, undefined, back), title: 'Preview and edit the combined task' }, action(sendLabel, `${mod}⏎`));
    const fileAt = h('span', { class: 'review-at' }, 'File');
    const changeAt = h('span', { class: 'review-at' }, 'Change');
    const fileToggle = h('button', { 'aria-label': 'Toggle review file list', 'aria-controls': list.id, title: `Show/hide files · ${mod}⌥1`, onclick: () => setLayout('files', !layout.files) }, action('Files', `${mod}⌥1`));
    const actionToggle = h('button', { 'aria-label': 'Toggle review actions', 'aria-controls': notes.id, title: `Show/hide the review items · ${mod}⌥2`, onclick: () => setLayout('actions', !layout.actions) }, action('Review items', `${mod}⌥2`));
    const workingToggle = h('button', { 'aria-label': 'Give working code more space', title: 'More space for working code; toggle for equal-width versions', onclick: () => setLayout('working', !layout.working) }, 'Working focus');
    const fullscreenLabel = h('span', {}, '⛶ Fullscreen');
    const fullscreenToggle = h('button', { 'aria-label': 'Toggle review fullscreen', title: `Use the full app window · ${mod}⇧⏎; Esc to return`, onclick: () => setLayout('fullscreen', !layout.fullscreen) }, action(fullscreenLabel, `${mod}⇧⏎`));
    const refreshButton = h('button', { onclick: () => void refreshReview(), title: 'Pin one fresh comparison', 'aria-label': 'Refresh review' }, 'Refresh comparison');
    // ⋯: everything that is not reading the diff - block operations, Open in Files, the layout toggles
    const more = h('div', { class: 'review-more hidden', role: 'group', 'aria-label': 'More review actions' },
      restore, remove, accept, undo, stage, open, refreshButton, h('hr', {}), fileToggle, actionToggle, workingToggle, fullscreenToggle);
    const moreButton = h('button', { class: 'icon review-more-btn', 'aria-label': 'More review actions', 'aria-haspopup': 'true', 'aria-expanded': 'false', title: 'More: Revert, Delete code, Undo, Open in Files, layout' }, h('i', { class: 'fi fi-more', 'aria-hidden': 'true' }));
    const setMore = (on: boolean) => { more.classList.toggle('hidden', !on); moreButton.setAttribute('aria-expanded', String(on)); };
    moreButton.addEventListener('click', () => setMore(more.classList.contains('hidden')));
    more.addEventListener('click', (event) => { if ((event.target as HTMLElement).closest('button')) setMore(false); });
    // F23: "Ask Claude ▾" beside the active block (or selection), not over its code; its card holds what used to be the 7-tile panel
    const askPop = h('div', { class: 'review-ask-pop hidden' }, palette);
    const askButton = h('button', { class: 'review-ask-btn', 'aria-haspopup': 'true', 'aria-expanded': 'false', title: 'Ask Claude about this change · 1–6' }, 'Ask Claude ▾');
    const ask = h('div', { class: 'review-ask hidden' }, h('div', { class: 'row' }, h('span', { class: 'mut small' }, 'for this change · or 1–6'), askButton), askPop);
    const setAsk = (on: boolean) => { askPop.classList.toggle('hidden', !on); askButton.setAttribute('aria-expanded', String(on)); };
    askButton.addEventListener('click', () => setAsk(askPop.classList.contains('hidden')));
    function placeAsk(): void {
      const at = viewer && targetFor() ? viewer.anchor() : null;
      ask.classList.toggle('hidden', !at);
      if (!at) return setAsk(false);
      ask.style.top = `${Math.max(0, at.top - 3)}px`; ask.style.right = `${at.right + 8}px`;
    }
    code.addEventListener('scroll', () => placeAsk(), true);
    const popsOut = (event: Event) => {   // a click elsewhere closes ⋯ and Ask Claude ▾
      const at = event.target as Node;
      if (!moreButton.parentElement!.contains(at)) setMore(false);
      if (!ask.contains(at)) setAsk(false);
    };
    const root = h('div', { class: 'reviewpane', tabindex: 0 },
      h('div', { class: 'review-toolbar row' }, scopePick,
        h('div', { class: 'review-nav', role: 'group', 'aria-label': 'File navigation' }, fileAt,
          h('button', { class: 'review-key', onclick: () => stepFile(-1), 'aria-label': 'Previous file', title: `Previous file · ${mod}⇧[` }, h('kbd', {}, 'p')),
          h('button', { class: 'review-key', onclick: () => stepFile(1), 'aria-label': 'Next file', title: `Next file · ${mod}⇧]` }, h('kbd', {}, 'j'))),
        h('div', { class: 'review-nav', role: 'group', 'aria-label': 'Change navigation' }, changeAt,
          h('button', { class: 'review-key', onclick: () => void stepChange(-1), title: 'Previous change' }, h('kbd', {}, '⇧F7')),
          h('button', { class: 'review-key', onclick: () => void stepChange(1), title: 'Next change' }, h('kbd', {}, 'F7'))),
        h('span', { class: 'grow' }), viewed, h('div', { class: 'review-more-wrap' }, moreButton, more),
        h('button', { class: 'review-close', 'aria-label': 'Close review', title: 'Close review · Esc', onclick: close }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }))),
      scopeHint, loadingStatus, banner,
      h('div', { class: 'review-body' }, list, h('div', { class: 'review-center' }, detail, h('div', { class: 'review-code-wrap' }, code, ask)), notes),
      h('div', { class: 'review-footer row' }, count, itemsLink, progress, h('span', { class: 'grow' }), commitNote, commitMessage, commit, send));
    deps.modal('Review', root);
    root.addEventListener('mousedown', popsOut);
    const heading = root.parentElement!.querySelector('h2');
    if (heading) root.querySelector('.review-toolbar')!.prepend(heading);
    function drawLayout(): void {
      root.classList.toggle('review-fullscreen', layout.fullscreen);
      root.classList.toggle('review-files-hidden', !layout.files);
      root.classList.toggle('review-actions-hidden', !layout.actions);
      root.classList.toggle('review-working-focus', layout.working);
      fileToggle.setAttribute('aria-expanded', String(layout.files));
      actionToggle.setAttribute('aria-expanded', String(layout.actions));
      workingToggle.setAttribute('aria-pressed', String(layout.working));
      fullscreenToggle.setAttribute('aria-pressed', String(layout.fullscreen));
      fullscreenLabel.textContent = layout.fullscreen ? '⛶ Exit fullscreen' : '⛶ Fullscreen';
      itemsLink.setAttribute('aria-expanded', String(layout.actions));
    }
    function setLayout(key: keyof typeof layout, value: boolean): void {
      layout[key] = value; drawLayout();
      try { localStorage.setItem('piper.review.layout', JSON.stringify({ ...layout, actions: false })); } catch { /* Layout still works when storage is unavailable. */ }
      viewer?.focus(); if (!viewer) root.focus();
    }
    function revealActions(): void { if (!layout.actions) setLayout('actions', true); }
    drawLayout();
    const observer = new MutationObserver(() => { if (!root.isConnected) cleanup(); });
    observer.observe(root.closest('#overlay') ?? root.parentElement!.parentElement!, { childList: true });   // W11: the layer goes, not a child of it
    const timer = window.setInterval(() => void checkLive(), 5000);
    const loadingTimer = window.setInterval(() => {
      if (loading) loadingText.textContent = markBidi(`${loadingPhase}… ${Math.floor((performance.now() - loadingStarted) / 1000)} s · Large changes may take a while.`);
    }, 1000);
    scopePick.addEventListener('change', () => {
      const value = scopePick.value;
      if (!['session', 'head', 'branch'].includes(value)) return;
      scope = value as typeof scope;
      describeScope();
      path = '';
      void refreshReview();
    });
    // Capture before CodeMirror's ⌘↓ moves the caret to the document end.
    root.parentElement!.addEventListener('keydown', (event) => {
      const target = event.target as HTMLElement;
      const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
      if (typing) return;
      if (event.isComposing || target.closest('select')) return;
      if (event.key === 'Escape' && (!more.classList.contains('hidden') || !askPop.classList.contains('hidden'))) { event.preventDefault(); event.stopPropagation(); setMore(false); setAsk(false); viewer?.focus(); return; }
      if (event.key === 'Escape' && !editor.classList.contains('hidden')) { event.preventDefault(); event.stopPropagation(); clear(editor); editor.classList.add('hidden'); viewer?.focus(); return; }
      if (event.key === 'Escape' && layout.fullscreen) { event.preventDefault(); event.stopPropagation(); setLayout('fullscreen', false); return; }
      if (sourceClick(event) && event.shiftKey && event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); setLayout('fullscreen', !layout.fullscreen); return; }
      if (sourceClick(event) && event.altKey && (event.code === 'Digit1' || event.code === 'Digit2')) { event.preventDefault(); event.stopPropagation(); const key = event.code === 'Digit1' ? 'files' : 'actions'; setLayout(key, !layout[key]); return; }
      if (sourceClick(event) && event.key === 'Enter') { event.preventDefault(); event.stopPropagation();
        const apply = editor.classList.contains('hidden') ? null : editor.querySelector<HTMLButtonElement>('button.primary');
        if (apply && !apply.disabled) apply.click(); else if (!send.disabled) prepareTask(id, undefined, back); return; }
      if (sourceClick(event) && event.altKey && event.key.toLowerCase() === 'z') { event.preventDefault(); event.stopPropagation(); void restoreBlock(); return; }
      if (sourceClick(event) && event.key === 'Backspace') { event.preventDefault(); event.stopPropagation(); void restoreBlock('delete'); return; }
      if (sourceClick(event) && !event.altKey && event.key.toLowerCase() === 'z') { event.preventDefault(); event.stopPropagation(); void undoLast(); return; }
      const digit = reviewDigit(event);
      if (digit !== null && viewer && ready) {
        const chosen = templates.find(x => x.key === digit);
        if (digit === 0 || chosen) { event.preventDefault(); event.stopPropagation(); choose(chosen ?? null); return; }
      }
      if (event.key === 'F7') { event.preventDefault(); event.stopPropagation(); void stepChange(event.shiftKey ? -1 : 1); }
      else if (event.key === 'F4' || (sourceClick(event) && event.key === 'ArrowDown')) { event.preventDefault(); event.stopPropagation(); jumpToSource(); }
      else if (sourceClick(event) && event.shiftKey && (event.code === 'BracketRight' || event.code === 'BracketLeft')) {
        event.preventDefault(); event.stopPropagation(); stepFile(event.code === 'BracketRight' ? 1 : -1);
      } else if (!event.metaKey && !event.ctrlKey && !event.altKey) {
        if (event.key === 'j' || event.key === 'p') { event.preventDefault(); event.stopPropagation(); stepFile(event.key === 'j' ? 1 : -1); }
        else if (event.key === 'a') { event.preventDefault(); event.stopPropagation(); void acceptBlock(); }
        else if (event.key === 'v') { event.preventDefault(); event.stopPropagation(); toggleViewed(); }
        else if (event.key === 'n') { event.preventDefault(); event.stopPropagation(); const c = viewer?.caret(); if (c) addAt(c.line, c.old, c.excerpt); }
        else if (event.key === 'u') { event.preventDefault(); event.stopPropagation(); void undoLast(); }
      }
    }, true);
    function cleanup(): void {
      if (closed) return;
      closed = true;
      generation++;
      clearInterval(timer); clearInterval(loadingTimer);
      observer.disconnect();
      viewer?.destroy();
      viewer = null;
    }
    function close(): void { cleanup(); deps.closeModal(); }
    function currentFile(): ReviewFile | undefined { return data?.files.find((file) => file.path === path); }
    function targetFor(): ReviewTarget | null {
      const file = currentFile();
      // a file hidden from Claude: reviewed by the human, never a target of a session's task
      if (!viewer || !file || !ready || file.withheld || file.sensitive) return null;
      return { ...viewer.target(wholeTarget), path: rel(id, file.path), sha: file.work_sha ?? null };
    }
    function drawTarget(): void {
      clear(palette);
      const target = targetFor();
      const pos = viewer?.position(), fileIndex = ordered().findIndex(x => x.path === path);
      fileAt.textContent = markBidi(ordered().length ? `File ${fileIndex + 1}/${ordered().length}` : 'File');
      changeAt.textContent = markBidi(pos?.total ? `Change ${pos.index}/${pos.total}` : 'Change');
      placeAsk();
      if (!target) return void palette.append(h('p', { class: 'mut small' }, currentFile()?.sensitive
        ? 'Hidden from Claude: review it yourself - no session task can name this file.' : 'Choose a text file to attach actions.'));
      const found = itemsOf(id).find(x => x.state === 'open' && reviewTargetKey(x) === reviewTargetKey(target));
      const selected = viewer?.hasSelection() ?? false;
      const scopeLabel = wholeTarget ? 'Whole file' : selected ? 'Selection' : 'Change block';
      palette.append(h('div', { class: 'row small' }, h('strong', { class: 'grow', 'aria-live': 'polite' }, `${target.old ? 'Original' : 'Working'} · ${scopeLabel} · ${target.from}–${target.to}`)),
        h('div', { class: 'review-target-controls' },
          selected ? h('button', { class: 'mini', onclick: () => { if (wholeTarget) wholeTarget = false; else viewer?.clearSelection(); drawTarget(); } }, wholeTarget ? 'Use selection' : 'Use change block') : null,
          h('button', { class: 'mini', 'aria-pressed': String(wholeTarget), onclick: () => { wholeTarget = !wholeTarget; drawTarget(); } }, wholeTarget ? 'Whole file ✓' : 'Whole file')),
        actionMenu(templates, found?.actions ?? [], choose),
        h('p', { class: 'mut small review-selection-hint' }, 'Select specific lines with the mouse or Shift + arrows. Actions and comments use that selection.'));
    }
    function choose(action: ReviewAction | null): void {
      const target = targetFor();
      if (!target) return;
      itemsUndo = structuredClone(itemsOf(id)); undoKind = 'items';
      if (action?.id === 'custom') {
        addAt(target.from, target.old, target.code);
      } else saveItems(id, toggleReviewAction(itemsOf(id), target, action));
      drawNotes(); drawControls(); viewer?.notesChanged();
      if (action?.id === 'custom') focusInstruction(); else viewer?.focus();
      if (action === null) { editor.classList.add('hidden'); clear(editor); deps.toast('Actions and custom instruction cleared. Undo: u'); }
    }
    function ordered(): ReviewFile[] { return reviewNavigationOrder(data?.files ?? []).filter((file) => !file.withheld); }
    function isViewed(file: ReviewFile): boolean { return !!data && state.viewed.get(file.path) === reviewKey(file, data.head) && !stale.has(file.path); }
    function noteLines(old: boolean): Map<number, string> {
      const file = currentFile();
      if (!file) return new Map();
      const name = rel(id, old ? file.orig ?? file.path : file.path);
      return new Map(itemsOf(id).filter(item => (item.path === name || item.path === rel(id, file.path)) && item.old === old).map((item): [number, string] => [item.from, item.state]));
    }
    function drawNotes(): void {
      const focus = notes.contains(document.activeElement) ? document.activeElement as HTMLElement : null;
      const selection = focus instanceof HTMLTextAreaElement ? [focus.selectionStart, focus.selectionEnd] as const : null;
      const scroll = notes.scrollTop;
      clear(notes);
      notes.append(editor); drawTarget();
      notes.append(h('div', { class: 'row left' }, h('strong', { class: 'grow' }, `Review items (${itemsOf(id).length})`),
        itemsOf(id).length ? h('button', { class: 'mini', 'aria-label': 'Clear all review items', title: 'Remove all actions and comments in this session · Undo: u', onclick: () => {
          itemsUndo = structuredClone(itemsOf(id)); undoKind = 'items'; clearItems(id);
          editor.classList.add('hidden'); clear(editor); drawNotes(); drawControls(); viewer?.notesChanged(); viewer?.focus();
        } }, 'Clear all') : null));
      for (const item of itemsOf(id)) {
        const row = data?.files.find(x => rel(id, x.path) === item.path);
        const changed = item.needsReview || row && (row.work_sha !== item.sha || stale.has(row.path));
        notes.append(h('div', { class: 'review-note note-' + item.state },
          h('div', { class: 'row small' }, h('code', { class: 'grow' }, showBidi(`${item.path}:${item.from}–${item.to}${item.old ? ' (original)' : ''}`)),
            h('span', { class: changed ? 'warn' : 'note-state' }, changed ? 'Changed · verify again' : item.state)),
          h('div', { class: 'review-badges' }, ...item.actions.map(a => h('span', { class: 'review-badge' }, `${a.key} ${a.label}`))),
          item.text ? h('p', { class: 'small' }, showBidi(item.text)) : null,   // what goes into the prompt, as it is
          h('div', { class: 'row' }, h('button', { class: 'mini', onclick: () => { item.state = item.state === 'resolved' ? 'open' : 'resolved'; saveItems(id); drawNotes(); drawControls(); viewer?.notesChanged(); } }, item.state === 'resolved' ? 'Reopen' : 'Resolve'),
            h('button', { class: 'mini', onclick: () => { saveItems(id, itemsOf(id).filter(x => x !== item)); drawNotes(); drawControls(); viewer?.notesChanged(); } }, 'Remove'),
            changed && row?.path === path ? h('button', { class: 'mini', onclick: () => { const target = targetFor(); if (target) { Object.assign(item, target, { state: 'open', needsReview: false }); saveItems(id); drawNotes(); drawControls(); } } }, 'Use current target') : null)));
      }
      if (!itemsOf(id).length) notes.append(h('p', { class: 'mut small' }, 'Select code or a change block, then Ask Claude ▾ (1–6). Press 6 or n for your specific solution.'));
      notes.scrollTop = scroll;
      if (focus?.isConnected) { focus.focus({ preventScroll: true }); if (selection && focus instanceof HTMLTextAreaElement) focus.setSelectionRange(...selection); }
    }
    function drawControls(): void {
      const file = currentFile();
      viewedLabel.textContent = file && isViewed(file) ? '✓ Viewed' : 'Viewed';
      viewed.setAttribute('aria-pressed', String(!!file && isViewed(file)));
      viewed.disabled = reviewing || loading || !ready || !file || file.withheld || stale.has(file.path) || loaded?.some((blob) => blob.state === 'too_large' || blob.state === 'unsupported') === true;
      open.disabled = reviewing || loading || !file || file.withheld || file.status === 'D';
      stage.classList.toggle('hidden', file?.live_status !== '??');
      stage.disabled = reviewing || loading || !file || !ready || stale.has(file.path) || !file.after?.oid;
      restore.disabled = reviewing || loading || !viewer || !ready || !file || (!file.work_sha && loaded?.[1].state !== 'missing') || !viewer.position().total || stale.has(file.path) || loaded?.some(x => x.mode === '120000') === true;
      remove.disabled = restore.disabled || viewer?.target().old === true;
      accept.disabled = restore.disabled;
      viewer?.operations(!restore.disabled);
      undo.disabled = !undoKind || reviewing || loading || !ready;
      scopePick.disabled = reviewing || loading;
      const picks = data?.files.filter((file) => selected.has(file.path)) ?? [];
      const missing = picks.filter((file) => !isViewed(file)).length;
      const outside = selected.size - picks.length;   // checked in the panel, not in this comparison (another scope)
      const strict = !!deps.requireReview?.();
      const seen = data?.files.filter(isViewed).length ?? 0, visible = data?.files.filter((file) => !file.withheld).length ?? 0;
      count.textContent = markBidi(`${seen}/${visible} viewed${missing ? ` · ${missing} checked not viewed` : ''} ·`);
      progress.max = Math.max(1, visible); progress.value = seen;
      const items = itemsOf(id).length;
      itemsLink.textContent = markBidi(`${items} review ${items === 1 ? 'item' : 'items'}`);
      commit.title = markBidi(`${selected.size} checked in the file list`);
      commit.textContent = markBidi(`${state.amend ? 'Amend' : 'Commit'} ${picks.length} ${picks.length === 1 ? 'file' : 'files'}`);
      commitNote.textContent = markBidi(outside ? `${outside} checked outside this comparison - choose Uncommitted changes` : strict && missing ? 'Review required: view every checked file' : '');
      commitNote.className = `small ${outside || (strict && missing) ? 'warn' : 'mut'}`;
      commit.disabled = reviewing || loading || !state.message.trim() || !picks.length || !!outside || (strict && !!missing) || picks.some((file) => !file.committable);
      const pending = itemsOf(id).filter(item => item.state === 'open' && (item.actions.length || item.text.trim())).length;
      sendLabel.textContent = markBidi(`Prepare task (${pending})`);
      send.disabled = reviewing || !pending;
      const primary = reviewPrimary(pending);
      send.classList.toggle('primary', primary === 'send');
      commit.classList.toggle('primary', primary === 'commit');
    }
    function drawList(follow = false): void {
      const scroll = list.scrollTop;
      clear(list);
      // F23: Lock files / Generated / Scratch / Withheld fold into one quiet line under the real changes
      const rest = reviewGroups.slice(2).filter((group) => data?.files.some((file) => reviewGroup(file) === group));
      const restFiles = data?.files.filter((file) => rest.includes(reviewGroup(file))).length ?? 0;
      const other = h('details', { class: 'review-group review-rest', open: expanded.has('Other') });
      other.addEventListener('toggle', () => { if ((other as HTMLDetailsElement).open) expanded.add('Other'); else expanded.delete('Other'); });
      other.append(h('summary', { title: rest.join(' · ') }, `${rest.map((group) => group.replace(' files', '')).join(' · ')} · ${restFiles} more`));
      for (const group of reviewGroups) {
        const files = reviewOrder(data?.files.filter((file) => reviewGroup(file) === group) ?? []);
        if (!files.length) continue;
        const section = h('details', { class: 'review-group', open: expanded.has(group) });
        section.addEventListener('toggle', () => { if ((section as HTMLDetailsElement).open) expanded.add(group); else expanded.delete(group); });
        section.append(h('summary', {}, h('span', { class: 'review-group-name' }, group), h('span', { class: 'review-group-count' }, ` (${files.length})`)));
        for (const file of files) {
          const mark = changeMark(file.live_status === '??' ? '??' : file.status);
          const cb = h('input', { type: 'checkbox', 'aria-label': 'Select ' + showBidi(file.path),
            disabled: file.withheld || !file.committable }) as HTMLInputElement;
          cb.checked = selected.has(file.path);
          cb.addEventListener('change', () => { if (cb.checked) selected.add(file.path); else selected.delete(file.path); drawControls(); if (id === current) draw(); });
          const label = showBidi(file.orig ? `${file.orig} → ${file.path}` : file.path);
          const button = h('button', { class: 'review-file-name', title: file.sensitive ? label + ' - ' + HIDDEN_TITLE : label, 'aria-label': 'Review ' + showBidi(file.path),
            disabled: file.withheld, onclick: (event: Event) => {
              if (sourceClick(event)) { if (file.status !== 'D') { close(); openSource(id, file.path); } }
              else { navigation.length = 0; navigationVersion++; void selectFile(file.path); }
            } }, showBidi(file.path.slice(file.path.lastIndexOf('/') + 1)));   // F23: the name; the path is its tooltip
          const statistics = file.add === null || file.del === null ? 'binary' : `+${file.add ?? 0} −${file.del ?? 0}`;
          section.append(h('div', { class: `review-file ch-${mark.cls}${file.path === path ? ' active' : ''}${stale.has(file.path) ? ' stale' : ''}` },
            cb, markEl(mark), button, file.sensitive ? hiddenMark() : null,
            h('span', { class: 'review-file-state small', title: stale.has(file.path) ? 'Changed since review; refresh' : isViewed(file) ? 'Viewed version' : 'Not viewed' },
              stale.has(file.path) ? 'Changed' : isViewed(file) ? '✓' : ''),
            h('span', { class: 'review-numstat small mut' }, file.withheld ? 'withheld' : statistics)));
        }
        (rest.includes(group) ? other : list).append(section);
      }
      if (rest.length) list.append(other);
      list.scrollTop = scroll;
      if (follow) list.querySelector(".review-file.active")?.scrollIntoView({ block: "nearest" });
    }
    function unfoldGroup(group: string): void { expanded.add(group); if (reviewGroups.indexOf(group) > 1) expanded.add('Other'); }
    function stepFile(dir: -1 | 1): void { queueNavigation(dir, false); }
    function stepChange(dir: -1 | 1): void { queueNavigation(dir, true); }
    function queueNavigation(dir: -1 | 1, change: boolean): void {
      if (closed || loading || reviewing || !data || navigation.length >= 64) return;
      navigation.push({ dir, change }); void runNavigation();
    }
    async function runNavigation(): Promise<void> {
      if (navigating || !ready || loading || closed) return;
      navigating = true;
      try {
        while (navigation.length && ready && !loading && !closed) {
          const { dir, change } = navigation.shift()!;
          if (change && atSummary) { if (dir < 0) { atSummary = false; viewer?.edge(-1); } continue; }
          if (change && viewer?.change(dir)) { drawTarget(); continue; }
          await advance(dir, change);
        }
      } finally { navigating = false; if (navigation.length && ready && !loading && !closed) void runNavigation(); }
    }
    async function advance(dir: -1 | 1, change: boolean): Promise<void> {
      if (!ready || advancing || reviewing || loading || !data) return;
      const files = ordered();
      if (!files.length) return;
      const index = files.findIndex((file) => file.path === path);
      const previous = currentFile()!;
      const requested = navigationVersion, pinned = data.id;
      const eligible = dir > 0 && !!viewer && !viewed.disabled && loaded?.every(x => x.mode !== '120000' && (x.state === 'text' || x.state === 'missing'));
      advancing = true;
      try {
        let canMark = false;
        if (eligible) {
          const check = (await api.reviewCheck(id, pinned, previous.path)).data;
          if (closed || data?.id !== pinned || navigationVersion !== requested) return;
          canMark = !!check && !check.head_changed && !check.changed.includes(previous.path) && !(previous.orig && check.changed.includes(previous.orig));
          if (!canMark) { stale.add(previous.path); state.viewed.delete(previous.path); }
        }
        const next = files[index + dir];
        if (next) {
          unfoldGroup(reviewGroup(next));
          await selectFile(next.path);
          if (!ready || path !== next.path || navigationVersion !== requested) return;
          atSummary = false;
          if (change) viewer?.edge(dir);
        } else if (dir > 0) {
          atSummary = true;
          banner.textContent = 'Review traversal complete. Prepare the task, revisit unchecked files, or commit selected viewed versions.';
          send.focus();
        } else return;
        if (canMark && !isViewed(previous)) {
          viewedUndo = { path: previous.path, key: state.viewed.get(previous.path) }; undoKind = 'viewed';
          state.viewed.set(previous.path, reviewKey(previous, data.head));
        }
        drawList(true); drawControls();
      } catch (error) { deps.toast(errText(error)); }
      finally { advancing = false; }
    }
    async function addToGit(): Promise<void> {
      if (!data || !currentFile() || stage.disabled) return;
      reviewing = true; drawControls();
      try { await api.reviewStage(id, data.id, path); await refresh(id); await refreshReview(); deps.toast('Added the reviewed version to Git.'); }
      catch (error) { showOperationError(error); }
      finally { reviewing = false; drawControls(); }
    }
    async function acceptBlock(): Promise<void> {
      const file = currentFile();
      if (!data || !viewer || !file || accept.disabled) return;
      reviewing = true; drawControls();
      try {
        const check = (await api.reviewCheck(id, data.id, file.path)).data;
        if (!check || check.head_changed || check.changed.includes(file.path) || file.orig && check.changed.includes(file.orig)) throw new Error('File changed since review; refresh before accepting.');
        const key = reviewKey(file, data.head), pos = viewer.position();
        acceptedUndo = { key, indices: new Set(accepted.get(key)), path: file.path, viewed: state.viewed.get(file.path) }; undoKind = 'accepted';
        const indices = new Set(accepted.get(key)); indices.add(pos.index); accepted.set(key, indices);
        if (indices.size === pos.total) state.viewed.set(file.path, key);
        banner.textContent = markBidi(`Accepted ${indices.size}/${pos.total} change blocks in ${file.path}; working code kept.`);
        drawList(); drawControls();
      } catch (error) { showOperationError(error); }
      finally { reviewing = false; drawControls(); }
    }
    function showOperationError(error: unknown): void {
      revealActions(); clear(editor); editor.classList.remove('hidden');
      editor.append(h('p', { class: 'bad', role: 'alert' }, errText(error)), h('button', { onclick: () => { clear(editor); editor.classList.add('hidden'); viewer?.focus(); } }, 'Close'));
      window.requestAnimationFrame(() => { if (!closed) editor.scrollIntoView({ block: 'nearest' }); });
    }
    async function restoreBlock(operation: 'revert' | 'delete' = 'revert'): Promise<void> {
      const file = currentFile();
      if (!data || !viewer || !file || restore.disabled) return;
      reviewing = true; drawControls();
      try {
        const replacement = operation === 'delete' ? viewer.remove() : viewer.restore();
        let read = file.work_sha ? (await api.read(data.project, rel(id, file.path))).data : null;
        if (file.work_sha && (!read || read.sha !== file.work_sha)) throw new Error('File changed since review; refresh before restoring.');
        while (read && read.lines.length < read.total_lines) {
          const more: FileText | null = (await api.read(data.project, rel(id, file.path), read.lines.length)).data;
          if (!more?.lines.length || more.sha !== read.sha) throw new Error('File changed while preparing the restore.');
          read = { ...read, lines: [...read.lines, ...more.lines] };
        }
        const normalized = read ? read.lines.map(raw => raw.slice(raw.indexOf('\t') + 1)).join('\n') + (read.total_lines && read.final_newline ? '\n' : '') : '';
        if (normalized !== replacement.right) throw new Error('The historical comparison differs from the current working file. Open HEAD review to restore this block.');
        const crlf = read ? read.eol === '\r\n' : loaded?.[0].text?.includes('\r\n');
        const before = read ? crlf ? normalized.replace(/\n/g, '\r\n') : normalized : null;
        const remove = loaded?.[0].state === 'missing' && replacement.text === '';
        const after = remove ? null : crlf ? replacement.text.replace(/\n/g, '\r\n') : replacement.text;
        const pinned = data.id, name = file.path, sha = file.work_sha ?? null;
        revealActions(); clear(editor); editor.classList.remove('hidden');
        editor.append(h('strong', {}, operation === 'delete' ? `Delete the selected working code in ${name}?` : `Revert ${name} to ${scope === 'head' ? 'HEAD' : scope === 'branch' ? 'the branch base' : 'session start'}?`),
          h('pre', { class: 'review-excerpt mono', tabindex: 0 }, after ?? 'Remove this added file (kept in Piper trash; Undo available).'),
          h('div', { class: 'row' }, h('button', { onclick: () => { editor.classList.add('hidden'); clear(editor); viewer?.focus(); } }, 'Cancel'),
            h('button', { class: 'primary', onclick: () => void apply() }, operation === 'delete' ? 'Delete code' : 'Revert')));
        editor.querySelector<HTMLButtonElement>('button.primary')?.focus({ preventScroll: true });
        window.requestAnimationFrame(() => { if (!closed) editor.scrollIntoView({ block: 'nearest', inline: 'nearest' }); });
        async function apply(): Promise<void> {
          if (reviewing || loading) return;
          reviewing = true; drawControls();
          const applyButton = editor.querySelector<HTMLButtonElement>('button.primary');
          if (applyButton) { applyButton.disabled = true; applyButton.textContent = 'Applying…'; }
          try {
            const result = (await api.reviewWrite(id, pinned, name, after, sha)).data;
            if (!result) throw new Error('Restore failed.');
            writeUndo = { review: pinned, path: name, text: before, sha: result.sha }; undoKind = 'write';
            await refreshReview(); await refresh(id);
            clear(editor); editor.classList.add('hidden');
            deps.toast(operation === 'delete' ? 'Code deleted. Undo: u' : 'Block reverted. Undo: u');
          } catch (error) { showOperationError(error); }
          finally { reviewing = false; drawControls(); }
        }
      } catch (error) { showOperationError(error); }
      finally { reviewing = false; drawControls(); }
    }
    async function undoLast(): Promise<void> {
      if (reviewing || loading) return;
      reviewing = true; drawControls();
      try {
        if (undoKind === 'write' && writeUndo) {
          await api.reviewWrite(id, writeUndo.review, writeUndo.path, writeUndo.text, writeUndo.sha);
          await refreshReview(); await refresh(id);
        } else if (undoKind === 'accepted' && acceptedUndo) {
          accepted.set(acceptedUndo.key, acceptedUndo.indices);
          if (acceptedUndo.viewed) state.viewed.set(acceptedUndo.path, acceptedUndo.viewed); else state.viewed.delete(acceptedUndo.path);
          drawList();
        } else if (undoKind === 'items' && itemsUndo) { saveItems(id, itemsUndo); drawNotes(); viewer?.notesChanged(); }
        else if (undoKind === 'viewed' && viewedUndo) {
          if (viewedUndo.key) state.viewed.set(viewedUndo.path, viewedUndo.key); else state.viewed.delete(viewedUndo.path);
          drawList();
        }
        undoKind = null;
      } catch (error) { showOperationError(error); }
      finally { reviewing = false; drawControls(); }
    }
    function jumpToSource(): void {
      const file = currentFile();
      if (!file || file.withheld || file.status === 'D') return;
      const line = viewer?.sourceLine() ?? 1;
      close(); openSource(id, file.path, line);
    }
    function toggleViewed(): void {
      const file = currentFile();
      if (!file || viewed.disabled || !data) return;
      if (isViewed(file)) state.viewed.delete(file.path);
      else state.viewed.set(file.path, reviewKey(file, data.head));
      drawList(); drawControls();
    }
    function addAt(line: number, old: boolean, excerpt: string): void {
      const file = currentFile();
      if (!file || file.withheld) return;
      if (file.sensitive) { deps.toast('Not for a session: ' + HIDDEN_TITLE); return; }
      const target = targetFor() ?? { path: rel(id, file.path), from: line, to: line, old, code: excerpt, sha: file.work_sha ?? null };
      const existing = itemsOf(id).find(x => x.state === 'open' && reviewTargetKey(x) === reviewTargetKey(target));
      clear(editor); editor.classList.remove('hidden');
      const name = rel(id, old ? file.orig ?? file.path : file.path);
      const text = h('textarea', { rows: 3, placeholder: 'Your specific solution or instruction…', 'aria-label': 'Custom review instruction' }, existing?.text ?? '') as HTMLTextAreaElement;
      const cancel = () => { editor.classList.add('hidden'); clear(editor); viewer?.focus(); };
      const save = () => {
        if (text.value.trim()) {
          let item = existing;
          const custom = templates.find(x => x.id === 'custom') ?? { id: 'custom', key: 6, label: 'Custom instruction', kind: 'edit' as const, prompt: '{{note}}' };
          if (!item?.actions.some(x => x.id === custom.id)) {
            saveItems(id, toggleReviewAction(itemsOf(id), target, custom));
            item = itemsOf(id).find(x => x.state === 'open' && reviewTargetKey(x) === reviewTargetKey(target))!;
          }
          item.text = text.value.trim(); saveItems(id);
          drawNotes(); drawControls(); viewer?.notesChanged();
        }
        cancel();
      };
      text.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && sourceClick(event)) { event.preventDefault(); event.stopPropagation(); save(); }
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancel(); }
      });
      editor.append(h('div', { class: 'mut small' }, showBidi(`${name}:${target.from}–${target.to}${target.old ? ' · original' : ''}`)),
        h('details', { class: 'review-instruction-code' }, h('summary', { class: 'mut small' }, `Selected code · ${target.from}–${target.to}`), h('pre', { class: 'review-excerpt mono small' }, showBidi(target.code))), text,
        h('div', { class: 'row' }, h('button', { class: 'primary', onclick: save }, action('Save instruction', `${mod}Enter`)), h('button', { onclick: cancel }, action('Cancel', 'Esc'))));
      focusInstruction();
    }
    function focusInstruction(): void {
      revealActions();
      const text = editor.querySelector('textarea');
      text?.focus({ preventScroll: true });
      window.requestAnimationFrame(() => {
        if (!closed && text?.isConnected) editor.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      });
    }
    async function commitViewed(): Promise<void> {
      if (!data || commit.disabled || reviewing) return;
      const pinned = data;
      const plan = commitPlan(selected, pinned, state.viewed);
      if (plan.missing.length || plan.blocked.length) { banner.textContent = markBidi(`${[...plan.missing, ...plan.blocked].join(', ')}: not committable in this comparison.`); return; }
      const busyHere = deps.workingIn?.(id) ?? [];
      if (busyHere.length) confirmWorking(busyHere, selected.size, () => void go(), over());
      else await go();
      async function go(): Promise<void> {
        if (closed || data !== pinned || reviewing) return;
        const push = state.pushAfter;
        state.pushAfter = false;
        reviewing = true; drawControls();
        try {
          const result = await api.commit(id, state.message, Object.keys(plan.versions), { review: pinned.id, head: pinned.head, versions: plan.versions }, state.amend);
          state.message = ''; commitMessage.value = ''; state.amend = false; state.amendFilled = ''; state.genNote = ''; state.genHeld = false;
          selected.clear();
          close();   // commit A: the commit is done - the review gives way (Push… is in the toast)
          await refresh(id);
          committed(id, result.data, push);
        } catch (error) {
          // only what changed loses Viewed (checkLive marks those files stale); the rest stays viewed
          banner.textContent = markBidi('Commit refused: ' + errText(error) + ' · Refresh and review the changed files.');
          reviewing = false; await checkLive(); drawList();
        } finally { reviewing = false; if (!closed) drawControls(); }
      }
    }
    async function checkLive(): Promise<void> {
      if (!data || closed || loading || reviewing || checking) return;
      checking = true;
      const pinned = data.id;
      try {
        const result = (await api.reviewCheck(id, pinned)).data;
        if (!result || closed || data?.id !== pinned) return;
        const was = stale.size;
        for (const file of data.files) if (result.head_changed || result.changed.includes(file.path) || (file.orig && result.changed.includes(file.orig))) {
          stale.add(file.path); state.viewed.delete(file.path);
        }
        if (result.head_changed || stale.size) banner.textContent = markBidi(`${result.head_changed ? 'HEAD changed. ' : ''}${stale.size} file(s) changed since this pinned review. Refresh to review their new versions.`);
        if (stale.size !== was) { drawList(); if (!editor.contains(document.activeElement)) drawNotes(); }
        drawControls();
      } catch (error) { if (!closed) { banner.textContent = markBidi(errText(error)); state.viewed.clear(); drawControls(); } }
      finally { checking = false; }
    }
    async function selectFile(next: string): Promise<void> {
      if (!data || closed) return;
      atSummary = false; wholeTarget = false;
      path = next; ready = false; loaded = null;
      viewer?.destroy(); viewer = null;
      clear(code); clear(detail); editor.classList.add('hidden'); clear(editor); root.focus({ preventScroll: true });
      drawList(true); drawControls();
      const file = currentFile();
      if (!file || file.withheld) { code.append(h('p', { class: 'mut' }, 'Withheld: outside the project or its repository.')); return; }
      const pinned = data;
      const serial = ++generation;
      detail.append(h('strong', {}, file.orig ? `${file.orig} → ${file.path}` : file.path));
      if (file.sensitive) detail.append(hiddenMark());
      code.append(h('p', { class: 'mut' }, 'Loading pinned file versions…'));
      try {
        const [a, b] = await Promise.all([
          api.reviewBlob(id, pinned.id, file.path, 'left'), api.reviewBlob(id, pinned.id, file.path, 'right'),
        ]);
        if (closed || serial !== generation || data?.id !== pinned.id || !a.data || !b.data) return;
        const left = a.data, right = b.data;
        loaded = [left, right];
        clear(code);
        const meta = h('div', { class: 'review-meta small mut' });
        const label = (name: string, blob: FileBlob) => {
          if (blob.state !== 'text') meta.append(h('span', {}, `${name}: ${blob.state === 'missing' ? 'absent' : blob.state === 'too_large' ? 'too large (2 MiB cap)' : blob.state}`));
          if (blob.mode === '120000') meta.append(h('span', {}, `${name}: symlink target text`));
          if (blob.state === 'text' && blob.text && !blob.final_newline) meta.append(h('span', {}, `${name}: no final newline`));
        };
        label('Original', left); label('Compared', right);
        if (left.mode !== right.mode) meta.append(h('span', {}, `Mode ${left.mode ?? 'absent'} → ${right.mode ?? 'absent'}`));
        detail.append(meta);
        if ([left, right].some((blob) => !['text', 'missing'].includes(blob.state))) {
          code.append(h('p', { class: 'mut' }, 'Text comparison unavailable. Review the metadata explicitly; oversized and unsupported files cannot be marked viewed.'));
        } else {
          const merge = await createMergeReview({ path: file.path, ...(file.orig ? { originalPath: file.orig } : {}),
            left: left.text ?? '', right: right.text ?? '', onNote: addAt, notes: noteLines,
            onBlockAction: action => { if (action === 'accept') void acceptBlock(); else void restoreBlock(action); },
            accepted: index => accepted.get(reviewKey(file, pinned.head))?.has(index) ?? false,
            onTarget: selectionChanged => { if (selectionChanged && viewer?.hasSelection()) wholeTarget = false; if (!closed) window.requestAnimationFrame(() => { if (!closed) drawTarget(); }); } });
          if (closed || serial !== generation || data?.id !== pinned.id) { merge.destroy(); return; }
          viewer = merge; code.append(merge.dom); merge.focus();
        }
        ready = true; drawControls(); drawNotes();
        if (!viewer) root.focus();
        void runNavigation();
      } catch (error) {
        if (!closed && serial === generation) { clear(code); code.append(h('p', { class: 'bad' }, errText(error))); drawControls(); }
      }
    }
    async function refreshReview(): Promise<void> {
      if (loading || closed) return;
      loading = true; ready = false; generation++; navigation.length = 0; navigationVersion++;
      scopePick.disabled = true; banner.textContent = ''; drawControls();
      loadingStarted = performance.now(); loadingPhase = 'Preparing comparison';
      loadingText.textContent = 'Preparing comparison… 0 s · Large changes may take a while.';
      loadingStatus.classList.remove('hidden'); root.setAttribute('aria-busy', 'true');
      viewer?.destroy(); viewer = null; loaded = null;
      clear(code); clear(detail); clear(list); editor.classList.add('hidden'); clear(editor);
      code.append(h('p', { class: 'mut pad' }, 'Loading files for the selected comparison…'));
      try {
        const [response, config] = await Promise.allSettled([api.reviewFiles(id, scope), api.reviewActions(id)]);
        if (response.status === 'rejected') throw response.reason;
        const result = response.value.data;
        templates = config.status === 'fulfilled' ? config.value.data?.actions ?? [] : [];
        if (!result || closed) return;
        data = result; stale.clear();
        const valid = new Set(data.files.map(file => reviewKey(file, data!.head)));
        for (const key of accepted.keys()) if (!valid.has(key)) accepted.delete(key);
        for (const key of state.viewed.keys()) {
          const file = data.files.find((row) => row.path === key);
          if (!file || state.viewed.get(key) !== reviewKey(file, data.head)) state.viewed.delete(key);
        }
        const known = new Set(data.files.map((file) => file.path));
        for (const key of selected) if (!known.has(key)) selected.delete(key);
        if (first) {
          if (!state.status) for (const p of reviewPreselect(data.files, null)) selected.add(p);   // the panel has no list yet
          first = false;
        }
        // F23: the routine "Pinned a → b" is the scope's tooltip; the banner only speaks when something is notable
        pinnedLine = markBidi(`Pinned ${data.from.slice(0, 8)} → ${data.to.slice(0, 8)} · ${data.files.length} files`); describeScope();
        banner.textContent = markBidi(config.status === 'rejected' ? 'Review actions unavailable: ' + errText(config.reason) + '. Restart Piper and refresh the page.' : data.note || '');
        path = data.files.some((file) => file.path === path && !file.withheld) ? path : ordered().find((file) => reviewGroup(file) === 'Changes')?.path ?? '';
        if (path) { loadingPhase = 'Loading first file'; unfoldGroup(reviewGroup(currentFile()!)); await selectFile(path); }
        else { clear(code); clear(detail); code.append(h('p', { class: 'mut' }, 'No visible changes in this scope.')); }
        drawList(); drawNotes();
      } catch (error) {
        if (!closed) {
          banner.textContent = markBidi(errText(error));
          data = null; clear(list); clear(detail);
          { clear(code); code.append(h('div', { class: 'pad', role: 'alert' },
            h('p', { class: 'bad' }, 'Could not load review: ' + errText(error)),
            h('button', { onclick: () => void refreshReview() }, 'Retry loading review'))); }
        }
      }
      finally { loading = false; loadingStatus.classList.add('hidden'); root.setAttribute('aria-busy', 'false'); scopePick.disabled = false; drawControls(); }
    }
    drawNotes(); await refreshReview();
  }

  let busy = false;   // one send / commit at a time: a double click must not repeat it

  async function toggleAmend(on: boolean): Promise<void> {
    const id = current;
    const s = of(id);
    s.amend = on;
    if (!on) { s.message = amendOffMessage(s.message, s.amendFilled); s.amendFilled = ''; }
    draw();
    const project = projectOf(id);
    // the message of HEAD to edit, unless one is already typed
    const hash = s.status?.log[0]?.split(' ')[0] ?? '';   // /api/git/commit takes a hash only
    if (!on || s.message.trim() || !project || !/^[0-9a-f]{4,40}$/.test(hash)) return;
    try {
      const head = (await api.gitCommitDetail(project, hash)).data;
      if (head && of(id).amend && !of(id).message.trim()) {
        of(id).message = of(id).amendFilled = head.message;
        if (id === current) draw();
      }
    } catch (e) {
      deps.toast('Could not read HEAD: ' + errText(e));
    }
  }

  async function generate(): Promise<void> {
    const id = current;
    const s = of(id);
    const files = [...s.selected];
    if (!files.length || s.generating) return;
    s.generating = true;
    draw();
    try {
      const r = (await api.gitMessage(id, files)).data;
      if (r?.message) {
        s.message = r.message;
        s.genNote = generatedNote(files.length - r.withheld.length, r.withheld, r.truncated);
        s.genHeld = r.withheld.length > 0;
      }
    } catch (e) {
      deps.toast('Generate failed: ' + errText(e));
    } finally {
      s.generating = false;
      if (id === current) draw();
    }
  }

  async function commit(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      await commitNow();
    } finally {
      busy = false;
    }
  }

  async function changedAfterPin(id: string, pinned: Record<string, BlobVersion>): Promise<string[]> {
    try {
      const review = (await api.reviewFiles(id, 'head')).data;
      return review ? changedSince(pinned, review) : [];
    } catch {
      return [];
    }
  }

  async function commitNow(): Promise<void> {
    const id = current;
    const s = of(id);
    let files = [...s.selected];
    const push = s.pushAfter;
    s.pushAfter = false;
    let reviewed: { review: string; head: string; versions: Record<string, BlobVersion> } | undefined;
    let r: Awaited<ReturnType<typeof api.commit>>;
    try {
      // commit A: pin the checked files at the click - piperd commits exactly these blobs or refuses (a message-only amend has none)
      if (files.length) {
        const review = (await api.reviewFiles(id, 'head')).data;
        if (!review) throw new Error('the comparison is unavailable');
        const plan = commitPlan(files, review, s.viewed);
        const off = [...plan.missing, ...plan.blocked];
        if (off.length) {
          deps.toast(`${off.map((p) => p.slice(p.lastIndexOf('/') + 1)).join(', ')} changed since you checked ${off.length === 1 ? 'it' : 'them'} - look at the list and commit again.`);
          await refresh(id);
          return;
        }
        if (plan.unviewed.length && deps.requireReview?.()) {   // Settings → Git: Require Review before a commit
          s.pushAfter = push;
          deps.toast(`Review required: ${plan.unviewed.length} of ${files.length} not viewed.`);
          void showReview(id, 'head', 0, plan.unviewed[0]);
          return;
        }
        files = Object.keys(plan.versions);
        reviewed = { review: review.id, head: review.head, versions: plan.versions };
      }
      r = await api.commit(id, s.message, files, reviewed, s.amend);
    } catch (e) {
      // piperd refused the pinned blobs: name what changed after the click (the review it means is the pin)
      const changed = reviewed ? await changedAfterPin(id, reviewed.versions) : [];
      deps.toast(changed.length ? `Nothing committed: ${changed.map((p) => p.slice(p.lastIndexOf('/') + 1)).join(', ')} changed after you clicked Commit - look at the list and commit again.`
        : reviewed && /changed since review/.test(errText(e)) ? 'Nothing committed: HEAD moved after you clicked Commit - commit again.'
          : 'Commit failed: ' + errText(e));
      await refresh(id);
      return;
    }
    s.message = '';
    s.amend = false;
    s.amendFilled = '';
    s.genNote = '';
    s.genHeld = false;
    s.selected.clear();
    await refresh(id);
    committed(id, r.data, push);
  }

  /** commit A: the toast offers the next step - Push… (Commit & Push opens it at once) */
  function committed(id: string, r: { commit: string; hooks_disabled: boolean; index_warning?: string } | null, push: boolean): void {
    const st = of(id).status;
    const text = `Commit ${r?.commit ?? ''}` + (r?.index_warning ? ' · ' + r.index_warning : r?.hooks_disabled ? ' · working-tree hooks disabled (the agent can change them)' : '');
    if (push) { deps.toast(text); confirmPush(id); return; }
    deps.toast(text, st && canPush(st) ? { label: 'Push…', run: () => confirmPush(id) } : undefined);
  }

  /** S5: the Push dialog shows what goes out (commits, files, diffs) and what looks wrong before it */
  /** W11: a dialog's opener over the open dialog - the first call adds the layer, its re-renders replace it */
  function over(): (title: string, ...content: (Node | null)[]) => void {
    return firstOver(deps.modalOver ?? deps.modal, deps.modal);
  }

  /** the Git window of a project (the history icon, the palette) */
  function gitWindow(project: string): void {
    openGitWindow({
      toast: deps.toast, modal: deps.modal, closeModal: deps.closeModal, push: (done) => confirmPush(current, done),
      history: (p, path) => openHistory({ toast: deps.toast, modal: over(), closeModal: deps.closeModal,
        ...(deps.openFile ? { openFile: (pj: string, f: string, line: number) => { deps.closeAllModals?.(); deps.openFile!(pj, f, line); } } : {}) }, p, path),
      ...(deps.openFile ? { openFile: deps.openFile } : {}),
      ...(deps.openSession ? { openSession: deps.openSession } : {}),
      ...(deps.newSession ? { newSession: deps.newSession } : {}),
    }, project);
  }

  function confirmPush(id = current, done?: () => void): void {
    const project = projectOf(id);
    const ci = ciState.get(project)?.ci ?? null;
    const gh = !!ci?.github;
    // F27: the MR rides along with the push - canOfferMr's conditions but "already pushed"
    const mr = canOfferMrWithPush(of(id).status, ci) && (gh || (deps.gitlab?.(id) ?? true)) && (deps.ciEnabled?.(gh ? 'github' : 'gitlab') ?? true) ? {
      gh,
      draft: async (target = '') => (await api.mrDraft(project, target)).data ?? null,
      generate: async (target: string) => (await api.mrMessage(id, target)).data ?? null,
    } : undefined;
    openPushDialog({
      modal: done ? over() : deps.modal, closeModal: deps.closeModal, project, mr,
      preview: async () => (await api.pushPreview(id)).data!,
      push: (open) => {
        api.push(id).then(() => {
          deps.toast('Push done');
          of(id).offerMr = true;
          if (open) void createMr(id, project, open, gh);   // only after the push went through
          else if (project) void loadCi(project, true);
        }).catch((e: unknown) => deps.toast('Push failed: ' + errText(e))).finally(() => { void refresh(id); done?.(); });
      },
    });
  }

  // ---- CI + MR (9.7 #7)
  async function loadCi(project: string, force = false): Promise<void> {
    if (deps.ciEnabled && !deps.ciEnabled('gitlab') && !deps.ciEnabled('github')) {
      ciState.delete(project);
      drawCi();
      return;
    }
    let c = ciState.get(project);
    if (!c) {
      c = { ci: null, at: 0, loading: false };
      ciState.set(project, c);
    }
    if (c.loading || (!force && Date.now() - c.at < CI_MS)) return;
    c.loading = true;
    try {
      const result = (await api.ci(project)).data;
      c.ci = result && (deps.ciEnabled?.(result.github ? 'github' : 'gitlab') ?? true) ? result : null;
      if (c.ci) deps.onCi?.(project, c.ci);
    } catch {
      /* no GitLab: the chip stays empty */
    }
    c.at = Date.now();
    c.loading = false;
    if (projectOf(current) === project) drawCi();
  }

  /** W23: the project's CI / review lives on GitHub (piperd's /api/git/ci said so) */
  function isGh(project: string): boolean {
    return !!ciState.get(project)?.ci?.github;
  }

  function drawCi(): void {
    const s = of(current);
    const cached = ciState.get(projectOf(current))?.ci ?? null;
    const ci = cached && (deps.ciEnabled?.(cached.github ? 'github' : 'gitlab') ?? true) ? cached : null;
    const offer = canOfferMr(s.status, ci) && ((deps.gitlab?.(current) ?? true) || !!ci?.github);
    const gh = !!ci?.github;
    // commit A: Create MR… is primary only when it is the state's next step (nothing checked, nothing to push)
    const mrPrimary = !!s.status && primaryAction(s.status, !!s.selected.size || s.amend, offer) === 'mr';
    const sig = JSON.stringify([current, ci?.pipeline?.status, ci?.pipeline?.web_url, ci?.mr?.iid, ci?.mr?.state, offer, mrPrimary, gh]);
    if (sig === ciSig) return;
    ciSig = sig;
    clear(ciEl);
    const p = ci?.pipeline;
    const tone = p ? ciTone(p.status) : 'mut';
    ciHead.className = p ? `cih ${tone}` : 'cih';
    ciHead.title = markBidi(p ? `pipeline ${ciLabel(p.status)}` : '');
    ciHead.textContent = markBidi(p ? ` · CI ${tone === 'ok' ? '✓' : tone === 'bad' ? '✗' : '…'}` : '');
    if (p) {
      ciEl.append(h('a', { class: `chip ci-${ciTone(p.status)}`, href: p.web_url, target: '_blank', rel: 'noopener noreferrer',
        title: `pipeline #${p.id} · ${p.ref} · ${p.sha.slice(0, 8)} · ${p.updated_at}` }, `pipeline ${ciLabel(p.status)}`));
    }
    if (ci?.mr) {
      ciEl.append(h('a', { class: 'chip', href: ci.mr.web_url, target: '_blank', rel: 'noopener noreferrer',
        title: showBidi(`${ci.mr.title} · ${ci.mr.source_branch} → ${ci.mr.target_branch}`) }, `${gh ? 'PR #' : 'MR !'}${ci.mr.iid}${ci.mr.state !== 'opened' ? ' · ' + ci.mr.state : ''}`));
    }
    if (offer) ciEl.append(h('button', { class: mrPrimary ? 'primary mini' : 'mini', onclick: () => void openMr(current) }, gh ? 'Create PR…' : 'Create MR…'));
    ciEl.classList.toggle('hidden', !ciEl.childElementCount);
  }

  async function openMr(id: string): Promise<void> {
    const project = projectOf(id);
    if (!project) return;
    deps.modal(isGh(project) ? 'Create pull request' : 'Create merge request', h('p', { class: 'mut' }, 'loading the draft…'), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel')));
    try {
      const d = (await api.mrDraft(project)).data;
      if (d) mrForm(id, project, { ...d, draft: false });
    } catch (e) {
      deps.closeModal();
      deps.toast('MR draft failed: ' + errText(e));
    }
  }

  function mrForm(id: string, project: string, v: MrDraft & { draft: boolean }): void {
    const title = h('input', { type: 'text', value: v.title, placeholder: 'title' }) as HTMLInputElement;
    const target = h('input', { type: 'text', value: v.target, class: 'mono', placeholder: 'target branch' }) as HTMLInputElement;
    const desc = h('textarea', { rows: 10, class: 'mono' }) as HTMLTextAreaElement;
    desc.value = v.description;
    const draft = h('input', { type: 'checkbox' }) as HTMLInputElement;
    draft.checked = v.draft;
    const values = () => ({ ...v, title: title.value.trim(), target: target.value.trim(), description: desc.value, draft: draft.checked });
    const next = () => {
      const x = values();
      if (!x.title || !x.target) {
        deps.toast('Title or target branch missing');
        return;
      }
      deps.closeModal();
      void createMr(id, project, x);
    };
    const regen = async () => {
      try {
        const d = (await api.mrDraft(project, target.value.trim())).data;
        if (d) {
          title.value = d.title;
          desc.value = d.description;
        }
      } catch (e) {
        deps.toast('MR draft failed: ' + errText(e));
      }
    };
    // commit A: Generate drafts the title and description from the branch (claude -p on this Mac, hidden files never sent)
    const genNote = h('p', { class: 'mut small gennote', hidden: true });
    const gen = h('button', { class: 'mini', title: 'draft the title and description from the branch\'s commits and diff (claude -p on this Mac, Haiku, no tools; files hidden from Claude are never sent)',
      onclick: () => void generateMr() }, h('i', { class: 'fi fi-sparkles' }), h('span', {}, 'Generate')) as HTMLButtonElement;
    const generateMr = async () => {
      gen.disabled = true;
      gen.lastElementChild!.textContent = 'Generating…';
      try {
        const d = (await api.mrMessage(id, target.value.trim())).data;
        if (d) {
          title.value = d.title || title.value;
          desc.value = d.description;
          genNote.textContent = markBidi(generatedNote('the branch', d.withheld, d.truncated));
          genNote.hidden = false;
        }
      } catch (e) {
        deps.toast('Generate failed: ' + errText(e));
      } finally {
        gen.disabled = false;
        gen.lastElementChild!.textContent = 'Generate';
      }
    };
    for (const x of [title, target, desc]) {
      x.addEventListener('keydown', (e) => {
        const k = e as KeyboardEvent;
        if (k.key === 'Enter' && (k.metaKey || k.ctrlKey)) {
          k.preventDefault();
          next();
        }
      });
    }
    deps.modal(isGh(project) ? 'Create pull request' : 'Create merge request',
      h('p', { class: 'mut small' }, `${project} · ${v.source} → `),
      h('div', { class: 'row left mrtarget' }, target, h('button', { class: 'mini', title: 'the title and description from the commit subjects for this target branch', onclick: () => void regen() }, 'Draft again'), gen),
      title, desc, genNote,
      h('label', { class: 'small' }, draft, isGh(project) ? 'Draft (GitHub blocks the merge until it is marked ready)' : 'Draft (GitLab blocks the merge until you clear the draft)'),
      h('div', { class: 'row' }, h('span', { class: 'grow' }), h('button', { onclick: deps.closeModal }, 'Cancel'),
        h('button', { class: 'primary', onclick: next, title: `create it in ${isGh(project) ? 'GitHub' : 'GitLab'} - no second confirmation` },
          isGh(project) ? 'Create PR' : 'Create MR', h('kbd', {}, kbd('⌘⏎', 'Ctrl')))));
    title.focus();
  }

  /** F27: the form (or the push dialog) is the confirmation - no second confirmation dialog */
  async function createMr(id: string, project: string, v: MrDraft & { draft: boolean }, gh = isGh(project)): Promise<void> {
    try {
      const mr = (await api.createMr(project, v.title, v.description, v.target, v.draft)).data;
      deps.toast(mrCreatedText(mr, gh));
      of(id).offerMr = false;
      const c = ciState.get(project);
      if (c?.ci && mr && typeof mr.iid === 'number') c.ci = { ...c.ci, mr };
      drawCi();
    } catch (e) {
      deps.toast(`Could not create the ${gh ? 'PR' : 'MR'}: ` + errText(e));
    }
    void loadCi(project, true);
  }

  // ---- drawing (S2 · Commit panel: IntelliJ's Commit tool window)
  const head = sec.actions;
  function drawHead(): void {
    const s = of(current);
    const project = projectOf(current);
    clear(head);
    head.append(h('button', { class: 'mini icon', title: 'refresh', 'aria-label': 'refresh', onclick: () => void refresh() }, s.loading ? '…' : h('i', { class: 'fi fi-refresh' })),
      h('button', { class: 'mini icon opt' + (tree ? ' on' : ''), title: tree ? 'files by directory - show a flat list' : 'flat list - group the files by directory', 'aria-pressed': String(tree),
        onclick: () => { tree = !tree; setTreePref(tree); draw(); } }, h('i', { class: 'fi fi-tree' })));
    head.append(h('button', { class: 'mini icon', title: 'Review the whole session', 'aria-label': 'Review the whole session', onclick: () => void showReview(current) }, h('i', { class: 'fi fi-eye' })));
    if (project) {
      // reference views: shown with the section open, the folded header keeps its room for the state
      // D12: one way into the repository - the history icon opens the Git window (its log is the history)
      head.append(h('button', { class: 'mini icon opt', title: 'Git window: branches, worktrees, the log and its commits', 'aria-label': 'Git window',
        onclick: () => gitWindow(project) }, h('i', { class: 'fi fi-history' })));
    }
  }

  /** the Commit buttons and the words beside them - also on every keystroke in the message (no full redraw) */
  function drawFoot(): void {
    const s = of(current);
    const st = s.status;
    if (!st) return;
    const picked = !!s.selected.size || s.amend;
    const hint = commitHint(s.selected.size, s.message, s.amend);
    footCount.textContent = markBidi(hint.text);
    footCount.className = `small grow ${hint.warn ? 'warn' : 'mut'}`;
    commitBtn.textContent = markBidi(commitLabel(s.selected.size, s.amend));
    commitBtn.disabled = !picked || !s.message.trim();
    commitBtn.classList.toggle('primary', primaryAction(st, picked, false) === 'commit');
    moreBtn.classList.toggle('primary', commitBtn.classList.contains('primary'));
    commitPushBtn.textContent = s.amend ? 'Amend & Push' : 'Commit & Push';
    commitPushBtn.disabled = commitBtn.disabled || !st.branch;
  }

  function draw(): void {
    // the commit message keeps its focus and caret across a redraw
    const typing = document.activeElement === msg;
    const caret = [msg.selectionStart, msg.selectionEnd] as const;
    const s = of(current);
    drawHead();
    drawTurn();
    const body = sec.body;
    clear(body);
    const st = s.status;
    const sync = st ? `${st.ahead ? ' ↑' + st.ahead : ''}${st.behind ? ' ↓' + st.behind : ''}` : '';
    // the header: Commit <branch> ↑1 (S2); the upstream in the tooltip
    sec.status.className = 'sec-s';
    sec.status.title = '';
    clear(sec.status);
    if (s.error) sec.status.textContent = 'unavailable';
    else if (st) {
      const branch = st.branch || '(detached)';
      sec.status.title = markBidi(`${branch}${st.upstream ? ' → ' + st.upstream : ' - not on a remote yet'}${sync}`);
      sec.status.append(h('span', { class: 'mono gbr' }, showBidi(branch)), sync ? h('b', { class: 'gsync' }, sync) : '');   // a branch name is the repo's text
    }
    sec.status.append(ciHead);
    if (s.error) {
      sec.auto(false);
      body.append(h('p', { class: 'mut' }, s.error));
      return;
    }
    if (!st) {
      body.append(h('p', { class: 'mut' }, 'loading…'));
      return;
    }
    const others = deps.sharedWith?.(current) ?? [];
    // open when there is something to commit, push or send; the header carries the rest
    drawCi();
    const failed = ciHead.classList.contains('bad');
    sec.auto(!!st.files.length || !!s.notes.length || !!others.length || failed || !!st.ahead);
    if (ciEl.childElementCount) body.append(ciEl);
    if (others.length) body.append(h('p', { class: 'warn small' }, `The working tree is shared with ${others.join(', ')}: the list holds everyone's changes. Commit only the files of this work.`));
    const [kept, junk] = splitJunk(st.files);
    // IntelliJ's Commit window: tracked changes are "Changes"; new files are "Unversioned" - folded, never ticked by
    // themselves, staged one by one after review (+ Git)
    const mine = new Set(st.session_files ?? []);
    const real = kept.filter((f) => f.x !== '?');
    const foreign = kept.filter((f) => f.x === '?');
    type F = GitStatus['files'][number];
    const row = (f: F, inTree: boolean) => {
      const untracked = f.x === '?';
      const cb = h('input', { type: 'checkbox' }) as HTMLInputElement;
      cb.checked = s.selected.has(f.path);
      cb.addEventListener('change', () => { if (cb.checked) s.selected.add(f.path); else s.selected.delete(f.path); draw(); });
      const m = changeMark(f.x + f.y);
      const lines = f.add === undefined ? '' : f.add === null ? 'bin' : stat(f.add, f.del ?? 0);
      return h('li', { class: `${f.sensitive ? 'sensitive' : ''} ch-${m.cls}${cb.checked ? '' : ' off'}${inTree ? ' in' : ''}` }, cb, markEl(m),
        fileButton(f.path, `${mine.has(f.path) ? 'this session · ' : ''}${m.word} · ${f.orig ? f.orig + ' → ' : ''}${f.path}${f.sensitive ? ' - ' + HIDDEN_TITLE + ' - check that it belongs in the commit' : ''} - show diff${isMac(navigator.platform) ? ' · ⌘+click opens Files' : ' · Ctrl+click opens Files'}`, fileAction(current, f.path, () => showDiff(f.path)), true, !inTree),
        h('span', { class: 'gstat' }, lines),
        f.sensitive ? hiddenMark() : h('span'),
        untracked ? h('button', { class: 'mini', title: 'Stage these reviewed bytes in Git', onclick: () => void stageFile(current, f.path) }, '+ Git') : null,
        rowActions(current, f.path, m.letter !== 'D', !untracked, () => showDiff(f.path)));
    };
    // a box over files: files the isolation policy marks are only ever ticked one by one
    const pickBox = (files: F[], what: string) => {
      const p = pickState(files, s.selected);
      const box = h('input', { type: 'checkbox', title: `${what} - all / none (not the files hidden from Claude)` }) as HTMLInputElement;
      box.checked = p.checked;
      box.indeterminate = p.indeterminate;
      box.disabled = p.disabled;
      box.addEventListener('click', (e) => e.stopPropagation());   // the box ticks, a summary folds
      box.addEventListener('change', () => { for (const x of p.bulk) if (box.checked) s.selected.add(x); else s.selected.delete(x); draw(); });
      return box;
    };
    // the files of a group: by directory (S2) or flat
    const list = (files: F[], group: string) => {
      if (!tree) return h('ul', { class: 'gitfiles vcs' }, ...files.map((f) => row(f, false)));
      const out: HTMLElement[] = [];
      for (const g of fileTree(files)) {
        const key = `${group}:${g.dir}`;
        const open = !foldedDirs.has(key);
        if (g.dir) out.push(h('li', { class: 'gdir' + (open ? ' open' : '') }, pickBox(g.files, g.dir),
          h('button', { class: 'link gdir-t', title: showBidi(g.dir), 'aria-expanded': String(open), onclick: () => { if (open) foldedDirs.add(key); else foldedDirs.delete(key); draw(); } },
            h('i', { class: 'fi fi-dir' }), h('span', {}, showBidi(shortDir(g.dir))))));
        if (open) out.push(...g.files.map((f) => row(f, !!g.dir)));
      }
      return h('ul', { class: 'gitfiles vcs tree' }, ...out);
    };
    const count = (n: number) => `${n} ${n === 1 ? 'file' : 'files'}`;
    if (real.length) {
      const box = h('details', { class: 'vgroup', open: changesOpen },
        h('summary', {}, pickBox(real, 'Changes'), h('span', { class: 'vg-name' }, 'Changes'), h('span', { class: 'vg-n' }, count(real.length))),
        list(real, 'c'));
      box.addEventListener('toggle', () => { changesOpen = box.open; });
      body.append(box);
    }
    if (foreign.length) {
      const picked = foreign.filter((f) => s.selected.has(f.path)).length;
      const box = h('details', { class: 'vgroup', open: foreignOpen },
        h('summary', { title: 'new files this session did not create (yours, another session\'s, older): not in git, not in the commit unless you tick them' },
          pickBox(foreign, 'Unversioned'), h('span', { class: 'vg-name' }, 'Unversioned'), h('span', { class: 'vg-n' }, `${count(foreign.length)}${picked ? ` · ${picked} selected` : ''}`)),
        list(foreign, 'u'));
      box.addEventListener('toggle', () => { foreignOpen = box.open; });
      body.append(box);
    }
    if (junk.length) {
      // OS droppings and development/ scratch: unchecked by default, folded so the real changes stand out
      const picked = junk.filter((f) => s.selected.has(f.path)).length;
      const kinds = [...new Set(junk.map((f) => /(^|\/)development\//.test(f.path) ? 'development/' : f.path.slice(f.path.lastIndexOf('/') + 1)))];
      const box = h('details', { class: 'junk', open: junkOpen },
        h('summary', {}, `${junk.length} scratch & OS ${junk.length === 1 ? 'file' : 'files'}${picked ? ` (${picked} selected)` : ''} · ${kinds.slice(0, 3).join(', ')}${kinds.length > 3 ? '…' : ''}`),
        h('ul', { class: 'gitfiles' }, ...junk.map((f) => row(f, false))));
      box.addEventListener('toggle', () => { junkOpen = box.open; });
      body.append(box);
    }
    const toPush = canPush(st) ? (st.ahead ? `${st.ahead} ${st.ahead === 1 ? 'commit' : 'commits'} to push` : 'Branch not on the remote yet') : '';
    if (st.files.length || s.amend) {
      if (msg.value !== s.message) msg.value = s.message;
      const blocked = amendBlock(st);
      if (blocked && s.amend) { s.amend = false; s.message = amendOffMessage(s.message, s.amendFilled); s.amendFilled = ''; }
      genBtn.disabled = s.generating || !s.selected.size;
      genBtn.lastElementChild!.textContent = s.generating ? 'Generating…' : 'Generate';
      drawFoot();
      // commit A: Review stays one click away - the line says how many checked files nobody viewed
      const unviewed = [...s.selected].filter((p) => !s.viewed.has(p)).length;
      const strict = !!deps.requireReview?.();
      const headHash = st.log[0]?.split(' ')[0] ?? '';
      body.append(h('div', { class: 'gmsg' },
        h('div', { class: 'row left msghead' }, h('span', { class: 'grow' }), genBtn),
        msg,
        h('p', { class: s.genHeld ? 'small gennote held' : 'mut small gennote', hidden: !s.genNote }, s.genHeld ? hiddenMark() : null, s.genNote),
        s.amend ? h('p', { class: 'small amendline' }, h('span', { class: 'grow' }, `Amending ${headHash || 'HEAD'} - the commit replaces it`),
          h('button', { class: 'link', onclick: () => void toggleAmend(false) }, 'Cancel')) : null,
        unviewed ? h('p', { class: `small unviewed${strict ? ' warn' : ' mut'}` },
          h('span', {}, strict ? `Review required: ${unviewed} of ${s.selected.size} not viewed · ` : `${unviewed} of ${s.selected.size} not viewed · `),
          h('button', { class: 'link', title: 'Review the checked files before the commit · ⌘D', onclick: () => void showReview(current, 'head') }, 'Review')) : null,
        // F07: Commit & Push lives in the ▾ menu only (a second button cut "Write a message" in the 300 px panel)
        h('div', { class: 'row left gfoot' }, footCount, h('span', { class: 'split' }, commitBtn, moreBtn)),
        toPush ? h('div', { class: 'row left gfoot gpush' }, h('span', { class: 'small mut grow' }, toPush),
          h('button', { class: primaryAction(st, !!s.selected.size || s.amend, false) === 'push' ? 'primary' : '', onclick: () => confirmPush() }, 'Push…')) : null));
      if (typing) {
        msg.focus({ preventScroll: true });
        msg.setSelectionRange(caret[0], caret[1]);
      }
    } else {
      // S2b: nothing to commit - the push is the panel's action
      body.append(h('p', { class: 'gclean' }, h('i', { class: 'fi fi-check' }), 'Nothing to commit'));
      if (toPush) body.append(h('div', { class: 'row left gfoot gpush solo' }, h('span', { class: 'small mut grow' }, toPush), h('button', { class: 'primary', onclick: () => confirmPush() }, 'Push…')));
    }
    {
      const items = itemsOf(current);
      if (items.length) body.append(h('div', { class: 'notes' },
        h('div', { class: 'row left' }, h('strong', { class: 'grow' }, `Review items (${items.length})`),
          h('button', { class: 'mini', 'aria-label': 'Clear all review items', title: 'Remove all review actions and comments in this session', onclick: () => { clearItems(current); draw(); } }, 'Clear all')),
        ...items.map(item => h('div', { class: 'small' }, h('code', {}, showBidi(`${item.path}:${item.from}`)),
          showBidi(` · ${item.needsReview ? 'Confirm code' : item.state} · ${item.actions.map(a => a.label).join(', ')} ${item.text}`))),
        h('button', { class: 'primary', onclick: () => prepareTask(current), disabled: !items.some(x => x.state === 'open') }, 'Prepare task…')));
    }
  }

  return {
    stageOf(id: string, path: string): void {
      const status = of(id).status;
      const file = status?.files.find(f => rel(id, f.path) === path);
      if (file) void stageFile(id, file.path); else deps.toast('Refresh Changes before adding this file to Git.');
    },
    reviewPathsOf(id: string): string[] {
      return reviewOrder(of(id).status?.files ?? []).map(x => rel(id, x.path)).filter(x => !x.startsWith('/'));
    },
    async viewedOf(id: string, path: string, sha: string | null, leftOid: string | null, leftMode: string | null): Promise<void> {
      const review = (await api.reviewFiles(id, 'head')).data;
      const row = review?.files.find(x => rel(id, x.path) === path && !x.withheld);
      if (!review || !row || row.work_sha !== sha || row.before?.oid !== leftOid || row.before?.mode !== leftMode) return;
      const check = (await api.reviewCheck(id, review.id)).data;
      if (!check || check.head_changed || check.changed.includes(row.path)) return;
      of(id).viewed.set(row.path, reviewKey(row, review.head));
    },
    actionsOf(id: string, target: ReviewTarget): void { void chooseActions(id, target); },
    prepareOf(id: string): void { prepareTask(id); },
    el,
    /** the last turn of the shown session: the header words ("20 · 3 files") and the body (picker, files, Full diff) */
    turn: { status: turnStatus, body: turnBody, has: (id: string) => !!of(id).turns?.length },
    /** changed paths of a session's project, project-relative (the file browser marks them) */
    changed(id: string): Set<string> {
      const st = state.get(id)?.status;
      if (!st) return new Set();
      return new Set(st.files.map((f) => projectRel(f.path, st.root, deps.projectPath(id))).filter((p) => !p.startsWith('/')));
    },
    /** the panel's "N changes" (scratch & OS files left out); null = no status fetched yet */
    changeCount(id: string): number | null {
      const st = state.get(id)?.status;
      return st ? st.files.filter((f) => !isJunk(f.path)).length : null;
    },
    /** change marks (M A U R D !) of a session's project, by project-relative path (the file browser's tree) */
    marks(id: string): Map<string, ChangeMark> {
      const st = state.get(id)?.status;
      const out = new Map<string, ChangeMark>();
      for (const f of st?.files ?? []) {
        const p = projectRel(f.path, st!.root, deps.projectPath(id));
        if (!p.startsWith('/')) out.set(p, changeMark(f.x + f.y));
      }
      return out;
    },
    /** the turn picked in the panel (the newest unless one is chosen) and its files, project-relative;
     *  files null = not loaded (yet); null = no turn (K7: the file browser's Turn scope) */
    pickedTurnFiles(id: string): { n: number; files: Set<string> | null } | null {
      const s = state.get(id);
      const t = s?.turns ? pickTurn(s.turns, s.turnN) : null;
      if (!t) return null;
      const d = s!.turnFiles.get(keyOf(t));
      return { n: t.n, files: d ? new Set(d.files.map((f) => rel(id, f.path)).filter((p) => !p.startsWith('/'))) : null };
    },
    /** the review notes of a session (project-relative paths): the editor's gutter marks (K6) */
    notes(id: string): Note[] {
      return state.get(id)?.notes ?? [];
    },
    /** unsent review notes or a commit message being written (the page asks before it unloads, K14) */
    dirty(): boolean {
      return [...state.values()].some((s) => s.notes.length > 0 || !!s.message.trim());
    },
    branch(id: string): string {
      return state.get(id)?.status?.branch ?? '';
    },
    /** the diff of a project-relative path (from the file browser) */
    diffOf(id: string, rel: string): void {
      const st = state.get(id)?.status;
      const f = st?.files.find((x) => projectRel(x.path, st.root, deps.projectPath(id)) === rel);
      if (f) showDiff(f.path, id);
      else void showReview(id, 'head', 0);
    },
    /** the session's files changed (a turn, a Files edit): fetch status and turns again now - the file browser's
     *  Changed scope and marks read this status even while the context panel is closed */
    invalidate(id: string): void {
      const s = of(id);
      s.fetched = 0;
      if (!s.loading) void refresh(id);
      else void loadTurns(id);
    },
    prefetch(id: string): void {
      const s = of(id);
      if (!s.loading && Date.now() - s.fetched > REFRESH_MS) void refresh(id);
    },
    /** switch to a session; fetches on first show and when the data is older than 15 s (CI: 60 s) */
    show(id: string): void {
      if (id !== current) {
        current = id;
        turnSig = '';
        ciSig = '';
        draw();
      }
      const s = of(id);
      if (!s.loading && Date.now() - s.fetched > REFRESH_MS) void refresh(id);
      const p = projectOf(id);
      if (p) void loadCi(p);
    },
    /** a review note from elsewhere (the file preview, project-relative path); same line = replaced */
    addNote(id: string, note: Note): void {
      saveItems(id, [...itemsOf(id), { id: crypto.randomUUID(), path: note.path, from: note.line, to: note.line, old: note.old,
        code: note.excerpt ?? '', sha: null, actions: [], text: note.text, state: 'open', needsReview: true }]);
      if (id === current) draw();
    },
    /** the Git window of a project (D12: the palette's "Git window") */
    gitWindow(project: string): void {
      gitWindow(project);
    },
    /** the diff of one turn (default: the newest) in the review modal */
    showTurn(id: string, n?: number): void {
      void showTurn(id, n);
    },
    /**
     * the newest turn of a session for the board card; `stamp` (Session.since) changing
     * triggers a refetch, the answer arrives with the board's next draw
     */
    turnInfo(id: string, stamp: number): { n: number; files: number | null; running: boolean } | null {
      const s = of(id);
      if (stamp !== s.turnStamp) {
        s.turnStamp = stamp;
        void loadTurns(id);
      }
      const t = lastTurn(s.turns ?? []);
      if (!t) return null;
      const f = s.turnFiles.get(keyOf(t));
      return { n: t.n, files: f ? f.files.length : null, running: t.to === null };
    },
  };
}

/** files a commit never wants by default (Finder / Explorer droppings, the development/ scratch
 *  scratch directory: exports, feeds, analyses); still listed, just unchecked */
export interface ChangeMark { letter: string; cls: 'mod' | 'new' | 'add' | 'del' | 'ren' | 'bad'; word: string }

/**
 * One letter per change (the VS Code convention) from `git status --porcelain` XY or a turn's
 * single status letter: M modified, U untracked, A added, D deleted, R renamed, ! conflict.
 * The tooltip word says what the index holds.
 */
export function changeMark(xy: string): ChangeMark {
  // a turn's status is one letter, a rename/copy with its score (R100); porcelain is two columns
  const turn = xy.length === 1 || /^[RC]\d+$/.test(xy);
  const code = turn ? xy[0]! + ' ' : xy.slice(0, 2);
  const [x, y] = [code[0]!, code[1]!];
  if (code === '??') return { letter: 'U', cls: 'new', word: 'untracked' };
  if (x === 'U' || y === 'U' || code === 'AA' || code === 'DD') return { letter: '!', cls: 'bad', word: 'conflict' };
  const stage = turn ? '' : x === ' ' ? ', not staged' : y === ' ' ? ', staged' : ', partly staged';
  if (x === 'D' || y === 'D') return { letter: 'D', cls: 'del', word: 'deleted' + stage };
  if (x === 'R' || x === 'C') return { letter: 'R', cls: 'ren', word: (x === 'C' ? 'copied' : 'renamed') + stage };
  if (x === 'A') return { letter: 'A', cls: 'add', word: 'added' + stage };
  return { letter: 'M', cls: 'mod', word: 'modified' + stage };
}

function markEl(m: ChangeMark): HTMLElement {
  return h('span', { class: `gmark ch-${m.cls}`, title: m.word, 'aria-label': m.word }, m.letter);
}

/** a file on one line: the name, then its directory (cut from the left when it does not fit) */
function fileButton(path: string, title: string, onclick: (event: Event) => void, source = false, dir = true): HTMLElement {
  const cut = dir ? path.lastIndexOf('/') : -1;
  return h('button', { class: 'link gf', title: showBidi(title), onclick, 'data-git-path': source ? path : undefined },
    h('span', { class: 'fn' }, showBidi(path.slice(path.lastIndexOf('/') + 1))),
    cut > 0 ? h('span', { class: 'dir' }, h('bdi', {}, showBidi(path.slice(0, cut)))) : null);
}

/** the commit list split into real changes and isJunk files (folded in the panel), order kept */
export function splitJunk<T extends { path: string }>(files: T[]): [T[], T[]] {
  return [files.filter((f) => !isJunk(f.path)), files.filter((f) => isJunk(f.path))];
}

export function isJunk(path: string): boolean {
  return /(^|\/)(\.DS_Store|Thumbs\.db|desktop\.ini|\._[^/]*)$/i.test(path) || /(^|\/)development\//.test(path);
}
