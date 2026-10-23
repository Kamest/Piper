// HTTP client for piperd's /api. The token arrives once in the URL fragment
// (`task piper:open` builds it), moves to localStorage and the address bar is
// scrubbed.

import type {
  ConversationHistory, HistoryPreview, SessionStartResult,
  ReviewAction, ReviewItem, ReviewFiles, ReviewCheck, ReviewedCommit, FileBlob, Agent, AuditEvent, BlameLine, BoardCard, CommentDraft, IssueStateUpdate, Inbox, InboxPreview, InboxRule, InboxStartBody, StartOpts, CiState, Commit, Conversation, ConvHit, Diagnostics, DirListing, FileText, GitBranches, GitCommitDetail, GitPushPreview, GitRow, GitStatus, GrepResult,
  IdeaProblems, Issue, IssueList, LineMarks, MattermostStatus, MergeRequest, MrDraft, StackJob, StackJobLog, StackStatus, StackVerb, State, SessionUnlock, SymbolResult, FileTurns, TurnFiles, TurnList, UsageReport, WriteResult,
} from './types.js';

const KEY = 'piper.token';
/** a hung piperd (not a refused connection) must still show the "not responding" banner */
const STATE_TIMEOUT_MS = 8000;

export class ApiError extends Error {
  /** json: the error body piperd sent (e.g. 409 hidden_paths of an import), null when there was none */
  constructor(readonly status: number, message: string, readonly token = '', readonly json: unknown = null) {
    super(message);
  }
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

// A token from this tab's URL is only a candidate until piperd accepts it: a stale or mistyped
// link in one tab must not overwrite (or, on its 401, wipe) the token every other tab uses.
let candidate = '';

/** takes `#token=` from the address bar (boot and every hashchange); true when there was one */
export function absorbFragmentToken(): boolean {
  const m = /(?:^|&)token=([^&]+)/.exec(location.hash.slice(1));
  if (!m?.[1]) return false;
  let raw = m[1];
  try {
    raw = decodeURIComponent(raw);
  } catch { /* a cut %-escape: the rest as it is */ }
  candidate = cleanToken(raw);
  history.replaceState(null, '', location.pathname + location.search);
  return true;
}

export function token(): string {
  return candidate || (storage()?.getItem(KEY) ?? '');
}

export function setToken(t: string): void {
  candidate = '';
  storage()?.setItem(KEY, cleanToken(t));
}

/** N8: piperd's tokens are URL-safe base64 - a zero-width / bidi / NBSP character copied with a link (or a paste)
 *  made every fetch throw (a header must be ISO-8859-1), which read as "piperd is not responding" */
function cleanToken(t: string): string {
  return t.replace(/[^\x21-\x7e]/g, '');
}

/** drops the token piperd refused; true when another (the stored one) is left to try */
export function forgetToken(failed: string): boolean {
  if (candidate && candidate === failed) {
    candidate = '';
    return !!token();
  }
  const st = storage();
  if (st?.getItem(KEY) === failed) st.removeItem(KEY);
  return !!token() && token() !== failed;
}

export async function call<T>(method: string, path: string, body?: unknown, etag?: string, timeoutMs = 0): Promise<{ data: T | null; etag: string }> {
  const sent = token();
  const headers: Record<string, string> = { Authorization: 'Bearer ' + sent };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (etag) headers['If-None-Match'] = etag;
  const init: RequestInit = { method, headers, cache: 'no-store' };
  if (body !== undefined) init.body = JSON.stringify(body);
  if (timeoutMs) init.signal = AbortSignal.timeout(timeoutMs);
  let r: Response;
  try {
    r = await fetch(path, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'TimeoutError') throw new Error(`no answer in ${Math.round(timeoutMs / 1000)} s`);
    throw e;
  }
  if (candidate && sent === candidate && r.ok) setToken(candidate);
  if (r.status === 304) return { data: null, etag: etag ?? '' };
  const text = await r.text();
  let json: unknown = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  if (!r.ok) {
    const msg = json && typeof json === 'object' && 'error' in json ? String((json as { error: unknown }).error) : r.statusText;
    throw new ApiError(r.status, msg, sent, json);
  }
  return { data: json as T, etag: r.headers.get('ETag') ?? '' };
}

export const api = {
  state: (etag: string) => call<State>('GET', '/api/state', undefined, etag, STATE_TIMEOUT_MS),
  ping: () => call<{ ok: boolean }>('GET', '/api/ping'),
  create: (project: string, task: string, title: string, opts?: StartOpts, agent: Agent = 'claude', remember = false, imageRebuild = false) =>
    call<SessionStartResult>('POST', '/api/sessions', { project, task, title, ...(opts && Object.keys(opts).length ? { opts } : {}), ...(agent !== 'claude' ? { agent } : {}), ...(remember ? { remember_for_project: true } : {}), ...(imageRebuild ? { image_rebuild: true } : {}) }),
  act: (id: string, action: 'stop' | 'approve' | 'retry' | 'forget') =>
    call('POST', `/api/sessions/${encodeURIComponent(id)}/${action}`, {}),
  rename: (id: string, title: string) => call('POST', `/api/sessions/${encodeURIComponent(id)}/title`, { title }),
  decide: (id: string, approve: boolean, always = false, sessionAlways = false) =>
    call('POST', `/api/pending/${encodeURIComponent(id)}`, { approve, ...(always ? { always } : {}), ...(sessionAlways ? { session_always: true } : {}) }),
  importProjects: () => call<{ added: string[] }>('POST', '/api/projects/import', {}),
  git: (id: string) => call<GitStatus>('GET', `/api/sessions/${encodeURIComponent(id)}/git`),
  /** amend (W6): replaces HEAD; piperd refuses it once HEAD is on a remote branch */
  commit: (id: string, message: string, files: string[], reviewed?: ReviewedCommit, amend = false) =>
    call<{ commit: string; hooks_disabled: boolean; index_warning?: string }>('POST', `/api/sessions/${encodeURIComponent(id)}/git/commit`, { message, files, ...(reviewed ? { reviewed } : {}), ...(amend ? { amend } : {}) }),
  /** W6: a commit message drafted by `claude -p` on the host from the checked files' diff (hidden files never sent) */
  gitMessage: (id: string, files: string[]) =>
    call<{ message: string; withheld: string[]; truncated: boolean }>('POST', `/api/sessions/${encodeURIComponent(id)}/git/message`, { files }),
  /** commit A: an MR title and description drafted the same way from the branch's commits and diff against `target` */
  mrMessage: (id: string, target: string) =>
    call<{ title: string; description: string; withheld: string[]; truncated: boolean }>('POST', `/api/sessions/${encodeURIComponent(id)}/git/mr-message`, { target }),
  /** `piper attach <id>` in a new Ghostty window; without Ghostty ok=false and the command to copy */
  openTerminal: (id: string) => call<{ ok: boolean; command: string; error?: string; terminal?: string }>('POST', `/api/sessions/${encodeURIComponent(id)}/open-terminal`, {}),
  fixExcludes: (id: string) => call<{ written: string[]; skipped: string[] }>('POST', `/api/sessions/${encodeURIComponent(id)}/fix-excludes`, {}),
  /** what a push would send: the commits, their files, checks (the Push dialog) */
  pushPreview: (id: string) => call<GitPushPreview>('GET', `/api/sessions/${encodeURIComponent(id)}/git?push=1`),

  /** accept the uncovered files in their mount root (policy library_allow) and retry the start */
  allowUncovered: (id: string) => call<{ allowed: string[] }>('POST', `/api/sessions/${encodeURIComponent(id)}/allow-uncovered`, {}),
  push: (id: string) => call<{ branch: string; output: string }>('POST', `/api/sessions/${encodeURIComponent(id)}/git/push`, {}),
  // workbench; a project key contains '/', so it travels in the query
  /** every file but .git - a file hidden from Claude too, flagged `hidden` (the GUI's full token) */
  list: (project: string, path: string) => call<DirListing>('GET', `/api/fs/list?${q({ project, path })}`),
  read: (project: string, path: string, offset = 0) =>
    call<FileText>('GET', `/api/fs/read?${q({ project, path, offset: String(offset), limit: '5000' })}`),
  files: (project: string) => call<{ files: string[]; truncated: boolean }>('GET', `/api/fs/files?${q({ project })}`),
  grep: (project: string, text: string, glob: string, opts: { cs?: boolean; limit?: number } = {}) =>
    call<GrepResult>('GET', `/api/fs/grep?${q({ project, q: text, glob, cs: opts.cs === false ? '0' : '', limit: opts.limit ? String(opts.limit) : '' })}`),
  /** file operations of the tree: the same guards as write (not while the session works, hidden never) */
  fsCreate: (project: string, path: string, kind: 'file' | 'dir', text = '') =>
    call<{ path: string; sha?: string }>('POST', '/api/fs/create', kind === 'file' ? { project, path, kind, text } : { project, path, kind }),
  fsRename: (project: string, from: string, to: string) => call<{ from: string; to: string }>('POST', '/api/fs/rename', { project, from, to }),
  fsDelete: (project: string, path: string) => call<{ trash: string }>('POST', '/api/fs/delete', { project, path }),
  fsRestore: (project: string, trash: string, replace = false) => call<{ path: string; replaced?: boolean }>('POST', '/api/fs/restore',
    replace ? { project, trash, replace } : { project, trash }),
  /** the turns of the session that changed one file, newest first */
  fileTurns: (id: string, path: string) => call<FileTurns>('GET', `/api/sessions/${encodeURIComponent(id)}/turns/file?${q({ path })}`),
  issues: (project: string, search: string) => call<IssueList>('GET', `/api/gitlab/issues?${q({ project, search })}`),
  issue: (project: string, iid: number) => call<Issue>('GET', `/api/gitlab/issue?${q({ project, iid: String(iid) })}`),
  conversation: (id: string) => call<Conversation>('GET', `/api/sessions/${encodeURIComponent(id)}/conversation`),
  searchConversations: (text: string) => call<{ hits: ConvHit[] }>('GET', `/api/conversations/search?${q({ q: text })}`),
  conversationHistory: (text = '', project = '', agent = '', period = '', offset = 0, external = false) =>
    call<ConversationHistory>('GET', `/api/conversations?${q({ q: text, project, agent, period, offset: String(offset), external: external ? '1' : '0' })}`),
  importConversation: (id: string, project: string, confirmCheckout = false, confirmHidden = false) =>
    call<{ session: string }>('POST', `/api/conversations/${encodeURIComponent(id)}/import`,
      { project, confirm_checkout: confirmCheckout, ...(confirmHidden ? { confirm_hidden: true } : {}) }),
  conversationPreview: (id: string) => call<HistoryPreview>('GET', `/api/conversations/${encodeURIComponent(id)}/preview`),
  resumeConversation: (id: string, fork = false) =>
    call<{ session: string; running?: boolean; fork?: boolean }>('POST', `/api/conversations/${encodeURIComponent(id)}/${fork ? 'fork' : 'resume'}`, {}),
  /** W8: an ended session goes on with a new task - resumed, compacted first, then the task typed */
  continueSession: (id: string, task: string) =>
    call<{ session: string }>('POST', `/api/conversations/${encodeURIComponent(id)}/continue`, { task }),
  /** 9.140: the other agent continues this project session's work (stops it, hands over a document) */
  switchAgent: (id: string, confirmHidden = false) =>
    call<{ session: string; from: string; agent: Agent }>('POST', `/api/sessions/${encodeURIComponent(id)}/switch-agent`, confirmHidden ? { confirm_hidden: true } : {}),
  board: () => call<{ cards: BoardCard[] }>('GET', '/api/board'),
  audit: (session: string, limit = 300) => call<{ events: AuditEvent[] }>('GET', `/api/audit?${q({ session, limit: String(limit) })}`),
  send: (id: string, text: string) => call('POST', `/api/sessions/${encodeURIComponent(id)}/send`, { text }),
  unlock: (id: string, what: keyof SessionUnlock, on: boolean) =>
    call<{ unlock: SessionUnlock }>('POST', `/api/sessions/${encodeURIComponent(id)}/unlock`, { what, on }),
  /** 9.54: arm / cancel `hole start -r` for the next start of any session */
  imageRebuild: (on: boolean) => call<{ image_rebuild: ImageRebuild }>('POST', '/api/image/rebuild', { on }),
  startOpt: (id: string, what: keyof StartOpts, on: boolean) =>
    call<{ opts: StartOpts }>('POST', `/api/sessions/${encodeURIComponent(id)}/start-opt`, { what, on }),
  allow: (id: string, domain: string) => call<{ domain: string; written: boolean }>('POST', `/api/sessions/${encodeURIComponent(id)}/allow`, { domain }),
  // 9.7: editor, navigation, git history, CI, stack, diagnostics, IntelliJ, usage
  marks: (project: string, path: string) => call<LineMarks>('GET', `/api/fs/marks?${q({ project, path })}`),
  /** name = exact symbol (go to definition); query = prefix/fuzzy (symbol search) */
  symbols: (project: string, name: string, query = '') => call<SymbolResult>('GET', `/api/fs/symbols?${q({ project, name, q: query })}`),
  /** save from the GUI: refused (409) when the file changed since `sha` or a session of the project is working */
  write: (project: string, path: string, text: string, sha: string) =>
    call<WriteResult>('POST', '/api/fs/write', { project, path, text, sha }),
  reviewFiles: (id: string, scope: string, n = 0) => call<ReviewFiles>('GET', `/api/sessions/${encodeURIComponent(id)}/review?${q({ scope, n: String(n) })}`),
  reviewBlob: (id: string, review: string, path: string, side: 'left' | 'right') =>
    call<FileBlob>('GET', `/api/sessions/${encodeURIComponent(id)}/review/blob?${q({ review, path, side })}`),
  reviewCheck: (id: string, review: string, path = '') => call<ReviewCheck>('GET', `/api/sessions/${encodeURIComponent(id)}/review/check?${q({ review, ...(path ? { path } : {}) })}`),
  reviewActions: (id: string) => call<{ actions: ReviewAction[] }>('GET', `/api/sessions/${encodeURIComponent(id)}/review/config`),
  reviewStage: (id: string, review: string, path: string) => call<{ staged: string }>('POST', `/api/sessions/${encodeURIComponent(id)}/review/stage`, { review, path }),
  reviewWrite: (id: string, review: string, path: string, text: string | null, sha: string | null) => call<{ sha: string | null }>('POST', `/api/sessions/${encodeURIComponent(id)}/review/write`, { review, path, text, sha }),
  reviewDispatch: (id: string, text: string, target: 'current' | 'new', items: ReviewItem[]) =>
    call<{ session: string; sent: boolean; agent?: Agent }>('POST', `/api/sessions/${encodeURIComponent(id)}/review/dispatch`, { text, target, items: items.map(({ path, sha }) => ({ path, sha })), edit: items.some(x => x.actions.some(a => a.kind === 'edit') || (!x.actions.length && !!x.text.trim())) }),
  reviewHistory: (id: string, review: string, path: string) => call<FileTurns>('GET', `/api/sessions/${encodeURIComponent(id)}/review/history?${q({ review, path })}`),
  turns: (id: string) => call<TurnList>('GET', `/api/sessions/${encodeURIComponent(id)}/turns`),
  /** files changed between two snapshots; to '' = the working tree now */
  turnFiles: (id: string, from: string, to: string) => call<TurnFiles>('GET', `/api/sessions/${encodeURIComponent(id)}/turn?${q({ from, to })}`),
  fileLog: (project: string, path: string, n = 50) => call<{ commits: Commit[] }>('GET', `/api/git/log?${q({ project, path, n: String(n) })}`),
  blame: (project: string, path: string) => call<{ lines: BlameLine[] }>('GET', `/api/git/blame?${q({ project, path })}`),
  graph: (project: string, n = 80) => call<{ lines: string[] }>('GET', `/api/git/graph?${q({ project, n: String(n) })}`),
  // the Git window (W4/W5): branches, worktrees, the log as rows, one commit's files and sides
  gitBranches: (project: string) => call<GitBranches>('GET', `/api/git/branches?${q({ project })}`),
  gitRows: (project: string, n = 300, ref = '') => call<{ rows: GitRow[] }>('GET', `/api/git/rows?${q({ project, n: String(n), ref })}`),
  gitCommitDetail: (project: string, commit: string) => call<GitCommitDetail>('GET', `/api/git/commit?${q({ project, commit })}`),
  gitBlob: (project: string, commit: string, path: string, side: 'old' | 'new') =>
    call<{ text: string; exists: boolean; symlink?: boolean }>('GET', `/api/git/blob?${q({ project, commit, path, side })}`),
  gitBranch: (project: string, body: { action: 'switch' | 'create' | 'delete'; name: string; start?: string; checkout?: boolean; force?: boolean }) =>
    call<{ deleted?: boolean; unmerged?: string[]; branch?: string }>('POST', '/api/git/branch', { project, ...body }),
  gitRemote: (project: string, action: 'fetch' | 'pull') => call<{ output: string }>('POST', `/api/git/${action}`, { project }),
  gitWorktree: (project: string, body: { action: 'add' | 'remove'; name: string; branch?: string; new?: boolean; start?: string }) =>
    call<{ path?: string; branch?: string; key?: string }>('POST', '/api/git/worktree', { project, ...body }),
  ci: (project: string) => call<CiState>('GET', `/api/git/ci?${q({ project })}`),
  mrDraft: (project: string, target = '') => call<MrDraft>('GET', `/api/git/mr-draft?${q({ project, target })}`),
  createMr: (project: string, title: string, description: string, target: string, draft: boolean) =>
    call<MergeRequest>('POST', '/api/git/mr', { project, title, description, target, draft }),
  stack: (project: string) => call<StackStatus>('GET', `/api/stack?${q({ project })}`),
  stackRestart: (project: string, service: string) => call<{ ok: boolean; job?: string }>('POST', '/api/stack/restart', { project, service }),
  stackAction: (project: string, verb: StackVerb, service: string) => call<{ ok: boolean; job: StackJob | null }>('POST', '/api/stack/action', { project, verb, service }),
  stackJob: (project: string, id: string, offset: number) => call<StackJobLog>('GET', `/api/stack/job?${q({ project, id, offset: String(offset) })}`),
  stackCancel: (project: string, id: string) => call<{ ok: boolean; state: string; signalled: boolean }>('POST', '/api/stack/cancel', { project, id }),
  diagnostics: (id: string) => call<Diagnostics>('GET', `/api/sessions/${encodeURIComponent(id)}/diagnostics`),
  ideaStatus: () => call<{ available: boolean; url: string; note?: string }>('GET', '/api/idea/status'),
  ideaProblems: (project: string, path: string) => call<IdeaProblems>('GET', `/api/idea/problems?${q({ project, path })}`),
  ideaSymbol: (project: string, path: string, line: number, col: number) =>
    call<{ available: boolean; info: string }>('GET', `/api/idea/symbol?${q({ project, path, line: String(line), col: String(col) })}`),
  usage: () => call<UsageReport>('GET', '/api/usage'),
  // GitLab inbox
  inbox: () => call<Inbox>('GET', '/api/inbox'),
  /** polls GitLab now (debounced by piperd) and answers with the fresh inbox (<= 20 s) */
  inboxRefresh: () => call<Inbox>('POST', '/api/inbox/refresh', {}),
  inboxItem: (key: string) => call<InboxPreview>('GET', `/api/inbox/item?${q({ key })}`),
  inboxSeen: (keys: string[] | 'all') => call<{ ok: boolean }>('POST', '/api/inbox/seen', keys === 'all' ? { all: true } : { keys }),
  inboxUnseen: (key: string) => call<{ ok: boolean }>('POST', '/api/inbox/unseen', { key }),
  /** marks every pending GitLab todo of the key as done (the GUI confirmed first) */
  inboxDone: (key: string) => call<{ done: number }>('POST', '/api/inbox/done', { key }),
  /** a new session (answers the session json) or, with `session`, a follow-up typed into it ({sent}) */
  inboxStart: (body: InboxStartBody) => call<SessionStartResult>('POST', '/api/inbox/start', body),
  inboxComment: (key: string, body: string) => call<{ note_id: number; web_url: string }>('POST', '/api/inbox/comment', { key, body }),
  inboxRules: (rules: InboxRule[]) => call<{ rules: InboxRule[] }>('POST', '/api/inbox/rules', { rules }),
  /** adds a discovered checkout (one of an item's candidates) to projects.json */
  projectAdd: (path: string, gl: string) => call<{ key: string }>('POST', '/api/projects/add', { path, gl }),
  draft: (id: string) => call<CommentDraft>('GET', `/api/sessions/${encodeURIComponent(id)}/draft`),
  /** 9.12 D: a prompt into the live session, or piperd's summary into the state file */
  issueState: (id: string) => call<IssueStateUpdate>('POST', `/api/sessions/${encodeURIComponent(id)}/issue-state`, {}),
  // 9.12: Mattermost (incoming webhook) + the half-written prompt
  presence: (visible: boolean) => call<{ ok: boolean }>('POST', '/api/presence', { visible }),
  mmStatus: () => call<MattermostStatus>('GET', '/api/mattermost'),
  mmTest: () => call<MattermostStatus>('POST', '/api/mattermost/test', {}),
  /** the prompt is empty: piperd may type its automatic text (a handoff line) again */
  inputClear: (id: string) => call<{ ok: boolean }>('POST', `/api/sessions/${encodeURIComponent(id)}/input-clear`, {}),
};

function q(params: Record<string, string>): string {
  return new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '')).toString();
}

/** one file's diff as plain text (the GUI shows everything; sessions get a filtered view via MCP) */
export async function gitDiff(id: string, path: string, untracked: boolean): Promise<string> {
  const q = `diff=${encodeURIComponent(path)}${untracked ? '&untracked=1' : ''}`;
  const r = await fetch(`/api/sessions/${encodeURIComponent(id)}/git?${q}`, {
    headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store',
  });
  const text = await r.text();
  if (!r.ok) throw new ApiError(r.status, text);
  return text;
}

/** upload a pasted / dropped image; returns its path inside the sandbox */
export async function dropImage(id: string, file: Blob): Promise<string> {
  const r = await fetch(`/api/sessions/${encodeURIComponent(id)}/drop`, {
    method: 'POST', body: file,
    headers: { Authorization: 'Bearer ' + token(), 'Content-Type': file.type || 'application/octet-stream' },
  });
  const j = (await r.json().catch(() => ({}))) as { path?: string; error?: string };
  if (!r.ok || !j.path) throw new ApiError(r.status, j.error ?? r.statusText);
  return j.path;
}

async function text(path: string): Promise<string> {
  const r = await fetch(path, { headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store' });
  const t = await r.text();
  if (!r.ok) {
    let msg = t;
    try { msg = String((JSON.parse(t) as { error?: unknown }).error ?? t); } catch { /* plain text */ }
    throw new ApiError(r.status, msg);
  }
  return t;
}

/** unified diff of a project file against HEAD (project-keyed; .git: 404) */
export const projectDiff = (project: string, path: string) => text(`/api/git/diff?${q({ project, path })}`);
/** unified diff of one commit */
export const commitDiff = (project: string, commit: string) => text(`/api/git/show?${q({ project, commit })}`);
/** a file only the session's sandbox has (its /tmp), read-only */
export const sandboxFile = (id: string, path: string) => text(`/api/sessions/${encodeURIComponent(id)}/sandbox-file?${q({ path })}`);
/** unified diff of one session turn (from/to snapshot trees; to '' = now), optionally one path */
export const turnDiff = (id: string, from: string, to: string, path = '') =>
  text(`/api/sessions/${encodeURIComponent(id)}/turn?${q({ from, to, diff: path || '.' })}`);
/** the tail of one service's log from the stack provider (scrubbed there) */
export const stackLogs = (project: string, service: string, tail = 300) =>
  text(`/api/stack/logs?${q({ project, service, tail: String(tail) })}`);

/** Download a binary artifact without navigating to a browser or executing an HTML report. */
export async function downloadFile(project: string, path: string): Promise<void> {
  const r = await fetch(`/api/fs/download?${q({ project, path })}`, { headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store' });
  if (!r.ok) throw new ApiError(r.status, 'Could not download this artifact');
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement('a');
  a.href = url; a.download = path.split('/').pop() || 'artifact';
  document.body.append(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}

/** an image of the project as a blob: URL (CSP img-src allows blob:); the caller revokes it */
export async function rawImage(project: string, path: string): Promise<string> {
  const r = await fetch(`/api/fs/raw?${q({ project, path })}`, { headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store' });
  if (!r.ok) throw new ApiError(r.status, (await r.text()).slice(0, 300));
  return URL.createObjectURL(await r.blob());
}

/** Files: the ⌘P index and the search, all=1 = the excluded tooling folders too */
export const fsIndex = (project: string, all = false) =>
  call<import('./types.js').FileIndex>('GET', `/api/fs/files?${q({ project, all: all ? '1' : '' })}`);
export const fsGrep = (project: string, text: string, glob: string, opts: { cs?: boolean; limit?: number; all?: boolean } = {}) =>
  call<GrepResult>('GET', `/api/fs/grep?${q({ project, q: text, glob, cs: opts.cs === false ? '0' : '', limit: opts.limit ? String(opts.limit) : '', all: opts.all ? '1' : '' })}`);

import type { ImageRebuild } from './types.js';   // 9.54, appended like the next one
import type { ProjectGitStatus } from './types.js';   // appended (api.ts is shared: no edit of the import above)
/** K2: a project's git status without a session (Files' Changed scope, tree marks, branch) */
export const projectGitStatus = (project: string) => call<ProjectGitStatus>('GET', `/api/git/status?${q({ project })}`);
