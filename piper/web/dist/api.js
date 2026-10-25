// HTTP client for piperd's /api. The token arrives once in the URL fragment
// (`task piper:open` builds it), moves to localStorage and the address bar is
// scrubbed.
const KEY = 'piper.token';
/** a hung piperd (not a refused connection) must still show the "not responding" banner */
const STATE_TIMEOUT_MS = 8000;
export class ApiError extends Error {
    status;
    token;
    json;
    /** json: the error body piperd sent (e.g. 409 hidden_paths of an import), null when there was none */
    constructor(status, message, token = '', json = null) {
        super(message);
        this.status = status;
        this.token = token;
        this.json = json;
    }
}
function storage() {
    try {
        return window.localStorage;
    }
    catch {
        return null;
    }
}
// A token from this tab's URL is only a candidate until piperd accepts it: a stale or mistyped
// link in one tab must not overwrite (or, on its 401, wipe) the token every other tab uses.
let candidate = '';
/** takes `#token=` from the address bar (boot and every hashchange); true when there was one */
export function absorbFragmentToken() {
    const m = /(?:^|&)token=([^&]+)/.exec(location.hash.slice(1));
    if (!m?.[1])
        return false;
    let raw = m[1];
    try {
        raw = decodeURIComponent(raw);
    }
    catch { /* a cut %-escape: the rest as it is */ }
    candidate = cleanToken(raw);
    history.replaceState(null, '', location.pathname + location.search);
    return true;
}
export function token() {
    return candidate || (storage()?.getItem(KEY) ?? '');
}
export function setToken(t) {
    candidate = '';
    storage()?.setItem(KEY, cleanToken(t));
}
/** N8: piperd's tokens are URL-safe base64 - a zero-width / bidi / NBSP character copied with a link (or a paste)
 *  made every fetch throw (a header must be ISO-8859-1), which read as "piperd is not responding" */
function cleanToken(t) {
    return t.replace(/[^\x21-\x7e]/g, '');
}
/** drops the token piperd refused; true when another (the stored one) is left to try */
export function forgetToken(failed) {
    if (candidate && candidate === failed) {
        candidate = '';
        return !!token();
    }
    const st = storage();
    if (st?.getItem(KEY) === failed)
        st.removeItem(KEY);
    return !!token() && token() !== failed;
}
export async function call(method, path, body, etag, timeoutMs = 0) {
    const sent = token();
    const headers = { Authorization: 'Bearer ' + sent };
    if (body !== undefined)
        headers['Content-Type'] = 'application/json';
    if (etag)
        headers['If-None-Match'] = etag;
    const init = { method, headers, cache: 'no-store' };
    if (body !== undefined)
        init.body = JSON.stringify(body);
    if (timeoutMs)
        init.signal = AbortSignal.timeout(timeoutMs);
    let r;
    try {
        r = await fetch(path, init);
    }
    catch (e) {
        if (e instanceof DOMException && e.name === 'TimeoutError')
            throw new Error(`no answer in ${Math.round(timeoutMs / 1000)} s`);
        throw e;
    }
    if (candidate && sent === candidate && r.ok)
        setToken(candidate);
    if (r.status === 304)
        return { data: null, etag: etag ?? '' };
    const text = await r.text();
    let json = null;
    try {
        json = text ? JSON.parse(text) : null;
    }
    catch {
        json = null;
    }
    if (!r.ok) {
        const msg = json && typeof json === 'object' && 'error' in json ? String(json.error) : r.statusText;
        throw new ApiError(r.status, msg, sent, json);
    }
    return { data: json, etag: r.headers.get('ETag') ?? '' };
}
export const api = {
    state: (etag) => call('GET', '/api/state', undefined, etag, STATE_TIMEOUT_MS),
    ping: () => call('GET', '/api/ping'),
    create: (project, task, title, opts, agent = 'claude', remember = false, imageRebuild = false) => call('POST', '/api/sessions', { project, task, title, ...(opts && Object.keys(opts).length ? { opts } : {}), ...(agent !== 'claude' ? { agent } : {}), ...(remember ? { remember_for_project: true } : {}), ...(imageRebuild ? { image_rebuild: true } : {}) }),
    act: (id, action) => call('POST', `/api/sessions/${encodeURIComponent(id)}/${action}`, {}),
    rename: (id, title) => call('POST', `/api/sessions/${encodeURIComponent(id)}/title`, { title }),
    decide: (id, approve, always = false, sessionAlways = false, similarHour = false) => call('POST', `/api/pending/${encodeURIComponent(id)}`, { approve, ...(always ? { always } : {}), ...(sessionAlways ? { session_always: true } : {}), ...(similarHour ? { similar_hour: true } : {}) }),
    revokeApproval: (id, scope) => call('POST', `/api/sessions/${encodeURIComponent(id)}/revoke-approval`, { scope }),
    importProjects: () => call('POST', '/api/projects/import', {}),
    git: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/git`),
    /** amend (W6): replaces HEAD; piperd refuses it once HEAD is on a remote branch */
    commit: (id, message, files, reviewed, amend = false) => call('POST', `/api/sessions/${encodeURIComponent(id)}/git/commit`, { message, files, ...(reviewed ? { reviewed } : {}), ...(amend ? { amend } : {}) }),
    /** W6: a commit message drafted by `claude -p` on the host from the checked files' diff (hidden files never sent) */
    gitMessage: (id, files) => call('POST', `/api/sessions/${encodeURIComponent(id)}/git/message`, { files }),
    /** commit A: an MR title and description drafted the same way from the branch's commits and diff against `target` */
    mrMessage: (id, target) => call('POST', `/api/sessions/${encodeURIComponent(id)}/git/mr-message`, { target }),
    /** `piper attach <id>` in a new Ghostty window; without Ghostty ok=false and the command to copy */
    openTerminal: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/open-terminal`, {}),
    fixExcludes: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/fix-excludes`, {}),
    /** what a push would send: the commits, their files, checks (the Push dialog) */
    pushPreview: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/git?push=1`),
    /** accept the uncovered files in their mount root (policy library_allow) and retry the start */
    allowUncovered: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/allow-uncovered`, {}),
    push: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/git/push`, {}),
    // workbench; a project key contains '/', so it travels in the query
    /** every file but .git - a file hidden from Claude too, flagged `hidden` (the GUI's full token) */
    list: (project, path) => call('GET', `/api/fs/list?${q({ project, path })}`),
    read: (project, path, offset = 0) => call('GET', `/api/fs/read?${q({ project, path, offset: String(offset), limit: '5000' })}`),
    files: (project) => call('GET', `/api/fs/files?${q({ project })}`),
    grep: (project, text, glob, opts = {}) => call('GET', `/api/fs/grep?${q({ project, q: text, glob, cs: opts.cs === false ? '0' : '', limit: opts.limit ? String(opts.limit) : '' })}`),
    /** file operations of the tree: the same guards as write (not while the session works, hidden never) */
    fsCreate: (project, path, kind, text = '') => call('POST', '/api/fs/create', kind === 'file' ? { project, path, kind, text } : { project, path, kind }),
    fsRename: (project, from, to) => call('POST', '/api/fs/rename', { project, from, to }),
    fsDelete: (project, path) => call('POST', '/api/fs/delete', { project, path }),
    fsRestore: (project, trash, replace = false) => call('POST', '/api/fs/restore', replace ? { project, trash, replace } : { project, trash }),
    /** the turns of the session that changed one file, newest first */
    fileTurns: (id, path) => call('GET', `/api/sessions/${encodeURIComponent(id)}/turns/file?${q({ path })}`),
    issues: (project, search) => call('GET', `/api/gitlab/issues?${q({ project, search })}`),
    issue: (project, iid) => call('GET', `/api/gitlab/issue?${q({ project, iid: String(iid) })}`),
    conversation: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/conversation`),
    searchConversations: (text) => call('GET', `/api/conversations/search?${q({ q: text })}`),
    conversationHistory: (text = '', project = '', agent = '', period = '', offset = 0, external = false) => call('GET', `/api/conversations?${q({ q: text, project, agent, period, offset: String(offset), external: external ? '1' : '0' })}`),
    importConversation: (id, project, confirmCheckout = false, confirmHidden = false) => call('POST', `/api/conversations/${encodeURIComponent(id)}/import`, { project, confirm_checkout: confirmCheckout, ...(confirmHidden ? { confirm_hidden: true } : {}) }),
    conversationPreview: (id) => call('GET', `/api/conversations/${encodeURIComponent(id)}/preview`),
    resumeConversation: (id, fork = false) => call('POST', `/api/conversations/${encodeURIComponent(id)}/${fork ? 'fork' : 'resume'}`, {}),
    /** W8: an ended session goes on with a new task - resumed, compacted first, then the task typed */
    continueSession: (id, task) => call('POST', `/api/conversations/${encodeURIComponent(id)}/continue`, { task }),
    /** 9.140: the other agent continues this project session's work (stops it, hands over a document) */
    switchAgent: (id, confirmHidden = false) => call('POST', `/api/sessions/${encodeURIComponent(id)}/switch-agent`, confirmHidden ? { confirm_hidden: true } : {}),
    board: () => call('GET', '/api/board'),
    audit: (session, limit = 300) => call('GET', `/api/audit?${q({ session, limit: String(limit) })}`),
    send: (id, text) => call('POST', `/api/sessions/${encodeURIComponent(id)}/send`, { text }),
    unlock: (id, what, on) => call('POST', `/api/sessions/${encodeURIComponent(id)}/unlock`, { what, on }),
    /** 9.54: arm / cancel `hole start -r` for the next start of any session */
    imageRebuild: (on) => call('POST', '/api/image/rebuild', { on }),
    startOpt: (id, what, on) => call('POST', `/api/sessions/${encodeURIComponent(id)}/start-opt`, { what, on }),
    allow: (id, domain) => call('POST', `/api/sessions/${encodeURIComponent(id)}/allow`, { domain }),
    // 9.7: editor, navigation, git history, CI, stack, diagnostics, IntelliJ, usage
    marks: (project, path) => call('GET', `/api/fs/marks?${q({ project, path })}`),
    /** name = exact symbol (go to definition); query = prefix/fuzzy (symbol search) */
    symbols: (project, name, query = '') => call('GET', `/api/fs/symbols?${q({ project, name, q: query })}`),
    /** save from the GUI: refused (409) when the file changed since `sha` or a session of the project is working */
    write: (project, path, text, sha) => call('POST', '/api/fs/write', { project, path, text, sha }),
    reviewFiles: (id, scope, n = 0) => call('GET', `/api/sessions/${encodeURIComponent(id)}/review?${q({ scope, n: String(n) })}`),
    reviewBlob: (id, review, path, side) => call('GET', `/api/sessions/${encodeURIComponent(id)}/review/blob?${q({ review, path, side })}`),
    reviewCheck: (id, review, path = '') => call('GET', `/api/sessions/${encodeURIComponent(id)}/review/check?${q({ review, ...(path ? { path } : {}) })}`),
    reviewActions: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/review/config`),
    reviewStage: (id, review, path) => call('POST', `/api/sessions/${encodeURIComponent(id)}/review/stage`, { review, path }),
    reviewWrite: (id, review, path, text, sha) => call('POST', `/api/sessions/${encodeURIComponent(id)}/review/write`, { review, path, text, sha }),
    reviewDispatch: (id, text, target, items) => call('POST', `/api/sessions/${encodeURIComponent(id)}/review/dispatch`, { text, target, items: items.map(({ path, sha }) => ({ path, sha })), edit: items.some(x => x.actions.some(a => a.kind === 'edit') || (!x.actions.length && !!x.text.trim())) }),
    reviewHistory: (id, review, path) => call('GET', `/api/sessions/${encodeURIComponent(id)}/review/history?${q({ review, path })}`),
    turns: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/turns`),
    reviewDone: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/review/done`, {}),
    revertPreview: (id, n, undo = '') => call('GET', `/api/sessions/${encodeURIComponent(id)}/review/revert?${q({ n: String(n), undo })}`),
    revertTurn: (id, preview) => call('POST', `/api/sessions/${encodeURIComponent(id)}/review/revert`, { n: preview.n, undo: preview.undo, current: preview.current }),
    reviewNotes: (id, text, items) => call('POST', `/api/sessions/${encodeURIComponent(id)}/review/dispatch`, { text, target: 'current', items, edit: true }),
    /** files changed between two snapshots; to '' = the working tree now */
    turnFiles: (id, from, to) => call('GET', `/api/sessions/${encodeURIComponent(id)}/turn?${q({ from, to })}`),
    fileLog: (project, path, n = 50) => call('GET', `/api/git/log?${q({ project, path, n: String(n) })}`),
    blame: (project, path) => call('GET', `/api/git/blame?${q({ project, path })}`),
    graph: (project, n = 80) => call('GET', `/api/git/graph?${q({ project, n: String(n) })}`),
    // the Git window (W4/W5): branches, worktrees, the log as rows, one commit's files and sides
    gitBranches: (project) => call('GET', `/api/git/branches?${q({ project })}`),
    gitRows: (project, n = 300, ref = '') => call('GET', `/api/git/rows?${q({ project, n: String(n), ref })}`),
    gitCommitDetail: (project, commit) => call('GET', `/api/git/commit?${q({ project, commit })}`),
    gitBlob: (project, commit, path, side) => call('GET', `/api/git/blob?${q({ project, commit, path, side })}`),
    gitBranch: (project, body) => call('POST', '/api/git/branch', { project, ...body }),
    gitRemote: (project, action) => call('POST', `/api/git/${action}`, { project }),
    gitWorktree: (project, body) => call('POST', '/api/git/worktree', { project, ...body }),
    ci: (project) => call('GET', `/api/git/ci?${q({ project })}`),
    mrDraft: (project, target = '') => call('GET', `/api/git/mr-draft?${q({ project, target })}`),
    createMr: (project, title, description, target, draft) => call('POST', '/api/git/mr', { project, title, description, target, draft }),
    stack: (project) => call('GET', `/api/stack?${q({ project })}`),
    stackRestart: (project, service) => call('POST', '/api/stack/restart', { project, service }),
    stackAction: (project, verb, service) => call('POST', '/api/stack/action', { project, verb, service }),
    stackJob: (project, id, offset) => call('GET', `/api/stack/job?${q({ project, id, offset: String(offset) })}`),
    stackCancel: (project, id) => call('POST', '/api/stack/cancel', { project, id }),
    diagnostics: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/diagnostics`),
    ideaStatus: () => call('GET', '/api/idea/status'),
    ideaProblems: (project, path) => call('GET', `/api/idea/problems?${q({ project, path })}`),
    ideaSymbol: (project, path, line, col) => call('GET', `/api/idea/symbol?${q({ project, path, line: String(line), col: String(col) })}`),
    usage: () => call('GET', '/api/usage'),
    // GitLab inbox
    inbox: () => call('GET', '/api/inbox'),
    /** polls GitLab now (debounced by piperd) and answers with the fresh inbox (<= 20 s) */
    inboxRefresh: () => call('POST', '/api/inbox/refresh', {}),
    inboxItem: (key) => call('GET', `/api/inbox/item?${q({ key })}`),
    inboxSeen: (keys) => call('POST', '/api/inbox/seen', keys === 'all' ? { all: true } : { keys }),
    inboxUnseen: (key) => call('POST', '/api/inbox/unseen', { key }),
    /** marks every pending GitLab todo of the key as done (the GUI confirmed first) */
    inboxDone: (key) => call('POST', '/api/inbox/done', { key }),
    /** a new session (answers the session json) or, with `session`, a follow-up typed into it ({sent}) */
    inboxStart: (body) => call('POST', '/api/inbox/start', body),
    inboxComment: (key, body) => call('POST', '/api/inbox/comment', { key, body }),
    inboxRules: (rules) => call('POST', '/api/inbox/rules', { rules }),
    /** adds a discovered checkout (one of an item's candidates) to projects.json */
    projectAdd: (path, gl) => call('POST', '/api/projects/add', { path, gl }),
    draft: (id) => call('GET', `/api/sessions/${encodeURIComponent(id)}/draft`),
    /** 9.12 D: a prompt into the live session, or piperd's summary into the state file */
    issueState: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/issue-state`, {}),
    // 9.12: Mattermost (incoming webhook) + the half-written prompt
    presence: (visible) => call('POST', '/api/presence', { visible }),
    mmStatus: () => call('GET', '/api/mattermost'),
    mmTest: () => call('POST', '/api/mattermost/test', {}),
    /** the prompt is empty: piperd may type its automatic text (a handoff line) again */
    inputClear: (id) => call('POST', `/api/sessions/${encodeURIComponent(id)}/input-clear`, {}),
};
function q(params) {
    return new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '')).toString();
}
/** one file's diff as plain text (the GUI shows everything; sessions get a filtered view via MCP) */
export async function gitDiff(id, path, untracked) {
    const q = `diff=${encodeURIComponent(path)}${untracked ? '&untracked=1' : ''}`;
    const r = await fetch(`/api/sessions/${encodeURIComponent(id)}/git?${q}`, {
        headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store',
    });
    const text = await r.text();
    if (!r.ok)
        throw new ApiError(r.status, text);
    return text;
}
/** upload a pasted / dropped image; returns its path inside the sandbox */
export async function dropImage(id, file) {
    const r = await fetch(`/api/sessions/${encodeURIComponent(id)}/drop`, {
        method: 'POST', body: file,
        headers: { Authorization: 'Bearer ' + token(), 'Content-Type': file.type || 'application/octet-stream' },
    });
    const j = (await r.json().catch(() => ({})));
    if (!r.ok || !j.path)
        throw new ApiError(r.status, j.error ?? r.statusText);
    return j.path;
}
async function text(path) {
    const r = await fetch(path, { headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store' });
    const t = await r.text();
    if (!r.ok) {
        let msg = t;
        try {
            msg = String(JSON.parse(t).error ?? t);
        }
        catch { /* plain text */ }
        throw new ApiError(r.status, msg);
    }
    return t;
}
/** unified diff of a project file against HEAD (project-keyed; .git: 404) */
export const projectDiff = (project, path) => text(`/api/git/diff?${q({ project, path })}`);
/** unified diff of one commit */
export const commitDiff = (project, commit) => text(`/api/git/show?${q({ project, commit })}`);
/** a file only the session's sandbox has (its /tmp), read-only */
export const sandboxFile = (id, path) => text(`/api/sessions/${encodeURIComponent(id)}/sandbox-file?${q({ path })}`);
/** unified diff of one session turn (from/to snapshot trees; to '' = now), optionally one path */
export const turnDiff = (id, from, to, path = '') => text(`/api/sessions/${encodeURIComponent(id)}/turn?${q({ from, to, diff: path || '.' })}`);
export const reviewDiff = (id, review) => text(`/api/sessions/${encodeURIComponent(id)}/review/diff?${q({ review })}`);
/** the tail of one service's log from the stack provider (scrubbed there) */
export const stackLogs = (project, service, tail = 300) => text(`/api/stack/logs?${q({ project, service, tail: String(tail) })}`);
/** Download a binary artifact without navigating to a browser or executing an HTML report. */
export async function downloadFile(project, path) {
    const r = await fetch(`/api/fs/download?${q({ project, path })}`, { headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store' });
    if (!r.ok)
        throw new ApiError(r.status, 'Could not download this artifact');
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = path.split('/').pop() || 'artifact';
    document.body.append(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}
/** an image of the project as a blob: URL (CSP img-src allows blob:); the caller revokes it */
export async function rawImage(project, path) {
    const r = await fetch(`/api/fs/raw?${q({ project, path })}`, { headers: { Authorization: 'Bearer ' + token() }, cache: 'no-store' });
    if (!r.ok)
        throw new ApiError(r.status, (await r.text()).slice(0, 300));
    return URL.createObjectURL(await r.blob());
}
/** Files: the ⌘P index and the search, all=1 = the excluded tooling folders too */
export const fsIndex = (project, all = false) => call('GET', `/api/fs/files?${q({ project, all: all ? '1' : '' })}`);
export const fsGrep = (project, text, glob, opts = {}) => call('GET', `/api/fs/grep?${q({ project, q: text, glob, cs: opts.cs === false ? '0' : '', limit: opts.limit ? String(opts.limit) : '', all: opts.all ? '1' : '' })}`);
/** K2: a project's git status without a session (Files' Changed scope, tree marks, branch) */
export const projectGitStatus = (project) => call('GET', `/api/git/status?${q({ project })}`);
//# sourceMappingURL=api.js.map