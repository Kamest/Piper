// W13: one decision, drawn the same
// everywhere - the floating card, the DB write card, the queue row and the Feed card. IntelliJ's dialog
// order: Deny · [always] · Allow, the grant on the right; a dangerous grant is red with its own words and
// never a key. The keys (n / y) are written only where they work (the queue). Pure: no DOM.
export const DANGER_TITLE = 'production, a host outside the environment, weaker isolation or a hidden file: read the whole card first';
export function decisionButtons(o = {}) {
    const k = o.keys || o.chips;
    const out = [{ role: 'deny', label: o.keys ? 'Deny n' : 'Deny', cls: '', title: k ? 'deny (n)' : 'deny', ...(k ? { key: 'n' } : {}) }];
    if (o.always)
        out.push({ role: 'always', label: o.always, cls: '', title: 'allow and keep the decision' });
    if (o.sessionAlways)
        out.push({ role: 'session', label: o.sessionAlways, cls: o.danger ? 'danger' : '', title: 'allow this action for the lifetime of this session, only in the displayed environment' + (o.danger ? '; ' + DANGER_TITLE : '') });
    if (o.danger)
        out.push({ role: 'allow', label: o.danger, cls: 'danger', title: DANGER_TITLE + (k ? ' (no y shortcut here)' : '') });
    else
        out.push({ role: 'allow', label: o.keys ? (o.allowLabel ?? 'Allow') + ' y' : (o.allowLabel ?? 'Allow'), cls: 'primary', title: k ? 'allow (y)' : 'allow', ...(k ? { key: 'y' } : {}) });
    return out;
}
/** the role of a Feed op (feed.py sends Allow · always · Deny); null = not a decision */
export function opRole(op) {
    if (op.op !== 'decide')
        return null;
    return op.args.ok === false ? 'deny' : op.args.session_always === true ? 'session' : op.args.always === true ? 'always' : 'allow';
}
const RANK = { deny: 0, always: 1, session: 2, allow: 3 };
/** the Feed card's ops in the one order: the decisions Deny · always · Allow first, the rest after them as sent */
export function orderOps(ops) {
    const dec = ops.filter((o) => opRole(o)).sort((a, b) => RANK[opRole(a)] - RANK[opRole(b)]);
    return [...dec, ...ops.filter((o) => !opRole(o))];
}
// F17: one approval card = a header (session · kind, the risk in a word), the one fact
// (command / domain / file) on a mono line, a context line, the full text folded under Details, the rule that asks.
/** the card header's kind, short (every feed KIND_TEXT kind; decision.test.mjs keeps them in step) */
export const KIND_SHORT = {
    git_commit: 'git commit', git_push: 'git push', git_run: 'git', git_run_project: 'git in another project',
    send_to_session: 'write to a session', stop_session: 'stop a session', propose_session: 'new session proposal',
    spawn_session: 'start a session', gitlab_comment: 'GitLab comment', gitlab_note: 'GitLab comment',
    gitlab_note_edit: 'edit a GitLab note', gitlab_issue_write: 'GitLab issue', gitlab_mr_write: 'merge request',
    gitlab_mr_merge: 'merge', gitlab_job_play: 'CI job', gitlab_job_protected: 'CI job, protected branch',
    gitlab_upload_file: 'GitLab upload', gitlab_job_log: 'CI job log', gitlab_read: 'GitLab read',
    github_comment: 'GitHub comment', github_note_edit: 'edit a GitHub comment', github_issue_write: 'GitHub issue',
    github_pr_write: 'pull request', github_job_log: 'Actions job log',
    write_exec: 'host-run file', edit_file: 'edit file', write_file: 'write file',
    http_auth_signin: 'HTTP sign-in', http_cert_passphrase: 'certificate passphrase', http_runtime_file: 'HTTP runtime file', http_runtime_code: 'HTTP runtime code', http_env_set: 'HTTP environment update', properties_set: 'properties update',
    http_dev: 'HTTP on dev', http_test: 'HTTP on test', http_prod: 'HTTP on PROD', http_host: 'HTTP, outside host',
    db_write_dev: 'DB write', db_write_test: 'DB write', db_write_prod: 'DB write', db_read_test: 'DB read',
    db_read_prod: 'DB read', db_other: "another project's DB", db_unmask: 'unmask',
    allow_domain: 'allow domain', settings_change: 'settings', stack_mode: 'dev stack',
};
/** N3: the module a card's action belongs to - with that module off the card does not float over the
 *  content (it still waits in the queue and on the rail); null = no module, always floats */
export function cardModule(kind) {
    if (['spawn_session', 'propose_session', 'send_to_session', 'stop_session'].includes(kind))
        return 'agent';
    const m = /^(gitlab|github|http|db)_/.exec(kind);
    return m ? m[1] : null;
}
/** A notebook provider's decision cannot depend on modules installed on its server. */
export function cardVisible(card, enabled, selected = false) {
    const module = cardModule(card.kind);
    return !!card.local_provider || !module || enabled(module) || selected;
}
/** kinds that only read or stay local: no risk word; everything else that leaves the sandbox is MEDIUM */
const LOW = new Set(['git_commit', 'propose_session', 'gitlab_job_log', 'gitlab_read', 'github_job_log', 'db_read_test', 'db_other']);
const lines = (s) => s.split('\n').map((l) => l.trim()).filter(Boolean);
/** an absolute path down to its last two parts: the whole path stays in Details */
export function shortPaths(s) {
    return s.replace(/(^|[\s(])\/(?:[^\s/]+\/)+([^\s/]+\/[^\s/)]+)/g, '$1…/$2');
}
const cut = (s, n = 160) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
const str = (v) => (typeof v === 'string' ? v : '');
/** the card's model; `danger` = its red Allow ('' ordinary, cardDanger) */
export function cardModel(p, danger = '') {
    const all = lines(p.summary);
    const warns = all.filter((l) => l.startsWith('⚠'));
    const plain = all.filter((l) => !l.startsWith('⚠'));
    const pl = p.payload ?? {};
    let fact = '';
    let context = '';
    let m;
    if (p.kind === 'git_push' && (m = /^Push of branch (\S+) → (.+?) \((\d+) commits? ahead\)/.exec(plain[0] ?? ''))) {
        fact = pl.evidence && str(pl.remote) && str(pl.branch) ? `git push ${str(pl.remote)} ${str(pl.branch)}`
            : `git push ${m[2].replace(/\/.*$/, '')} ${m[1]}`;
        context = pl.evidence ? '' : `${m[3]} commit${m[3] === '1' ? '' : 's'} ahead · → ${m[2]}`;
    }
    else if (p.kind === 'git_commit' && (m = /^Commit on branch (\S+) \((\d+) files?\)/.exec(plain[0] ?? ''))) {
        fact = plain[1] ?? 'git commit';
        context = `branch ${m[1]} · ${m[2]} file${m[2] === '1' ? '' : 's'} · the content is what they hold at the commit`;
    }
    else if ((p.kind === 'git_run' || p.kind === 'git_run_project') && Array.isArray(pl.args)) {
        fact = 'git ' + pl.args.map(String).join(' ');
        // a warning the summary already shows as a ⚠ line is not said twice
        const w = (Array.isArray(pl.warnings) ? pl.warnings.map(String) : []).filter((x) => !warns.some((l) => l.includes(x)));
        context = w.length ? w.join(' · ') : '';
    }
    else if (p.kind === 'allow_domain' && str(pl.domain)) {
        fact = str(pl.domain);
        context = 'network.allow in .hole/settings.json · applies from the next start';
    }
    else if (p.kind === 'http_auth_signin') {
        fact = `${str(pl.backend)} · ${str(pl.grant)}${pl.pkce === true ? ' (PKCE)' : ''}\n${str(pl.provider)}`;
        context = `environment ${str(pl.env) || '-'} · ${str(pl.file)} · this run only · until ${typeof pl.deadline === 'number' ? new Date(pl.deadline * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '?'}`;
    }
    else if (p.kind === 'http_cert_passphrase') {
        const entry = pl.entry && typeof pl.entry === 'object' ? pl.entry : {};
        fact = `${cut(str(entry.file))}\n${typeof entry.bytes === 'number' ? entry.bytes.toLocaleString('en') : '?'} bytes · sha256 ${/^[0-9a-f]{64}$/.test(str(entry.sha256)) ? str(entry.sha256) : '?'}`;
        context = `SSLConfiguration · environment ${str(pl.env) || '-'} · ${str(pl.file)} · this run only`;
    }
    else if (p.kind === 'http_runtime_code') {
        const entry = pl.entry && typeof pl.entry === 'object' ? pl.entry : {};
        const origin = pl.origin && typeof pl.origin === 'object' ? pl.origin : {};
        const site = pl.site && typeof pl.site === 'object' ? pl.site : {};
        const size = (n) => typeof n === 'number' && n >= 0 ? n.toLocaleString('en') : '?';
        const hash = /^[0-9a-f]{64}$/.test(str(entry.sha256)) ? str(entry.sha256) : '?';
        fact = `${cut(str(entry.file))}\n${size(entry.bytes)} bytes · sha256 ${hash}\n${size(pl.files)} files · ${size(pl.bytes)} bytes with its imports`;
        const index = typeof origin.index === 'number' ? ` #${origin.index + 1}` : '';
        const deadline = typeof pl.deadline === 'number' ? new Date(pl.deadline * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '?';
        const trigger = pl.trigger === 'downloaded' ? 'code from a response of this run' : pl.trigger === 'script_path' ? 'script path picked at run time' : 'computed import';
        context = `${trigger} in ${cut(str(site.file), 64)}${typeof site.line === 'number' ? ':' + site.line : ''} · ${pl.phase === 'pre' ? 'the pre-request script' : 'the response handler'} of ${cut(str(origin.file), 64)}${index} ${cut(str(origin.method), 24)} ${cut(str(origin.target), 64)} · environment ${cut(str(pl.env), 32) || '-'} · this code, this run only · run deadline ${deadline} · Piper loads exactly these ${size(pl.files)} files; a computed import inside them asks again`;
    }
    else if (p.kind === 'http_runtime_file') {
        const origin = pl.origin && typeof pl.origin === 'object' ? pl.origin : {};
        const size = typeof pl.bytes === 'number' && pl.bytes >= 0 ? pl.bytes.toLocaleString('en') : '?';
        const hash = /^[0-9a-f]{64}$/.test(str(pl.sha256)) ? str(pl.sha256) : '?';
        fact = `${cut(str(pl.file))}\n${size} bytes · sha256 ${hash}`;
        const index = typeof origin.index === 'number' ? ` #${origin.index + 1}` : '';
        const deadline = typeof pl.deadline === 'number' ? new Date(pl.deadline * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '?';
        context = `picked by the pre-request script of ${cut(str(origin.file), 64)}${index} ${cut(str(origin.method), 24)} ${cut(str(origin.target), 64)} · environment ${cut(str(pl.env), 32) || '-'} · this file, this run only · run deadline ${deadline}`;
    }
    else if (p.kind.startsWith('http_') && str(pl.file)) {
        fact = str(pl.request_line) || str(pl.file);
        const hosts = Array.isArray(pl.hosts) ? pl.hosts.map(String) : [];
        context = [`environment ${str(pl.env) || '-'}`, hosts.length ? `outside host ${hosts.join(', ')}` : ''].filter(Boolean).join(' · ');
        if (Array.isArray(pl.auth)) {
            const backends = pl.auth.map((item) => {
                const backend = item && typeof item === 'object' ? item : {};
                return `${str(backend.grant).toLowerCase()} → ${str(backend.endpoint) || 'Mock'}`;
            });
            if (backends.length)
                context += ` · auth backend: ${backends.join(' · ')}`;
        }
    }
    else if (p.kind === 'settings_change') {
        fact = plain.find((l) => / → /.test(l)) ?? plain[0] ?? '';
        const n = plain.filter((l) => / → /.test(l)).length;
        context = [str(pl.section) && `Settings › ${str(pl.section).charAt(0).toUpperCase()}${str(pl.section).slice(1)}`, n > 1 ? `${n} values` : ''].filter(Boolean).join(' · ');
    }
    else if (p.kind.startsWith('db_read_') && str(pl.sql)) {
        fact = str(pl.sql);
    }
    else if (['edit_file', 'write_file', 'write_exec'].includes(p.kind) && str(pl.path)) {
        fact = [str(pl.project), str(pl.path)].filter(Boolean).join('/');
    }
    else if (p.kind.startsWith('db_') && pl.card) {
        fact = '';
    }
    else {
        fact = plain[0] ?? '';
        context = plain[1] ?? '';
    }
    const prod = /prod/.test(p.kind) || /\bPROD\b/.test(danger);
    const risk = danger ? { word: prod ? 'PROD' : 'HIGH', level: 'high' }
        : LOW.has(p.kind) ? { word: 'LOW', level: 'low' } : { word: 'MEDIUM', level: 'medium' };
    const why = p.why && p.why.group ? `Security › ${p.why.group} › ${p.why.label.replace(/\s*\([^)]*\)\s*$/, '')}` : null;
    const runtime = p.kind === 'http_auth_signin' || p.kind === 'http_cert_passphrase' || p.kind === 'http_runtime_file' || p.kind === 'http_runtime_code';
    return { short: KIND_SHORT[p.kind] ?? p.kind, risk, fact: runtime ? fact : cut(shortPaths(fact)), context: runtime ? context : cut(shortPaths(context), 200), warns, why };
}
/** the bar under the card: "2 more waiting · question · allow domain" */
export function moreLine(kinds) {
    if (!kinds.length)
        return '';
    const uniq = [...new Set(kinds)];
    return `${kinds.length} more waiting · ${uniq.slice(0, 3).join(' · ')}${uniq.length > 3 ? ' …' : ''}`;
}
/** which pending card the stack shows: the one picked (bar / queue), else the first red one, else the oldest */
export function shownCard(list, picked, isDanger) {
    return list.find((p) => p.id === picked) ?? list.find(isDanger) ?? list[0];
}
/** y / n on the focused card: never inside a text field, never a red grant, never with a modifier */
export function cardKey(e, danger, editing) {
    if (editing || e.metaKey || e.ctrlKey || e.altKey)
        return null;
    if (e.key === 'n' || e.key === 'N')
        return 'deny';
    if ((e.key === 'y' || e.key === 'Y') && !danger)
        return 'allow';
    return null;
}
//# sourceMappingURL=decision.js.map