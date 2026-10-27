export const hosts = new Map();
const PREFIX = 'piper-ref:';
export function scoped(host, kind, raw) {
    if (!raw)
        return raw;
    if (raw.startsWith(PREFIX))
        throw new Error('Reserved Piper reference in server data');
    return host ? PREFIX + host + ':' + kind + ':' + encodeURIComponent(raw) : raw;
}
export function reference(value, expected) {
    if (!value.startsWith(PREFIX))
        return { host: '', raw: value };
    const match = /^piper-ref:([A-Za-z0-9][A-Za-z0-9_-]{0,39}):(session|project|pending|conversation|http|result|action|terminal):(.+)$/.exec(value);
    if (!match || expected && match[2] !== expected)
        throw new Error('Invalid Piper reference');
    let raw;
    try {
        raw = decodeURIComponent(match[3]);
    }
    catch {
        throw new Error('Invalid Piper reference encoding');
    }
    if (!raw || raw.startsWith(PREFIX))
        throw new Error('Invalid nested Piper reference');
    return { host: match[1], kind: match[2], raw };
}
/** Deep links may carry an explicit scoped ID or a raw ID in the emitting session's host. */
export function linkedSessionRef(id, host = '', source = '') {
    if (!id)
        return '';
    const ref = reference(id, 'session');
    if (ref.host) {
        if (host && ref.host !== host)
            throw new Error('Conflicting session link hosts');
        return id;
    }
    return scoped(host || (source ? refHost(source) : ''), 'session', id);
}
export const refHost = (value) => reference(value).host;
export const rawProjectKey = (value) => reference(value, 'project').raw;
export const baseProjectRef = (value) => { const r = reference(value, 'project'); return scoped(r.host, 'project', r.raw.split('@')[0] ?? ''); };
export const projectName = (value) => rawProjectKey(value).split('/').pop() || rawProjectKey(value);
export function hostName(value) { const id = refHost(value); return id ? hosts.get(id)?.host.label ?? 'Server' : 'This computer'; }
export function hostState(value) { return hosts.get(refHost(value))?.snapshot; }
export function sshOnly(value) { return hosts.get(refHost(value))?.host.mode === 'ssh'; }
export function hostOffline(value) { const id = refHost(value); return !!id && hosts.get(id)?.online !== true; }
/** Loopback OAuth callbacks require the browser on the HTTP execution computer. Device flow works across hosts. */
export function signinBrowserError(pending, mobile) {
    return pending.payload?.grant === 'Authorization Code' && (mobile || !!refHost(pending.id) && !pending.local_provider)
        ? 'Authorization Code requires a browser on the computer running HTTP. Use Device Authorization here, or sign in from that computer’s desktop GUI.' : '';
}
/** Kept for old deep links only; it never influences HTTP or websocket routing. */
export function environmentId(search = globalThis.location?.search ?? '') {
    const value = new URLSearchParams(search).get('environment') ?? '';
    return /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(value) && value !== 'local' ? value : '';
}
export const machineKey = (key) => key;
export const machineStorage = (storage) => storage;
export function terminalQuery(id) { const host = refHost(id); return host ? '?environment=' + encodeURIComponent(host) : ''; }
function field(row, key, host, kind) {
    if (typeof row[key] === 'string' && row[key])
        row[key] = scoped(host, kind, row[key]);
}
export function scopeSession(host, value) {
    const row = structuredClone(value);
    row.id = scoped(host, 'session', row.id);
    row.project = scoped(host, 'project', row.project);
    if (row.parent)
        row.parent = scoped(host, 'session', row.parent);
    if (row.switched_from)
        row.switched_from.session = scoped(host, 'session', row.switched_from.session);
    return row;
}
function scopePending(host, value) {
    const row = structuredClone(value);
    row.id = scoped(host, 'pending', row.id);
    row.session = scoped(host, 'session', row.session);
    // Notebook provider payloads belong to the notebook. Only the requesting session/decision is remote.
    if (row.payload && !value.id.startsWith('local-')) {
        field(row.payload, 'project', host, 'project');
        field(row.payload, 'session', host, 'session');
    }
    return row;
}
function scopeSubject(host, value) {
    const row = { ...value };
    if (row.session)
        row.session = scoped(host, 'session', row.session);
    if (row.project)
        row.project = scoped(host, 'project', row.project);
    if (row.type === 'session')
        row.key = scoped(host, 'session', row.key);
    if (row.type === 'confirm')
        row.key = scoped(host, 'pending', row.key);
    if (row.type !== 'session' && row.type !== 'confirm')
        row.key = scoped(host, 'action', row.key);
    return row;
}
export function scopeAction(host, value) {
    const row = structuredClone(value);
    row.key = scoped(host, 'action', row.key);
    row.subject = scopeSubject(host, row.subject);
    if (row.project)
        row.project = scoped(host, 'project', row.project);
    for (const op of row.actions) {
        const args = op.args;
        if (typeof args.session === 'string')
            args.session = scoped(host, 'session', args.session);
        if (typeof args.project === 'string')
            args.project = scoped(host, 'project', args.project);
        if (typeof args.id === 'string' && ['open', 'reply', 'decide'].includes(op.op))
            args.id = scoped(host, op.op === 'decide' ? 'pending' : 'session', args.id);
    }
    return row;
}
export function scopeState(host, value) {
    const row = structuredClone(value);
    row.sessions = value.sessions.map(s => scopeSession(host, s));
    row.projects = value.projects.map(p => ({ ...p, key: scoped(host, 'project', p.key) }));
    row.pending = value.pending.map(p => scopePending(host, p));
    if (value.pending_gone)
        row.pending_gone = value.pending_gone.map(p => ({ ...p, id: scoped(host, 'pending', p.id), session: scoped(host, 'session', p.session) }));
    if (row.project_session_defaults)
        row.project_session_defaults = Object.fromEntries(Object.entries(row.project_session_defaults).map(([k, v]) => [scoped(host, 'project', k), v]));
    if (row.queue)
        row.queue.actions = row.queue.actions.map(a => scopeAction(host, a));
    if (row.host_terms?.items)
        row.host_terms.items = row.host_terms.items.map(t => ({ ...t, id: scoped(host, 'terminal', t.id), project: scoped(host, 'project', t.project), ...(t.session ? { session: scoped(host, 'session', t.session) } : {}) }));
    return row;
}
/** Only collections with explicit ownership are projected; visual settings and control plane stay local. */
export function projectHosts(local) {
    const snapshots = [...hosts.values()].filter(h => h.host.id && h.snapshot).map(h => h.snapshot);
    return { ...local,
        sessions: [...local.sessions, ...snapshots.flatMap(s => s.sessions)],
        projects: [...local.projects, ...snapshots.flatMap(s => s.projects)],
        pending: [...local.pending, ...snapshots.flatMap(s => s.pending)],
        pending_gone: [...local.pending_gone ?? [], ...snapshots.flatMap(s => s.pending_gone ?? [])],
        project_session_defaults: Object.assign({}, local.project_session_defaults, ...snapshots.map(s => s.project_session_defaults ?? {})),
        queue: { ...local.queue, actions: [...local.queue?.actions ?? [], ...snapshots.flatMap(s => (s.queue?.actions ?? []).filter(a => a.subject.session || ['session', 'confirm'].includes(a.subject.type)))] },
    };
}
/** Endpoint manifest: identity fields only. SQL, file text, task, params and connection names are opaque. */
export function routeRequest(path, body, explicitHost) {
    const url = new URL(path, 'http://piper.invalid');
    const target = new Set();
    if (explicitHost !== undefined)
        target.add(explicitHost);
    const use = (value, kind) => {
        if (!value)
            return value;
        const ref = reference(value, kind === 'action' && url.pathname === '/api/feed/mute' ? undefined : kind);
        if (kind === 'action' && url.pathname === '/api/feed/mute' && ref.kind && !['action', 'session', 'pending', 'project'].includes(ref.kind))
            throw new Error('Invalid mute reference');
        target.add(ref.host);
        return ref.raw;
    };
    let pathname = url.pathname;
    const sessionPath = /^\/api\/(sessions|conversations|host-terminals)\/([^/]+)(.*)$/.exec(pathname);
    if (sessionPath && pathname !== '/api/conversations/search') {
        const kind = sessionPath[1] === 'conversations' ? 'conversation' : sessionPath[1] === 'host-terminals' ? 'terminal' : 'session';
        const value = decodeURIComponent(sessionPath[2]);
        // Piper's conversation endpoints also accept session IDs for resume/continue.
        const actual = reference(value).kind;
        const raw = use(value, kind === 'conversation' && actual === 'session' ? 'session' : kind);
        pathname = '/api/' + sessionPath[1] + '/' + encodeURIComponent(raw) + sessionPath[3];
    }
    const pendingPath = /^\/api\/pending\/([^/]+)$/.exec(pathname);
    if (pendingPath)
        pathname = '/api/pending/' + encodeURIComponent(use(decodeURIComponent(pendingPath[1]), 'pending'));
    const authPath = /^\/api\/http\/auth\/([^/]+)\/(open|closed)$/.exec(pathname);
    if (authPath)
        pathname = '/api/http/auth/' + encodeURIComponent(use(decodeURIComponent(authPath[1]), 'pending')) + '/' + authPath[2];
    const projectEndpoint = /^\/api\/(fs\/|git\/|db\/|http\/|stack(?:\/|$)|gitlab\/|projects\/|conversations(?:$|\/))/.test(pathname) || pathname === '/api/sessions';
    const sessionEndpoint = projectEndpoint || /^\/api\/(audit|hold|interrupted|timesheet\/focus|feed(?:\/|$))/.test(pathname);
    const kinds = {};
    if (projectEndpoint)
        kinds.project = 'project';
    if (sessionEndpoint)
        kinds.session = 'session';
    if (pathname === '/api/host-terminals') {
        kinds.project = 'project';
        kinds.session = 'session';
    }
    if (pathname === '/api/http/run' || pathname === '/api/http/share')
        kinds.id = 'http';
    if (pathname === '/api/db/fetch')
        kinds.result_id = 'result';
    if (pathname === '/api/db/cancel')
        kinds.owner = 'session';
    if (pathname === '/api/feed/dismiss' || pathname === '/api/feed/mute')
        kinds.key = 'action';
    for (const [key, kind] of Object.entries(kinds))
        if (url.searchParams.has(key) && !(key === 'owner' && url.searchParams.get(key) === 'gui'))
            url.searchParams.set(key, use(url.searchParams.get(key), kind));
    let data = body;
    if (body && typeof body === 'object' && !Array.isArray(body)) {
        const input = body;
        const next = { ...input };
        for (const [key, kind] of Object.entries(kinds))
            if (typeof next[key] === 'string' && !(key === 'owner' && next[key] === 'gui'))
                next[key] = use(next[key], kind);
        // Unknown scoped identity fields must never fall back to this computer.
        for (const [key, value] of Object.entries(input))
            if (!kinds[key] && ['id', 'project', 'session', 'owner', 'key', 'result_id'].includes(key) && typeof value === 'string' && value.startsWith(PREFIX))
                throw new Error('Unsupported remote operation');
        data = next;
    }
    for (const [key, value] of url.searchParams)
        if (!kinds[key] && ['id', 'project', 'session', 'owner', 'key', 'result_id'].includes(key) && value.startsWith(PREFIX))
            throw new Error('Unsupported remote operation');
    if (target.size > 1)
        throw new Error('Operation combines references from different hosts');
    const host = [...target][0] ?? '';
    if (host && pathname.startsWith('/api/environments'))
        throw new Error('Server settings belong to this computer');
    return { path: pathname + url.search, body: data, host };
}
export function assertHostWrite(host, method, path) {
    if (!host)
        return;
    const row = hosts.get(host);
    if (method !== 'GET' && row?.online !== true)
        throw new Error('Server unavailable. Refresh its state before another operation.');
    if (row?.host.mode === 'ssh' && path !== '/api/state' && !(method === 'POST' && (path === '/api/sessions' || /^\/api\/sessions\/[^/]+\/(stop|title|forget)$/.test(path))))
        throw new Error('SSH only supports direct sessions and their terminal, stop, rename and remove.');
}
/** Scope only declared response identities. Never traverse user content, rows or parameters. */
export function scopeResponse(host, path, value) {
    if (!value || typeof value !== 'object')
        return value;
    const pathname = new URL(path, 'http://piper.invalid').pathname;
    if (pathname === '/api/state')
        return scopeState(host, value);
    const data = structuredClone(value);
    if (pathname === '/api/sessions' && data.id)
        return scopeSession(host, data);
    const set = (row, key, kind) => field(row, key, host, kind);
    if (pathname === '/api/board' && Array.isArray(data.cards))
        for (const row of data.cards) {
            set(row, 'id', 'session');
            set(row, 'session', 'session');
            set(row, 'project', 'project');
        }
    if (pathname === '/api/git/worktree' || pathname === '/api/projects/add')
        set(data, 'key', 'project');
    if (pathname === '/api/projects/import' && Array.isArray(data.added))
        data.added = data.added.map((p) => scoped(host, 'project', p));
    if (/^\/api\/(sessions|conversations)\//.test(pathname) || pathname === '/api/feed/chat/start') {
        set(data, 'session', 'session');
        set(data, 'from', 'session');
    }
    if (pathname === '/api/conversations') {
        for (const row of data.items ?? []) {
            set(row, 'session', 'session');
            set(row, 'conversation', 'conversation');
            set(row, 'project', 'project');
            if (row.import_from)
                set(row.import_from, 'conversation', 'conversation');
            if (row.fork_from) {
                set(row.fork_from, 'session', 'session');
                set(row.fork_from, 'conversation', 'conversation');
            }
        }
        for (const row of [...data.projects ?? [], ...data.targets ?? []])
            set(row, 'key', 'project');
    }
    if (pathname === '/api/conversations/search')
        for (const row of data.hits ?? []) {
            set(row, 'session', 'session');
            set(row, 'id', 'session');
            set(row, 'project', 'project');
        }
    if (pathname === '/api/host-terminals') {
        set(data, 'id', 'terminal');
        set(data, 'project', 'project');
        set(data, 'session', 'session');
    }
    if (pathname === '/api/http/run' || pathname === '/api/http/share') {
        set(data, 'id', 'http');
        set(data, 'project', 'project');
    }
    if (pathname === '/api/http/runs')
        for (const row of data.runs ?? [])
            set(row, 'id', 'http');
    if (pathname === '/api/http/files')
        for (const row of data.files ?? [])
            if (row.last)
                set(row.last, 'id', 'http');
    if (pathname.startsWith('/api/db/')) {
        set(data, 'result_id', 'result');
        set(data, 'project', 'project');
        for (const row of data.connections ?? [])
            set(row, 'project', 'project');
        if (pathname === '/api/db/all')
            for (const group of data.projects ?? []) {
                set(group, 'project', 'project');
                for (const row of group.connections ?? [])
                    set(row, 'project', 'project');
            }
        for (const row of data.running ?? [])
            if (row.owner && row.owner !== 'gui')
                set(row, 'owner', 'session');
        for (const row of data.queries ?? [])
            set(row, 'session', 'session');
    }
    return data;
}
//# sourceMappingURL=environment.js.map