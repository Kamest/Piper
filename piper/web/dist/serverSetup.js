/** Server setup boards A–D: durable backend facts; mutations only after an explicit click. */
import { ApiError, call } from './api.js';
import { fi, h } from './dom.js';
import { hosts } from './environment.js';
import { pairingQR } from './mobilesettings.js';
import { markBidi } from './text.js';
import { mobileLimited } from './transport.js';
const errorText = (e) => e instanceof Error ? e.message : 'Request failed';
const request = async (method, path, body) => {
    const r = await call(method, path, body, undefined, 15000, '');
    if (!r.data)
        throw new Error('No result received. Check the current state before repeating a change.');
    return r.data;
};
export function setupSummary(s, outdated = false) {
    if (s.status === 'running')
        return 'Updating server…';
    const reasons = s.steps.filter(row => row.state !== 'ok').map(row => row.title);
    if (outdated)
        reasons.unshift('new Piper version');
    if (reasons.length)
        return 'Out of date · ' + reasons.join(', ');
    return `Ready · service ✓ · Hole ✓ · extensions ✓ · ${s.projects.length} projects · config current · phone backup ✓`;
}
export function createServerSetup(server, deps, visible) {
    const el = h('div', { class: 'server-setup' });
    const summaries = new Set();
    const path = `/api/environments/${encodeURIComponent(server.id)}/setup`;
    let state = null, loading = false, busy = false, error = '', uncertain = false;
    let timer = 0, platform = '';
    const permitted = () => server.mode === 'service' && !mobileLimited();
    const shown = () => permitted() && visible() && el.isConnected && !document.hidden && !!el.getClientRects().length;
    const blocked = () => busy || loading || state?.status === 'running';
    const button = (label, go, primary = false) => h('button', { type: 'button', class: primary ? 'primary' : '', disabled: blocked(), onclick: go }, label);
    function schedule() {
        window.clearTimeout(timer);
        timer = 0;
        if (state?.status === 'running' && shown() && !loading && !busy)
            timer = window.setTimeout(() => { timer = 0; if (shown())
                void read(); }, 2000);
    }
    async function read() {
        if (loading || !permitted())
            return;
        loading = true;
        try {
            const next = await request('GET', path);
            if (!Array.isArray(next.steps) || next.environment !== server.id)
                throw new Error('Server setup state is unavailable.');
            state = next;
            uncertain = false;
            error = '';
        }
        catch (e) {
            error = errorText(e);
        }
        finally {
            loading = false;
            draw();
        }
    }
    async function mutate(action, body = action.body ?? {}) {
        if (blocked() || !permitted() || !action.path)
            return;
        busy = true;
        error = '';
        draw();
        try {
            const next = await request(action.method ?? 'POST', action.path, body);
            if (!Array.isArray(next.steps) || next.environment !== server.id)
                throw new Error('Setup outcome is not confirmed. Check result before continuing.');
            state = next;
            uncertain = false;
            deps.changed();
        }
        catch (e) {
            error = errorText(e);
            uncertain = !(e instanceof ApiError && e.status >= 400 && e.status < 500);
            // A read may reveal the durable reconcile/retry action; never replay the write.
            try {
                state = await request('GET', path);
            }
            catch { /* keep the last facts and explicit Check result */ }
        }
        finally {
            busy = false;
            draw();
        }
    }
    function pending() {
        const step = state?.unknown && state.steps.find(r => r.id === state.unknown.step);
        const a = step && step.action;
        if (!a || !a.path || (a.kind !== 'reconcile' && a.kind !== 'retry'))
            return null;
        return { label: a.kind === 'reconcile' ? 'Check result' : 'Retry', action: a };
    }
    function drawSummaries() {
        for (const summary of summaries) {
            const remote = hosts.get(server.id)?.snapshot;
            const outdated = remote?.code_outdated === true || !!(window.__PIPER__?.guiBuild && remote?.gui_build && window.__PIPER__.guiBuild !== remote.gui_build);
            const text = state ? setupSummary(state, outdated) : error ? 'Setup status unavailable' : 'Checking setup…';
            summary.replaceChildren(h('span', { class: 'server-setup-summary-text' + (state && (outdated || state.steps.some(r => r.state !== 'ok')) ? ' server-setup-warning' : '') }, text));
            const resolve = pending();
            if (resolve)
                summary.append(button(resolve.label, () => void mutate(resolve.action), true));
            else if (state && (state.status !== 'done' || outdated || state.steps.some(r => r.state !== 'ok')))
                summary.append(button(state.status === 'running' ? 'Updating server…' : 'Update server', () => {
                    if (uncertain || state?.unknown)
                        deps.toast('Open the server and Check result before retrying.');
                    else
                        void mutate({ kind: 'fix', method: 'POST', path });
                }, true));
        }
    }
    function draw() {
        drawSummaries();
        el.replaceChildren();
        if (!permitted()) {
            schedule();
            return;
        }
        const running = state?.status === 'running';
        // A lost server answer blocks a new pass; the header offers the one action that resolves it.
        const resolve = pending();
        const start = resolve ? button(resolve.label, () => void mutate(resolve.action), true) :
            button(running ? 'Updating server…' : state?.updated ? 'Update server' : 'Set up server', () => void mutate({ kind: 'fix', method: 'POST', path }), true);
        if (!resolve)
            start.disabled = blocked() || !state || uncertain || !!state.unknown;
        el.append(h('div', { class: 'server-setup-header' }, h('div', { class: 'grow' }, h('h3', { class: 'server-title' }, server.label), h('p', { class: 'mut small' }, `${server.host} · Piper service · ${platform || 'OS not checked'}`)), start));
        if (error)
            el.append(h('p', { class: 'bad small', role: 'alert' }, error), button('Check result', () => void read()));
        if (!state) {
            if (!error)
                el.append(h('p', { class: 'mut small', role: 'status' }, 'Loading server setup…'));
            schedule();
            return;
        }
        const current = state.steps.find(r => r.state === 'running') ?? state.steps.find(r => r.state !== 'ok') ?? state.steps.at(-1);
        if (current)
            el.append(h('div', { class: 'server-setup-progress', role: 'status', 'aria-live': 'polite' }, running ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : fi(state.steps.every(r => r.state === 'ok') ? 'check' : 'info'), `Step ${current.number} of 10 · ${current.title} · ${state.steps.filter(r => r.state === 'ok').length} done, ${state.steps.filter(r => r.state === 'needs_user').length} need you`));
        const rows = h('div', { class: 'server-list server-setup-rows', role: 'list', 'aria-label': 'Server setup' });
        for (const row of state.steps) {
            const icon = h('span', { class: `server-setup-icon server-setup-${row.state}`, role: 'img', 'aria-label': row.state.replace('_', ' ') }, row.state === 'running' ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : row.state === 'todo' ? null : fi(row.state === 'ok' ? 'check' : row.state === 'failed' ? 'x' : 'alert'));
            const actions = h('div', { class: 'server-setup-actions' });
            if (row.id === 'projects' && !state.unknown) {
                const choose = button('Choose…', () => void projects());
                choose.disabled ||= uncertain;
                actions.append(choose);
            }
            if (row.action) {
                const a = row.action;
                const labels = { configuration: 'Review…', secrets: 'Enter…', terminal: 'Sign in…', public_key: 'Show key…', reconcile: 'Check result', retry: 'Retry', set_up: 'Set up', pair_phone: 'Pair phone…', fix: 'Fix' };
                const b = button(labels[a.kind] ?? a.label ?? 'Fix', () => void dispatch(a));
                b.disabled ||= uncertain;
                actions.append(b);
            }
            rows.append(h('div', { class: `server-setup-row server-setup-row-${row.state}`, role: 'listitem', 'data-setup-step': row.id, 'aria-current': row.state === 'running' ? 'step' : undefined }, icon, h('div', { class: 'server-row-main' }, h('span', {}, row.title), h('span', { class: 'mut small server-setup-detail' }, row.detail)), actions));
        }
        el.append(h('h3', { class: 'server-group' }, 'Server setup'), rows, h('p', { class: 'mut small server-setup-note' }, 'Never copied: logins (.credentials.json, auth.json), tokens, .env, plugins, transcripts. Docker itself needs sudo and is only checked.'));
        schedule();
    }
    async function dispatch(a) {
        if (blocked() || uncertain || !permitted())
            return;
        if (a.kind === 'configuration')
            return review(a);
        if (a.kind === 'secrets')
            return secrets(a);
        if (a.kind === 'pair_phone') {
            busy = true;
            draw();
            try {
                await pair(a);
            }
            finally {
                busy = false;
                draw();
            }
            return;
        }
        if (a.kind === 'public_key') {
            deps.modal('Git access', h('p', { class: 'mut small' }, 'Add this public key to your forge account or as a deploy key.'), h('p', {}, (a.hosts ?? []).join(', ')), h('pre', { class: 'server-setup-key' }, a.public_key ?? ''), h('div', { class: 'row' }, h('button', { onclick: () => void navigator.clipboard.writeText(a.public_key ?? '').then(() => deps.toast('Copied')).catch(() => deps.toast('Could not copy')) }, fi('copy'), 'Copy'), h('button', { onclick: deps.closeModal }, 'Close'), a.fix ? button('Check again', () => { deps.closeModal(); void mutate(a.fix); }, true) : null));
            return;
        }
        if (a.kind === 'terminal') {
            if (a.environment && a.environment !== server.id) {
                deps.toast('Sign-in action belongs to another server. Reopen this server.');
                return;
            }
            deps.modal('Sign in on Server', ...Object.entries(a.commands ?? {}).map(([agent, command]) => button(agent === 'claude' ? 'Claude' : agent === 'codex' ? 'Codex' : agent, () => void terminal(command))));
            return;
        }
        return mutate(a);
    }
    async function projects() {
        if (blocked() || uncertain || !permitted())
            return;
        busy = true;
        draw();
        try {
            const local = await request('GET', '/api/state');
            const active = new Set(local.sessions.filter(s => !s.moved && !['exited', 'error', 'blocked'].includes(s.state)).map(s => s.project.split('@')[0]));
            const selected = new Set(state?.updated ? state.projects : active);
            const choices = local.projects.filter(p => p.exists && !p.key.includes('@'));
            const list = h('div', { class: 'server-setup-projects' });
            const inputs = choices.map(p => {
                const input = h('input', { type: 'checkbox', value: p.key });
                input.checked = selected.has(p.key);
                list.append(h('label', { class: 'row left' }, input, p.title || p.key, h('span', { class: 'mut small mono' }, p.key)));
                return input;
            });
            if (!choices.length)
                list.append(h('p', { class: 'mut' }, 'No local project checkouts.'));
            let submitted = false;
            deps.modal('Choose projects', list, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: () => {
                    if (submitted)
                        return;
                    submitted = true;
                    deps.closeModal();
                    void mutate({ kind: 'fix', method: 'POST', path: path + '/projects' }, { projects: inputs.filter(i => i.checked).map(i => i.value) });
                } }, 'Save projects')));
        }
        catch (e) {
            deps.toast(errorText(e));
        }
        finally {
            busy = false;
            draw();
        }
    }
    async function review(a) {
        if (!a.path)
            return;
        busy = true;
        draw();
        try {
            const plan = await request(a.method ?? 'POST', a.path, a.body ?? {});
            const root = h('div', { class: 'server-setup-review' });
            for (const [prefix, label, files] of [['+', 'added', plan.added], ['~', 'changed', plan.changed], ['−', 'removed on server', plan.removed_on_server]])
                root.append(h('div', { class: 'server-setup-diff' }, h('span', {}, `${prefix} ${files.length}`), h('div', {}, label, h('p', { class: 'mut small' }, files.slice(0, 5).join(', '), files.length > 5 ? ` · and ${files.length - 5} more` : ''))));
            if (plan.edited_on_server.length)
                root.append(h('div', { class: 'server-setup-edited', role: 'note' }, h('b', {}, 'Edited on the server'), h('p', {}, plan.edited_on_server.join(', ') + ' was changed there since the last copy. Copying replaces it.')));
            root.append(h('div', { class: 'server-setup-left' }, h('b', {}, `Left out (${plan.left_out.length})`), h('ul', { class: 'mut small' }, ...plan.left_out.map(f => h('li', {}, `${f.path} · ${f.reason}`)))));
            const message = h('p', { class: 'bad small', role: 'alert', hidden: true });
            const again = h('button', { hidden: true, onclick: () => { deps.closeModal(); void dispatch({ ...a, kind: 'configuration' }); } }, 'Review again');
            const count = plan.added.length + plan.changed.length + plan.removed_on_server.length;
            let submitted = false;
            const apply = h('button', { class: 'primary', disabled: !count, onclick: async () => {
                    if (submitted || blocked() || !permitted())
                        return;
                    submitted = true;
                    apply.disabled = true;
                    busy = true;
                    draw();
                    try {
                        await request('POST', path + '/configuration/apply', { plan_id: plan.plan_id });
                        if (root.isConnected)
                            deps.closeModal();
                        deps.changed();
                        await read();
                    }
                    catch (e) {
                        // A refused/stale plan must be reviewed again; an ambiguous result is checked, never applied twice.
                        message.hidden = false;
                        message.textContent = markBidi(errorText(e));
                        again.hidden = !(e instanceof ApiError && e.status === 400);
                        uncertain = again.hidden;
                        await read();
                    }
                    finally {
                        busy = false;
                        draw();
                    }
                } }, `Copy ${count} files`);
            deps.modal('Copy configuration to Server?', root, message, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), again, apply));
        }
        catch (e) {
            deps.toast(errorText(e));
        }
        finally {
            busy = false;
            draw();
        }
    }
    async function secrets(a) {
        if (!a.path)
            return;
        busy = true;
        draw();
        try {
            const data = await request(a.method ?? 'GET', a.path);
            const root = h('form', { class: 'server-setup-secrets', autocomplete: 'off' });
            const inputs = data.secrets.map((s, i) => {
                const input = h('input', { id: `setup-secret-${i}`, type: 'password', autocomplete: 'new-password', spellcheck: 'false', maxlength: 16384, 'aria-label': s.label || s.key });
                root.append(h('div', { class: 'server-setup-secret' }, h('p', { class: 'mut small mono' }, s.file), h('label', { for: input.id }, s.label || s.key, h('span', { class: 'mut small' }, ` · ${s.key} · ${s.state}`)), input));
                return { id: s.id, input };
            });
            const clear = () => { for (const s of inputs)
                s.input.value = ''; };
            const observer = new MutationObserver(() => { if (!root.isConnected) {
                clear();
                observer.disconnect();
            } });
            const message = h('p', { class: 'bad small', role: 'alert', hidden: true });
            let submitted = false;
            const save = h('button', { type: 'submit', class: 'primary' }, 'Save on Server');
            root.addEventListener('submit', event => {
                event.preventDefault();
                if (submitted || blocked() || !permitted())
                    return;
                submitted = true;
                save.disabled = true;
                busy = true;
                for (const s of inputs)
                    s.input.disabled = true;
                draw();
                const values = {};
                for (const s of inputs)
                    if (s.input.value)
                        values[s.id] = s.input.value;
                clear();
                // call() serializes synchronously. Clear both the DOM and this temporary object immediately.
                const pending = request('POST', a.path, { values });
                for (const key of Object.keys(values))
                    delete values[key];
                void pending.then(async () => { if (root.isConnected)
                    deps.closeModal(); deps.changed(); await read(); }).catch(async () => {
                    // Do not echo a response/transport message that could contain a submitted value.
                    message.hidden = false;
                    message.textContent = 'Save outcome is not confirmed. Close this dialog and Check result before entering values again.';
                    uncertain = true;
                    await read();
                }).finally(() => { clear(); busy = false; draw(); });
            });
            root.append(h('p', { class: 'mut small' }, 'Existing values on the server are kept unless you type a new one.'), message, h('div', { class: 'row' }, h('button', { type: 'button', onclick: () => { clear(); deps.closeModal(); } }, 'Cancel'), save));
            deps.modal('Secrets on Server', root);
            observer.observe(root.closest('#overlay') ?? document.body, { childList: true, subtree: true });
        }
        catch {
            deps.toast('Server secret names are unavailable.');
        }
        finally {
            busy = false;
            draw();
        }
    }
    async function pair(a) {
        if (!a.path)
            return;
        const root = h('div', { class: 'mobile-pair server-setup-pair' });
        let invitation = null, renewing = false, clock = 0, closed = false, failed = false;
        const clear = () => { if (invitation)
            invitation.url = ''; invitation = null; root.replaceChildren(); };
        const stop = () => { closed = true; clear(); window.clearInterval(clock); observer.disconnect(); };
        const render = () => {
            const left = Math.max(0, Math.ceil((invitation?.expires ?? 0) - Date.now() / 1000));
            if (!left) {
                if (invitation)
                    invitation.url = '';
                invitation = null;
            }
            const qr = h('div', { class: 'mobile-qr' + (left ? '' : ' expired') }, left && invitation ? pairingQR(invitation.url) : fi('clock'));
            root.replaceChildren(h('div', { class: 'mobile-pair-body' }, qr, h('div', { class: 'mobile-pair-side' }, h('p', {}, 'Open Piper on your phone, scan this code and pair this server as your backup.'), h('p', { class: 'mut small', role: 'status', 'data-setup-countdown': '' }, failed ? 'Pairing code is unavailable.' : left ? `Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}` : 'QR code expired'))), h('div', { class: 'row' }, h('button', { onclick: () => { stop(); deps.closeModal(); } }, 'Close'), h('button', { class: 'primary', disabled: renewing, onclick: () => void generate() }, 'New code')));
        };
        const generate = async () => {
            if (renewing || closed || !permitted())
                return;
            renewing = true;
            failed = false;
            clear();
            root.append(h('p', { role: 'status' }, 'Preparing code…'));
            try {
                const next = await request(a.method ?? 'POST', a.path, a.body ?? {});
                if (closed || !root.isConnected) {
                    next.url = '';
                    return;
                }
                if (typeof next.url !== 'string' || !next.url || !Number.isFinite(next.expires) || next.expires <= Date.now() / 1000) {
                    next.url = '';
                    throw new Error('Invalid pairing code.');
                }
                invitation = next;
            }
            catch {
                failed = true;
            }
            finally {
                renewing = false;
                if (!closed && root.isConnected)
                    render();
            }
        };
        const observer = new MutationObserver(() => { if (!root.isConnected)
            stop(); });
        deps.modal('Pair phone with Server', root);
        observer.observe(root.closest('#overlay') ?? document.body, { childList: true, subtree: true });
        clock = window.setInterval(() => {
            if (!root.isConnected) {
                stop();
                return;
            }
            if (!invitation)
                return;
            const left = Math.max(0, Math.ceil(invitation.expires - Date.now() / 1000));
            if (!left)
                render();
            else {
                const countdown = root.querySelector('[data-setup-countdown]');
                if (countdown)
                    countdown.textContent = markBidi(`Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`);
            }
        }, 1000);
        await generate();
    }
    async function terminal(command) {
        if (blocked() || !permitted())
            return;
        busy = true;
        draw();
        try {
            const remote = (await call('GET', '/api/state', undefined, undefined, 15000, server.id)).data;
            const project = remote?.projects.find(p => p.exists);
            if (!project)
                throw new Error('Choose and set up a project before opening the server terminal.');
            const shell = (await call('POST', '/api/host-terminals', { project: project.key, cols: 100, rows: 28 }, undefined, 15000, server.id)).data;
            if (!shell)
                throw new Error('Terminal outcome is not confirmed. Check Host shells before opening another.');
            const { TermView } = await import('./term.js');
            const root = h('div', { class: 'server-setup-terminal' });
            let inserted = false;
            const term = new TermView(shell.id, shell.path, {
                onRefused: () => deps.toast('Server terminal unavailable.'), onExit: () => undefined,
                onConnection: up => { if (up && !inserted) {
                    inserted = true;
                    term.insert(command.map(arg => "'" + arg.replace(/'/g, "'\\''") + "'").join(' '));
                } },
                onImage: () => Promise.reject(new Error('Use a session to upload images.')),
            });
            deps.modal('Sign in on Server', h('p', { class: 'mut small' }, 'Press Enter to run the sign-in command on this server.'), root, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Close')));
            term.setInputState(true, true, 'Server sign-in');
            term.mount(root);
            const observer = new MutationObserver(() => { if (!root.isConnected) {
                term.dispose();
                observer.disconnect();
            } });
            observer.observe(root.closest('#overlay') ?? document.body, { childList: true, subtree: true });
        }
        catch (e) {
            deps.toast(errorText(e));
        }
        finally {
            busy = false;
            draw();
        }
    }
    document.addEventListener('visibilitychange', schedule);
    return {
        el,
        show(os = '', label = server.label) { server.label = label; platform = os; draw(); void read(); queueMicrotask(schedule); },
        hide() { window.clearTimeout(timer); timer = 0; },
        summary() { const node = h('div', { class: 'server-setup-summary' }); summaries.clear(); summaries.add(node); drawSummaries(); void read(); return node; },
        refresh: drawSummaries,
    };
}
//# sourceMappingURL=serverSetup.js.map