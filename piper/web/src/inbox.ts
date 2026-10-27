import { machineStorage } from './environment.js';
// GitLab inbox: the "GitLab" section
// at the top of Overview. piperd polls GitLab (todos, assigned issues, own MRs) and counts what is
// new locally; the GUI refetches GET /api/inbox only when state.inbox.v changes, diffs the items
// for notifications, and shows them grouped (New / Waiting for you / Assigned / My MRs) with one
// context-aware primary action per row (continue the linked session, open a session, map the
// checkout) and the rest in a "⋯" menu. Rows are keyed and updated in place like board cards:
// a refetch never collapses an open preview, drops the focus or jumps the scroll.
// GitLab text is untrusted: it is rendered through markdown.ts / textContent only.

import { isSend, sendHint } from './sendkeys.js';
import { kbd } from './clipkeys.js';
import { api } from './api.js';
import { toggleMenu, type MenuEntry } from './menu.js';
import { openComment, renderRich } from './comment.js';
import { ago, agoText, clear, fi, h } from './dom.js';
import { cutText, ISSUE_FILES, newNotesLabel, plural, taskFor, markBidi } from './text.js';
import type {
  BoardFlow, Inbox, InboxItem, InboxPreview, InboxRule, InboxSummary, IssueFiles, IssueStateSummary, RuleEvent, Session, SessionIssue, SessionStartResult, StartOpts, Agent,
} from './types.js';

// ---------------------------------------------------------------- pure helpers (unit-tested)

export interface ReasonInfo {
  label: string;
  cls: string;
  hint: string;
  /** notification wording for a new item / todo */
  news: string;
}

export const REASONS: Record<string, ReasonInfo> = {
  directly_addressed: { label: 'Addressed', cls: 'r-addr', hint: 'a comment addresses you directly (@name at the start) - it is waiting on you', news: 'Addressed' },
  mentioned: { label: 'Mention', cls: 'r-mention', hint: 'someone mentioned you (@name)', news: 'Mention' },
  assigned: { label: 'Assigned', cls: 'r-assigned', hint: 'assigned to you', news: 'New assignment' },
  build_failed: { label: 'Build failed', cls: 'r-bad', hint: 'the merge request pipeline failed', news: 'Build failed' },
  unmergeable: { label: 'Cannot merge', cls: 'r-bad', hint: 'the merge request conflicts with the target branch', news: 'Cannot merge' },
  review_requested: { label: 'Review', cls: 'r-review', hint: 'someone asks you for a review', news: 'Review request' },
  approval_required: { label: 'Approval', cls: 'r-review', hint: 'the merge request is waiting for your approval', news: 'Waiting for approval' },
  marked: { label: 'To-do', cls: 'r-mut', hint: 'a to-do you added yourself', news: 'To-do' },
  authored: { label: 'My MR', cls: 'r-mine', hint: 'your open merge request', news: 'My MR' },
};

/** reasons that mean a human wants something from you (they notify); order = importance */
export const HUMAN_REASONS = ['directly_addressed', 'mentioned', 'assigned', 'build_failed', 'unmergeable', 'review_requested', 'approval_required'];

export function reasonInfo(r: string): ReasonInfo {
  return REASONS[r] ?? { label: r.replace(/_/g, ' '), cls: 'r-mut', hint: r, news: r.replace(/_/g, ' ') };
}

/** the item's reasons in display order (the most pressing first), duplicates dropped */
export function sortedReasons(reasons: string[]): string[] {
  const rank = (r: string) => {
    const i = [...HUMAN_REASONS, 'marked', 'authored'].indexOf(r);
    return i < 0 ? 50 : i;
  };
  return [...new Set(reasons)].sort((a, b) => rank(a) - rank(b));
}

/** 9.6x F15: reasons that ask for an action - the row keeps one of them as a coloured chip */
export const ACTION_REASONS = ['build_failed', 'unmergeable', 'directly_addressed', 'review_requested', 'approval_required'];

/** F15: a row shows at most two reasons - at most one coloured action chip (the most pressing),
 *  the informational ones (Assigned, Mention, My MR…) as quiet text; the rest goes into "+N" (its tooltip) */
export function rowReasons(reasons: string[]): { chip: string | null; text: string[]; more: string[] } {
  const all = sortedReasons(reasons);
  const chip = all.find((r) => ACTION_REASONS.includes(r)) ?? null;
  const text = all.filter((r) => r !== chip).slice(0, chip ? 1 : 2);
  return { chip, text, more: all.filter((r) => r !== chip && !text.includes(r)) };
}

export const isFresh = (i: InboxItem): boolean => i.new || i.new_notes > 0;

export type Group = 'new' | 'todo' | 'assigned' | 'mine';
export const GROUPS: { id: Group; title: string; empty: string }[] = [
  { id: 'new', title: 'New', empty: '' },
  { id: 'todo', title: 'Waiting for you', empty: '' },
  { id: 'assigned', title: 'Assigned', empty: '' },
  { id: 'mine', title: 'My MRs', empty: '' },
];

/** New (never seen / new notes) → Waiting for you (pending todo) → Assigned → My MRs */
export function groupOf(i: InboxItem): Group {
  if (isFresh(i)) return 'new';
  if (i.todos.length) return 'todo';
  if (i.reasons.some((r) => r !== 'authored')) return 'assigned';
  return 'mine';
}

/** items per group in the server's order (it sorts by importance and recency);
 *  `pinned` keeps a row in the group it had while its preview is open */
export function groupItems(items: InboxItem[], pinned: Map<string, Group> = new Map()): Record<Group, InboxItem[]> {
  const out: Record<Group, InboxItem[]> = { new: [], todo: [], assigned: [], mine: [] };
  for (const i of items) out[pinned.get(i.key) ?? groupOf(i)].push(i);
  return out;
}

/** assigned to you now - what GitLab's "Assigned to me" lists; an "assigned" to-do alone may be stale
 *  (closed or reassigned since); an older piperd without `listed` falls back to the reasons */
export const isAssigned = (i: InboxItem): boolean => (i.listed ?? i.reasons).includes('assigned');

export type Filter = 'all' | 'new' | 'assigned' | 'mentions' | 'mr';
export const FILTERS: { id: Filter; label: string; title: string }[] = [
  { id: 'all', label: 'All', title: 'all items' },
  { id: 'new', label: 'New', title: 'new items and items with new comments' },
  { id: 'assigned', label: 'Assigned', title: 'issues and MRs assigned to you' },
  { id: 'mentions', label: 'Mentions', title: 'mentions and direct addresses' },
  { id: 'mr', label: 'MR', title: 'merge requests' },
];

export function matchesFilter(i: InboxItem, f: Filter): boolean {
  switch (f) {
    case 'all': return true;
    case 'new': return isFresh(i);
    case 'assigned': return isAssigned(i);
    case 'mentions': return i.reasons.includes('mentioned') || i.reasons.includes('directly_addressed');
    case 'mr': return i.type === 'mr';
  }
}

const fold = (s: string): string => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

/** the project a row shows: the local key, else the last segment of the GitLab path */
export function projectLabel(i: Pick<InboxItem, 'project' | 'gl'>): string {
  return i.project ?? i.gl.split('/').pop() ?? i.gl;
}

/** W23: GitHub item (its gl is "gh:owner/repo") */
export const isGh = (i: Pick<InboxItem, 'gl'>): boolean => i.gl.startsWith('gh:');

/** "GitLab" / "GitHub": where the item lives (the menu, the preview, the dialogs) */
export const forgeName = (i: Pick<InboxItem, 'gl'>): string => (isGh(i) ? 'GitHub' : 'GitLab');

/** 9.43: the segment's counts under the current project + search (the chip filter left out, like the chips') */
export function forgeCounts(items: InboxItem[], v: Pick<View, 'project' | 'query'>): Record<'all' | 'gitlab' | 'github', number> {
  const out = { all: 0, gitlab: 0, github: 0 };
  for (const i of items) {
    if ((v.project && i.gl !== v.project) || !matchesQuery(i, v.query)) continue;
    out.all++;
    out[isGh(i) ? 'github' : 'gitlab']++;
  }
  return out;
}

/** the view / rail / pill name: GitLab, GitHub (only its module on) or Inbox (both on) - from Inbox or the summary */
export function inboxName(d: { status: string; github?: unknown } | null | undefined): string {
  if (!d?.github) return 'GitLab';
  return d.status === 'off' ? 'GitHub' : 'Inbox';
}

/** A1: the palette rows of the inbox, named by the forge (GitLab inbox / GitHub inbox / Inbox, not "Inbox inbox"); the automation rules
 *  are GitLab's (one rule per GitLab path), so GitHub alone has no automation row */
export function inboxCommands(forge: string, gitlabOn: boolean, fresh: number): { label: string; hint: string; act: 'focus' | 'refresh' | 'rules' }[] {
  const what = gitlabOn ? 'assigned, mentions, MRs' : 'assigned, mentions, PRs';
  const rows: { label: string; hint: string; act: 'focus' | 'refresh' | 'rules' }[] = [
    { label: forge === 'Inbox' ? 'Inbox' : `${forge} inbox`, hint: fresh ? `${fresh} new · ${what}` : what, act: 'focus' },
    { label: `Refresh ${forge}`, hint: 'load the inbox now', act: 'refresh' },
  ];
  if (gitlabOn) rows.push({ label: 'GitLab automation…', hint: 'automatic analysis of new assignments', act: 'rules' });
  return rows;
}

/** #iid for an issue and a GitHub PR, !iid for a GitLab MR */
export const ref = (i: Pick<InboxItem, 'type' | 'iid'> & { gl?: string }): string => `${i.type === 'mr' && !i.gl?.startsWith('gh:') ? '!' : '#'}${i.iid}`;

/** search: every word must hit the title, #iid / !iid, the project, the GitLab path or a label
 *  (case and diacritics ignored) */
export function matchesQuery(i: InboxItem, query: string): boolean {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = fold([i.title, `#${i.iid}`, `!${i.iid}`, String(i.iid), i.gl, i.project ?? '', ...i.labels].join(' \u0001 '));
  return words.every((w) => hay.includes(w));
}

/** 9.43: the GitLab / GitHub segment; '' = both */
export type Forge = '' | 'gitlab' | 'github';

/** the item lives in that forge ('' = any) */
export const matchesForge = (i: Pick<InboxItem, 'gl'>, f: Forge | undefined): boolean => !f || (f === 'github') === isGh(i);

export interface View {
  filter: Filter;
  /** GitLab path, '' = all */
  project: string;
  query: string;
  /** 9.43: GitLab / GitHub only ('' or missing = both) */
  forge?: Forge;
}

/** the chip counts: what each chip would show under the current forge + project + search */
export function filterCounts(items: InboxItem[], v: Omit<View, 'filter'>): Record<Filter, number> {
  const base = items.filter((i) => matchesForge(i, v.forge) && (!v.project || i.gl === v.project) && matchesQuery(i, v.query));
  const out = { all: 0, new: 0, assigned: 0, mentions: 0, mr: 0 } as Record<Filter, number>;
  for (const i of base) for (const f of FILTERS) if (matchesFilter(i, f.id)) out[f.id]++;
  return out;
}

export function visibleItems(items: InboxItem[], v: View, keep: Set<string> = new Set()): InboxItem[] {
  return items.filter((i) => keep.has(i.key)
    || (matchesForge(i, v.forge) && (!v.project || i.gl === v.project) && matchesQuery(i, v.query) && matchesFilter(i, v.filter)));
}

/** states in which a linked session counts as live (the primary action continues it) */
const LINK_LIVE = ['preflight', 'preparing', 'spawning', 'trust', 'starting', 'attesting', 'ready', 'working', 'waiting', 'attention'];
/** states in which piperd can type a follow-up into it */
const FOLLOW_UP = ['ready', 'waiting', 'attention'];
/** W8: an ended linked session with a conversation goes on (resume + /compact + the new task) instead of a new one */
const RESUMABLE = ['blocked', 'error', 'exited'];

export type Primary =
  | { kind: 'continue'; session: Session; followUp: boolean }
  | { kind: 'resume'; session: Session }
  | { kind: 'open' }
  | { kind: 'add'; candidate: { path: string; key: string } }
  | { kind: 'choose'; candidates: { path: string; key: string }[] }
  | { kind: 'none' };

/** W9: the comment a task answers (older piperd: the first new one) */
export function targetNote(i: Pick<InboxItem, 'target_note' | 'first_new_note'>): number | null {
  return i.target_note !== undefined ? i.target_note : i.first_new_note;
}

/** the one obvious next step of a row */
export function primaryAction(i: InboxItem, sessions: Session[]): Primary {
  for (const id of i.sessions) {
    const s = sessions.find((x) => x.id === id);
    if (s && !s.hidden && LINK_LIVE.includes(s.state)) {
      return { kind: 'continue', session: s, followUp: i.new_notes > 0 && FOLLOW_UP.includes(s.state) && s.pid !== null
        && s.at_prompt !== false };   // a permission dialog: piperd would refuse (older piperd: no flag, as before)
    }
  }
  for (const id of i.sessions) {   // newest first: the last session that worked on it
    const s = sessions.find((x) => x.id === id);
    if (s && !s.hidden && RESUMABLE.includes(s.state) && s.claude_session) return { kind: 'resume', session: s };
  }
  if (i.project) return { kind: 'open' };
  if (i.candidates.length === 1) return { kind: 'add', candidate: i.candidates[0]! };
  if (i.candidates.length > 1) return { kind: 'choose', candidates: i.candidates };
  return { kind: 'none' };
}

export interface Snapshot {
  notes: number;
  todos: number[];
  auto: number;
}

export type InboxEvent =
  | { kind: 'new'; item: InboxItem; reason: string }
  | { kind: 'todo'; item: InboxItem; reason: string }
  | { kind: 'notes'; item: InboxItem; added: number }
  | { kind: 'auto'; item: InboxItem; session: string };

/** N10-D-10: the user marked the item unread - piperd then counts its old comments as new: not news (a toast said
 *  "1 new comment"). The snapshot takes every comment of the item as already known (notes_count bounds new_notes). */
export function markedUnread(prev: Map<string, Snapshot>, i: Pick<InboxItem, 'key' | 'notes_count'>): void {
  const sn = prev.get(i.key);
  if (sn) sn.notes = Math.max(sn.notes, i.notes_count);
}

export function snapshot(items: InboxItem[]): Map<string, Snapshot> {
  return new Map(items.map((i) => [i.key, { notes: i.new_notes, todos: i.todos.map((t) => t.id), auto: i.auto.length }]));
}

/** what changed since the previous list, as things worth a notification. `prev` null = the first
 *  list the GUI sees: nothing is news then (piperd's own baseline covers its first poll). */
export function diffInbox(prev: Map<string, Snapshot> | null, items: InboxItem[]): InboxEvent[] {
  if (!prev) return [];
  const out: InboxEvent[] = [];
  for (const i of items) {
    const was = prev.get(i.key);
    const human = sortedReasons(i.reasons).find((r) => HUMAN_REASONS.includes(r));
    if (!was) {
      if (human) out.push({ kind: 'new', item: i, reason: human });
      else if (i.new_notes > 0) out.push({ kind: 'notes', item: i, added: i.new_notes });
    } else {
      const fresh = i.todos.filter((t) => !was.todos.includes(t.id) && HUMAN_REASONS.includes(t.action));
      if (fresh.length) {
        out.push({ kind: 'todo', item: i, reason: sortedReasons(fresh.map((t) => t.action))[0]! });
      } else if (i.new_notes > was.notes) {
        out.push({ kind: 'notes', item: i, added: i.new_notes - was.notes });
      }
    }
    if (i.auto.length > (was?.auto ?? 0)) out.push({ kind: 'auto', item: i, session: i.auto[i.auto.length - 1]!.session });
  }
  return out;
}

/** Keep GitLab usable quietly, without silencing GitHub updates. */
export function notificationEvents(events: InboxEvent[], enabled: (forge: string) => boolean): InboxEvent[] {
  return events.filter(e => enabled(e.item.forge ?? 'gitlab'));
}

/** The notification / toast wording of an event. */
export function eventText(e: InboxEvent): { title: string; body: string } {
  const i = e.item;
  const body = `${projectLabel(i)} · ${ref(i)} ${i.title}`;
  switch (e.kind) {
    case 'new':
    case 'todo': return { title: `${reasonInfo(e.reason).news}: ${ref(i)} ${i.title}`, body };
    case 'notes': return { title: `${newNotesLabel(e.added)} in ${ref(i)} ${i.title}`, body };
    case 'auto': return { title: `Analysis of ${ref(i)} started automatically`, body };
  }
}

export interface FlowStep {
  id: 'analysis' | 'impl' | 'verify' | 'comment' | 'commit' | 'push';
  label: string;
  /** done | the next expected one | skipped (a later step is done) | not yet */
  state: 'done' | 'next' | 'skipped' | 'todo';
  /** the draft exists but is not sent */
  partial: boolean;
  title: string;
}

/** the workflow strip of a board card: Analysis → Implementation → Review → Comment → Commit → Push */
export function flowSteps(f: BoardFlow): FlowStep[] {
  const raw: Omit<FlowStep, 'state'>[] = [
    { id: 'analysis', label: 'Analysis', partial: false,
      title: 'Analysis: Claude wrote notes or an analysis to development/*.md (other than the comment draft)' },
    { id: 'impl', label: 'Implementation', partial: false,
      title: 'Implementation: Claude changed files outside development/ (Write/Edit in the transcript)' },
    { id: 'verify', label: f.verify > 1 ? `Review ×${f.verify}` : 'Review', partial: false,
      title: `Review: an independent review (/verify or the independent-reviewer agent) - ${f.verify}× so far` },
    { id: 'comment', label: 'Comment', partial: !!f.draft && !f.draft.sent,
      title: f.draft ? (f.draft.sent ? `Comment: ${f.draft.path} is sent to ${f.draft.path.includes('/github-') ? 'GitHub' : 'GitLab'}` : `Comment: the draft ${f.draft.path} is waiting to be reviewed and sent from the GUI`)
        : 'Comment: a comment draft in development/ (the session\'s task names the file); ticked once sent from the GUI' },
    { id: 'commit', label: f.commits > 1 ? `Commit ×${f.commits}` : 'Commit', partial: false,
      title: `Commit: your commits since the session was created (${f.commits})` },
    { id: 'push', label: 'Push', partial: false,
      title: f.pushed ? 'Push: the branch is on the server, nothing waiting' : f.ahead ? `Push: ${f.ahead} ${plural(f.ahead, 'commit waits', 'commits wait', 'commits wait')} for a push` : 'Push: git @{u}..HEAD' },
  ];
  const done = [f.analysis, f.impl, f.verify > 0, !!f.draft?.sent, f.commits > 0, f.pushed && f.commits > 0];
  const last = done.lastIndexOf(true);
  const next = done.findIndex((d, k) => !d && k > last);
  return raw.map((s, k) => ({ ...s, state: done[k] ? 'done' : k === next ? 'next' : k < last ? 'skipped' : 'todo' }));
}

/** a session that never started (blocked before its spawn) did no work: the repository's uncommitted
 *  files and commits are not its steps - the comment draft (the issue's) stays */
export function sessionFlow(f: BoardFlow, s: Pick<Session, 'pid' | 'claude_session'>): BoardFlow {
  return s.pid || s.claude_session ? f : { ...f, analysis: false, impl: false, verify: 0, commits: 0, ahead: null, pushed: false };
}

/** a rule's `since`: now when it becomes enabled, else what it had */
export function stampRules(rules: InboxRule[], before: InboxRule[], now: number): InboxRule[] {
  return rules.map((r) => {
    const was = before.find((b) => b.id === r.id);
    return { ...r, since: r.enabled && !(was?.enabled) ? now : was?.since ?? r.since ?? now };
  });
}

// ---------------------------------------------------------------- GUI

export interface InboxDeps {
  notifications?(forge: string): boolean;
  sessions(): Session[];
  /** projects.json + discovered: their gitlab paths join the automation dialog */
  projects(): { key: string; gitlab: string[] }[];
  label(state: string): string;
  select(id: string): void;
  /** switch to a session as soon as /api/state lists it */
  openSession(id: string): void;
  /** poll /api/state now */
  kick(): void;
  started?(result: SessionStartResult | null): void;
  toast(text: string, action?: { label: string; run(): void }): void;
  notify(title: string, body: string, tag: string, onClick: () => void): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  /** the new-session dialog prefilled; `launch` starts it (false = keep the dialog) */
  editTask(p: { project: string; task: string; title: string; launch(project: string, task: string, opts: StartOpts, agent: Agent, remember: boolean, runtime: 'hole' | 'direct'): Promise<boolean> }): void;
  /** make Overview the view (the inbox lives there) */
  showBoard(): void;
  boardVisible(): boolean;
  /** Settings on one section (GitLab token) */
  openSettings(section: string): void;
  /** Esc with no preview open: the keyboard back to the queue (like the queue's Esc goes back to the detail) */
  back?(): void;
}

interface Row {
  el: HTMLElement;
  main: HTMLElement;
  act: HTMLElement;
  pv: HTMLElement;
  sig: string;
}

interface PreviewState {
  data: InboxPreview | null;
  error: string;
  /** the item's notes_count / updated_at the preview was loaded at */
  at: string;
  loading: boolean;
  seq: number;
}

const COLLAPSE_KEY = 'piper.inbox.collapsed';
const GROUP_LIMIT = 6;
const VIEW_KEY = 'piper.inbox.view';
const SEEN_AFTER_MS = 1500;
const TOKEN_CMD = 'sh scripts/piper.sh gitlab-token < pat.txt';

function readJson<T>(key: string, dflt: T): T {
  try {
    const v = JSON.parse(machineStorage(window.localStorage).getItem(key) ?? 'null') as T | null;
    return v ?? dflt;
  } catch {
    return dflt;
  }
}

function writeJson(key: string, v: unknown): void {
  try {
    machineStorage(window.localStorage).setItem(key, JSON.stringify(v));
  } catch {
    /* private window: lasts until reload */
  }
}

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));
const isoAgo = (iso: string | null | undefined): string => {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(t) ? ago(t / 1000) : '';
};
/** "5 min ago" / "just now" of an ISO time ('' without one) - never "now ago" */
export const isoAgoText = (iso: string | null | undefined): string => {
  const a = isoAgo(iso);
  return a === '' ? '' : a === 'now' ? 'just now' : `${a} ago`;
};
const isoDate = (iso: string | null | undefined): string => {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(t) ? new Date(t).toLocaleString() : '';
};

export function createInbox(deps: InboxDeps) {
  let data: Inbox | null = null;
  const files = (): IssueFiles => data?.issue_files ?? ISSUE_FILES;
  let summary: InboxSummary | undefined;
  let lastV: number | null = null;
  let loadErr = '';
  let fetching = false;
  let refetch = false;
  let refreshing = false;
  let prev: Map<string, Snapshot> | null = null;
  const saved = readJson<Partial<View>>(VIEW_KEY, {});
  const view: View = { filter: FILTERS.some((f) => f.id === saved.filter) ? saved.filter! : 'all', project: '', query: '',
    forge: saved.forge === 'gitlab' || saved.forge === 'github' ? saved.forge : '' };
  const collapsed = new Set<Group>(readJson<Group[]>(COLLAPSE_KEY, ['mine']));
  // a long group shows its first GROUP_LIMIT rows (the sessions below stay in reach); a filter or search shows all
  const showAll = new Set<Group>();
  const rows = new Map<string, Row>();
  const expanded = new Set<string>();
  const pinned = new Map<string, Group>();
  const previews = new Map<string, PreviewState>();
  const busy = new Set<string>();
  const seenTimers = new Map<string, number>();
  let focusKey = '';

  // ---- skeleton of the section (built once; draw() only fills it)
  const el = h('section', { class: 'inbox hidden', 'aria-label': 'GitLab inbox' });
  const title = h('h3', { class: 'ibx-title' }, 'GitLab');
  const updated = h('span', { class: 'ibx-updated mut small', role: 'status' });
  const refreshBtn = h('button', { class: 'icon', title: 'load GitLab now', 'aria-label': 'Refresh GitLab', onclick: () => void refresh() }, fi('refresh', 'g gi')) as HTMLButtonElement;
  // F15: Mark all as read and Automation… live in the header's ⋯ (the counts and accounts in its foot)
  const headMore = h('button', { class: 'icon ibx-hmore', 'aria-label': 'inbox actions', 'aria-haspopup': 'menu', 'aria-expanded': 'false', title: 'Mark all as read, Automation…' }, '⋯') as HTMLButtonElement;
  headMore.addEventListener('click', () => openHeadMenu());
  const chips = h('div', { class: 'ibx-chips', role: 'group', 'aria-label': 'filter' });
  const chipEls = new Map<Filter, HTMLButtonElement>();
  for (const f of FILTERS) {
    const b = h('button', { class: 'pill', title: f.title, 'aria-pressed': 'false', onclick: () => setFilter(f.id) }) as HTMLButtonElement;
    chipEls.set(f.id, b);
    chips.append(b);
  }
  // 9.43: All · GitLab · GitHub in front of the chips, only while the inbox holds both
  const forgeSeg = h('div', { class: 'ibx-forge', role: 'group', 'aria-label': 'GitLab or GitHub' });
  const forgeEls = new Map<Forge, HTMLButtonElement>();
  for (const [f, label] of [['', 'All'], ['gitlab', 'GitLab'], ['github', 'GitHub']] as [Forge, string][]) {
    const b = h('button', { class: 'pill', title: f ? `only ${label}` : 'GitLab and GitHub', 'aria-pressed': 'false', onclick: () => setForge(f) }) as HTMLButtonElement;
    b.dataset['label'] = label;
    forgeEls.set(f, b);
    forgeSeg.append(b);
  }
  const projSel = h('select', { class: 'ibx-proj', 'aria-label': 'project' }) as HTMLSelectElement;
  projSel.addEventListener('change', () => { view.project = projSel.value; draw(); });
  let projSig = '';
  const search = h('input', { type: 'search', class: 'ibx-search', placeholder: 'Search title, #id, project, label', autocomplete: 'off', 'aria-label': 'search the inbox' }) as HTMLInputElement;
  const searchBox = h('label', { class: 'ibx-sbox' }, h('span', { class: 'ibx-sglyph', 'aria-hidden': 'true' }, h('i', { class: 'fi fi-search' })), search, h('kbd', { 'aria-hidden': 'true', title: '/ focuses the search' }, '/'));
  let typing = 0;
  search.addEventListener('input', () => { clearTimeout(typing); typing = window.setTimeout(() => { view.query = search.value; draw(); }, 120); });
  search.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); focusRow(orderedKeys()[0] ?? ''); }
    else if (e.key === 'Escape' && search.value) { e.preventDefault(); e.stopPropagation(); search.value = ''; view.query = ''; draw(); }
  });
  const statusEl = h('div', { class: 'ibx-status' });
  const list = h('div', { class: 'ibx-list' });
  const groupEls = new Map<Group, { box: HTMLElement; head: HTMLButtonElement; rows: HTMLElement; more: HTMLButtonElement }>();
  for (const g of GROUPS) {
    const head = h('button', { class: 'ibx-ghead sec-h', 'aria-expanded': 'true', onclick: () => toggleGroup(g.id) }) as HTMLButtonElement;
    const rs = h('div', { class: 'ibx-rows', role: 'list' });
    const more = h('button', { class: 'link small ibx-showmore hidden', onclick: () => toggleAll(g.id) }) as HTMLButtonElement;
    const box = h('div', { class: `ibx-group g-${g.id}` }, head, rs, more);
    groupEls.set(g.id, { box, head, rows: rs, more });
    list.append(box);
  }
  const empty = h('div', { class: 'ibx-empty mut hidden' });
  const keysHint = h('p', { class: 'ibx-keys mut small' }, '↑↓ / j k move · Enter main action · Space / o preview · e read · g open ↗ · / search · Esc back');
  el.append(
    h('div', { class: 'ibx-head' }, title, chips, h('span', { class: 'grow' }), updated, refreshBtn, headMore),
    h('div', { class: 'ibx-tools' }, searchBox, projSel, forgeSeg),
    statusEl, list, empty, keysHint);
  list.addEventListener('keydown', onKey);
  list.addEventListener('focusin', (e) => {
    const r = (e.target as HTMLElement).closest('.irow') as HTMLElement | null;
    if (r?.dataset['key']) setFocusKey(r.dataset['key']);
  });
  window.setInterval(() => {
    if (!el.isConnected) return;
    drawUpdated();
    for (const a of list.querySelectorAll('.iage')) a.textContent = markBidi(isoAgo((a as HTMLElement).dataset['iso']));
  }, 20000);

  // the preview counts as read after it has been on screen for a moment
  const io = typeof IntersectionObserver === 'function' ? new IntersectionObserver((entries) => {
    for (const en of entries) {
      const key = (en.target as HTMLElement).dataset['key'] ?? '';
      if (en.isIntersecting && !document.hidden) armSeen(key);
      else disarmSeen(key);
    }
  }, { threshold: 0.25 }) : null;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !io) return;
    for (const k of expanded) {
      const r = rows.get(k);
      if (r) { io.unobserve(r.pv); io.observe(r.pv); }
    }
  });

  // ---------------------------------------------------------------- data
  function sync(s: InboxSummary | undefined): void {
    summary = s;
    el.classList.toggle('hidden', !s);
    if (!s) return;
    if (s.v !== lastV) {
      lastV = s.v;
      void load();
    } else if (!data) drawStatus();
  }

  async function load(): Promise<void> {
    if (fetching) {
      refetch = true;
      return;
    }
    fetching = true;
    try {
      const r = await api.inbox();
      if (r.data) apply(r.data);
      loadErr = '';
    } catch (e) {
      loadErr = errText(e);
      draw();
    } finally {
      fetching = false;
      if (refetch) {
        refetch = false;
        void load();
      }
    }
  }

  function apply(d: Inbox): void {
    data = d;
    if ((d.status !== 'starting' && d.status !== 'no-token' && d.status !== 'off' && d.at !== null) || (d.github?.status === 'ok' && d.github.at !== null)) {
      const events = diffInbox(prev, d.items);
      prev = snapshot(d.items);
      announce(events);
    }
    for (const k of [...expanded]) if (!d.items.some((i) => i.key === k)) collapse(k, false);
    draw();
  }

  function item(key: string): InboxItem | undefined {
    return data?.items.find((i) => i.key === key);
  }

  /** a local change before piperd's next list (the refetch on v replaces it) */
  function patch(key: string, fn: (i: InboxItem) => void, redraw = true): void {
    const i = item(key);
    if (i) fn(i);
    if (prev && i) prev = snapshot(data!.items);   // a local change is not news
    if (redraw) draw();
  }

  function announce(events: InboxEvent[]): void {
    events = notificationEvents(events, forge => deps.notifications?.(forge) !== false);
    if (!events.length) return;
    const quiet = !document.hidden && deps.boardVisible();
    if (events.length > 3) {
      const t = `${events.length} ${plural(events.length, 'update', 'updates', 'updates')} in GitLab`;
      if (!quiet) deps.notify(t, events.slice(0, 4).map((e) => eventText(e).title).join('\n'), 'inbox', () => reveal(''));
      deps.toast(t, { label: 'Open', run: () => reveal(events[0]!.item.key) });
      return;
    }
    for (const e of events) {
      const t = eventText(e);
      const open = e.kind === 'auto' ? () => deps.select(e.session) : () => reveal(e.item.key);
      if (!quiet || e.kind === 'auto') deps.notify(t.title, t.body, `inbox:${e.item.key}:${e.kind}`, open);
      deps.toast(t.title, { label: 'Open', run: open });
    }
  }

  async function refresh(): Promise<void> {
    if (refreshing) return;
    refreshing = true;
    drawUpdated();
    try {
      const r = await api.inboxRefresh();
      if (r.data) apply(r.data);
    } catch (e) {
      deps.toast(`Could not load GitLab: ${errText(e)}`);
    } finally {
      refreshing = false;
      drawUpdated();
    }
  }

  async function markSeen(keys: string[] | 'all'): Promise<void> {
    const list2 = keys === 'all' ? (data?.items ?? []).filter(isFresh).map((i) => i.key) : keys;
    for (const k of list2) patch(k, (i) => { i.new = false; i.new_notes = 0; }, false);
    draw();
    try {
      await api.inboxSeen(keys);
    } catch (e) {
      deps.toast(`Could not mark as read: ${errText(e)}`);
      void load();
    }
  }

  async function markAllSeen(): Promise<void> {
    const filtered = !!(view.project || view.query.trim() || view.filter !== 'all');
    const keys = filtered ? visibleItems(data?.items ?? [], view).filter(isFresh).map((i) => i.key) : 'all';
    if (keys !== 'all' && !keys.length) return;
    await markSeen(keys);
  }

  async function toggleSeen(i: InboxItem): Promise<void> {
    if (isFresh(i)) return markSeen([i.key]);
    patch(i.key, (x) => { x.new = true; });
    if (prev) markedUnread(prev, i);
    try {
      await api.inboxUnseen(i.key);
    } catch (e) {
      deps.toast(`Could not mark as unread: ${errText(e)}`);
      void load();
    }
  }

  // ---------------------------------------------------------------- actions
  async function withBusy(key: string, fn: () => Promise<void>): Promise<void> {
    if (busy.has(key)) return;
    busy.add(key);
    draw();
    try {
      await fn();
    } catch (e) {
      deps.toast(errText(e));
    } finally {
      busy.delete(key);
      draw();
    }
  }

  function primary(i: InboxItem): void {
    const p = primaryAction(i, deps.sessions());
    if (p.kind === 'continue') {
      if (!p.followUp) return deps.select(p.session.id);
      void withBusy(i.key, async () => {
        await api.inboxStart({ key: i.key, session: p.session.id });
        patch(i.key, (x) => { x.new = false; x.new_notes = 0; });
        deps.toast(`New comment on ${ref(i)} handed to ${p.session.title}`);
        deps.select(p.session.id);
      });
    } else if (p.kind === 'resume') resumeTask(i, p.session);
    else if (p.kind === 'open') editTask(i, 'work');
    else openCheckout(i, p.kind === 'add' ? [p.candidate] : p.kind === 'choose' ? p.candidates : []);
  }

  /** F15: "Add checkout…" - the discovered checkouts of the project as a choice (the paths live here, not in the row);
   *  none found = how to get one */
  function openCheckout(i: InboxItem, cands: { path: string; key: string }[]): void {
    const where = isGh(i) ? i.gl.slice(3) : i.gl;
    if (!cands.length) {
      deps.modal(`Add checkout: ${where}`, h('p', {}, noCheckoutText(i)),
        h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Close')));
      return;
    }
    const name = `ibx-co-${i.iid}`;
    const radios = cands.map((c, k) => {
      const rb = h('input', { type: 'radio', name, value: c.path, checked: k === 0 }) as HTMLInputElement;
      return h('label', { class: 'ibx-co-opt' }, rb, h('span', { class: 'ibx-co-path' }, c.path));
    });
    const go = h('button', { class: 'primary' }, 'Add checkout') as HTMLButtonElement;
    go.addEventListener('click', () => {
      const pick = (radios.map((l) => l.querySelector('input') as HTMLInputElement).find((x) => x.checked) ?? null)?.value;
      if (!pick) return;
      deps.closeModal();
      void addProject(i, pick);
    });
    deps.modal(`Add checkout: ${where}`,
      h('p', { class: 'mut small' }, `${cands.length > 1 ? 'These checkouts under PROJECT_PATH have' : 'This checkout under PROJECT_PATH has'} the origin ${where}. The chosen one goes to projects.json; then you can open a session.`),
      h('div', { class: 'ibx-co-list', role: 'radiogroup', 'aria-label': 'checkout' }, ...radios),
      h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), go));
    ((radios[0]?.querySelector('input') as HTMLInputElement | null) ?? go).focus();
  }

  function addProject(i: InboxItem, path: string): Promise<void> {
    return withBusy(i.key, async () => {
      const r = (await api.projectAdd(path, i.gl)).data;
      if (!r?.key) return;
      for (const x of data?.items ?? []) if (x.gl === i.gl) { x.project = r.key; x.candidates = []; }
      deps.kick();
      deps.toast(`Project ${r.key} added to projects.json (${path}) - you can open a session now`);
      window.requestAnimationFrame(() => rows.get(i.key)?.el.focus({ preventScroll: true }));
    });
  }

  const noCheckoutText = (i: InboxItem) =>
    `There is no checkout under PROJECT_PATH whose git origin is ${i.gl}. Clone it there (or add it to projects.json with ${isGh(i) ? `"github": ["${i.gl.slice(3)}"]` : `"gitlab": ["${i.gl}"]`}) and refresh the inbox.`;

  /** every new session from the inbox goes through the new-session dialog: task + start options (DinD, network, IDEA, agent) */
  function editTask(i: InboxItem, mode: 'work' | 'analyze' = 'work'): void {
    const project = i.project ?? i.candidates[0]?.key ?? '';
    const note = targetNote(i);
    const prefilled = taskFor(i, mode, note, files());
    deps.editTask({
      project, title: `${ref(i)} ${i.title}`.slice(0, 120), task: prefilled,
      launch: async (proj, task, opts, agent, remember, runtime) => {
        try {
          // an untouched task is left to piperd: it appends the issue state hint (9.12 D)
          const r = (await api.inboxStart({ key: i.key, mode, runtime, ...(proj && proj !== i.project ? { project: proj } : {}), ...(note ? { note_id: note } : {}), ...(task !== prefilled ? { task } : {}), ...(Object.keys(opts).length ? { opts } : {}), ...(agent !== 'claude' ? { agent } : {}), ...(remember ? { remember_for_project: true } : {}) })).data;
          deps.started?.(r);
          deps.kick();
          patch(i.key, (x) => { x.new = false; x.new_notes = 0; if (r?.id) x.sessions = [r.id, ...x.sessions]; });
          if (!r?.id) return true;
          if (mode === 'work') deps.openSession(r.id);
          else deps.toast(`Analysis of ${ref(i)} started in ${proj || i.project || ''} - it changes and sends nothing`, { label: 'Open', run: () => deps.select(r.id) });
          return true;
        } catch (e) {
          deps.toast(`Could not start the session: ${errText(e)}`);
          return false;
        }
      },
    });
  }

  /** W8: the session that worked on the item goes on - its conversation resumed and compacted, then this task */
  function resumeTask(i: InboxItem, s: Session): void {
    const note = targetNote(i);
    const prefilled = taskFor(i, 'work', note, files());
    const ta = h('textarea', { rows: '10', class: 'ibx-resume-task', 'aria-label': 'the task for the resumed session' }) as HTMLTextAreaElement;
    ta.value = prefilled;
    const go = h('button', { class: 'primary', title: sendHint('doc') }, kbd('Resume ⌘⏎', 'Ctrl')) as HTMLButtonElement;
    // W13: a document field - ⌘⏎ sends, as the new session's task
    ta.addEventListener('keydown', (e) => { if (isSend(e, 'doc')) { e.preventDefault(); e.stopPropagation(); go.click(); } });
    go.addEventListener('click', () => {
      const task = ta.value;
      if (!task.trim()) return deps.toast('The task is empty');
      go.disabled = true;
      void withBusy(i.key, async () => {
        try {
          // an untouched task is left to piperd: it appends the issue state hint (9.12 D)
          await api.inboxStart({ key: i.key, session: s.id, ...(note ? { note_id: note } : {}), ...(task !== prefilled ? { task } : {}) });
          patch(i.key, (x) => { x.new = false; x.new_notes = 0; });
          deps.closeModal();
          deps.kick();
          deps.toast(`${s.title} resumes: the conversation is compacted first, then it gets the task`);
          deps.openSession(s.id);
        } catch (e) {
          go.disabled = false;
          deps.toast(`Could not resume the session: ${errText(e)}`);
        }
      });
    });
    // R1: which session goes on and how far behind the issue it is; a fresh one instead is one click away
    const seen = i.seen_at ? new Date(i.seen_at * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
    const behind = [seen ? `Last read: ${seen}` : 'Not read here yet', i.new_notes ? `${newNotesLabel(i.new_notes)} since` : 'no new comments'].join(' · ');
    deps.modal(`${ref(i)} already has a session`,
      h('div', { class: 'ibx-resume-card' },
        h('b', {}, markBidi(`${s.title} · ${s.state === 'exited' ? 'stopped' : s.state} ${agoText(s.since)}`)),
        h('span', { class: 'mut small' }, behind)),
      h('p', { class: 'mut small' }, 'Resume compacts that conversation (/compact) and then gives it the task below - the newest comment, editable.'),
      ta,
      h('div', { class: 'row ibx-resume-foot' }, h('button', { onclick: () => { deps.closeModal(); editTask(i); }, title: 'a fresh session instead of resuming the one that worked on it' }, 'New session instead'),
        go));
    ta.focus();
    ta.setSelectionRange(0, 0);   // the task reads from its first line
    ta.scrollTop = 0;
  }

  function openGitlab(i: InboxItem): void {
    window.open(i.web_url, '_blank', 'noopener');
  }

  function confirmDone(i: InboxItem): void {
    const gh = isGh(i);
    if (!i.todos.length) return deps.toast(gh ? 'The item has no unread GitHub notification' : 'The item has no pending to-do in GitLab');
    const go = h('button', { class: 'primary' }, gh ? 'Mark read in GitHub' : `Mark ${i.todos.length} to-do as done`) as HTMLButtonElement;
    go.addEventListener('click', () => {
      go.disabled = true;
      go.textContent = 'Closing…';
      doneTodos(i).then(() => deps.closeModal()).catch((e: unknown) => {
        go.disabled = false;
        go.textContent = 'Try again';
        deps.toast(`Could not close the to-do: ${errText(e)}`);
      });
    });
    deps.modal(gh ? `Mark read in GitHub: ${ref(i)}` : `Done in GitLab: ${ref(i)}`,
      h('p', {}, gh ? 'Marks the GitHub notification read (the issue or PR itself stays open):'
        : 'Closes these pending to-dos in GitLab (the issue itself stays open and assigned):'),
      h('ul', { class: 'ibx-todos' }, ...i.todos.map((t) => h('li', {},
        h('span', { class: `ibx-reason ${reasonInfo(t.action).cls}` }, reasonInfo(t.action).label), ' ',
        h('b', {}, t.author), h('span', { class: 'mut small' }, ` · ${isoAgoText(t.created_at)}`),
        t.body ? h('div', { class: 'mut small ibx-todo-body' }, t.body) : null))),
      h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), go));
    go.focus();
  }

  async function doneTodos(i: InboxItem): Promise<number> {
    const n = (await api.inboxDone(i.key)).data?.done ?? 0;
    patch(i.key, (x) => { x.todos = []; });
    deps.toast(isGh(i) ? 'Marked read in GitHub' : `Closed ${n} to-do in GitLab`);
    void load();
    return n;
  }

  function commentOn(i: InboxItem, text = '', source: { path: string; mtime: number | null } | null = null): void {
    openComment({ modal: deps.modal, closeModal: deps.closeModal, toast: deps.toast }, {
      key: i.key, target: `${i.gl} ${ref(i)} ${i.title}`, webUrl: i.web_url, text, source,
      todos: i.todos.map((t) => ({ action: reasonInfo(t.action).label, author: t.author })),
      markDone: () => doneTodos(i),
      onSent: () => { const pv = previews.get(i.key); if (pv) pv.at = ''; void load(); },
    });
  }

  /** the draft of a session (board card button) in the comment dialog */
  async function openDraft(sessionId: string, issue: SessionIssue | null | undefined): Promise<void> {
    try {
      const d = (await api.draft(sessionId)).data;
      if (!d?.path) return deps.toast(`The session has no comment draft yet (development/${issue?.gl.startsWith('gh:') ? 'github-<number>' : 'gitlab-<iid>'}-${files().comment}.md).`);
      const key = d.key ?? issue?.key ?? '';
      const it = item(key);
      const base: InboxItem = it ?? {
        key, gl: issue?.gl ?? '', type: issue?.type ?? 'issue', iid: issue?.iid ?? 0, title: issue?.title ?? '', state: '',
        web_url: issue?.web_url ?? '', labels: [], author: '', updated_at: '', due_date: null, milestone: null, reasons: [],
        todos: [], notes_count: 0, new_notes: 0, first_new_note: null, new: false, seen_at: null, project: null,
        candidates: [], sessions: [], auto: [],
      };
      if (!key) return deps.toast('The draft is not linked to any issue.');
      commentOn(base, d.text ?? '', { path: d.path, mtime: d.mtime ?? null });
    } catch (e) {
      deps.toast(`Could not load the draft: ${errText(e)}`);
    }
  }

  // ---------------------------------------------------------------- ⋯ menu
  /** W11: the one menu component; a disabled item says why (its title) */
  function openMenu(i: InboxItem, anchor: HTMLButtonElement): void {
    const mapped = !!i.project;
    const noProject = !mapped && 'add the project (checkout) first';
    const entries: MenuEntry[] = [
      { label: 'Analyze only', run: () => editTask(i, 'analyze'), disabled: noProject,
        title: 'the session only analyzes and proposes options - it changes and sends nothing' },
      primaryAction(i, deps.sessions()).kind === 'resume'
        ? { label: 'New session instead…', run: () => editTask(i), disabled: noProject, title: 'a fresh session instead of resuming the one that worked on it' }
        : { label: 'Edit task…', run: () => editTask(i), disabled: !i.candidates.length && noProject, title: 'the task for a new session before it starts' },
      { label: 'Write a comment…', run: () => commentOn(i) },
      null,
      { label: `Open in ${forgeName(i)} ↗`, run: () => openGitlab(i) },
      { label: isFresh(i) ? 'Mark as read' : 'Mark as unread', run: () => void toggleSeen(i) },
      isGh(i)   // W23: GitHub's to-do is the notification thread
        ? { label: 'Mark read in GitHub…', run: () => confirmDone(i), disabled: !i.todos.length && 'no unread notification',
          title: 'marks the GitHub notification read' }
        : { label: i.todos.length ? `Done in GitLab (${i.todos.length} to-do)…` : 'Done in GitLab…', run: () => confirmDone(i),
          disabled: !i.todos.length && 'no pending to-do', title: 'closes the pending to-dos in GitLab' },
    ];
    toggleMenu(anchor, () => entries, { label: `actions ${ref(i)}` });
  }

  // ---------------------------------------------------------------- preview
  function toggle(key: string): void {
    if (expanded.has(key)) collapse(key, true);
    else expand(key);
  }

  function expand(key: string): void {
    const i = item(key);
    if (!i) return;
    expanded.add(key);
    pinned.set(key, groupOf(i));
    draw();   // creates the row when a filter hid it, syncs the box and loads the preview
  }

  function collapse(key: string, redraw: boolean): void {
    expanded.delete(key);
    pinned.delete(key);
    disarmSeen(key);
    syncOpen(key);
    if (redraw) draw();
  }

  /** the row's preview box follows `expanded` */
  function syncOpen(key: string): void {
    const r = rows.get(key);
    if (!r) return;
    const open = expanded.has(key);
    if (r.el.getAttribute('aria-expanded') === String(open)) return;
    r.el.setAttribute('aria-expanded', String(open));
    r.pv.classList.toggle('hidden', !open);
    if (open) io?.observe(r.pv);
    else io?.unobserve(r.pv);
  }

  const pvAt = (i: InboxItem) => `${i.notes_count}|${i.updated_at}`;

  async function loadPreview(i: InboxItem): Promise<void> {
    let st = previews.get(i.key);
    if (st && (st.loading || (st.at === pvAt(i) && st.data))) return drawPreview(i.key);
    st = st ?? { data: null, error: '', at: '', loading: false, seq: 0 };
    previews.set(i.key, st);
    st.loading = true;
    st.error = '';
    const seq = ++st.seq;
    const at = pvAt(i);
    const first = !st.data;
    drawPreview(i.key);
    try {
      const d = (await api.inboxItem(i.key)).data;
      if (seq !== st.seq) return;
      st.data = d;
      st.at = at;
    } catch (e) {
      if (seq !== st.seq) return;
      st.error = errText(e);
    } finally {
      if (seq === st.seq) st.loading = false;
    }
    drawPreview(i.key, first);
  }

  function armSeen(key: string): void {
    if (seenTimers.has(key)) return;
    const i = item(key);
    const st = previews.get(key);
    if (!i || !isFresh(i) || !st?.data) return;
    seenTimers.set(key, window.setTimeout(() => {
      seenTimers.delete(key);
      const cur = item(key);
      if (cur && isFresh(cur) && expanded.has(key)) void markSeen([key]);
    }, SEEN_AFTER_MS));
  }

  function disarmSeen(key: string): void {
    const t = seenTimers.get(key);
    if (t) window.clearTimeout(t);
    seenTimers.delete(key);
  }

  function drawPreview(key: string, fresh = false): void {
    const r = rows.get(key);
    const st = previews.get(key);
    const i = item(key);
    if (!r || !st || !i) return;
    const box = r.pv;
    const top = box.scrollTop;
    clear(box);
    if (!st.data) {
      if (st.error) {
        box.append(h('p', { class: 'bad' }, `Could not load the preview: ${st.error} `),
          h('button', { class: 'small', onclick: () => { st.at = ''; void loadPreview(i); } }, 'Try again'));
      } else box.append(h('p', { class: 'mut' }, h('span', { class: 'spinner' }), ` loading ${ref(i)}…`));
      return;
    }
    const d = st.data;
    const meta = [d.author && `author ${d.author}`, isoDate(d.created_at) && `created ${isoDate(d.created_at)}`,
      d.assignees.length ? `assignee ${d.assignees.join(', ')}` : 'nobody assigned', d.state && d.state !== 'opened' ? `state ${d.state}` : '',
      i.milestone ? `milestone ${i.milestone}` : '', i.labels.length ? `labels ${i.labels.join(', ')}` : ''].filter(Boolean).join(' · ');   // F15: labels live here
    box.append(h('div', { class: 'ipv-meta mut small' }, meta));
    if (d.mr) box.append(mrBlock(d.mr));
    if (d.state_file) box.append(stateBlock(d.state_file));
    box.append(h('div', { class: 'ipv-desc' }, (d.description ?? '').trim() ? renderRich(d.description!, d.web_url || i.web_url) : h('p', { class: 'mut' }, '(no description)')));
    const notesBox = h('div', { class: 'ipv-notes' });
    const newCount = d.notes.filter((n) => n.new).length;
    notesBox.append(h('div', { class: 'ipv-nhead' }, `Comments (${d.notes.length})`, newCount ? h('span', { class: 'ibx-badge' }, newNotesLabel(newCount)) : null));
    if (!d.notes.length) notesBox.append(h('p', { class: 'mut small' }, 'No comments yet.'));
    let firstNew: HTMLElement | null = null;
    for (const n of d.notes) {
      const ne = h('article', { class: 'ipv-note' + (n.new ? ' new' : '') + (n.mine ? ' mine' : ''), 'aria-label': `comment ${n.author}` },
        h('div', { class: 'ipv-nmeta small' }, h('b', {}, n.author), n.mine ? h('span', { class: 'ibx-you' }, 'you') : null,
          h('span', { class: 'mut', title: isoDate(n.created_at) }, ` · ${isoAgoText(n.created_at)}`),
          n.new ? h('span', { class: 'ibx-newtag' }, 'new') : null),
        renderRich(n.body, d.web_url || i.web_url));
      if (n.new && !firstNew) firstNew = ne;
      notesBox.append(ne);
    }
    box.append(notesBox);
    box.append(h('div', { class: 'ipv-foot' },
      h('button', { class: 'small', onclick: () => commentOn(i) }, 'Write a comment…'),
      h('a', { class: 'btn small', href: i.web_url, target: '_blank', rel: 'noopener noreferrer' }, `Open in ${forgeName(i)} ↗`),
      h('button', { class: 'small', onclick: () => { collapse(key, true); r.el.focus({ preventScroll: true }); } }, 'Collapse')));
    if (fresh && firstNew) {
      const target = firstNew;
      window.requestAnimationFrame(() => { box.scrollTop = Math.max(0, target.offsetTop - box.offsetTop - 8); });
    } else box.scrollTop = top;
    if (r.pv.isConnected && !r.pv.classList.contains('hidden') && io) {   // re-evaluate visibility now that data is in
      io.unobserve(r.pv);
      io.observe(r.pv);
    }
  }

  /** 9.12 D: Status + Waiting on from the issue's status file - plain text (the sandbox writes it) */
  function stateBlock(sf: IssueStateSummary): HTMLElement {
    const box = h('div', { class: 'ipv-state small', title: sf.path },
      h('div', { class: 'ipv-nhead' }, 'Work status ', h('span', { class: 'mut' }, `· ${sf.path} · edited ${agoText(sf.mtime)}`)));
    if (sf.status) box.append(h('div', { class: 'ipv-status' }, sf.status));
    if (sf.waiting) box.append(h('div', { class: 'ipv-waiting' }, h('b', {}, 'Waiting on: '), sf.waiting));
    if (!sf.status && !sf.waiting) box.append(h('p', { class: 'mut' }, 'Neither Status nor "Waiting on" is filled in yet.'));
    return box;
  }

  function mrBlock(mr: NonNullable<InboxPreview['mr']>): HTMLElement {
    const pipe: Record<string, [string, string]> = {
      success: ['success', 'ok'], failed: ['failed', 'bad'], running: ['running', 'run'], pending: ['pending', 'run'],
      created: ['pending', 'run'], canceled: ['canceled', 'mut'], skipped: ['skipped', 'mut'], manual: ['manual step', 'run'],
    };
    const merge: Record<string, [string, string]> = {
      can_be_merged: ['can be merged', 'ok'], cannot_be_merged: ['conflict - cannot merge', 'bad'],
      cannot_be_merged_recheck: ['conflict? checking', 'run'], checking: ['checking', 'run'], unchecked: ['not checked', 'mut'],
    };
    const [pl, pc] = mr.pipeline ? pipe[mr.pipeline] ?? [mr.pipeline, 'mut'] : ['none', 'mut'];
    const [ml, mc] = merge[mr.merge_status] ?? [mr.merge_status, 'mut'];
    return h('div', { class: 'ipv-mr small' },
      h('code', {}, mr.source_branch), ' → ', h('code', {}, mr.target_branch),
      mr.draft ? h('span', { class: 'ibx-pill mut' }, 'draft') : null,
      h('span', { class: `ibx-pill ${pc}` }, `pipeline: ${pl}`),
      h('span', { class: `ibx-pill ${mc}` }, ml));
  }

  // ---------------------------------------------------------------- rows
  function makeRow(key: string): Row {
    const main = h('div', { class: 'irow-main' });
    // F15: the row's actions show on hover / focus / an open preview (ra-row), over the age
    const act = h('div', { class: 'irow-act row-actions' });
    const pv = h('div', { class: 'ipv hidden', 'data-key': key, role: 'region', 'aria-label': 'preview' });
    const rowEl = h('div', { class: 'irow ra-row', role: 'listitem', tabindex: '-1', 'aria-expanded': 'false', 'data-key': key }, main, act, pv);
    main.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).closest('button, a, select')) return;
      toggle(key);
      rowEl.focus({ preventScroll: true });
    });
    return { el: rowEl, main, act, pv, sig: '' };
  }

  function fillRow(r: Row, i: InboxItem): void {
    const sess = i.sessions.map((id) => deps.sessions().find((s) => s.id === id)).filter((s): s is Session => !!s && !s.hidden);
    const p = primaryAction(i, deps.sessions());
    const sig = JSON.stringify([i, sess.map((s) => [s.id, s.state, s.title, s.pid]), p.kind, busy.has(i.key)]);
    r.el.classList.toggle('fresh', isFresh(i));
    r.el.classList.toggle('busy', busy.has(i.key));
    r.el.setAttribute('aria-label', markBidi(`${isGh(i) ? 'GitHub ' : ''}${i.type === 'mr' ? (isGh(i) ? 'pull request' : 'merge request') : 'issue'} ${ref(i)} ${i.title}, ${projectLabel(i)}`));
    if (sig === r.sig) return;
    r.sig = sig;
    const hadFocus = r.el.contains(document.activeElement) && !r.pv.contains(document.activeElement);
    clear(r.main);
    // F15: #id + title (bold while new), under it project · at most two reasons (one coloured action
    // chip, the rest quiet text, "+N") · new comments · due · linked sessions; labels only in the tooltip and preview
    const rr = rowReasons(i.reasons);
    const due = i.due_date ? Date.parse(i.due_date) : NaN;
    const overdue = Number.isFinite(due) && due < Date.now() - 86400000;
    const sep = () => h('span', { class: 'isep', 'aria-hidden': 'true' }, '·');
    const reasonEl = (x: string) => h('span', { class: `ireason ${reasonInfo(x).cls}`, title: reasonInfo(x).hint }, reasonInfo(x).label);
    r.main.append(
      // the gutter marks a new row (bold title + a small dot); the type reads from #id / !id and the row's label
      h('span', { class: `iglyph ${i.type}`, title: isFresh(i) ? (i.new ? 'new item' : 'new comments') : '', 'aria-hidden': 'true' }, isFresh(i) ? '●' : ''),
      h('div', { class: 'irow-text' },
        h('div', { class: 'irow-l1' },
          h('span', { class: 'iref' }, ref(i)),
          h('span', { class: 'ititle', title: markBidi(i.labels.length ? `${i.title}\nlabels: ${i.labels.join(', ')}` : i.title) }, i.title),
          isGh(i) ? h('span', { class: 'iforge', title: 'GitHub' }, 'GH') : null),
        h('div', { class: 'irow-l2' },
          h('span', { class: 'iproj', title: isGh(i) ? i.gl.slice(3) : i.gl }, projectLabel(i)),
          ...rr.text.flatMap((x) => [sep(), reasonEl(x)]),
          rr.chip ? sep() : null,
          rr.chip ? h('span', { class: `ibx-reason ${reasonInfo(rr.chip).cls}`, title: reasonInfo(rr.chip).hint }, reasonInfo(rr.chip).label) : null,
          rr.more.length ? h('span', { class: 'imore', title: rr.more.map((x) => reasonInfo(x).label).join(', ') }, `+${rr.more.length}`) : null,
          i.new_notes ? sep() : null,
          i.new_notes ? h('span', { class: 'inotes', title: 'comments from others since you last read it' }, newNotesLabel(i.new_notes)) : null,
          i.due_date ? h('span', { class: 'ibx-due' + (overdue ? ' over' : ''), title: `due ${i.due_date}` }, `due ${new Date(due).toLocaleDateString(undefined, { day: 'numeric', month: 'numeric' })}`) : null,
          ...sess.slice(0, 2).map((s) => h('button', {
            class: 'ibx-sess', title: `session ${s.title} · ${deps.label(s.state)} - switch to it`,
            onclick: (e: Event) => { e.stopPropagation(); deps.select(s.id); },
          }, h('span', { class: 'dot st-' + s.state, 'aria-hidden': 'true' }), `${s.title.length > 24 ? cutText(s.title, 23) + '…' : s.title} · ${deps.label(s.state)}`)),
          sess.length > 2 ? h('span', { class: 'mut small' }, `+${sess.length - 2}`) : null,
          i.auto.length ? h('button', {
            class: 'ibx-auto', title: i.auto.map((a) => `${a.event} · ${new Date(a.at * 1000).toLocaleString()}`).join('\n'),
            onclick: (e: Event) => { e.stopPropagation(); deps.select(i.auto[i.auto.length - 1]!.session); },
          }, 'automatic analysis') : null)),
      h('span', { class: 'iage mut small', 'data-iso': i.updated_at, title: `changed ${isoDate(i.updated_at)}` }, isoAgo(i.updated_at)));
    clear(r.act);
    r.act.append(primaryEl(i, p));
    const more = h('button', { class: 'icon', 'aria-label': `more actions ${ref(i)}`, 'aria-haspopup': 'menu', 'aria-expanded': 'false', title: 'more actions' }, '⋯') as HTMLButtonElement;
    more.addEventListener('click', (e) => { e.stopPropagation(); openMenu(i, more); });
    r.act.append(more);
    if (hadFocus) r.el.focus({ preventScroll: true });
  }

  function primaryEl(i: InboxItem, p: Primary): HTMLElement {
    const working = busy.has(i.key);
    // F15: a quiet button (no green, no light fill) - one row's action is not the page's primary action; Enter runs it
    const btn = (label: string, title: string, run: () => void, cls = '') => h('button', {
      class: `small trunc ibx-main ${cls}`, title, disabled: working, onclick: (e: Event) => { e.stopPropagation(); run(); },
    }, working ? h('span', { class: 'spinner' }) : null, working ? ' ' : null, label);
    switch (p.kind) {
      case 'continue': {
        const short = p.session.title.length > 22 ? cutText(p.session.title, 21) + '…' : p.session.title;
        return btn(`Continue in ${short}`, p.followUp ? `sends the session a link to the new comment and switches to it (${p.session.title})` : `switch to session ${p.session.title}`, () => primary(i));
      }
      case 'resume': {
        const short = p.session.title.length > 22 ? cutText(p.session.title, 21) + '…' : p.session.title;
        return btn(`Resume ${short}`, `resumes ${p.session.title}, compacts its conversation and gives it the task${targetNote(i) ? ' (the latest comment)' : ''} - New session instead… in ⋯`, () => primary(i));
      }
      case 'open':
        return btn('Open session', `a new session in ${i.project}: the task is a link to ${ref(i)}${targetNote(i) ? ' and its latest comment' : ''} (Edit task… in ⋯)`, () => primary(i));
      case 'add':
      case 'choose':
      case 'none':
        // F15: a grey link; the path(s) only in the dialog
        return btn('Add checkout…', p.kind === 'none' ? noCheckoutText(i) : `choose the checkout of ${i.gl} for projects.json`, () => primary(i), 'link ibx-addco');
    }
  }

  // ---------------------------------------------------------------- draw
  function setFilter(f: Filter): void {
    view.filter = f;
    writeJson(VIEW_KEY, { filter: f, forge: view.forge });
    draw();
  }

  function setForge(f: Forge): void {
    view.forge = f;
    writeJson(VIEW_KEY, { filter: view.filter, forge: f });
    draw();
  }

  function toggleAll(g: Group): void {
    if (showAll.has(g)) showAll.delete(g);
    else showAll.add(g);
    draw();
  }

  function toggleGroup(g: Group): void {
    if (collapsed.has(g)) collapsed.delete(g);
    else collapsed.add(g);
    writeJson(COLLAPSE_KEY, [...collapsed]);
    draw();
  }

  const narrowed = () => view.filter !== 'all' || !!view.forge || !!view.project || !!view.query.trim();

  function drawUpdated(): void {
    clear(updated);
    refreshBtn.classList.toggle('spin', refreshing);
    refreshBtn.disabled = refreshing;
    if (refreshing) return void updated.append(markBidi(`loading ${inboxName(data)}…`));
    const at = Math.max(data?.at ?? 0, data?.github?.at ?? 0);   // W23: the later of the two halves
    if (!at) return;
    const a = ago(at);
    updated.append(a === 'now' ? 'just now' : `${a} ago`);   // F15: "2 min ago" next to ↻
    updated.title = markBidi(`${new Date(at * 1000).toLocaleString()} · automatically every ${Math.round((data?.interval || 90) / 60 * 10) / 10} min`);
  }

  let statusSig = '';
  function drawStatus(): void {
    const st = data?.status ?? summary?.status ?? 'starting';
    // rebuilt only when it changes: a poll must not steal the focus of its buttons
    const gh = data?.github ?? null;
    const sig = JSON.stringify([st, loadErr, !!data, data?.error, data?.at ? ago(data.at) : '', gh?.status, gh?.error, gh?.at ? ago(gh.at) : '']);
    if (sig === statusSig) return;
    statusSig = sig;
    clear(statusEl);
    statusEl.className = 'ibx-status';
    if (loadErr && !data) {
      statusEl.classList.add('bad');
      statusEl.append(h('span', {}, `Could not load the inbox: ${loadErr} `), h('button', { class: 'small', onclick: () => void load() }, 'Try again'));
    } else if (st === 'no-token') {
      const copy = h('button', {
        class: 'small', onclick: () => {
          void navigator.clipboard?.writeText(TOKEN_CMD).then(() => deps.toast('Command copied'), () => deps.toast('Copying does not work - select the command by hand'));
        },
      }, 'Copy');
      statusEl.classList.add('info');
      statusEl.append(h('p', {}, h('b', {}, 'Piper has no GitLab token. '),
        'Create a personal access token in GitLab (Preferences → Access tokens, scope ', h('code', {}, 'api'),
        ' - for sending comments and closing to-dos) and paste it in ',
        h('button', { class: 'link', onclick: () => deps.openSettings('gitlab') }, 'Settings → GitLab'), ', or on the host:'),
        h('div', { class: 'ibx-cmd' }, h('code', {}, TOKEN_CMD), copy),
        h('p', { class: 'mut small' }, 'The token stays in piperd on the host, it never reaches the sandbox. The inbox loads by itself within a few minutes (or refresh it).'));
    } else if (st === 'error' && data) {
      statusEl.classList.add('bad');
      statusEl.append(h('span', {}, `GitLab does not respond: ${data.error ?? 'unknown error'}`),
        data.at ? h('span', { class: 'mut' }, ` · showing data from ${agoText(data.at)}`) : '', ' ',
        h('button', { class: 'small', onclick: () => void refresh() }, 'Try again'));
    } else if (st === 'starting' || (!data && !loadErr)) {
      statusEl.append(h('p', { class: 'mut small' }, h('span', { class: 'spinner' }), ' Loading GitLab - the first pass takes a few seconds…'));
    }
    if (loadErr && data) statusEl.append(h('p', { class: 'bad small' }, `The last load failed: ${loadErr}`));
    // W23: GitHub's half, under GitLab's
    if (gh?.status === 'no-token') {
      statusEl.append(h('p', { class: 'warn small' }, h('b', {}, 'GitHub: not signed in. '), 'On this Mac run ', h('code', {}, 'gh auth login'),
        ', then refresh the inbox (', h('button', { class: 'link', onclick: () => deps.openSettings('github') }, 'Settings → GitHub → Verify'), ').',
        st === 'off' ? '' : ' GitLab rows stay.'));
    } else if (gh?.status === 'error') {
      statusEl.append(h('p', { class: 'bad small' }, `GitHub does not respond: ${gh.error ?? 'unknown error'}`,
        gh.at ? h('span', { class: 'mut' }, ` · showing data from ${agoText(gh.at)}`) : '', ' ',
        h('button', { class: 'small', onclick: () => void refresh() }, 'Try again')));
    } else if (gh?.status === 'starting' && st !== 'starting') {
      statusEl.append(h('p', { class: 'mut small' }, h('span', { class: 'spinner' }), ' Loading GitHub…'));
    }
  }

  function drawCounts(): void {
    const name = inboxName(data ?? summary);   // W23: GitLab / GitHub / Inbox by the modules on
    if (title.textContent !== name) {
      title.textContent = markBidi(name);
      el.setAttribute('aria-label', markBidi(`${name} inbox`));
      refreshBtn.title = markBidi(`load ${name} now`);
    }
    const on = data?.rules.filter((r) => r.enabled).length ?? 0;
    headMore.classList.toggle('on', !!on);   // an automation that runs stays visible on the ⋯
  }

  /** F15: the header's ⋯ - Mark all / shown as read, Automation…; the counts and the accounts in its foot */
  function openHeadMenu(): void {
    const items = data?.items ?? [];
    const fresh = items.filter(isFresh).length;
    const needs = items.filter((i) => i.todos.length || i.new_notes > 0).length;
    const on = data?.rules.filter((r) => r.enabled).length ?? 0;
    const canMark = narrowed() ? visibleItems(items, view).some(isFresh) : fresh > 0;
    const foot = [items.length ? `${fresh} new · ${needs} waiting · ${items.length} total` : '',
      data?.me ? `${data.github ? 'GitLab ' : ''}@${data.me}` : '', data?.github?.me ? `GitHub @${data.github.me}` : ''].filter(Boolean).join(' · ');
    toggleMenu(headMore, () => [
      { label: narrowed() ? 'Mark shown as read' : 'Mark all as read', run: () => void markAllSeen(), disabled: !canMark && 'nothing new' },
      (data ?? summary)?.status === 'off' ? false   // A1: the rules are GitLab's; GitHub alone has none
        : { label: 'Automation…', hint: on ? `${on} on` : undefined, run: () => openRules(), title: 'automatic analysis of new assignments and mentions' },
    ], { label: 'inbox actions', side: 'right', ...(foot ? { foot } : {}) });
  }

  function drawTools(): void {
    const items = data?.items ?? [];
    const c = filterCounts(items, view);
    for (const f of FILTERS) {
      const b = chipEls.get(f.id)!;
      b.textContent = '';
      b.append(f.label, h('span', { class: 'n' }, String(c[f.id])));
      b.setAttribute('aria-pressed', String(view.filter === f.id));
      b.classList.toggle('on', view.filter === f.id);
    }
    // 9.43: the segment only while both forges have items (or one is picked - it must stay removable)
    const fc = forgeCounts(items, view);
    const both = items.some(isGh) && items.some((i) => !isGh(i));
    forgeSeg.classList.toggle('hidden', !both && !view.forge);
    for (const [f, b] of forgeEls) {
      b.textContent = '';
      b.append(b.dataset['label'] ?? '', h('span', { class: 'n' }, String(fc[f || 'all'])));
      b.setAttribute('aria-pressed', String((view.forge ?? '') === f));
      b.classList.toggle('on', (view.forge ?? '') === f);
    }
    const gls = [...new Map(items.filter((i) => matchesForge(i, view.forge)).map((i) => [i.gl, projectLabel(i)] as [string, string])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
    const sig = JSON.stringify(gls);
    if (sig !== projSig) {
      projSig = sig;
      clear(projSel);
      projSel.append(h('option', { value: '' }, `All projects (${gls.length})`), ...gls.map(([gl, label]) => h('option', { value: gl, title: gl }, `${label} (${items.filter((i) => i.gl === gl).length})`)));
    }
    if (view.project && !gls.some(([gl]) => gl === view.project)) view.project = '';
    projSel.value = view.project;
    projSel.classList.toggle('hidden', gls.length < 2);
  }

  function orderedKeys(): string[] {
    return [...list.querySelectorAll('.ibx-group:not(.hidden) .ibx-rows:not(.hidden) > .irow:not(.hidden)')].map((x) => (x as HTMLElement).dataset['key'] ?? '');
  }

  function draw(): void {
    drawStatus();
    drawUpdated();
    drawCounts();
    drawTools();
    const st = data?.status ?? summary?.status ?? 'starting';
    const skeleton = !data || ((st === 'starting' || (st === 'off' && data.github?.status === 'starting')) && !data.items.length);
    list.classList.toggle('skeleton', skeleton && st !== 'no-token' && !loadErr);
    if (skeleton) {
      for (const g of groupEls.values()) g.box.classList.add('hidden');
      empty.classList.add('hidden');
      keysHint.classList.add('hidden');
      if (st !== 'no-token' && !loadErr && !list.querySelector('.iskel')) {
        list.append(...[0, 1, 2].map(() => h('div', { class: 'iskel', 'aria-hidden': 'true' }, h('span'), h('span'), h('span'))));
      }
      return;
    }
    for (const s of [...list.querySelectorAll('.iskel')]) s.remove();
    const items = data!.items;
    const shown = visibleItems(items, view, expanded);
    const groups = groupItems(shown, pinned);
    // moving a node drops its focus and a clear+append clamps the scroll: remember both
    const scroller = el.closest('.board') as HTMLElement | null;
    const top = scroller?.scrollTop ?? 0;
    const active = document.activeElement as HTMLElement | null;
    const hadFocus = !!active && list.contains(active);
    let moved = false;
    const needPreview: InboxItem[] = [];
    for (const g of GROUPS) {
      const ge = groupEls.get(g.id)!;
      const its = groups[g.id];
      ge.box.classList.toggle('hidden', !its.length);
      const closed = collapsed.has(g.id) && !narrowed();
      ge.head.setAttribute('aria-expanded', String(!closed));
      clear(ge.head);
      ge.head.append(markBidi(`${g.title} · `), h('span', { class: 'n' }, String(its.length)), h('span', { class: 'caret', 'aria-hidden': 'true' }));
      // Assigned keeps its rows, but its count is GitLab's: the fresh / waiting ones sit in the groups above
      const allAssigned = g.id === 'assigned' ? shown.filter(isAssigned).length : 0;
      if (allAssigned > its.length) {
        ge.head.append(h('span', { class: 'n', title: `${allAssigned} open issues assigned to you in GitLab; the new and waiting ones are listed in the groups above` }, `of ${allAssigned}`));
      }
      ge.rows.classList.toggle('hidden', closed);
      const want = its.map((i) => {
        let r = rows.get(i.key);
        if (!r) {
          r = makeRow(i.key);
          rows.set(i.key, r);
        }
        fillRow(r, i);
        syncOpen(i.key);
        if (expanded.has(i.key)) {
          const st = previews.get(i.key);
          if (!st) needPreview.push(i);
          else if (!st.loading && !st.error && st.at !== pvAt(i)) needPreview.push(i);   // new notes while open
          else if (!r.pv.firstChild) needPreview.push(i);
        }
        return r.el;
      });
      const box = ge.rows;
      want.forEach((node, k) => {
        if (box.children[k] !== node) {
          box.insertBefore(node, box.children[k] ?? null);
          moved = true;
        }
      });
      while (box.children.length > want.length) box.lastElementChild!.remove();
      // rows past the limit are hidden, not removed: an open preview or the focused row always stays
      const all = showAll.has(g.id) || narrowed();
      let hiddenN = 0;
      its.forEach((i, k) => {
        const over = !all && k >= GROUP_LIMIT && !expanded.has(i.key) && !(hadFocus && rows.get(i.key)!.el.contains(active));
        rows.get(i.key)!.el.classList.toggle('hidden', over);
        if (over) hiddenN++;
      });
      const extra = its.length > GROUP_LIMIT && !narrowed() && !closed;
      ge.more.classList.toggle('hidden', !extra);
      if (extra) ge.more.textContent = markBidi(showAll.has(g.id) ? 'Show less' : `Show more (${hiddenN})`);
    }
    for (const [k, r] of [...rows]) {
      if (!items.some((i) => i.key === k)) {
        r.el.remove();
        rows.delete(k);
        previews.delete(k);
        io?.unobserve(r.pv);
      }
    }
    // roving tabindex: one row in the Tab order
    const keys = orderedKeys();
    if (!keys.includes(focusKey)) focusKey = keys[0] ?? '';
    for (const [k, r] of rows) r.el.tabIndex = k === focusKey ? 0 : -1;
    if (moved) {
      if (scroller) scroller.scrollTop = top;
      if (hadFocus && active && active.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
    }
    for (const i of needPreview) void loadPreview(i);
    empty.classList.toggle('hidden', !!shown.length);
    clear(empty);
    if (!shown.length) {
      if (items.length) {
        empty.append('Nothing matches the filter. ', h('button', { class: 'link', onclick: () => { view.filter = 'all'; view.forge = ''; view.project = ''; view.query = ''; search.value = ''; setFilter('all'); } }, 'Clear filter'));
      } else if (st === 'ok') {
        empty.append('No pending to-dos, assigned issues or own open MRs. ✓');
      }
    }
    keysHint.classList.toggle('hidden', !shown.length);
  }

  // ---------------------------------------------------------------- keyboard
  function setFocusKey(k: string): void {
    if (k === focusKey) return;
    focusKey = k;
    for (const [key, r] of rows) r.el.tabIndex = key === k ? 0 : -1;
  }

  function focusRow(key: string): void {
    const r = rows.get(key);
    if (!r) return;
    setFocusKey(key);
    r.el.focus({ preventScroll: true });
    r.el.scrollIntoView({ block: 'nearest' });
  }

  function onKey(e: KeyboardEvent): void {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target as HTMLElement;
    if (t.closest('input, textarea, select')) return;
    const rowEl = t.closest('.irow') as HTMLElement | null;
    const key = rowEl?.dataset['key'] ?? '';
    const i = item(key);
    if (!rowEl || !i) return;
    const onRow = t === rowEl;
    const keys = orderedKeys();
    const at = keys.indexOf(key);
    const move = (d: number) => focusRow(keys[Math.max(0, Math.min(keys.length - 1, at + d))] ?? key);
    switch (e.key) {
      case 'ArrowDown': case 'j': move(1); break;
      case 'ArrowUp': case 'k': move(-1); break;
      case 'Home': focusRow(keys[0] ?? key); break;
      case 'End': focusRow(keys[keys.length - 1] ?? key); break;
      case 'Enter': if (!onRow) return; primary(i); break;
      case ' ': if (!onRow) return; toggle(key); break;
      case 'Escape': {
        if (!expanded.has(key)) {   // nothing to collapse: back to the queue (G10)
          if (!deps.back) return;
          deps.back();
          break;
        }
        collapse(key, true);
        // a row only an open preview kept visible (filter) is gone now: focus its neighbour
        if (rowEl.isConnected) rowEl.focus({ preventScroll: true });
        else focusRow(keys[at + 1] ?? keys[at - 1] ?? '');
        break;
      }
      case 'e': void toggleSeen(i); break;
      // G10: o opens the detail, as in the queue; the browser has its own key
      case 'o': if (!expanded.has(key)) expand(key); break;
      case 'g': openGitlab(i); break;
      case '/': search.focus(); break;
      default: return;
    }
    e.preventDefault();
    e.stopPropagation();
  }

  /** show an item: Overview, filters that hide it cleared, preview open, row focused */
  function reveal(key: string): boolean {
    deps.showBoard();
    const i = item(key);
    if (!i) return false;
    if (!visibleItems(data!.items, view, expanded).some((x) => x.key === key)) {
      view.project = '';
      view.query = '';
      search.value = '';
      view.filter = 'all';
      view.forge = '';
    }
    collapsed.delete(pinned.get(key) ?? groupOf(i));
    if (!expanded.has(key)) expand(key);
    else draw();
    window.requestAnimationFrame(() => {
      const r = rows.get(key);
      if (!r) return;
      setFocusKey(key);
      r.el.focus({ preventScroll: true });
      r.el.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    return true;
  }

  // ---------------------------------------------------------------- automation rules
  const EVENTS: { id: RuleEvent; label: string; hint: string }[] = [
    { id: 'assigned', label: 'New assignment', hint: 'someone assigned an issue to you' },
    { id: 'mentioned', label: 'Mention', hint: 'someone mentioned you by @name' },
    { id: 'directly_addressed', label: 'Addressed', hint: 'a comment addresses you directly' },
    { id: 'new_note', label: 'New comment', hint: 'a new comment from someone else on an inbox issue' },
  ];

  const DEFAULT_ON: RuleEvent[] = ['assigned', 'mentioned', 'directly_addressed'];

  function openRules(): void {
    if (!data) return deps.toast('The inbox is still loading');
    const before = data.rules;
    const newId = () => (typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID().slice(0, 8) : Math.random().toString(36).slice(2, 10));
    // one row per GitLab project of the inbox (+ mapped projects, + "every mapped one"); a project
    // without a rule gets an unsaved, switched-off row that is sent only once touched
    const mapped = new Map<string, string | null>();
    for (const p of deps.projects()) for (const gl of p.gitlab) if (!mapped.get(gl)) mapped.set(gl, p.key);
    for (const i of data.items) if (!mapped.get(i.gl)) mapped.set(i.gl, i.project);
    for (const r of before) if (r.gl !== '*' && !mapped.has(r.gl)) mapped.set(r.gl, null);
    // mapped projects by name first, the unmapped ones (a rule there cannot fire yet) last
    const order = ['*', ...[...mapped.entries()].sort((x, y) => Number(!x[1]) - Number(!y[1]) || (x[1] ?? x[0]).localeCompare(y[1] ?? y[0])).map(([gl]) => gl)];
    const seeded = new Set<string>();
    const touched = new Set<string>();
    const rules: InboxRule[] = [];
    for (const gl of order) {
      const own = before.filter((r) => r.gl === gl);
      if (own.length) rules.push(...own.map((r) => ({ ...r, on: [...r.on] })));
      else {
        const r: InboxRule = { id: newId(), gl, on: [...DEFAULT_ON], action: 'analyze', enabled: false, since: 0 };
        seeded.add(r.id);
        rules.push(r);
      }
    }
    const count = (gl: string) => data!.items.filter((i) => gl === '*' ? !!i.project : i.gl === gl).length;
    const listEl = h('div', { class: 'rules' });
    const err = h('p', { class: 'bad small', role: 'alert' });
    const save = h('button', { class: 'primary' }, 'Save') as HTMLButtonElement;
    const validate = () => {
      const bad = rules.find((r) => r.enabled && !r.on.length);
      err.textContent = markBidi(bad ? `An enabled rule (${bad.gl === '*' ? 'all projects' : mapped.get(bad.gl) ?? bad.gl}) without an event starts nothing - tick at least one, or switch it off.` : '');
      save.disabled = !!bad;
    };
    for (const r of rules) {
      const touch = () => { touched.add(r.id); validate(); };
      const en = h('input', { type: 'checkbox', role: 'switch', checked: r.enabled }) as HTMLInputElement;
      const name = r.gl === '*' ? 'All mapped projects' : mapped.get(r.gl) ?? r.gl;
      en.setAttribute('aria-label', markBidi(`automation for ${name}`));
      const evs = h('div', { class: 'rule-events', role: 'group', 'aria-label': `events for ${name}` }, ...EVENTS.map((ev) => {
        const cb = h('input', { type: 'checkbox', checked: r.on.includes(ev.id) }) as HTMLInputElement;
        cb.addEventListener('change', () => { r.on = cb.checked ? [...new Set([...r.on, ev.id])] : r.on.filter((x) => x !== ev.id); touch(); });
        return h('label', { title: ev.hint }, cb, ev.label);
      }));
      const unmapped = r.gl !== '*' && !mapped.get(r.gl);
      const since = h('span', {}, r.enabled && r.since ? ` · counts events since ${new Date(r.since * 1000).toLocaleString()}` : '');
      const card = h('div', { class: 'rule' + (r.enabled ? '' : ' off') + (r.gl === '*' ? ' all' : '') },
        h('div', { class: 'rule-top' },
          h('label', { class: 'switch' }, en, h('span', { class: 'track', 'aria-hidden': 'true' })),
          h('span', { class: 'rule-name' }, h('b', {}, name), r.gl !== '*' && mapped.get(r.gl) ? h('span', { class: 'mut small' }, ` ${r.gl}`) : null),
          h('span', { class: 'mut small' }, `${count(r.gl)} in the inbox`)),
        evs,
        h('div', { class: 'mut small' }, 'Action: ', h('b', {}, 'analysis only'), ' (changes and sends nothing)', since),
        unmapped ? h('p', { class: 'warn small' }, 'The project is not mapped to a checkout - until it is (Add project in the inbox row), the rule starts nothing.') : null);
      en.addEventListener('change', () => {
        r.enabled = en.checked;
        card.classList.toggle('off', !r.enabled);
        since.textContent = r.enabled ? ' · counts only events after saving' : '';
        touch();
      });
      listEl.append(card);
    }
    validate();
    save.addEventListener('click', () => {
      save.disabled = true;
      save.textContent = 'Saving…';
      const out = stampRules(rules.filter((r) => !seeded.has(r.id) || r.enabled || touched.has(r.id)), before, Date.now() / 1000);
      api.inboxRules(out).then((r) => {
        if (data && r.data) data.rules = r.data.rules;
        deps.closeModal();
        const on = (r.data?.rules ?? out).filter((x) => x.enabled).length;
        deps.toast(on ? `Automation saved - ${on} ${plural(on, 'rule', 'rules', 'rules')} on` : 'Automation saved - off');
        draw();
      }).catch((e: unknown) => {
        save.disabled = false;
        save.textContent = 'Save';
        err.textContent = markBidi(`Not saved: ${errText(e)}`);
      });
    });
    const lim = data.limits;
    deps.modal('GitLab automation',
      h('div', { class: 'rules-intro' },
        h('p', {}, 'When a selected event shows up in GitLab, Piper starts a session by itself that ', h('b', {}, 'only analyzes'),
          ': it reads the issue including comments, finds the affected code and saves solution options with a recommendation to ', h('code', {}, `development/gitlab-<iid>-${files().analysis}.md`),
          '. It changes nothing, does not commit and sends nothing - in the inbox you see "automatic analysis" and decide yourself.'),
        h('ul', { class: 'small' },
          h('li', {}, `At most ${lim.per_hour} ${plural(lim.per_hour, 'start', 'starts', 'starts')} per hour (this hour ${lim.used_hour}).`),
          h('li', {}, 'Only mapped projects and only issues without a live session; each event at most once.'),
          h('li', {}, 'It does not start when fewer than 2 free session slots are left.'),
          h('li', {}, 'Only events that arrive after the rule is switched on count - old to-dos start nothing.'))),
      listEl, err,
      h('p', { class: 'ibx-secnote small' }, h('b', {}, 'Security: '),
        'the text of issues and comments is written by clients and colleagues - it is untrusted input. The session reads it only through Piper, scoped to the project, and runs in the regular sandbox without any extra privilege. Writing to GitLab (a comment, closing a to-do) always stays your click. Rules can be changed only here in the GUI.'),
      h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), save));
    (listEl.querySelector('input') as HTMLInputElement | null)?.focus();
  }

  return {
    el,
    sync,
    reveal,
    refresh: () => void refresh(),
    openRules,
    openDraft: (id: string, issue: SessionIssue | null | undefined) => void openDraft(id, issue),
    /** focus the list (palette "GitLab inbox") */
    focus(): void {
      deps.showBoard();
      window.requestAnimationFrame(() => {
        const k = focusKey || orderedKeys()[0];
        if (k) focusRow(k);
        else search.focus();
        el.scrollIntoView({ block: 'start' });
      });
    },
    /** board redraw (session states changed): session chips and primary actions follow */
    redraw(): void {
      if (data) draw();
    },
  };
}
