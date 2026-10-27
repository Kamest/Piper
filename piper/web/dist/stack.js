import { machineStorage } from './environment.js';
// Stack of the project from the dev stack provider: services with
// state and health, what the provider offers per service and for the whole stack (its card),
// the running job with its log, the tail of a service's log. piperd talks to the provider on
// the host; nothing of it reaches a sandbox. Only start/stop/restart/down/up/build/rebuild
// are posted here - anything else stays in the provider's own dashboard.
// Inspector section: the element is persistent and redrawn only when the data changed (the
// inspector re-renders on every poll and calls show() - that is the 10 s refresh while it
// is visible); a running job is polled on its own every 1.5 s.
import { api, stackLogs } from './api.js';
import { marked } from './convo.js';
import { clear, fi, fold, h } from './dom.js';
import { keepOpen } from './inspector.js';
import { markBidi } from './text.js';
import { closeMenu, toggleMenu } from './menu.js';
const REFRESH_MS = 10000;
const LOG_MS = 3000;
const JOB_MS = 1500;
const DONE_MS = 8000;
const JOB_LOG_MAX = 256 << 10;
const JOB_FAILS = 5;
/** the provider answers at most 64 KiB per log read - a full chunk (minus a held-back partial UTF-8
 * sequence of up to 3 bytes) means more is waiting */
const LOG_CHUNK = 65536 - 3;
const JOB_BOXES = 3;
/** dot tone of a service: health wins over state */
export function serviceTone(s) {
    const health = (s.health ?? '').toLowerCase();
    const state = s.state.toLowerCase();
    if (health === 'unhealthy')
        return 'bad';
    if (state === 'exited' && s.exitCode)
        return 'warn'; // ended by itself or killed - not a plain Stop
    if (health === 'starting' || ['restarting', 'starting', 'created', 'paused', 'removing'].includes(state))
        return 'warn';
    if (['dead', 'error', 'failed'].includes(state))
        return 'bad';
    if (state === 'running' || state === 'up' || health === 'healthy')
        return 'ok';
    return 'off';
}
/** log lines containing q (case-insensitive); all lines for an empty q */
export function filterLog(text, q) {
    const lines = text.replace(/\n$/, '').split('\n');
    if (!q)
        return lines;
    const ql = q.toLowerCase();
    return lines.filter((l) => l.toLowerCase().includes(ql));
}
const ING = {
    start: 'Starting', stop: 'Stopping', restart: 'Restarting', down: 'Taking down', up: 'Recreating',
    build: 'Building', rebuild: 'Rebuilding', worktree_adopt: 'Setting up',
};
const ED = {
    start: 'Started', stop: 'Stopped', restart: 'Restarted', down: 'Took down', up: 'Recreated',
    build: 'Built', rebuild: 'Rebuilt', worktree_adopt: 'Set up',
};
const VERB_LABEL = {
    start: 'Start', stop: 'Stop', restart: 'Restart', down: 'Down…', up: 'Recreate', build: 'Build only', rebuild: 'Rebuild',
};
const VERB_HINT = {
    up: 'from the model, policy-checked', rebuild: 'build + restart', down: 'removes the containers',
};
/** piperd's "no dev stack for ..." - not an error, the project simply has none */
const NO_STACK = /no dev stack/;
const PROBLEM = { bad: 0, warn: 1, off: 2, ok: 3 };
/** the header: "4/5 up" (+ tone) from the provider's card, else counted from the rows */
export function stackSummary(s, error = '') {
    if (error)
        return { text: 'unavailable', tone: 'faint' };
    if (!s)
        return { text: '', tone: 'faint' };
    if (!s.stack)
        return { text: !s.error || NO_STACK.test(s.error) ? 'none' : 'unavailable', tone: 'faint' };
    const long = s.services.filter((x) => !x.oneShot);
    const total = s.total ?? long.length;
    const up = s.running ?? long.filter((x) => serviceTone(x) === 'ok').length;
    if (!s.services.length)
        return { text: 'not started', tone: 'faint' };
    if (!total)
        return { text: 'no services', tone: 'faint' };
    // a stopped stack is a state, not a failure: red only when something is broken
    if (up === 0)
        return long.some((x) => serviceTone(x) === 'bad') ? { text: `0/${total} up`, tone: 'bad' } : { text: 'stopped', tone: 'faint' };
    return { text: `${up}/${total} up`, tone: up === total ? 'ok' : 'warn' };
}
/** the job line: "Rebuilding app" / "Rebuilt app" / "Rebuild app failed" */
export function jobTitle(j, service = '') {
    const what = service || 'the stack';
    if (j.state === 'running')
        return `${ING[j.verb] ?? j.verb} ${what}`;
    if (j.state === 'done')
        return `${ED[j.verb] ?? j.verb} ${what}`;
    const word = (VERB_LABEL[j.verb] ?? (j.verb === 'worktree_adopt' ? 'Set up' : j.verb)).replace('…', '');
    return `${word} ${what} ${j.state === 'canceled' ? 'canceled' : 'failed'}`;
}
/** a linked worktree's source row: whose stack the panel shows */
export function worktreeSource(s, settingUp) {
    const w = s.worktree;
    if (!w)
        return null;
    if (w.mode === 'own') {
        return { chip: "WORKTREE'S OWN", tone: 'own', text: s.stack ?? (settingUp ? 'setting up' : 'not set up'), switchTo: 'main', switchLabel: 'Use main' };
    }
    return { chip: 'MAIN CHECKOUT', tone: 'main', text: w.main.split('/').filter(Boolean).slice(-2).join('/'), switchTo: 'own', switchLabel: 'Own stack…' };
}
/** "0:42" / "12:05" */
export function elapsed(sec) {
    const s = Math.max(0, Math.floor(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
/** the last n non-empty lines of a log */
export function tailLines(text, n = 3) {
    return text.split('\n').map((l) => l.replace(/\s+$/, '')).filter(Boolean).slice(-n);
}
/** follow a job of the server's list? `local` = our copy of the SAME job id (null: unknown).
 * A running one unless we poll it already (also to resync a copy that lost contact); a finished
 * one only when it is news - a failure we have not shown (a dismissed one stays known) */
export function shouldTrack(local, server) {
    if (!server)
        return false;
    if (local?.lost)
        return true; // our copy only says "lost contact": the server knows how it went
    if (server.state === 'running')
        return !local || local.state !== 'running';
    return !local && server.state !== 'done';
}
/** what deserves a modal: Down, and stopping / restarting / rebuilding the whole stack.
 * One service's actions and a whole-stack start / recreate / build run at once - the job box shows them. */
export function needsConfirm(verb, service) {
    if (verb === 'down')
        return true;
    return !service && ['stop', 'restart', 'rebuild'].includes(verb);
}
/** problems first (unhealthy, starting, stopped), then running; one-shots apart, at the end */
export function sortServices(list) {
    const main = list.filter((s) => !s.oneShot);
    main.sort((a, b) => PROBLEM[serviceTone(a)] - PROBLEM[serviceTone(b)] || a.name.localeCompare(b.name));
    return { main, oneShots: list.filter((s) => s.oneShot) };
}
/** the one verb a problem row shows inline: restart a broken service, start a stopped one -
 * the latter only while the rest runs (a stopped stack has its Start in the header, not on every row) */
export function fixVerb(s, stackUp = true) {
    const v = s.verbs ?? [];
    const tone = serviceTone(s);
    if (tone === 'bad' && v.includes('restart'))
        return 'restart';
    if ((tone === 'off' || (s.state === 'exited' && s.exitCode)) && stackUp && !s.oneShot && v.includes('start'))
        return 'start';
    return null;
}
/** "unhealthy · 2 min", "4h", "stopped · 3 min" */
export function serviceState(s) {
    const tone = serviceTone(s);
    const word = s.health && s.health !== 'healthy' ? s.health : tone === 'ok' ? '' : s.state === 'exited'
        ? (s.exitCode ? `exited (${s.exitCode})` : 'stopped') : s.state;
    return [word, s.age, s.location ? `in ${s.location}` : ''].filter(Boolean).join(' · ');
}
const DISMISSED_KEY = 'piper.stack.dismissed';
/** ids of failed job boxes closed with ×, for this tab (a reload must not bring them back) */
function dismissedJobs() {
    try {
        const v = JSON.parse(machineStorage(sessionStorage).getItem(DISMISSED_KEY) ?? '[]');
        return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
    }
    catch {
        return [];
    }
}
function dismissJob(id) {
    try {
        machineStorage(sessionStorage).setItem(DISMISSED_KEY, JSON.stringify([...dismissedJobs().filter((x) => x !== id), id].slice(-50)));
    }
    catch {
        /* private window: the box just comes back after a reload */
    }
}
export function createStack(deps) {
    const body = h('div', { class: 'stackbody' });
    const primaryBtn = h('button', { class: 'mini hidden' });
    const more = h('button', { class: 'mini', title: 'more actions', 'aria-label': 'more stack actions', 'aria-haspopup': 'menu' }, h('i', { class: 'fi fi-more', 'aria-hidden': 'true' }));
    const reload = h('button', { class: 'mini icon', title: 'refresh', 'aria-label': 'refresh the stack', onclick: () => void load(project, true) }, fi('refresh'));
    const sec = fold('stack', 'Stack');
    sec.el.classList.add('stack');
    sec.actions.append(primaryBtn, more, reload);
    sec.body.append(body);
    const el = sec.el;
    const cache = new Map();
    const runs = new Map(); // project -> job id -> its job (several may run)
    /** services with an action posted and no job yet: the row turns amber at once */
    const pending = new Map(); // `${project}\0${service}` -> verb
    let project = '';
    let sig = '';
    // a service that dies on its own (or a change made elsewhere) shows without a click or a job
    window.setInterval(() => { if (project && el.isConnected && !document.hidden) {
        void load(project);
        draw();
    } }, REFRESH_MS);
    async function load(p, force = false) {
        let c = cache.get(p);
        if (!c) {
            c = { data: null, error: '', at: 0, loading: false };
            cache.set(p, c);
        }
        if (c.loading || (!force && Date.now() - c.at < REFRESH_MS))
            return;
        c.loading = true;
        reload.textContent = '…';
        try {
            c.data = (await api.stack(p)).data;
            c.error = '';
            // jobs started elsewhere (a dashboard, a command line, another tab) or before a reload
            for (const j of c.data?.jobs ?? []) {
                const local = runsOf(p).get(j.id);
                if (!local && dismissedJobs().includes(j.id))
                    continue; // closed before a reload: stays closed
                if (shouldTrack(local ? { ...local.job, lost: local.lost } : null, j))
                    track(p, j, local?.service ?? (j.services?.length === 1 ? j.services[0] ?? '' : ''));
            }
        }
        catch (e) {
            c.error = String(e instanceof Error ? e.message : e);
        }
        c.at = Date.now();
        c.loading = false;
        reload.replaceChildren(fi('refresh'));
        if (p === project)
            draw();
    }
    // ---------------------------------------------------------------- jobs
    function runsOf(p) {
        let m = runs.get(p);
        if (!m) {
            m = new Map();
            runs.set(p, m);
        }
        return m;
    }
    function track(p, job, service) {
        pending.delete(`${p}\0${service}`); // the job's own state shows on the row from now on
        const m = runsOf(p);
        const old = m.get(job.id);
        if (old) { // the same job again (a resync after lost contact): keep its log, poll anew
            window.clearTimeout(old.timer);
            old.job = job;
            old.fails = 0;
            old.lost = false;
            old.dismissed = false;
            old.ended = job.state === 'running' ? 0 : Date.now();
            if (job.state === 'running')
                poll(p, old);
            else
                void fetchJob(p, old).then(() => { if (p === project)
                    draw(); }); // the real rc, error and the rest of the log
            return;
        }
        const r = { job, service, log: '', offset: 0, timer: 0, ended: job.state === 'running' ? 0 : Date.now(), dismissed: false,
            timed: job.state === 'running', fails: 0, cut: false, lost: false };
        m.set(job.id, r);
        for (const [id, x] of m) { // forget what is no longer shown, beyond a few
            if (m.size <= 20)
                break;
            if (x.job.state !== 'running' && !shown(x))
                m.delete(id);
        }
        if (job.state === 'running')
            poll(p, r);
        else
            void fetchJob(p, r); // a finished one: its log (the error tail is in the job already)
    }
    async function fetchJob(p, r) {
        try {
            // read on while the provider hands out full chunks: a finished job's whole log, a running one's backlog
            for (let i = 0; i < 64; i++) {
                const d = (await api.stackJob(p, r.job.id, r.offset)).data;
                if (!d)
                    break;
                const got = d.offset - r.offset;
                r.job = d.job;
                r.offset = d.offset;
                const log = r.log + d.chunk;
                if (log.length > JOB_LOG_MAX)
                    r.cut = true;
                r.log = log.slice(-JOB_LOG_MAX);
                if (d.state && d.state !== 'running')
                    r.job = { ...r.job, state: d.state };
                if (got < LOG_CHUNK)
                    break;
            }
            r.fails = 0;
        }
        catch (e) {
            // keep polling; after JOB_FAILS misses in a row the box says contact is lost (the job may still run)
            if (++r.fails >= JOB_FAILS && r.job.state === 'running') {
                r.lost = true;
                r.job = { ...r.job, state: 'failed', error: `lost contact with the job: ${String(e instanceof Error ? e.message : e)} - refresh checks again` };
            }
        }
        if (logView?.run === r)
            logView.render();
    }
    function poll(p, r) {
        void fetchJob(p, r).then(() => {
            if (runsOf(p).get(r.job.id) !== r)
                return;
            if (r.job.state === 'running') {
                r.timer = window.setTimeout(() => poll(p, r), JOB_MS);
                void load(p); // the rows follow the job (a whole-stack Stop), at most every REFRESH_MS
            }
            else {
                r.ended = Date.now();
                if (r.job.state === 'done')
                    r.timer = window.setTimeout(() => { if (p === project)
                        draw(); }, DONE_MS + 50);
                void load(p, true);
            }
            if (p === project)
                draw();
        });
    }
    function shown(r) {
        if (r.dismissed)
            return false;
        return !(r.job.state === 'done' && r.ended && Date.now() - r.ended > DONE_MS);
    }
    /** the job boxes: running ones first, then the newest; at most JOB_BOXES */
    function shownRuns(p) {
        return [...runsOf(p).values()].filter(shown)
            .sort((a, b) => Number(b.job.state === 'running') - Number(a.job.state === 'running') || b.job.started - a.job.started)
            .slice(0, JOB_BOXES);
    }
    // ---------------------------------------------------------------- actions
    function act(p, verb, service) {
        if (needsConfirm(verb, service))
            confirmAct(p, verb, service);
        else
            void run(p, verb, service);
    }
    async function run(p, verb, service) {
        const key = `${p}\0${service}`;
        pending.set(key, verb);
        if (p === project)
            draw();
        try {
            // no provider card (an older piperd or provider): the restart route both have always had
            const legacy = !cache.get(p)?.data?.verbs && verb === 'restart';
            const r = legacy ? { ok: true, job: null } : (await api.stackAction(p, verb, service)).data;
            if (legacy)
                await api.stackRestart(p, service);
            if (r?.job)
                track(p, r.job, service);
            else {
                pending.delete(key);
                deps.toast(`${VERB_LABEL[verb].replace('…', '')} ${service || 'the stack'}: accepted`);
                window.setTimeout(() => void load(p, true), 2000);
            }
        }
        catch (e) {
            pending.delete(key);
            deps.toast(`${VERB_LABEL[verb].replace('…', '')} ${service || 'the stack'} failed: ${String(e instanceof Error ? e.message : e)}`);
        }
        if (p === project)
            draw();
    }
    function confirmAct(p, verb, service) {
        const what = service ? `service ${service}` : `all services of ${p}`;
        const text = {
            start: `Starts ${what}.`,
            stop: `Stops ${what}; Start brings them back.`,
            restart: `Restarts ${what} with their current containers.`,
            down: `Stops and removes the containers of ${p}. Data volumes stay; the next Start recreates them from the model.`,
            up: `Recreates ${what} from the compose model. The provider may check the model first.`,
            build: `Builds ${what} (no restart).`,
            rebuild: `Builds ${what} and restarts it - this can take minutes.`,
        };
        const word = VERB_LABEL[verb].replace('…', '');
        deps.modal(verb === 'down' ? `Take ${service ? `service ${service}` : 'the whole stack'} down?` : `${word} ${service || 'the whole stack'}?`, h('p', {}, text[verb]), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', {
            class: verb === 'down' || verb === 'stop' ? 'danger' : 'primary',
            onclick: () => { deps.closeModal(); void run(p, verb, service); },
        }, word)));
    }
    async function setMode(p, mode) {
        try {
            await api.stackMode(p, mode);
            deps.toast(mode === 'own' ? "This worktree uses its own stack" : "This worktree uses the main checkout's stack");
        }
        catch (e) {
            deps.toast('Switching the stack failed: ' + String(e instanceof Error ? e.message : e));
        }
        await load(p, true);
    }
    function confirmSetup(p) {
        const name = p.split('/').pop() || p;
        deps.modal("Set up this worktree's own stack?", h('p', {}, `${name} gets its own ports and data folder, its data are restored from the backup store when missing (this can take minutes), and its stack starts - policy-checked. The main checkout's stack keeps running. This session's database view and the agent's dev connection switch to it; sessions started from now on get this stack.`), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: () => { deps.closeModal(); void setup(p); } }, 'Set up and start')));
    }
    async function setup(p) {
        try {
            const r = (await api.stackSetup(p)).data;
            if (r?.job?.id)
                track(p, r.job, '');
        }
        catch (e) {
            deps.toast('Set up failed: ' + String(e instanceof Error ? e.message : e));
        }
        await load(p, true);
    }
    async function cancelJob(p, r) {
        try {
            await api.stackCancel(p, r.job.id);
            deps.toast('Cancel sent - the job stops after its current step');
        }
        catch (e) {
            deps.toast('Cancel failed: ' + String(e instanceof Error ? e.message : e));
        }
    }
    function openMenu(anchor, items) {
        toggleMenu(anchor, () => items, { label: anchor.getAttribute('aria-label') || 'stack actions' });
    }
    const verbItem = (p, v, service) => ({ label: service || v === 'down' ? VERB_LABEL[v] : `${VERB_LABEL[v]} all`,
        hint: VERB_HINT[v], danger: v === 'down', run: () => act(p, v, service) });
    function stackItems(p, s) {
        const verbs = (s.verbs ?? []).filter((v) => v !== s.primary?.verb);
        const order = ['restart', 'rebuild', 'up', 'start', 'stop', 'down'];
        const vs = order.filter((v) => verbs.includes(v));
        const items = vs.filter((v) => v !== 'stop' && v !== 'down').map((v) => verbItem(p, v, ''));
        if (s.worktree?.mode === 'own')
            items.push({ label: "Use the main checkout's stack", hint: 'this one keeps running', run: () => void setMode(p, 'main') });
        items.push(null, ...vs.filter((v) => v === 'stop' || v === 'down').map((v) => verbItem(p, v, '')));
        if (s.dashboard)
            items.push(null, { label: 'Stack dashboard ↗', hint: 'data, shell, tunnels', run: () => window.open(s.dashboard, '_blank', 'noopener') });
        if (!s.verbs)
            items.unshift({ label: 'Restart all', run: () => act(p, 'restart', '') }, null); // a provider without a card: what it always took
        return items;
    }
    function serviceItems(p, svc) {
        const items = [{ label: 'Logs', run: () => openLogs(p, svc.name) }];
        for (const u of svc.links ?? [])
            items.push({ label: u.replace(/^https?:\/\//, '').replace(/\/$/, '') + ' ↗', run: () => window.open(u, '_blank', 'noopener') });
        const vs = svc.verbs ?? (svc.location ? [] : ['restart']);
        const order = ['start', 'stop', 'restart', 'rebuild', 'build', 'up'];
        const acts = order.filter((v) => vs.includes(v)).map((v) => verbItem(p, v, svc.name));
        if (acts.length)
            items.push(null, ...acts);
        if (svc.ports?.length)
            items.push(null, ...svc.ports.slice(0, 3).map((port) => ({ label: `Copy port ${port}`, run: () => void navigator.clipboard?.writeText(port).then(() => deps.toast(`Copied ${port}`)) })));
        return items;
    }
    // ---------------------------------------------------------------- draw
    function draw() {
        const c = cache.get(project);
        const s = c?.data ?? null;
        const rs = shownRuns(project);
        const r = rs.find((x) => x.job.state === 'running') ?? null; // the header follows the newest running one
        const pend = [...pending].filter(([k]) => k.startsWith(project + '\0'));
        const next = JSON.stringify([project, c?.error, s, rs.map((x) => [x.job, x.service, tailLines(x.log)]), pend]);
        if (next === sig) {
            tick();
            return;
        }
        sig = next;
        clear(body);
        const busy = !!r;
        const sum = stackSummary(s, s ? '' : c?.error ?? ''); // a failed refresh keeps the last state
        sec.status.className = 'sec-s ' + (busy ? 'warn' : sum.tone);
        sec.status.textContent = markBidi(r ? `${(ING[r.job.verb] ?? r.job.verb).toLowerCase()}…` : sum.text); // the box below says what
        sec.el.toggleAttribute('data-empty', !busy && sum.text === 'none' && !s?.worktree); // F13: no dev stack = "Not in use" at the panel's end
        if (!busy && s?.clock?.value && s.clock.kind === 'up' && sum.tone !== 'faint') {
            sec.status.append(h('span', { class: 'faint' }, ` · ${s.clock.value}`));
        }
        // the primary action: the provider's (Start while something is down, Stop when all is up); a job blocks it
        const pr = s?.stack ? s.primary ?? null : null;
        primaryBtn.classList.toggle('hidden', !pr);
        if (pr) {
            primaryBtn.textContent = markBidi((pr.verb === 'stop' ? '■ ' : '▶ ') + pr.label);
            primaryBtn.className = 'mini stack-primary ' + (pr.verb === 'stop' ? '' : 'go');
            primaryBtn.disabled = busy;
            primaryBtn.title = markBidi(busy ? 'a job is running' : pr.verb === 'up' ? 'create and start the stack (policy-checked)' : `${pr.label.toLowerCase()} the stack`);
            primaryBtn.onclick = () => act(project, pr.verb, '');
        }
        more.classList.toggle('hidden', !s?.stack);
        more.onclick = (e) => { e.stopPropagation(); if (s)
            openMenu(more, stackItems(project, s)); };
        // open by itself while a job runs, a service is broken, or the stack is only partly up (a stopped stack stays folded)
        const tones = (s?.stack ? s.services.filter((x) => !x.oneShot) : []).map(serviceTone);
        sec.auto(rs.length > 0 || tones.includes('bad') || (tones.includes('ok') && tones.some((t) => t !== 'ok')));
        if (!c || (!s && !c.error)) {
            body.append(h('p', { class: 'mut small' }, 'loading…'));
            return;
        }
        if (!s) {
            body.append(h('p', { class: 'mut small' }, c.error));
            return;
        }
        // a failed refresh keeps the last data (and the job boxes) on screen, with the reason
        if (c.error)
            body.append(h('p', { class: 'stack-err small' }, `refresh failed: ${c.error}`));
        const src = worktreeSource(s, busy);
        if (src) {
            body.append(h('div', { class: `stack-src ${src.tone}` }, h('span', { class: 'chip' }, src.chip), h('span', { class: 'src-t mut small', title: src.tone === 'main' ? s.worktree?.main ?? '' : '' }, src.text), h('button', { class: 'link small', onclick: () => void setMode(project, src.switchTo) }, src.switchLabel)));
        }
        if (!s.stack && s.worktree?.mode === 'own') {
            for (const x of rs)
                body.append(jobBox(project, x));
            if (!busy) {
                body.append(h('div', { class: 'stack-setup' }, h('b', {}, 'This worktree has no stack of its own yet.'), h('p', { class: 'mut small' }, "Set up gives it its own ports and data folder, restores the data when they are missing, then starts it (policy-checked). The main checkout's stack keeps running."), h('button', { class: 'primary', onclick: () => confirmSetup(project) }, '▶ Set up and start')));
            }
            return;
        }
        if (!s.stack) {
            body.append(h('p', { class: 'mut small' }, s.error && !NO_STACK.test(s.error) ? s.error : 'No dev stack for this project.'));
            return;
        }
        body.append(h('p', { class: 'stack-meta mut small' }, s.stack, s.domainBase ? h('span', { class: 'faint' }, ` · *.${s.domainBase}`) : null));
        if (s.error)
            body.append(h('p', { class: 'stack-err small' }, s.error));
        for (const x of rs)
            body.append(jobBox(project, x));
        const { main, oneShots } = sortServices(s.services);
        const list = h('ul', { class: 'plain services' });
        const anyUp = main.some((x) => serviceTone(x) === 'ok');
        for (const svc of main)
            list.append(serviceRow(project, svc, anyUp));
        if (main.length)
            body.append(list);
        else if (!s.services.length)
            body.append(h('p', { class: 'mut small' }, s.primary?.verb === 'up' ? 'No containers yet - Start creates them.' : 'No services.'));
        if (oneShots.length) {
            const ok = oneShots.every((x) => x.state !== 'running' && serviceTone(x) !== 'bad');
            // W16: the summary carries the live state - the fold is kept by key, not by that text
            const det = keepOpen(h('details', { class: 'oneshots' }, h('summary', { class: 'mut small' }, `${oneShots.length} one-shot${oneShots.length > 1 ? 's' : ''} ${ok ? 'done' : ''} · ${oneShots.map((x) => x.name).join(', ')}`)), `oneshots:${project}`);
            const ul = h('ul', { class: 'plain services' });
            for (const svc of oneShots)
                ul.append(serviceRow(project, svc, false));
            det.append(ul);
            body.append(det);
        }
    }
    function serviceRow(p, svc, stackUp) {
        const pv = pending.get(`${p}\0${svc.name}`);
        const job = [...runsOf(p).values()].find((x) => x.job.state === 'running' && x.service === svc.name);
        const working = pv || job?.job.verb || '';
        const tone = working ? 'warn' : serviceTone(svc);
        const fix = working ? null : fixVerb(svc, stackUp);
        const state = working ? `${(ING[working] ?? working).toLowerCase()}…` : serviceState(svc);
        const moreBtn = h('button', { class: 'ic', title: 'logs, links and actions', 'aria-label': `actions of ${svc.name}`, 'aria-haspopup': 'menu' }, h('i', { class: 'fi fi-more', 'aria-hidden': 'true' }));
        moreBtn.addEventListener('click', (e) => { e.stopPropagation(); openMenu(moreBtn, serviceItems(p, svc)); });
        const link = tone === 'ok' ? svc.links?.[0] : undefined; // a stopped service's URL: in its ⋯ only
        return h('li', { class: `svc ${tone}${fix ? ' problem' : ''}` }, working ? h('span', { class: 'spinner sdot-spin', 'aria-hidden': 'true' }) : h('span', { class: `sdot ${tone}`, title: `${svc.state}${svc.health ? ' · ' + svc.health : ''}` }), h('span', { class: 'sname', title: svc.ports?.length ? `ports ${svc.ports.join(', ')}` : '' }, svc.name, h('span', { class: `sstate ${tone}` }, state ? ` ${state}` : '')), h('span', { class: 'sacts' }, link ? h('a', { class: 'ic', href: link, target: '_blank', rel: 'noopener noreferrer', title: link, 'aria-label': `open ${svc.name}` }, '↗') : null, h('button', { class: 'ic', title: 'log tail', 'aria-label': `logs of ${svc.name}`, onclick: () => openLogs(p, svc.name) }, '≡'), fix ? h('button', { class: 'mini fix', title: `${fix} ${svc.name}`, onclick: () => act(p, fix, svc.name) }, VERB_LABEL[fix]) : null, moreBtn));
    }
    /** "build · 0:42" / "rc 1 · 1:57" */
    function jobMeta(r) {
        const j = r.job;
        const secs = (r.ended || Date.now()) / 1000 - j.started;
        return [j.state === 'running' && j.stage ? j.stage : '', j.state !== 'running' && j.rc !== null && j.rc !== 0 ? `rc ${j.rc}` : '',
            j.started && r.timed ? elapsed(secs) : ''].filter(Boolean).join(' · ');
    }
    /** the running job's clock moves without a redraw (a redraw would take the focus away) */
    function tick() {
        for (const r of shownRuns(project)) {
            const m = body.querySelector(`.stackjob[data-job="${r.job.id}"] .jm`);
            if (m)
                m.textContent = markBidi(jobMeta(r));
        }
    }
    function jobBox(p, r) {
        const j = r.job;
        const kind = j.state === 'running' ? 'run' : j.state === 'done' ? 'ok' : 'bad';
        const meta = jobMeta(r);
        const lines = kind === 'bad' && j.error ? tailLines(j.error, 4) : kind === 'run' ? tailLines(r.log) : [];
        const acts = h('span', { class: 'jacts' }, h('button', { class: 'mini', title: 'the whole output', onclick: () => openJobLog(p, r) }, 'Log'), kind === 'run' ? h('button', { class: 'mini', title: 'stop the job after its current step', onclick: () => void cancelJob(p, r) }, 'Cancel') : null, kind !== 'run' ? h('button', { class: 'ic', title: 'dismiss', 'aria-label': 'dismiss', onclick: () => { r.dismissed = true; dismissJob(r.job.id); draw(); } }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' })) : null);
        return h('div', { class: `stackjob ${kind}`, 'data-job': j.id, role: kind === 'bad' ? 'alert' : 'status' }, h('div', { class: 'jh' }, h('span', { class: kind === 'run' ? 'spinner' : `jdot ${kind}`, 'aria-hidden': 'true' }), h('b', { class: 'jt', title: jobTitle(j, r.service) }, jobTitle(j, r.service)), acts), meta ? h('div', { class: 'jm mut small' }, meta) : null, lines.length ? h('pre', { class: 'jtail' }, lines.join('\n')) : null);
    }
    // ---------------------------------------------------------------- log views
    let logView = null;
    function openJobLog(p, r) {
        const pre = h('pre', { class: 'logs' });
        const state = h('span', { class: 'mut small grow' });
        const render = () => {
            if (!pre.isConnected && logView) {
                logView = null;
                return;
            }
            const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
            pre.textContent = markBidi((r.cut ? `(the first part of the output is not shown - the job wrote more than ${JOB_LOG_MAX >> 10} KiB)\n` : '')
                + (r.log || (r.job.state === 'running' ? 'waiting for output…' : r.job.error || '(no output)')));
            state.textContent = markBidi(`${jobTitle(r.job, r.service)} · ${r.job.state}${r.job.stage ? ' · ' + r.job.stage : ''}`);
            if (atBottom)
                pre.scrollTop = pre.scrollHeight;
        };
        logView = { run: r, render };
        deps.modal(`Job: ${jobTitle(r.job, r.service)}`, pre, h('div', { class: 'row' }, state, h('button', { onclick: () => { logView = null; deps.closeModal(); } }, 'Close')));
        render();
        pre.scrollTop = pre.scrollHeight;
        if (r.job.state !== 'running' && !r.log)
            void fetchJob(p, r).then(render);
    }
    function openLogs(p, service) {
        const pre = h('pre', { class: 'logs' }, 'loading…');
        const search = h('input', { type: 'search', placeholder: 'Search the log (matching lines only)…', autocomplete: 'off' });
        const auto = h('input', { type: 'checkbox' });
        const tail = h('select', { title: 'number of lines' });
        for (const n of [300, 1000, 2000])
            tail.append(h('option', { value: String(n) }, `${n} lines`));
        const count = h('span', { class: 'mut small grow' });
        let text = '';
        let timer = 0;
        const render = () => {
            const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
            const prev = pre.scrollTop;
            const lines = filterLog(text, search.value);
            clear(pre);
            for (const l of lines)
                pre.append(...marked(l, search.value), '\n');
            count.textContent = markBidi(search.value ? `${lines.length} ${lines.length === 1 ? 'match' : 'matches'}` : `${lines.length} ${lines.length === 1 ? 'line' : 'lines'}`);
            pre.scrollTop = atBottom ? pre.scrollHeight : prev;
        };
        const fetchLog = async () => {
            try {
                text = await stackLogs(p, service, Number(tail.value));
            }
            catch (e) {
                text = String(e instanceof Error ? e.message : e);
            }
            if (pre.isConnected)
                render();
        };
        search.addEventListener('input', render);
        tail.addEventListener('change', () => void fetchLog());
        auto.addEventListener('change', () => {
            window.clearInterval(timer);
            // the overlay can close without telling us (Esc): the timer stops itself
            if (auto.checked)
                timer = window.setInterval(() => { if (!pre.isConnected)
                    window.clearInterval(timer);
                else
                    void fetchLog(); }, LOG_MS);
        });
        deps.modal(`Log: ${service}`, h('div', { class: 'row left' }, search, tail, h('label', { class: 'small nowrap' }, auto, 'refresh every 3 s'), h('button', { class: 'mini', title: 'reload', 'aria-label': 'reload the log', onclick: () => void fetchLog() }, fi('refresh', 'gi'))), pre, h('div', { class: 'row' }, count, h('button', { onclick: () => { window.clearInterval(timer); deps.closeModal(); } }, 'Close')));
        void fetchLog().then(() => { pre.scrollTop = pre.scrollHeight; });
        search.focus();
    }
    return {
        el,
        /** the project of the shown session; refetches when the data is older than 10 s */
        show(p) {
            if (p !== project) {
                project = p;
                sig = '';
                closeMenu();
                draw();
            }
            void load(p);
            draw(); // the job box's elapsed time and the done box's expiry move without new data
        },
    };
}
//# sourceMappingURL=stack.js.map