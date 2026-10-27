// The /api/state payload (piper/sessions.py Session.to_json, Manager.pending_list).

import type { FeedAction } from './feed.js';

export type SessionState =
  | 'preflight' | 'blocked' | 'preparing' | 'spawning' | 'trust' | 'starting' | 'attesting'
  | 'ready' | 'working' | 'waiting' | 'attention' | 'stopping' | 'exited' | 'error';

export interface Block {
  kind: 'settings-changed' | 'settings-invalid' | 'uncovered' | 'forbidden-key' | 'protected-mounted'
    | 'forbidden-port' | 'scan-incomplete' | 'settings-legacy' | 'worktree-link-unverified';
  path: string;
  detail?: string;
  previous?: string | null;
  /** settings-changed: unified diff against the last approved content */
  diff?: string;
  /** protected-mounted: the mount that overlaps the protected dir, how it is mounted, what the dir is */
  source?: string;
  via?: 'project' | 'library' | 'include';
  rw?: boolean;
  /** protected-mounted: 'rw' = only a read-write mount is forbidden, 'any' = any mount */
  mode?: 'rw' | 'any';
  what?: string;
}

export interface Session {
  quiet?: string;
  runtime?: 'hole' | 'direct';
  review_pending?: boolean;
  review_summary?: ReviewSummary;
  approvals?: string[];
  approval_grants?: { scope: string; description: string; project: string; expires_at: number }[];
  id: string;
  /** 'prodcheck': the morning production check - no project, one report tool */
  kind: 'project' | 'advisor' | 'prodcheck';
  project: string;
  path: string;
  title: string;
  title_source?: string;
  task: string;
  first_prompt?: string;
  last_prompt?: string;
  state: SessionState;
  /** SSH-only observes process lifetime; agent turn/approval state is unknown. */
  process_only?: boolean;
  phase: string;
  phase_since?: number;
  detail: string;
  created: number;
  pid: number | null;
  instance: string | null;
  blocks: Block[];
  attest: { ok: boolean; failures: string[]; checks: number; skipped?: boolean } | null;
  rc: number | null;
  /** 'inbox': started by a GitLab inbox automation rule */
  origin: 'gui' | 'advisor' | 'inbox' | 'handoff' | 'prod' | 'tty';
  /** handoff: id of the project session that proposed this one */
  parent?: string | null;
  claude_session: string | null;
  /** 9.140: continued the work of the other agent's session (a handoff document); absent on an older piperd */
  switched_from?: { session: string; agent: Agent; conversation: string; title?: string; worktree?: string; branch?: string } | null;
  /** 9.54: this start took the pending image rebuild (hole -r); absent on an older piperd */
  rebuild?: boolean;
  /** 9.141: a restart (or ptyhost / piperd) ended it - what it was doing; cleared by a start, Remove or Dismiss */
  interrupted?: Interrupted | null;
  gitlab: string[];
  /** GitLab projects outside `gitlab` the user let the session read (a card each; absent on an older piperd) */
  gitlab_extra?: string[];
  /** W23: GitHub repositories at the start while the github module was on (absent on an older piperd) */
  github?: string[];
  profile: string | null;
  hidden: boolean;
  notice: string;
  /** 'host': the sandbox mounts the host's real ~/.claude (policy claude_dir=host, K2 accepted) */
  claude_mode: 'session' | 'host' | 'none';   // none: a Codex session (own ~/.codex, no ~/.claude)
  task_sent: boolean;
  network: { unrestricted?: boolean; allow?: string[]; gateway?: string[]; denied?: string[] | null; allowed_count?: number };
  /** L5: sensitive paths that appeared after the start and are NOT over-mounted */
  watch: string[];
  /** when the state last changed (epoch s) */
  since: number;
  /** the GitLab issue / MR the session works on (started from the inbox); absent on an older piperd */
  issue?: SessionIssue | null;
  /** the checkout's current git branch (from .git/HEAD); "" without a repository */
  branch?: string;
  /** files the session's agent edited (PostToolUse Edit/Write, project-relative, newest first; '*' = unknown) - Files re-reads them */
  fsedit?: { seq: number; paths: string[]; unknown: number };
  /** 9.12: piperd holds automatic text back - the human has a half-written prompt; "" = none */
  hold?: string;
  /** 9.12: Claude sits at its prompt - piperd may type a follow-up / issue status prompt (older piperd: absent) */
  at_prompt?: boolean;
  /** active native Claude / Codex subagents, keyed by agent ID */
  subagents?: Record<string, string>;
  background_tasks?: Record<string, 'shell' | 'monitor'>;
  /** what the user unlocked for this session at runtime (sessions.UNLOCKS); absent on an older piperd */
  unlock?: SessionUnlock;
  /** hole start options picked in the new-session dialog (sessions.START_OPTS); absent on an older piperd */
  opts?: StartOpts;
  /** attached in a native terminal (`piper attach`), which sizes the PTY; absent on an older piperd */
  tty?: number;
  /** sessions.AGENTS: the CLI hole started; absent on an older piperd (= claude) */
  agent?: Agent;
  /** the sandbox has the real ~/.claude (claude_dir=host - a Codex one too); absent on an older piperd */
  host_claude?: boolean;
  host_codex?: boolean;
}

/** sessions.AGENTS: the agent CLI a new session runs (fixed for its life) */
export type Agent = 'claude' | 'codex';

/** sessions.START_OPTS: they shape the sandbox, so they are fixed for the session's life */
export interface StartOpts {
  /** hole --with-docker: the DinD sidecar (a privileged container) */
  docker?: boolean;
  /** hole -u: no egress filter (the policy unrestricted_network does it for every session) */
  unrestricted?: boolean;
  /** IntelliJ's MCP as `idea` (the policy idea_in_sessions does it for every session) */
  idea?: boolean;
}

/** The explicit choices of a project's next new session, shared by its worktrees. */
export interface ProjectSessionDefaults {
  agent: Agent;
  opts: (keyof StartOpts)[];
}

export type SessionStartResult = Session & {
  project_defaults?: ProjectSessionDefaults;
  defaults_warning?: string;
  sent?: boolean;
};

/** sessions.UNLOCKS: served by piperd over MCP, so they apply without a sandbox restart */
export interface SessionUnlock {
  projects?: boolean;
  /** edit_file / write_file into other checkouts, each confirmed on a card; implies projects */
  projects_write?: boolean;
  /** git_run on the host checkout, each command confirmed on a card */
  git?: boolean;
  /** db_execute / db_script to another project's database connection, each on a card */
  db_write_other?: boolean;
}

/** GET /api/mattermost (piper/mattermost.py Notifier.status; incoming webhook only) */
export interface MattermostStatus {
  configured: boolean;
  mode: 'webhook';
  replies: boolean;
  detail: 'none' | 'command' | 'full';
  quiet: boolean;
  visible: boolean;
  last_error: string;
  last_ok: number;
  webhook_file: string;
  config_file: string;
  note: string;
}

export interface HistoryItem {
  session: string;
  conversation: string;
  project: string;
  agent: Agent;
  title: string;
  activity: number;
  created: number;
  source: 'piper' | 'local';
  cwd: string;
  import_from: { conversation: string; agent: Agent; cwd: string } | null;
  state: string;
  closed: boolean;
  messages: number;
  first: string;
  last: string;
  answer: string;
  branch: string;
  snippet: string;
  available: boolean;
  issue: SessionIssue | null;
  fork_from: { session: string; conversation: string } | null;
}

export interface ConversationHistory {
  items: HistoryItem[];
  total: number;
  projects: { key: string; count: number }[];
  targets: { key: string; path: string }[];
  index: { busy: boolean; done: number; total: number; error: string };
}

export interface HistoryPreview {
  session: string;
  available: boolean;
  note: string;
  first: string;
  last: string;
  answer: string;
  messages: { role: string; text: string; ts: number }[];
}

export interface SessionIssue {
  key: string;
  gl: string;
  type: 'issue' | 'mr';
  iid: number;
  title: string;
  web_url: string;
}

export interface GitFile {
  path: string;
  x: string;
  y: string;
  orig?: string;
  sensitive: boolean;
  /** S2: lines added / removed against HEAD (null = binary, absent = none, e.g. untracked) */
  add?: number | null;
  del?: number | null;
}

export interface GitStatus {
  root: string;
  branch: string;
  upstream: string;
  ahead: number;
  behind: number;
  files: GitFile[];
  log: string[];
  /** what this session changed (its snapshots + live edits); absent = unknown */
  session_files?: string[];
}

export interface Pending {
  similar?: string;
  suggestion?: { text: string; link: string };
  id: string;
  kind: string;
  session: string;
  title: string;
  summary: string;
  created: number;
  /** Executed by the optional notebook provider, independent of server modules. */
  local_provider?: boolean;
  /** label of a third button that keeps the decision ("Allow always"); absent = Allow / Deny only */
  always?: string;
  /** grants this action kind and environment only for this session */
  session_always?: string;
  /** what the card is about (piperd's payload); a db_* write carries `card` (dbview.dbCard) */
  payload?: Record<string, unknown>;
  /** F17: the Settings › Security switch that asks (security.ASK_GROUPS) */
  why?: { group: string; label: string };
}

/** F17: a card that went unanswered (the agent moved on, it timed out) - the queue says so for 10 s */
export interface PendingGone { id: string; kind: string; session: string; title: string; at: number; note?: string }

export interface Project {
  key: string;
  title: string;
  path: string;
  gitlab: string[];
  /** null/absent: trusted origins have not been resolved yet. */
  issue_tracker?: boolean | null;
  exists: boolean;
  source?: 'projects.json' | 'discovered';
}

/** W2: a login shell on the host, no sandbox (hostterm.py); rc while it ran is null */
export interface HostTerm {
  id: string;
  session?: string | null;
  project: string;
  path: string;
  title: string;
  created: number;
  rc: number | null;
  alive: boolean;
  /** D5: when it exited (epoch s) - an ended shell outside the grid closes itself after 10 minutes */
  ended?: number | null;
}

export interface QuietParticipant {
  id: string; title: string; generation: string; status: 'finishing' | 'waiting' | 'unknown' | 'exempt'; meta?: string; detail: string; at: number | null;
  /** operator's exception from this window: allowed = keeps working, returning = removed, must stop and confirm with `rev` */
  exempt?: 'allowed' | 'returning'; rev?: number;
  confirmed_by?: string;
  delivered_at?: number | null; delivery_detail?: string; activity?: string; stale?: boolean;
  resume?: { status: 'pending' | 'sent'; detail?: string };
}
export interface QuietRequest {
  id: string; owner: string; title: string; generation: string; reason: string; minutes?: number | null;
  phase: 'queued' | 'draining' | 'settling' | 'ready' | 'working' | 'recovering' | 'released';
  participants?: QuietParticipant[]; problem?: string; lease_until?: number; work?: boolean;
  ready?: number; selected?: number; now?: number; requested?: number; requested_by?: string; ended?: number; outcome?: string;
  /** a participant has or is returning from an exception: the window is shared, not quiet */
  shared?: boolean;
}
export interface QuietEvent {
  seq: number; at: number; window: string; session: string; generation?: string; title?: string; kind: string; detail?: string; source?: string;
}
export interface QuietSummary {
  events?: QuietEvent[]; event_dropped?: Record<string, number>;
  enabled: boolean; active: QuietRequest | null; queue: QuietRequest[]; last: QuietRequest | null;
  starts?: { id: string; title: string; project: string; phase: string; error?: string }[];
  scope: string; operations: { session: string; kind: string }[];
}

export interface State {
  gui_build?: string;
  quiet?: QuietSummary | null;
  /** Current executable availability on a remote server; null/absent = unknown. */
  agent_availability?: Record<Agent, boolean> | null;
  environment?: { id: string; label: string; host: string; mode: string };
  default_runtime?: 'hole' | 'direct';
  gitlab_host?: string;
  sessions: Session[];
  pending: Pending[];
  /** F17: cards gone unanswered in the last seconds; absent on an older piperd */
  pending_gone?: PendingGone[];
  projects: Project[];
  /** W2 host terminals: sent to the GUI's full-scope token only ({} otherwise); absent on an older piperd */
  host_terms?: { on?: boolean; user?: string; items?: HostTerm[] };
  terminal_app?: string;   // D4: the app Open in terminal uses ("Ghostty"), "" = none
  /** GitLab inbox summary; absent on a piperd without the inbox */
  inbox?: InboxSummary;
  /** morning production check summary; absent on an older piperd */
  prod?: ProdSummary;
  /** Feed; `{on: false}` while the feature is off, absent on an older piperd */
  feed?: FeedSummary;
  /** the Overview queue's open actions (feed.derive_actions), also with the Feed off; absent on an older piperd */
  queue?: { actions: FeedAction[]; dismissed?: number };
  /** Legacy policy fallback before a project saves its first explicit preference. */
  start_opts_default?: (keyof StartOpts)[];
  project_session_defaults?: Record<string, ProjectSessionDefaults>;
  start_opts_enforced?: (keyof StartOpts)[];
  /** 9.54: the pending image rebuild - {} none, {at} armed, + {session} once a start took it */
  image_rebuild?: ImageRebuild;
  /** feature modules; absent on an older piperd = everything on */
  features?: FeaturesSummary;
  /** the one-time Security screen after the Minimal change (security.notice); null once shown, absent on an older piperd */
  security_notice?: SecurityNotice | null;
  /** 9.141: the restore card - sessions a restart interrupted, until POST /api/interrupted {seen}; absent on an older piperd */
  interrupted_notice?: InterruptedNotice | null;
  /** the dev stack provider answers */
  stack_up?: boolean;
  /** opt-in GUI behaviour from policy.json (absent on an older piperd = all off) */
  gui_flags?: { gitlab_notifications?: boolean; open_links_on_host?: boolean; clipboard_arm_enter?: boolean; restore_view?: boolean; term_click_opens_files?: boolean; nav_history?: boolean; commit_requires_review?: boolean; theme?: string; git_hole?: boolean; unlocks_required?: boolean; attestation?: boolean; l5_watch?: boolean };
  /** Codex plan limits; null when Codex or Usage is off, absent on an older piperd. */
  codex_limits?: LimitsSummary | null;
  /** Claude plan limits (limits.py): the block under the sessions; absent on an older piperd */
  limits?: LimitsSummary | null;   // null: the usage module is off (D16)
}

/** One provider quota window: percent used, reset (epoch s), and API severity. */
export interface LimitWindow {
  percent: number | null;
  resets_at: number | null;
  severity: string;
  window_seconds?: number | null;
}

/** /api/state `limits` / `codex_limits`: the last quota response, never credentials or account IDs. */
export interface LimitsSummary {
  source?: 'host' | 'template';
  v: number;
  /** no-login is hidden; expired/error keep last data; unavailable includes API-key auth. */
  status: 'ok' | 'no-login' | 'expired' | 'error' | 'unavailable';
  /** epoch s of the last good answer (0 none) */
  fetched_at: number;
  error?: string;
  /** subscriptionType of the login (team, max, pro…) */
  plan?: string;
  /** the 5-hour window */
  session?: LimitWindow | null;
  /** the 7-day window, all models */
  week?: LimitWindow | null;
  /** model-scoped weekly limits (label = the model's display name) */
  scoped?: (LimitWindow & { label: string })[];
}

export type Module = 'quiet' | 'workflow' | 'gitlab' | 'agent' | 'grid' | 'http' | 'db' | 'diag' | 'stack' | 'prod' | 'timesheet' | 'usage' | 'mattermost' | 'codex' | 'github' | 'host' | 'composer';

/** 9.141 a session's `interrupted`: reboot = the machine restarted, ptyhost = Piper's terminal host did, piperd = Piper
 *  restarted during its start; was = the state it had; resumes = Continue resumes its conversation (else it starts again) */
export interface Interrupted {
  at: number;
  was: string;
  cause: 'reboot' | 'ptyhost' | 'piperd';
  resumes: boolean;
}

/** /api/state `interrupted_notice` (sessions.interrupted_notice): the card shown once per id */
export interface InterruptedNotice {
  id: number;
  cause: Interrupted['cause'];
  at: number;
  /** the concurrent-session limit (Settings › Sessions): the card offers only the free slots */
  limit: number;
  /** the card opened already (in any tab): only the section's Restore… shows it now */
  seen: boolean;
  sessions: { id: string; title: string; project: string; issue: SessionIssue | null; was: string; at: number; since: number; resumes: boolean }[];
}

/** /api/state `security_notice`: profile = a saved / default profile preselected, custom = an own mix, only informed */
export interface SecurityNotice {
  kind: 'profile' | 'custom';
  /** hole (Minimal) | recommended | guarded (Strict) | custom */
  profile: string;
  on: number;
  total: number;
}

/** /api/state `features` (features.summary) */
export interface FeaturesSummary {
  modules: Partial<Record<Module, boolean>>;
  onboarded: boolean;
  /** basic | dev | all | custom */
  preset: string;
  /** [id, label, what it hides] in display order */
  list: [Module, string, string][];
  /** [id, label, text] */
  presets: [string, string, string][];
  /** preset id → the modules it turns on (the chips on a preset card); absent on an older piperd */
  preset_modules?: Record<string, Module[]>;
}

/** GET /api/doctor (doctor.py): one row per check */
export interface DoctorRow {
  key: string;
  level: 'fail' | 'warn' | 'info';
  ok: boolean;
  title: string;
  text: string;
  fix: string;
  settings: string;
  action: '' | 'http-install' | 'db-install' | 'deep';
}

/** 9.54 /api/state image_rebuild */
export interface ImageRebuild {
  at?: number;
  session?: string;
}

/** 9.54 GET /api/image: hole's agent images, newest first */
export interface ImageInfo {
  ok: boolean;
  error?: string;
  images?: { ref: string; size: string; age: string; created: string; in_use: number }[];
  /** containers running any agent image */
  in_use?: number;
}

export interface DoctorReport {
  rows: DoctorRow[];
  at: number;
  deep: boolean;
  fail: number;
  warn: number;
}

/** /api/state `feed`: `v` changes whenever actions, events, seen or the chat queue change */
export interface FeedSummary {
  on: boolean;
  v?: number;
  open?: number;
  unread?: number;
  chat_queued?: number;
}

/** /api/state `prod`: `v` changes whenever the report, mutes or mapping change */
export interface ProdSummary {
  v: number;
  status: 'idle' | 'waiting' | 'running' | 'ok' | 'error';
  /** new, unmuted items in today's report */
  new: number;
  /** when the latest report was saved: a notification only for a new report, not for an unmute */
  report_at?: number | null;
  /** some project is mapped to an observability project */
  on: boolean;
}

// ---- GitLab inbox

/** 'off' = the GitLab module is off (W23: only GitHub's half of the inbox runs) */
export type InboxStatus = 'starting' | 'ok' | 'no-token' | 'error' | 'off';

/** /api/state `inbox`: `v` changes whenever items / rules / seen change */
export interface InboxSummary {
  v: number;
  status: InboxStatus;
  /** items with `new` or `new_notes > 0` */
  new: number;
  total: number;
  /** items with a pending todo or new notes */
  needs: number;
  /** W23: GitHub's poll status, null = the GitHub module is off (older piperd: absent) */
  github?: InboxStatus | null;
}

/** assigned | mentioned | directly_addressed | review_requested | build_failed | unmergeable |
 *  approval_required | authored | marked | ... (unknown ones are shown as they come) */
export type InboxReason = string;

export interface InboxTodo {
  id: number;
  action: string;
  author: string;
  /** ISO 8601 */
  created_at: string;
  note_id: number | null;
  body: string;
}

export interface InboxAuto {
  event: string;
  session: string;
  /** epoch s */
  at: number;
}

export interface InboxItem {
  /** "<gl path>#<issue|mr>#<iid>" */
  key: string;
  /** W23: "github" = gl is "gh:owner/repo" (a PR is type "mr"); absent = GitLab */
  forge?: 'gitlab' | 'github';
  gl: string;
  type: 'issue' | 'mr';
  iid: number;
  title: string;
  state: string;
  web_url: string;
  labels: string[];
  author: string;
  /** ISO 8601 */
  updated_at: string;
  due_date: string | null;
  milestone: string | null;
  reasons: InboxReason[];
  /** the GitLab lists that returned it now: "assigned" (open, assigned to you) / "authored" (own open MR);
   *  a to-do alone may be stale (closed or reassigned since) */
  listed?: ('assigned' | 'authored')[];
  todos: InboxTodo[];
  notes_count: number;
  new_notes: number;
  first_new_note: number | null;
  /** W9: the latest comment by someone else (older piperd: absent) */
  last_note?: number | null;
  /** W9: the comment a task from the inbox answers - addressed to me, else the first new, else the latest */
  target_note?: number | null;
  /** the key was never seen before */
  new: boolean;
  seen_at: number | null;
  /** the local project key, null = not mapped */
  project: string | null;
  /** discovered checkouts whose origin is `gl` (when project is null) */
  candidates: { path: string; key: string }[];
  /** non-hidden linked sessions, newest first */
  sessions: string[];
  auto: InboxAuto[];
}

export type RuleEvent = 'assigned' | 'mentioned' | 'directly_addressed' | 'new_note';

export interface InboxRule {
  id: string;
  /** "*" = every mapped project */
  gl: string;
  on: RuleEvent[];
  action: 'analyze';
  enabled: boolean;
  /** epoch s: only events after this fire */
  since: number;
}

export interface Inbox {
  status: InboxStatus;
  /** epoch s of the last successful poll */
  at: number | null;
  error: string | null;
  me: string | null;
  interval: number;
  items: InboxItem[];
  rules: InboxRule[];
  limits: { per_hour: number; used_hour: number };
  /** W23: GitHub's half, null = the GitHub module is off (older piperd: absent) */
  github?: { status: InboxStatus; at: number | null; error: string | null; me: string | null } | null;
  /** what an issue's files in development/ are called (absent on an older piperd) */
  issue_files?: IssueFiles;
}

/** <stem>-<name>.md of an issue's status / analysis / comment file, and how the comment draft reads */
export interface IssueFiles {
  status: string;
  analysis: string;
  comment: string;
  comment_style: string;
}

export interface InboxNote {
  id: number;
  author: string;
  body: string;
  /** ISO 8601 */
  created_at: string;
  new: boolean;
  mine: boolean;
}

/** GET /api/inbox/item */
export interface InboxPreview {
  key: string;
  type: 'issue' | 'mr';
  iid: number;
  title: string;
  state: string;
  web_url: string;
  author: string;
  created_at: string;
  assignees: string[];
  labels: string[];
  description: string | null;
  notes: InboxNote[];
  mr?: { source_branch: string; target_branch: string; merge_status: string; pipeline: string | null; draft: boolean };
  /** 9.12 D: absent on an older piperd */
  state_file?: IssueStateSummary | null;
}

/** GET /api/sessions/<id>/draft */
export interface CommentDraft {
  path: string | null;
  text?: string;
  /** epoch s */
  mtime?: number;
  key?: string;
  sent?: boolean;
}

export interface InboxStartBody {
  runtime?: 'hole' | 'direct';
  key: string;
  mode?: 'work' | 'analyze';
  project?: string;
  note_id?: number;
  task?: string;
  session?: string;
  /** a new session only (with `session` piperd refuses them) */
  opts?: StartOpts;
  /** a new session only; absent = claude */
  agent?: Agent;
  remember_for_project?: boolean;
}

/** board card: where the session is in the issue workflow (transcript + draft + git) */
export interface BoardFlow {
  analysis: boolean;
  impl: boolean;
  verify: number;
  draft: { path: string; mtime: number; sent: boolean } | null;
  commits: number;
  ahead: number | null;
  pushed: boolean;
}

// ---- workbench (piper/sessions.py Manager.fs / issues / conversation / board / audit_log)
export interface DirEntry {
  name: string;
  type: 'dir' | 'file';
  size?: number | null;
  link?: boolean;
  /** hidden from Claude by the isolation policy / excludes (the GUI shows it like any other, marked) */
  hidden?: boolean;
  /** compact packages: the single-subdirectory chain below this directory (`com/acme/shop`) */
  chain?: string;
}

export interface DirListing {
  path: string;
  entries: DirEntry[];
  truncated: boolean;
  /** S2: the isolation policy cannot be read - no entry is marked hidden, the GUI says so once */
  policy_unreadable?: boolean;
  policy_error?: string;
}

export interface FileText {
  path: string;
  total_lines: number;
  offset: number;
  /** "<n>\t<text>" */
  lines: string[];
  /** sha256 (hex) of the whole file as read - the optimistic lock of POST /api/fs/write (9.7 #6) */
  sha?: string;
  /** dominant line ending; the write puts it back ("\n" is sent) */
  eol?: '\n' | '\r\n';
  final_newline?: boolean;
  /** the file is hidden from Claude (isolation policy / excludes) */
  hidden?: boolean;
}

export interface FileBlob {
  path: string;
  tree: string;
  state: 'text' | 'missing' | 'binary' | 'too_large' | 'unsupported';
  text?: string;
  mode?: string;
  oid?: string;
  size?: number;
  final_newline?: boolean;
}

export interface GrepResult {
  matches: { path: string; line: number; text: string }[];
  truncated: boolean;
  /** git grep or the Python walker; ere = this git has no PCRE (`-E` instead of `-P`) */
  engine?: 'git' | 'python';
  regex?: 'pcre' | 'ere';
}

export interface IssueRow {
  iid: number;
  title: string;
  state: string;
  labels?: string[];
  updated_at?: string;
  web_url?: string;
  /** the token owner's relation to the issue (piper/gitlab.py _mine_first); these come first */
  mine?: ('assigned' | 'mentioned')[];
}

export interface IssueList {
  issues: IssueRow[];
  /** GitLab paths, or one `gh:owner/repo` when the project's issues come from GitHub (W23) */
  projects: string[];
  forge?: 'github';
}

export interface Issue extends IssueRow {
  description?: string | null;
  author?: string;
  assignees?: string[];
  created_at?: string;
  notes?: { id: number; body: string; author?: string; created_at?: string }[];
  forge?: 'github';
}

export interface Turn {
  role: 'user' | 'assistant';
  text: string;
  ts?: string | null;
  /** F05: the details of the text's `[tool X]` markers, in their order (Claude transcripts) */
  tools?: ToolCall[];
}

/** F05: one tool call of a turn - never its result, only how long it ran and how many lines it gave */
export interface ToolCall {
  name: string;
  arg: string;
  ms?: number;
  lines?: number;
  error?: boolean;
}

export interface Conversation {
  agent?: 'claude' | 'codex';
  turns: Turn[];
  usage: { input: number; output: number; cache_read: number; cache_write: number; messages: number } | null;
  note?: string;
}

export interface ConvHit {
  session: string;
  title: string;
  turn: number;
  role: string;
  ts?: string | null;
  text: string;
}

export interface BoardCard {
  id: string;
  /** Claude's last answer that is more than a tool call */
  last: string;
  /** the last prompt the human typed */
  asked: string;
  changed: number | null;
  tokens: number | null;
  /** absent on an older piperd */
  issue?: SessionIssue | null;
  flow?: BoardFlow | null;
  /** 9.12 D: the issue's status file in development/ (absent on an older piperd) */
  istate?: IssueStateCard | null;
}

/** 9.12 D: board card - the state file of the session's issue */
export interface IssueStateCard {
  path: string;
  exists: boolean;
  /** epoch s, null = no file yet */
  mtime: number | null;
  /** the last finished turn did not touch the prose (piperd's facts block does not count) and the session does not work */
  stale: boolean;
  live: boolean;
  /** Claude sits at its prompt: the button types one prompt (a live session that does not gets no button) */
  prompt: boolean;
}

/** 9.12 D: POST /api/sessions/<id>/issue-state */
export interface IssueStateUpdate {
  path: string;
  sent?: boolean;
  written?: string;
}

/** 9.12 D: inbox preview - Status and Waiting on of the state file */
export interface IssueStateSummary {
  path: string;
  mtime: number;
  status: string;
  waiting: string;
}

export interface AuditEvent {
  event: string;
  ts: string;
  session?: string;
  [k: string]: unknown;
}

// ---- 9.7: IDEA-replacement workbench (piper/sessions.py; routes in piperd.py)

/** changed lines of a working-tree file against HEAD (GET /api/fs/marks); new-file line numbers */
export interface LineMarks {
  path: string;
  /** [first, last] ranges of added lines */
  added: [number, number][];
  /** [first, last] ranges of modified lines */
  changed: [number, number][];
  /** lines removed just AFTER these line numbers (0 = at the top) */
  deleted: number[];
  /** untracked file: everything is new */
  untracked: boolean;
}

export interface SymbolHit {
  name: string;
  /** class | interface | enum | record | method | function | const | bean | property | key | ... */
  kind: string;
  path: string;
  line: number;
  /** the source line (trimmed, <= 200 chars) */
  text: string;
}

export interface SymbolResult {
  symbols: SymbolHit[];
  truncated: boolean;
  /** files in the index */
  indexed: number;
}

export interface WriteResult {
  sha: string;
  total_lines: number;
}

/** one turn of a session: snapshot trees when the prompt was submitted / when Claude stopped */
export interface TurnSnap {
  n: number;
  prompt: string;
  started: number;
  ended: number | null;
  /** git tree id of the working tree at UserPromptSubmit */
  from: string;
  /** tree id at Stop; null while the turn runs (then "now" is diffed) */
  to: string | null;
  summary?: ReviewSummary;
  reverted?: boolean;
  unavailable?: string;
}

export interface ReviewSummary {
  files: number;
  add: number;
  del: number;
  turns: number;
  tests?: { status: 'passed' | 'failed'; turn: number };
}

export interface TurnRevert {
  id: string;
  n: number;
  at: number;
  paths: string[];
  undone: boolean;
  status: string;
}

export interface RevertPreview {
  n: number;
  undo: string;
  current: string;
  files: { path: string; removed: boolean; added_in?: number }[];
}

export interface TurnList {
  turns: TurnSnap[];
  note?: string;
  reverts?: TurnRevert[];
}

export interface TurnFile {
  path: string;
  /** A | M | D | R */
  status: string;
  orig?: string;
  /** matches the isolation policy (like GitFile.sensitive) */
  sensitive: boolean;
}

/** GET /api/sessions/<id>/turns/file: the turns that changed one file */
export interface FileTurns {
  /** newest first; `to` null = the open turn (diffed against now); add/del null for a binary file */
  turns: { index: number; n: number; prompt: string; from: string; to: string | null; at: number; add: number | null; del: number | null }[];
  /** the path relative to the repository: `diff=` of /api/sessions/<id>/turn */
  diff_path: string;
}

export interface TurnFiles {
  from: string;
  to: string;
  files: TurnFile[];
}

export interface BlobVersion { oid: string | null; mode: string | null }
export interface ReviewFile extends TurnFile {
  live_status?: string;
  before?: BlobVersion;
  after?: BlobVersion;
  orig_after?: BlobVersion;
  add?: number | null;
  del?: number | null;
  work_sha?: string | null;
  work_mode?: string | null;
  committable?: boolean;
  /** no content in the review (outside the session's project or repository, unreadable); `sensitive` is
   * only the "hidden from Claude" mark - the human reviews those files, they never go to a session */
  withheld?: boolean;
}
export interface ReviewFiles extends TurnFiles {
  root?: string;
  id: string;
  scope: 'session' | 'head' | 'turn' | 'branch' | 'index' | 'unstaged';
  head: string;
  project: string;
  files: ReviewFile[];
  turns: TurnSnap[];
  note: string;
}
export interface ReviewedCommit {
  review: string;
  head: string;
  versions: Record<string, BlobVersion>;
}
export interface ReviewCheck { head_changed: boolean; changed: string[] }
export interface ReviewAction { id: string; key: number; label: string; kind: 'read' | 'edit'; prompt: string }
export interface ReviewTarget {
  path: string; from: number; to: number; old: boolean; code: string; sha: string | null;
}
export interface ReviewItem extends ReviewTarget {
  id: string; actions: ReviewAction[]; text: string; state: 'open' | 'sent' | 'resolved';
  needsReview?: boolean;
}

export interface Commit {
  hash: string;
  short: string;
  author: string;
  /** ISO 8601 */
  date: string;
  subject: string;
  refs?: string;
}

/** W4: GET /api/git/branches - local + remote branches and the worktrees of the project's repository */
export interface GitBranch {
  name: string;
  upstream?: string;
  ahead?: number;
  behind?: number;
  /** the upstream was deleted on the remote */
  gone?: boolean;
  head: string;
  subject: string;
  /** ISO 8601 */
  date: string;
  /** the path of a worktree that has it checked out ('' = none) */
  worktree?: string;
}

export interface GitWorktree {
  path: string;
  head: string;
  branch: string;
  main: boolean;
  locked: boolean;
  prunable: boolean;
  /** the Piper project key (`<base>@<name>`), '' when Piper cannot open it */
  key: string;
  /** live sessions in it */
  sessions: string[];
}

export interface GitBranches {
  current: string;
  detached: boolean;
  local: GitBranch[];
  remote: GitBranch[];
  worktrees: GitWorktree[];
  project: string;
  /** the main checkout's project key */
  base: string;
  root: string;
}

/** W5: one row of `git log --graph`; a row without hash is a lane-only line */
export interface GitRow {
  graph: string;
  hash?: string;
  short?: string;
  author?: string;
  date?: string;
  subject?: string;
  refs?: string;
  parents?: string[];
}

export interface GitCommitFile {
  path: string;
  old_path?: string;
  /** M A D R C T */
  status: string;
  add: number;
  del: number;
  binary: boolean;
  /** hidden from Claude: shown to the human, marked */
  withheld: boolean;
}

export interface GitCommitDetail {
  hash: string;
  short: string;
  parents: string[];
  author: string;
  email: string;
  date: string;
  committer: string;
  committed: string;
  refs: string;
  message: string;
  files: GitCommitFile[];
  truncated: boolean;
}

/** GET /api/sessions/<id>/git?push: what a push would send (the Push dialog) */
export interface GitPushCommit {
  hash: string;
  short: string;
  author: string;
  date: string;
  subject: string;
  merge: boolean;
}

export interface GitPushCheck {
  /** block = Push disabled */
  level: 'block' | 'warn' | 'info';
  text: string;
}

export interface GitPushPreview {
  branch: string;
  remote: string;
  /** the remote branch the push goes to */
  target: string;
  upstream: string;
  /** the remote branch does not exist yet */
  new: boolean;
  command: string;
  /** the commit the files are compared with ("" = the push starts at a root commit) */
  base: string;
  behind: number;
  commits: GitPushCommit[];
  more: boolean;
  files: GitCommitFile[];
  truncated: boolean;
  checks: GitPushCheck[];
}

export interface BlameLine {
  line: number;
  /** short hash; "" for not committed yet */
  commit: string;
  author: string;
  /** epoch s */
  time: number;
  summary: string;
  text: string;
}

export interface Pipeline {
  id: number;
  status: string;
  ref: string;
  sha: string;
  web_url: string;
  updated_at: string;
}

export interface MergeRequest {
  iid: number;
  title: string;
  state: string;
  web_url: string;
  source_branch: string;
  target_branch: string;
}

export interface CiState {
  project: string;
  branch: string;
  pipeline: Pipeline | null;
  mr: MergeRequest | null;
  /** W23: the project's GitHub repo ("owner/repo") - pipeline = Actions run, mr = the open PR */
  github?: string;
  note?: string;
}

export interface MrDraft {
  project: string;
  source: string;
  target: string;
  title: string;
  description: string;
}

/** the verbs the panel may post (stackview.py VERBS); anything else stays in the provider's own dashboard */
export type StackVerb = 'start' | 'stop' | 'restart' | 'down' | 'up' | 'build' | 'rebuild';

export interface StackService {
  name: string;
  state: string;
  health?: string;
  /** e.g. "3 min" (plain status) or "4h" (the provider's card) */
  age?: string;
  ports?: string[];
  /** the provider's card only: what it offers for this service (none for a DinD one) */
  verbs?: StackVerb[];
  links?: string[];
  /** builder, minio-seed, *-restore: not counted, its exit is no failure */
  oneShot?: boolean;
  /** "sandbox" etc. when the service runs in a sandbox's DinD, not on the host daemon */
  location?: string;
  /** an exited container's non-zero exit code (137 = killed): it ended, nobody may have stopped it */
  exitCode?: number;
}

/** a job of the stack (stackview.py job_view) */
export interface StackJob {
  id: string;
  verb: string;
  /** running | done | failed | canceled */
  state: string;
  stage: string;
  rc: number | null;
  error: string;
  /** epoch seconds */
  started: number;
  /** the services the job acts on (empty: the whole stack) */
  services?: string[];
}

export interface StackStatus {
  /** the stack's name, null when the project has no dev stack */
  stack: string | null;
  services: StackService[];
  dashboard?: string;
  error?: string;
  /** the provider's card (absent when it has none): long-running services up / expected */
  running?: number;
  total?: number;
  primary?: { verb: StackVerb; label: string } | null;
  /** the whole-stack verbs besides the primary one */
  verbs?: StackVerb[];
  domainBase?: string;
  clock?: { kind: string; value: string };
  sandbox?: boolean;
  /** the stack's running jobs and those that ended in the last two minutes, running first */
  jobs?: StackJob[];
  /** a linked worktree: whose stack this is - the main checkout's (default) or its own;
   * "own" with stack null = not set up yet ("Set up and start"; its setup job in `jobs`) */
  worktree?: { mode: 'main' | 'own'; main: string };
}

export interface StackJobLog {
  job: StackJob;
  offset: number;
  chunk: string;
  state: string;
}

export interface Diagnostic {
  /** mvn | javac | tsc | eslint | gradle | python | junit | ... */
  tool: string;
  severity: 'error' | 'warning';
  /** project-relative when inside the project, else as printed */
  path: string;
  line: number;
  col: number | null;
  message: string;
}

export interface Diagnostics {
  /** timestamp of the tool result the items come from */
  at: string | null;
  /** the command that produced them (first line) */
  source: string;
  items: Diagnostic[];
  note?: string;
}

export interface IdeaProblem {
  line: number;
  col: number | null;
  severity: string;
  message: string;
}

export interface IdeaProblems {
  available: boolean;
  problems: IdeaProblem[];
  note?: string;
}

export interface UsageRow {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

export interface UsageReport {
  days: (UsageRow & { day: string })[];
  projects: (UsageRow & { project: string; sessions: number })[];
  total: UsageRow;
}

export interface ThemeSet {
  auto: { light: string; dark: string };
  themes: Record<string, { scheme: 'light' | 'dark'; tokens: Record<string, string> }>;
}

declare global {
  interface Window {
    __PIPER__?: { origins?: string[]; version: number; guiBuild?: string; port: number; themes: ThemeSet; theme?: string; dashboardUrl: string };
  }
}

// ---- Files: noise, hidden marks, policy state - merged into the interfaces above
export interface DirEntry {
  /** tooling noise (projectfs.NOISE, the one list): folded in the tree, left out of ⌘P / search unless all=1 */
  noise?: boolean;
}

export interface DirListing {
  /** S2: the isolation policy could not be read - every entry reads as hidden (fail closed); the GUI shows one banner */
  policy_unreadable?: boolean;
  /** why it cannot be read */
  policy_error?: string;
}

/** /api/fs/files: paths; `hidden` = the ones hidden from Claude (K10); all=1 includes the noise folders (K1) */
export interface FileIndex {
  files: string[];
  truncated: boolean;
  hidden?: string[];
  policy_unreadable?: boolean;
}

/** a search hit of /api/fs/grep with its hidden flag (K10) */
export interface GrepHit { path: string; line: number; text: string; hidden?: boolean }

/** GET /api/git/status?project= (K2): a project's git state without a session; `root` = the repository
 *  root when the project is a subdirectory (paths are repository-relative then) */
export interface ProjectGitStatus { root?: string; branch: string; files: GitFile[] }
