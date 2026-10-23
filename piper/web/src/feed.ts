// Feed: one panel on the right for what waits for you (actions),
// what happened (events) and the chat with the Global agent (messages). Three kinds, three
// looks that never mix: an action is a card with buttons, an event one grey line, and only
// you and the Global agent speak in bubbles; text of others (a GitLab comment) is a quote.
// The input at the bottom always writes to the Global agent - a session is answered in its
// own card. piperd derives everything (feed.py); this module only shows it. Off = not built.

import { isSend, sendHint } from './sendkeys.js';
import { ApiError, call } from './api.js';
import { opRole, orderOps } from './decision.js';
import { ago, clear, fi, h } from './dom.js';
import type { FeedSummary } from './types.js';
import { markBidi } from './text.js';

export type Level = 'security' | 'permission' | 'confirm' | 'blocked' | 'waiting' | 'ci' | 'gitlab' | 'prod';
export type Source = 'session' | 'confirm' | 'ci' | 'gitlab' | 'prod' | 'agent' | 'system';

export interface Subject {
  type: string;
  key: string;
  label: string;
  session?: string | null;
  project?: string;
}

export interface FeedOp {
  op: 'open' | 'reply' | 'decide' | 'inbox' | 'prod' | 'url';
  label: string;
  args: Record<string, string | boolean | number>;
}

export interface FeedAction {
  key: string;
  kind: 'action';
  level: Level;
  source: Source;
  title: string;
  body: string;
  quote: { author: string; text: string } | null;
  subject: Subject;
  since: number;
  actions: FeedOp[];
  /** a net-failed row (2026-10-10): the trash entries of the originals git could not put back, and their project */
  trash?: { id: string; path: string }[];
  project?: string;
}

export interface FeedEvent {
  id: string;
  key: string;
  kind: 'event';
  source: Source;
  title: string;
  body: string;
  subject: Subject;
  ts: number;
  n: number;
}

export interface FeedData {
  on: boolean;
  v?: number;
  actions?: FeedAction[];
  events?: FeedEvent[];
  seen?: number;
  dismissed?: number;
  muted?: Record<string, number>;
}

export interface ChatTurn {
  role: 'user' | 'assistant';
  text: string;
  ts?: string | null;
  chat: string[];
  from_terminal: boolean;
}

export interface ChatQueued {
  id: string;
  text: string;
  ts: number;
  /** sent: typed / given as a start task, not in the transcript yet · lost: the agent ended before reading it */
  state: 'queued' | 'error' | 'sent' | 'lost';
  error?: string;
}

export interface ChatView {
  on: boolean;
  agent?: { id: string; state: string; title: string; hold: string } | null;
  turns?: ChatTurn[];
  queue?: ChatQueued[];
  note?: string;
  /** F21: the sessions the agent started (origin "advisor"), newest first */
  started?: { id: string; title: string; project: string; state: string }[];
  /** F21: the agent's cards waiting for your answer in the queue */
  cards?: { kind: string; project: string }[];
  /** F21: what the empty chat's suggestions are made of */
  context?: { assigned: number; waiting: number; review: number };
}

/** F21: three suggestions for the empty chat, from what Piper knows (a click puts the text in the field) */
export function chatSuggestions(c: ChatView['context']): string[] {
  const out: string[] = [];
  if (c?.assigned) out.push(c.assigned === 1 ? 'Plan my assigned issue' : `Plan my ${c.assigned} assigned issues`);
  out.push(c?.waiting ? `What is waiting for me? (${c.waiting} ${c.waiting === 1 ? 'session' : 'sessions'})` : 'What is waiting for me?');
  if (c?.review) out.push(c.review === 1 ? 'Start a session for the MR to review' : `Start sessions for the ${c.review} MRs to review`);
  for (const x of ['What did my sessions do today?', 'Which of my issues is due soon?']) if (out.length < 3) out.push(x);
  return out.slice(0, 3);
}

/** F21: the chat's line while the agent waits for a card's answer */
export function cardLine(c: { kind: string; project: string }): string {
  return c.kind === 'spawn_session' ? `Asked to start a session${c.project ? ` in ${c.project}` : ''} - card is in the queue (⌘J)`
    : `Waits for your answer (${c.kind.replace(/_/g, ' ')}) - card is in the queue (⌘J)`;
}

/** 9.136: a started session that is over - folded under "N ended" */
export const ENDED_STATES = ['exited', 'error'];

/** F21: a started session's state as a word and a tone ("done" for an ended one) */
export function startedState(state: string): { word: string; tone: 'ok' | 'warn' | 'bad' | 'run' } {
  if (state === 'exited') return { word: 'done', tone: 'ok' };
  if (state === 'error') return { word: 'error', tone: 'bad' };
  if (['waiting', 'attention', 'trust', 'blocked'].includes(state)) return { word: state === 'attention' ? 'needs you' : state, tone: 'warn' };
  return { word: state, tone: 'run' };
}

/** F21: the agent's state once (the drawer's header shows it; `live` false = Start offered in the chat) */
export function agentStatus(c: ChatView): { live: boolean; text: string } {
  const a = c.agent;
  const live = !!a && LIVE_AGENT.includes(a.state);
  return { live, text: live ? (AGENT_WORDS[a!.state] ?? a!.state) : 'not running' };
}
const LIVE_AGENT = ['ready', 'working', 'waiting', 'attention', 'starting', 'attesting', 'trust', 'preflight', 'preparing', 'spawning'];
const AGENT_WORDS: Record<string, string> = {
  preflight: 'isolation check…', preparing: 'preparing…', spawning: 'spawning…', starting: 'starting…',
  attesting: 'verifying isolation…', trust: 'waiting for trust in the terminal', ready: 'ready', working: 'working…',
  waiting: 'waiting', attention: 'needs attention in the terminal',
};

export const feedApi = {
  feed: () => call<FeedData>('GET', '/api/feed'),
  chat: () => call<ChatView>('GET', '/api/feed/chat'),
  seen: (ts: number) => call('POST', '/api/feed/seen', { ts }),
  dismiss: (key: string) => call('POST', '/api/feed/dismiss', { key }),
  undismiss: () => call<{ restored: number }>('POST', '/api/feed/undismiss', {}),
  mute: (key: string, minutes: number) => call('POST', '/api/feed/mute', { key, minutes }),
  send: (text: string) => call<ChatQueued & { agent: boolean }>('POST', '/api/feed/chat', { text }),
  cancel: (id: string) => call('POST', '/api/feed/chat/cancel', { id }),
  retry: (id: string) => call('POST', '/api/feed/chat/retry', { id }),
  start: () => call<{ session: string; sent: string[] }>('POST', '/api/feed/chat/start', {}),
};

// ------------------------------------------------------------------ pure helpers (feed.test.mjs)
export const LEVEL: Record<Level, { icon: string; label: string }> = {
  security: { icon: '⛔', label: 'security' },
  permission: { icon: '⚑', label: 'permission' },
  confirm: { icon: '⚑', label: 'confirmation' },
  blocked: { icon: '✕', label: 'start' },
  waiting: { icon: '●', label: 'waiting' },
  ci: { icon: '✕', label: 'CI' },
  gitlab: { icon: '◆', label: 'GitLab' },
  prod: { icon: '▲', label: 'production' },
};

/** a confirmation card's kind as the queue row says it (feed.py KIND_TEXT, the same words; feed.test.mjs
 *  checks they match): "<session>: wants to commit" on the floating card, the queue row, the notification */
export const KIND_TEXT: Record<string, string> = {
  git_commit: 'wants to commit', git_push: 'wants to push', send_to_session: 'wants to write to a session',
  stop_session: 'wants to stop a session', propose_session: 'proposes a new session',
  spawn_session: 'wants to start a session',
  gitlab_comment: 'wants to post a GitLab comment',
  gitlab_issue_write: 'wants to create or change a GitLab issue', gitlab_mr_write: 'wants to create or change a merge request',
  gitlab_mr_merge: 'wants to merge a merge request', gitlab_job_play: 'wants to start or retry a CI job',
  gitlab_job_protected: 'wants to start or retry a CI job on a PROTECTED branch', gitlab_note_edit: 'wants to edit a posted GitLab note',
  gitlab_upload_file: 'wants to upload a file attachment to GitLab',
  github_comment: 'wants to post a GitHub comment', github_note_edit: 'wants to edit a posted GitHub comment',
  github_issue_write: 'wants to create or change a GitHub issue',
  github_pr_write: 'wants to create or change a pull request',
  github_job_log: 'wants to read a failed GitHub Actions job\'s log',
  git_run_project: 'wants to run git in another project',
  gitlab_job_log: 'wants to read a failed CI job\'s log', gitlab_note: 'wants to post a GitLab comment',
  git_run: 'wants to run git', write_exec: 'wants to change a file the host runs (hook, build, IDE) in another project',
  edit_file: 'wants to edit another project', write_file: 'wants to write to another project',
  gitlab_read: 'wants to read another GitLab project',
  http_env_set: 'wants to update HTTP environment values',
  properties_set: 'wants to update properties',
  http_dev: 'wants to run HTTP requests on dev', http_test: 'wants to run HTTP requests on test',
  http_prod: 'wants to run HTTP requests on PRODUCTION',
  http_host: 'wants to call a host outside the environment',
  db_write_dev: 'wants to write to a dev database', db_write_test: 'wants to write to a TEST database',
  db_write_prod: 'wants to write to a PRODUCTION database', db_read_test: 'wants to read a test database',
  db_read_prod: 'wants to read a PRODUCTION database', db_other: 'wants a database of another project',
  db_unmask: 'wants to see masked personal data',
  allow_domain: 'wants to allow a network domain', settings_change: 'wants to change Piper\'s settings',
};

/** a card answered meanwhile (another tab, the queue, a second click): piperd says 404 (R-5) - nothing to report */
export function decidedElsewhere(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

/** the red label of a feed action's Allow (feed.py args.danger), '' = an ordinary one */
export function dangerAllow(op: FeedOp): string {
  if (op.op !== 'decide' || op.args.ok !== true || op.args.always === true || !op.args.danger) return '';
  return dangerWord(String(op.args.danger));
}

/** feed.py args.danger of a kind that is red only by its payload (no DANGER_KINDS kind): a weakening settings
 *  change, a git_run that takes / moves a hidden file (sessions._git_run payload.danger) */
const WEAKENING = 'Allow weakening';
const HIDDEN_FILE = 'Allow, hidden file';
const THIS_MACHINE = 'Allow, this machine';   // N9: allow_domain of a name that reaches this machine (sessions._reaches_this_machine)
const MARKED_LABEL: Record<string, string> = { settings_change: WEAKENING, git_run: HIDDEN_FILE, git_run_project: HIDDEN_FILE, allow_domain: THIS_MACHINE };

/** the red Allow's word for feed.py's args.danger; `own` = the floating card's word for an unknown mark - never green */
export function dangerWord(danger: string, own = ''): string {
  return DANGER_LABEL[danger] ?? MARKED_LABEL[danger] ?? (own || 'Allow on PROD');
}

/** the floating card's red Allow ('' = an ordinary one): a DANGER_KINDS kind, a settings change that weakens the
 *  isolation (payload.weakens), a git_run that takes a hidden file into git or moves it (payload.danger) - feed.py _danger */
export function cardDanger(p: { kind: string; payload?: Record<string, unknown> | null }): string {
  const own = DANGER_LABEL[p.kind];
  if (own) return own;
  if (p.kind === 'settings_change') return p.payload?.weakens ? WEAKENING : '';
  if (p.kind === 'git_run' || p.kind === 'git_run_project') return p.payload?.danger === true ? HIDDEN_FILE : '';
  if (p.kind === 'allow_domain') return p.payload?.danger === true ? THIS_MACHINE : '';
  return '';
}

/** the ⚠ lines of a card's text - a folded card shows them (sessions.py puts them first) */
export function warnLines(summary: string): string[] {
  return summary.split('\n').filter((l) => l.startsWith('⚠'));
}

/** kinds whose Allow is a red, deliberate click (feed.py DANGER_KINDS) and its label - the floating card
 *  and the queue row say the same word */
export const DANGER_LABEL: Record<string, string> = { http_prod: 'Allow on PROD', http_host: 'Allow host', db_write_prod: 'Allow on PROD',
  db_read_prod: 'Allow on PROD', db_unmask: 'Unmask', write_exec: 'Allow, the host runs it',
  gitlab_job_protected: 'Allow on protected branch' };

export const SOURCE_ICON: Record<string, string> = {
  session: '●', confirm: '⚑', ci: '⚙', gitlab: '◆', prod: '▲', agent: '✦', system: '·',
};

export type Filter = 'all' | 'me' | 'sessions' | 'gitlab' | 'agent';
export const FILTERS: { id: Filter; label: string; title: string }[] = [
  { id: 'all', label: 'All', title: 'actions, events and chat' },
  { id: 'me', label: 'For me', title: 'only what is waiting for you' },
  { id: 'sessions', label: 'Sessions', title: 'sessions, confirmations and CI' },
  { id: 'gitlab', label: 'GitLab', title: 'the GitLab inbox' },
  { id: 'agent', label: 'Agent', title: 'only the chat with the Global agent' },
];

/** does an action / event belong under the filter (chat bubbles: only all + agent) */
export function matches(x: { kind: string; source: string }, f: Filter): boolean {
  if (f === 'all') return true;
  if (f === 'me') return x.kind === 'action';
  if (f === 'agent') return x.kind === 'chat' || x.source === 'agent';
  if (f === 'gitlab') return x.source === 'gitlab';
  return x.source === 'session' || x.source === 'confirm' || x.source === 'ci';
}

/** the reference handed to the Global agent: an id, never the notification's text (injection) */
export function refFor(s: Subject): string {
  if (s.type === 'session') return `(ref: session ${s.key}${s.label ? ` "${s.label}"` : ''})`;
  if (s.type === 'issue' || s.type === 'mr') {
    const [gl, kind, iid] = s.key.split('#');
    return `(ref: gitlab ${gl}${kind === 'mr' ? '!' : '#'}${iid})`;
  }
  if (s.type === 'project') return `(ref: project ${s.key})`;
  return `(ref: ${s.type} ${s.key})`;
}

const dayKey = (ts: number): string => {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
};

export function dayLabel(ts: number, now = Date.now() / 1000): string {
  if (dayKey(ts) === dayKey(now)) return 'Today';
  if (dayKey(ts) === dayKey(now - 86400)) return 'Yesterday';
  const d = new Date(ts * 1000);
  return `${d.getDate()}. ${d.getMonth() + 1}.`;
}

export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** a chat item of the timeline */
export interface Bubble {
  kind: 'chat';
  source: 'agent';
  who: 'you' | 'agent' | 'tools' | 'queued';
  text: string;
  ts: number;
  terminal?: boolean;
  id?: string;
  state?: string;
  error?: string;
}

const TOOL_LINE = /^\[tool ([^\]\n]{1,80})\]$/;

/** the agent's transcript as bubbles: an assistant turn of only `[tool x]` lines folds into one
 *  grey "tools" row (joined with its neighbours); a turn without a time gets its predecessor's */
export function bubbles(v: ChatView): Bubble[] {
  const out: Bubble[] = [];
  let last = 0;
  for (const t of v.turns ?? []) {
    const ts = t.ts ? Date.parse(t.ts) / 1000 || last : last;
    last = ts;
    if (t.role === 'assistant') {
      const lines = t.text.split('\n').filter((l) => l.trim());
      const tools = lines.map((l) => TOOL_LINE.exec(l.trim())?.[1]).filter((x): x is string => !!x);
      if (lines.length && tools.length === lines.length) {
        const prev = out.at(-1);
        if (prev?.who === 'tools') {
          prev.text = [...new Set([...prev.text.split(', '), ...tools])].join(', ');
          continue;
        }
        out.push({ kind: 'chat', source: 'agent', who: 'tools', text: [...new Set(tools)].join(', '), ts });
        continue;
      }
      const text = lines.filter((l) => !TOOL_LINE.test(l.trim())).join('\n');
      out.push({ kind: 'chat', source: 'agent', who: 'agent', text, ts });
    } else {
      out.push({ kind: 'chat', source: 'agent', who: 'you', text: t.text, ts, terminal: t.from_terminal });
    }
  }
  for (const q of v.queue ?? []) {
    out.push({ kind: 'chat', source: 'agent', who: 'queued', text: q.text, ts: q.ts, id: q.id, state: q.state, ...(q.error ? { error: q.error } : {}) });
  }
  return out;
}

export type Row = { type: 'day'; label: string } | { type: 'new' } | { type: 'event'; e: FeedEvent } | { type: 'chat'; b: Bubble };

/** events and chat in time order, with day separators and one "new" line after `seen` */
export function timeline(events: FeedEvent[], chat: Bubble[], seen: number, f: Filter, now = Date.now() / 1000): Row[] {
  const items: ({ ts: number; e?: FeedEvent; b?: Bubble })[] = [
    ...events.filter((e) => matches(e, f)).map((e) => ({ ts: e.ts, e })),
    ...(f === 'all' || f === 'agent' ? chat.map((b) => ({ ts: b.ts, b })) : []),
  ];
  items.sort((a, b) => a.ts - b.ts || (a.b ? 1 : 0) - (b.b ? 1 : 0));
  const rows: Row[] = [];
  let day = '';
  let marked = false;
  for (const it of items) {
    const d = dayKey(it.ts);
    if (d !== day) {
      rows.push({ type: 'day', label: dayLabel(it.ts, now) });
      day = d;
    }
    if (!marked && seen && it.e && it.ts > seen) {
      rows.push({ type: 'new' });
      marked = true;
    }
    rows.push(it.e ? { type: 'event', e: it.e } : { type: 'chat', b: it.b! });
  }
  return rows;
}

/** "Since 14:05: 3× turn, 1× resolved, 2× GitLab" over the unread events; '' = nothing new */
export function sinceLine(events: FeedEvent[], seen: number): string {
  const fresh = events.filter((e) => e.ts > seen);
  if (!seen || !fresh.length) return '';
  const count = new Map<string, number>();
  const kind = (e: FeedEvent): string => {
    const p = e.key.split(':')[0] ?? '';
    return ({ turn: 'turn', exit: 'session end', new: 'new session', handoff: 'handoff result', notes: 'GitLab comment',
      ci: 'CI', done: 'resolved' } as Record<string, string>)[p] ?? 'event';
  };
  for (const e of fresh) count.set(kind(e), (count.get(kind(e)) ?? 0) + (e.n || 1));
  return `Since ${clock(seen)}: ` + [...count].map(([k, n]) => `${n}× ${k}`).join(', ');
}

export function badge(s: FeedSummary | undefined): string {
  return s?.on && s.open ? String(s.open) : '';
}

// ------------------------------------------------------------------ the panel
export interface FeedDeps {
  select(id: string): void;
  openInbox(key: string): void;
  openProd(): void;
  decide(id: string, ok: boolean, always?: boolean, sessionAlways?: boolean): Promise<void>;
  reply(id: string, text: string): Promise<boolean>;
  toast(text: string): void;
  /** the panel is on screen (and the window visible): what it shows counts as seen */
  shown(): boolean;
  /** Esc: back to the terminal */
  back(): void;
  /** the rail's session title (the agent's own is "Global agent") */
  title(id: string): string;
  /** the drawer (⌘0): only the chat with the Global agent - actions and events are in the queue */
  chatOnly?: boolean;
  /** new data was drawn (the queue shows the events) */
  changed?(): void;
}

const CHAT_MS = 4000;
const readFilter = (): Filter => {
  try {
    const f = window.localStorage.getItem('piper.feed.filter');
    return FILTERS.some((x) => x.id === f) ? f as Filter : 'all';
  } catch {
    return 'all';
  }
};

export function createFeed(deps: FeedDeps) {
  let data: FeedData = { on: false };
  let chat: ChatView = { on: false };
  let v: number | undefined;      // the version of what is drawn: set only after a successful load
  let wantV: number | undefined;  // the version /api/state announced
  let loadErr = '';
  let shownTop = 0;               // the newest event actually drawn (markSeen: never what a filter hides)
  let filter: Filter = deps.chatOnly ? 'agent' : readFilter();
  let chatAt = 0;
  let loading = false;
  let refs: string[] = [];
  let replyOpen = '';   // the action key whose reply box is open
  let focusKey = '';    // the row the keyboard is on (action key / event id)
  let seenPosted = 0;
  let marker: number | null = null;   // `seen` as it was when the panel came on screen
  let replyFocus = false;             // the reply box was just opened: give it the focus once
  const openBodies = new Set<string>();

  const head = h('div', { class: 'feed-head' });
  const actionsEl = h('div', { class: 'feed-actions' });
  const lineEl = h('div', { class: 'feed-line' });
  const scroll = h('div', { class: 'feed-scroll', tabindex: '-1' }, actionsEl, lineEl);
  const refsEl = h('div', { class: 'feed-refs' });
  // F21: the started sessions above the chat; a short placeholder, the keys as chips under the field, Send primary
  const startedEl = h('div', { class: 'feed-started' });
  let startedDoneOpen = false;   // 9.136: the ended ones wait folded; a click opens them for this page (a redraw keeps it)
  scroll.prepend(startedEl);
  const input = h('textarea', { class: 'feed-input', rows: '2', placeholder: 'Ask the agent…', 'aria-label': `message to the Global agent (${sendHint('chat')})` });
  const agentEl = h('div', { class: 'feed-agent' });   // only when the agent is not running: Start / Start and send (n)
  const sendBtn = h('button', { class: 'primary small feed-send', title: 'send to the Global agent', onclick: () => void send() }, 'Send', h('kbd', {}, '⏎'));
  const keysEl = h('div', { class: 'feed-keys mut small' }, h('kbd', {}, '⏎'), ' send ', h('kbd', {}, '⇧⏎'), ' new line', h('span', { class: 'feed-spacer' }), sendBtn);
  const compose = h('div', { class: 'feed-compose' }, agentEl, refsEl, input, keysEl);
  const el = h('div', { class: 'feed', onkeydown: (e: Event) => keys(e as KeyboardEvent) }, head, scroll, compose);

  let escArmed = false;   // Esc with a written message warned once
  input.addEventListener('keydown', (e) => {
    if (isSend(e, 'chat')) {
      e.preventDefault();
      void send();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      // like a dialog: a written message is never lost to one Esc - the first warns, the second discards
      if ((input.value.trim() || refs.length) && !escArmed) {
        escArmed = true;
        deps.toast('The unsent message will be discarded - Esc once more');
        return;
      }
      escArmed = false;
      if (input.value || refs.length) {
        input.value = '';
        refs = [];
        drawRefs();
      }
      deps.back();
    }
  });
  input.addEventListener('input', () => { escArmed = false; });   // written more after the warning: warn again

  async function load(withChat: boolean): Promise<void> {
    if (loading) return;
    loading = true;
    const target = wantV;
    let ok = false;
    try {
      const [f, c] = await Promise.all([feedApi.feed(), withChat ? feedApi.chat() : Promise.resolve(null)]);
      if (f.data) data = f.data;
      if (c?.data) {
        chat = c.data;
        chatAt = Date.now();
      }
      v = target;
      loadErr = '';
      ok = true;
      draw();
    } catch (e) {
      loadErr = String(e instanceof Error ? e.message : e);   // shown in the panel; the next state poll retries
      draw();
    } finally {
      loading = false;
    }
    if (ok && on && v !== wantV) void load(false);   // announced while this load was on its way
  }

  let on = false;
  /** every changed /api/state: refetch when `v` moved */
  function sync(s: FeedSummary | undefined): void {
    on = !!s?.on;
    if (!s?.on) {
      data = { on: false };
      return;
    }
    wantV = s.v;
    if (s.v !== v) void load(!chatAt || deps.shown());   // skipped while the chat poll loads: caught up after it
  }

  // the transcript grows without /api/state changing (its ETag stays): the chat has its own
  // clock while the panel is on screen - faster while a message waits for the agent
  window.setInterval(() => {
    if (!on || !deps.shown()) return;
    const waiting = (chat.queue ?? []).some((q) => q.state === 'queued') || chat.agent?.state === 'working';
    if (Date.now() - chatAt > (waiting ? 1500 : CHAT_MS)) void loadChat();
  }, 750);

  async function loadChat(): Promise<void> {
    if (loading) return;
    loading = true;
    try {
      const c = await feedApi.chat();
      chatAt = Date.now();
      if (c.data && JSON.stringify(c.data) !== JSON.stringify(chat)) {
        chat = c.data;
        draw();
      }
    } catch {
      chatAt = Date.now();   // piperd away: the state poll shows the banner
    } finally {
      loading = false;
    }
    if (on && v !== wantV) void load(false);   // a new version came while the chat was loading
  }

  function markSeen(): void {
    if (!deps.shown()) return;
    const top = shownTop;
    if (top > (data.seen ?? 0) && top > seenPosted) {
      seenPosted = top;
      void feedApi.seen(top).catch(() => { seenPosted = 0; });
    }
  }

  // ---------------------------------------------------------------- drawing
  function draw(): void {
    const scrolledDown = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40;
    const keep = el.contains(document.activeElement) ? document.activeElement : null;
    const keepIdx = keep instanceof HTMLElement ? rows().indexOf(keep) : -1;
    // a half-written reply survives the rebuild (a refetch comes every few seconds)
    const typing = actionsEl.querySelector<HTMLTextAreaElement>('.feed-reply textarea');
    const draft = typing ? { value: typing.value, a: typing.selectionStart, b: typing.selectionEnd, focused: keep === typing } : null;
    if (!deps.shown()) marker = null;
    else if (marker === null) marker = data.seen ?? 0;   // "new" stays where it was when the panel opened
    drawHead();
    drawStarted();
    drawActions();
    drawLine();
    drawAgent();
    if (scrolledDown) scroll.scrollTop = scroll.scrollHeight;
    const again = actionsEl.querySelector<HTMLTextAreaElement>('.feed-reply textarea');
    if (draft && again) {
      again.value = draft.value;
      if (draft.focused) {
        again.focus();
        again.setSelectionRange(draft.a, draft.b);
      }
    } else if (keep instanceof HTMLElement && !keep.isConnected && focusKey) {
      // the row is gone (resolved, dismissed): the keyboard goes on with the one now in its place
      if (!focusRow(focusKey) && keepIdx >= 0) {
        const list = rows();
        list[Math.min(keepIdx, list.length - 1)]?.focus();
      }
    }
    markSeen();
    deps.changed?.();
  }

  function drawHead(): void {
    clear(head);
    if (deps.chatOnly) return;
    const line = sinceLine(data.events ?? [], marker ?? data.seen ?? 0);
    head.append(
      line ? h('div', { class: 'feed-title' }, h('span', { class: 'mut feed-since', title: 'since you last looked' }, line)) : '',
      h('div', { class: 'feed-filters', role: 'tablist' }, ...FILTERS.map((f) => h('button', {
        class: 'tab' + (f.id === filter ? ' on' : ''), title: f.title, role: 'tab', 'aria-selected': f.id === filter,
        onclick: () => {
          filter = f.id;
          try {
            window.localStorage.setItem('piper.feed.filter', filter);
          } catch { /* private window */ }
          draw();
        },
      }, f.label))));
  }

  function drawActions(): void {
    clear(actionsEl);
    const acts = (data.actions ?? []).filter((a) => filter === 'all' || filter === 'me' || matches(a, filter));
    if (filter === 'agent') return;
    if (!acts.length) {
      if (filter === 'me') actionsEl.append(h('p', { class: 'mut feed-empty' }, 'Nothing is waiting for you.'));
      return;
    }
    for (const a of acts) actionsEl.append(card(a));
    const muted = Object.keys(data.muted ?? {});
    if (data.dismissed || muted.length) {
      actionsEl.append(h('p', { class: 'mut feed-note' },
        data.dismissed ? `dismissed: ${data.dismissed} ` : '',
        data.dismissed ? h('button', { class: 'mini link', onclick: () => void undismiss() }, 'restore') : null,
        muted.length ? `${data.dismissed ? ' · ' : ''}muted: ${muted.map((k) => deps.title(k)).join(', ')} ` : '',
        muted.length ? h('button', { class: 'mini link', onclick: () => void unmute(muted) }, 'unmute') : null));
    }
  }

  function card(a: FeedAction): HTMLElement {
    const run = (op: FeedOp): void => void doOp(a, op);
    const long = a.body.length > 240 || a.body.split('\n').length > 4;
    const bodyOpen = openBodies.has(a.key);
    const c = h('div', { class: `feed-card lv-${a.level}`, tabindex: '0', 'data-key': a.key },
      h('div', { class: 'feed-card-head' },
        h('span', { class: 'feed-ico', 'aria-hidden': 'true' }, LEVEL[a.level]?.icon ?? '•'),
        h('span', { class: 'feed-card-title' }, a.title),
        h('span', { class: 'mut feed-age', title: new Date(a.since * 1000).toLocaleString() }, ago(a.since))),
      subjectChip(a.subject),
      // a confirmation's warnings (⚠ push --force, reset --hard, .envrc…) never hide in the folded text
      ...(a.level === 'confirm' ? a.body.split('\n').filter((l) => l.startsWith('⚠')).map((l) => h('div', { class: 'feed-warn' }, l)) : []),
      a.body ? h('pre', { class: 'feed-body' + (long && !bodyOpen ? ' clamp' : '') }, a.body) : null,
      long ? h('button', { class: 'link mini', onclick: () => { if (bodyOpen) openBodies.delete(a.key); else openBodies.add(a.key); draw(); } },
        bodyOpen ? 'less' : 'more') : null,
      a.quote ? h('blockquote', { class: 'feed-quote' }, h('span', { class: 'mut' }, `↳ ${a.quote.author || 'GitLab'}:`), ' ', a.quote.text) : null,
      h('div', { class: 'feed-card-acts' },
        // W13: decision.ts order Deny · always · Allow; a dangerous Allow is red with its own words, Deny plain
        ...orderOps(a.actions).map((op, i) => h('button', {
          class: 'mini' + (dangerAllow(op) ? ' danger' : opRole(op) === 'allow' || (i === 0 && !opRole(op)) ? ' primary' : ''),
          onclick: (e: Event) => { e.stopPropagation(); run(op); },
        }, (opRole(op) === 'session' ? op.label : dangerAllow(op)) || (opRole(op) === 'deny' ? 'Deny' : op.label))),
        h('span', { class: 'feed-spacer' }),
        h('button', { class: 'mini link', title: 'hand over to the Global agent as a reference (a)', onclick: () => handOver(a.subject) }, '→ agent'),
        a.level !== 'security' ? h('button', { class: 'mini link', title: 'dismiss (e)', onclick: () => void dismiss(a) }, 'dismiss') : null,
        a.level !== 'security' ? h('button', { class: 'mini link', title: 'mute this subject for an hour (m)', onclick: () => void mute(a.subject) }, 'mute 1 h') : null));
    if (replyOpen === a.key) c.append(replyBox(a));
    c.addEventListener('focus', () => { focusKey = a.key; });
    return c;
  }

  function replyBox(a: FeedAction): HTMLElement {
    const sid = String(a.subject.session ?? a.subject.key);
    const ta = h('textarea', { rows: '2', placeholder: `Reply to ${a.subject.label} · ${sendHint('chat')} · Esc cancels` });
    ta.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') {
        replyOpen = '';
        draw();
      } else if (isSend(e, 'chat') && ta.value.trim()) {
        e.preventDefault();
        const text = ta.value;
        ta.disabled = true;
        void deps.reply(sid, text).then((ok) => {
          ta.disabled = false;
          if (ok) {
            replyOpen = '';
            deps.toast(`Sent to ${a.subject.label}`);
            draw();
          }
        });
      }
    });
    if (replyFocus) {
      replyFocus = false;
      window.requestAnimationFrame(() => ta.focus());
    }
    return h('div', { class: 'feed-reply' }, h('span', { class: 'feed-reply-to' }, `→ ${a.subject.label}`), ta);
  }

  function subjectChip(s: Subject): HTMLElement {
    return h('button', {
      class: 'feed-subj', title: 'open',
      onclick: (e: Event) => { e.stopPropagation(); openSubject(s); },
    }, s.label || s.key);
  }

  function openSubject(s: Subject): void {
    if (s.type === 'session' || s.session) deps.select(String(s.session ?? s.key));
    else if (s.type === 'issue' || s.type === 'mr') deps.openInbox(s.key);
    else if (s.type === 'prod') deps.openProd();
  }

  /** F21: "Started by the agent" - its sessions with their state and a link (the toast alone used to vanish) */
  function drawStarted(): void {
    clear(startedEl);
    const list = filter === 'agent' || filter === 'all' ? chat.started ?? [] : [];
    if (!list.length) return;
    const row = (x: (typeof list)[number]) => {
      const st = startedState(x.state);
      return h('div', { class: `feed-st t-${st.tone}` },
        h('span', { class: 'feed-st-ico', 'aria-hidden': 'true' }, st.tone === 'ok' ? '✓' : st.tone === 'bad' ? '✕' : st.tone === 'warn' ? '●' : '◇'),
        h('span', { class: 'feed-st-title' }, x.title),
        h('button', { class: 'link small feed-st-link', title: 'open the session', onclick: () => deps.select(x.id) }, markBidi(`${x.project} · ${st.word} ↗`)));
    };
    // 9.136: the running ones on show, the ended (done, failed) folded behind "N ended" until clicked
    const ended = list.filter((x) => ENDED_STATES.includes(x.state));
    const done = ended.length ? h('details', { class: 'feed-st-done' }, h('summary', { class: 'mut small' }, `${ended.length} ended`), ...ended.map(row)) : null;
    if (done) { done.open = startedDoneOpen; done.addEventListener('toggle', () => { startedDoneOpen = done.open; }); }
    startedEl.append(h('h3', { class: 'sec-h' }, 'Started by the agent'), ...list.filter((x) => !ENDED_STATES.includes(x.state)).map(row), done ?? '',
      h('h3', { class: 'sec-h' }, 'Chat'));
  }

  function drawLine(): void {
    clear(lineEl);
    // F21: the drawer is the chat only - the events ("Global agent opened session …") stay in the queue
    const rows = timeline(deps.chatOnly ? [] : data.events ?? [], bubbles(chat), marker ?? data.seen ?? 0, filter);
    shownTop = filter === 'me' ? 0 : Math.max(0, ...rows.map((r) => (r.type === 'event' ? r.e.ts : 0)));
    if (loadErr) lineEl.append(h('p', { class: 'bad small feed-empty' }, `The Feed could not be loaded: ${loadErr}`));
    if (filter === 'me') return;
    if (!rows.length) {
      lineEl.append(h('p', { class: 'mut feed-empty' }, filter === 'agent' ? 'Tell the Global agent what you need.'
        : filter === 'all' ? 'Nothing has happened yet.' : 'Nothing under this filter.'));
      drawSuggestions();
      drawCards();
      return;
    }
    for (const r of rows) {
      if (r.type === 'day') lineEl.append(h('div', { class: 'feed-day' }, r.label));
      else if (r.type === 'new') lineEl.append(h('div', { class: 'feed-new' }, 'new'));
      else if (r.type === 'event') lineEl.append(eventRow(r.e));
      else lineEl.append(bubble(r.b));
    }
    if (!rows.some((r) => r.type === 'chat')) drawSuggestions();   // no conversation yet (the agent's events alone)
    drawCards();
  }

  /** F21: the empty chat offers three suggestions from real data - a click puts the text in the field, never sends */
  function drawSuggestions(): void {
    if (filter !== 'agent') return;
    lineEl.append(h('div', { class: 'feed-sugg' }, ...chatSuggestions(chat.context).map((t) => h('button', { class: 'small feed-sugg-b',
      onclick: () => { input.value = t; input.focus(); input.setSelectionRange(t.length, t.length); } }, markBidi(t)))));
  }

  /** F21: while the agent waits for a card's answer the chat says where the card is */
  function drawCards(): void {
    if (filter !== 'agent' && filter !== 'all') return;
    for (const c of chat.cards ?? []) {
      lineEl.append(h('div', { class: 'feed-cardline' }, h('span', { class: 'feed-msg-who mut' }, 'agent'), markBidi(cardLine(c))));
    }
  }

  function eventRow(e: FeedEvent): HTMLElement {
    const row = h('div', { class: 'feed-ev', tabindex: '0', 'data-key': e.id, title: new Date(e.ts * 1000).toLocaleString(),
      onclick: () => openSubject(e.subject) },
    h('span', { class: 'feed-ico', 'aria-hidden': 'true' }, SOURCE_ICON[e.source] ?? '·'),
    h('span', { class: 'feed-ev-title' }, e.title, e.n > 1 ? h('span', { class: 'mut' }, ` ×${e.n}`) : null),
    h('span', { class: 'mut feed-time' }, clock(e.ts)),
    h('button', { class: 'mini link feed-ev-agent', title: 'hand over to the Global agent as a reference (a)',
      onclick: (ev: Event) => { ev.stopPropagation(); handOver(e.subject); } }, '→'));
    row.addEventListener('focus', () => { focusKey = e.id; });
    return row;
  }

  function bubble(b: Bubble): HTMLElement {
    if (b.who === 'tools') return h('div', { class: 'feed-tools mut' }, `✦ tools: ${b.text}`);
    const who = b.who === 'agent' ? 'Global agent' : b.who === 'queued' ? 'You · ' + queuedLabel(b) : b.terminal ? 'You · from the terminal' : 'You';
    const long = b.text.length > 700 || b.text.split('\n').length > 14;
    const key = `b:${b.ts}:${b.who}`;
    const opened = openBodies.has(key);
    return h('div', { class: `feed-msg ${b.who === 'agent' ? 'agent' : 'you'}${b.who === 'queued' ? ' queued' : ''}` },
      h('div', { class: 'feed-msg-who mut' }, who, h('span', { class: 'feed-time' }, b.ts ? clock(b.ts) : ''),
        b.who === 'queued' && b.id && (b.state === 'queued' || b.state === 'error')
          ? h('button', { class: 'mini link', title: b.state === 'error' ? 'discard' : 'cancel while it is not sent yet', 'aria-label': b.state === 'error' ? 'discard' : 'cancel',
            onclick: () => void cancel(b.id!) }, fi('x', 'gi')) : null,
        b.who === 'queued' && b.id && (b.state === 'lost' || b.state === 'error')
          ? h('button', { class: 'mini link', title: 'back to the queue', onclick: () => void retry(b.id!) }, 'Send again') : null),
      h('div', { class: 'feed-msg-text' + (long && !opened ? ' clamp' : '') }, b.text),
      long ? h('button', { class: 'mini link', onclick: () => { if (opened) openBodies.delete(key); else openBodies.add(key); draw(); } },
        opened ? 'less' : 'more') : null);
  }

  function queuedLabel(b: Bubble): string {
    if (b.state === 'error') return b.error ? `unsent · ${b.error}` : 'unsent';
    if (b.state === 'lost') return 'not delivered · the agent ended before reading it';
    if (b.state === 'sent') return 'sent · waiting for the agent to read it';
    const a = chat.agent;
    if (!a || !['ready', 'working', 'waiting', 'attention', 'starting', 'attesting', 'trust', 'preflight', 'preparing', 'spawning'].includes(a.state)) {
      return 'queued · the agent is not running';
    }
    if (a.hold) return 'queued · waiting until you finish typing in its terminal';
    if (a.state === 'working') return 'queued · the agent is working';
    if (a.state === 'ready') return 'queued · sent after its first turn';
    return 'queued';
  }

  function drawAgent(): void {
    clear(agentEl);
    // F21: the state is said once, in the drawer's header (agentStatus); here only Start when it is not running
    const live = agentStatus(chat).live;
    agentEl.classList.toggle('hidden', live);
    if (!live) {
      const n = (chat.queue ?? []).filter((q) => q.state === 'queued').length;
      agentEl.append(h('span', { class: 'mut' }, 'not running'),
        h('button', { class: 'mini primary', onclick: () => void start() }, n ? `Start and send (${n})` : 'Start'));
    }
  }

  function drawRefs(): void {
    clear(refsEl);
    for (const r of refs) {
      refsEl.append(h('span', { class: 'feed-ref' }, r, h('button', { class: 'mini link', title: 'remove', 'aria-label': `remove ${r}`, onclick: () => {
        refs = refs.filter((x) => x !== r);
        drawRefs();
      } }, fi('x', 'gi'))));
    }
  }

  // ---------------------------------------------------------------- actions
  async function doOp(a: FeedAction, op: FeedOp): Promise<void> {
    if (op.op === 'open') deps.select(String(op.args.session));
    else if (op.op === 'reply') {
      replyOpen = replyOpen === a.key ? '' : a.key;
      replyFocus = replyOpen === a.key;
      draw();
    } else if (op.op === 'decide') {
      try {
        await deps.decide(String(op.args.id), op.args.ok === true, op.args.always === true, op.args.session_always === true);
        void load(false);
      } catch (e) {
        if (decidedElsewhere(e)) void load(false);
        else deps.toast('Not decided: ' + String(e instanceof Error ? e.message : e));
      }
    } else if (op.op === 'inbox') deps.openInbox(String(op.args.key));
    else if (op.op === 'prod') deps.openProd();
    else if (op.op === 'url' && /^https?:\/\//.test(String(op.args.url))) window.open(String(op.args.url), '_blank', 'noopener');
  }

  function handOver(s: Subject): void {
    const r = refFor(s);
    if (!refs.includes(r)) refs.push(r);
    drawRefs();
    input.focus();
  }

  async function dismiss(a: FeedAction): Promise<void> {
    try {
      await feedApi.dismiss(a.key);
      await load(false);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  async function undismiss(): Promise<void> {
    try {
      await feedApi.undismiss();
      await load(false);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  async function unmute(keys: string[]): Promise<void> {
    try {
      for (const k of keys) await feedApi.mute(k, 0);
      await load(false);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  async function mute(s: Subject): Promise<void> {
    try {
      await feedApi.mute(s.key, 60);
      deps.toast(`${s.label || s.key}: muted for an hour`);
      await load(false);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  async function send(): Promise<void> {
    const text = [...refs, input.value.trim()].filter(Boolean).join('\n');
    if (!input.value.trim()) return;
    input.disabled = true;
    try {
      await feedApi.send(text);
      input.value = '';
      refs = [];
      drawRefs();
      if (filter !== 'all' && filter !== 'agent') filter = deps.chatOnly ? 'agent' : 'all';
      await load(true);
      scroll.scrollTop = scroll.scrollHeight;
    } catch (e) {
      deps.toast('Not sent: ' + String(e instanceof Error ? e.message : e));
    } finally {
      input.disabled = false;
      input.focus();
    }
  }

  async function retry(id: string): Promise<void> {
    try {
      await feedApi.retry(id);
      await load(true);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  async function cancel(id: string): Promise<void> {
    try {
      await feedApi.cancel(id);
      await load(true);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  async function start(): Promise<void> {
    try {
      const r = await feedApi.start();
      const n = r.data?.sent.length ?? 0;
      deps.toast(n ? `Global agent is starting, it gets ${n === 1 ? 'your message' : `${n} messages`}` : 'Global agent is starting');
      await load(true);
    } catch (e) {
      deps.toast(String(e instanceof Error ? e.message : e));
    }
  }

  // ---------------------------------------------------------------- keyboard
  function rows(): HTMLElement[] {
    return [...scroll.querySelectorAll<HTMLElement>('.feed-card, .feed-ev')];
  }

  function focusRow(key: string): boolean {
    const r = rows().find((x) => x.dataset.key === key);
    r?.focus({ preventScroll: false });
    return !!r;
  }

  function keys(e: KeyboardEvent): void {
    const t = e.target;
    if (t instanceof HTMLTextAreaElement || t instanceof HTMLInputElement || e.metaKey || e.ctrlKey || e.altKey) return;
    const list = rows();
    const cur = list.findIndex((x) => x === document.activeElement);
    const act = (data.actions ?? []).find((a) => a.key === focusKey && cur >= 0 && list[cur]?.dataset.key === a.key);
    const ev = (data.events ?? []).find((x) => x.id === focusKey && cur >= 0 && list[cur]?.dataset.key === x.id);
    const op = (kind: FeedOp['op'], ok?: boolean) => act?.actions.find((o) => o.op === kind && (ok === undefined || o.args.ok === ok));
    let done = true;
    if (e.key === 'j' || e.key === 'ArrowDown') list[Math.min(list.length - 1, cur + 1)]?.focus();
    else if (e.key === 'k' || e.key === 'ArrowUp') list[Math.max(0, cur - 1)]?.focus();
    else if (e.key === 'Enter' && act?.actions[0] && !dangerAllow(act.actions[0])) void doOp(act, act.actions[0]);
    else if (e.key === 'Enter' && ev) openSubject(ev.subject);
    else if (e.key === 'y' && act && op('decide', true) && !dangerAllow(op('decide', true)!)) void doOp(act, op('decide', true)!);   // never one key for a red Allow
    else if (e.key === 'n' && act && op('decide', false)) void doOp(act, op('decide', false)!);
    else if (e.key === 'o' && (act || ev)) openSubject((act ?? ev)!.subject);
    else if (e.key === 'a' && (act || ev)) handOver((act ?? ev)!.subject);
    else if (e.key === 'e' && act && act.level !== 'security') void dismiss(act);
    else if (e.key === 'm' && act && act.level !== 'security') void mute(act.subject);
    else if (e.key === 'Escape') deps.back();
    else if (e.key === '/' || e.key === 'c') input.focus();
    else done = false;
    if (done) e.preventDefault();
  }

  return {
    el,
    sync,
    /** the panel became visible: fresh data, the input or the first action focused */
    show(focus: boolean): void {
      void load(true);
      if (focus) window.requestAnimationFrame(() => (rows()[0] && (data.actions ?? []).length ? rows()[0]!.focus() : input.focus()));
    },
    focusInput(): void {
      input.focus();
    },
    /** what the queue shows of the Feed: its events and what was seen / dismissed */
    data: (): FeedData => data,
    /** a reference to the Global agent's input (the queue's `a`) */
    handOver,
    /** reload now (after a dismiss / mute from the queue) */
    reload: () => void load(false),
    /** F21: the agent's state for the drawer's header (said once there; the chip and the line under the field are gone) */
    status: () => agentStatus(chat),
  };
}
