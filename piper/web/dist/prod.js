// Overview section "Production": the morning production check.
// piperd runs one short hole session with the observability plugin every workday morning;
// this shows its last report - new errors on top, then rate jumps and alerts, the known
// ones folded - with Investigate (a project session with the summary) and Mute per item,
// "Check now", and a failure with Try again. F16: a check older than 24 h is an amber bar
// ("Last check 3 days ago … data is stale" + Check now); row actions on hover (Investigate · Mute ▾ · Grafana ↗). Options live in $PIPER_HOME/obs/config.json
// (the settings page), the mapping as "obs" in projects.json. Styles are the inbox's.
import { call } from './api.js';
import { clear, fi, h } from './dom.js';
import { toggleMenu } from './menu.js';
import { markBidi } from './text.js';
const KIND_ORDER = { alert: 0, error: 1, rate: 2 };
const KIND_LABEL = { error: 'error', rate: 'error rate', alert: 'alert' };
const DAY = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
/** new (unmuted) first, then the known ones, then the muted; within a group alerts, errors, rates, by count */
export function prodGroups(items) {
    const by = (a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || b.count - a.count || a.title.localeCompare(b.title);
    return {
        fresh: items.filter((i) => i.new && !i.muted).sort(by),
        known: items.filter((i) => !i.new && !i.muted).sort(by),
        muted: items.filter((i) => i.muted).sort(by),
    };
}
/** "Mon–Fri 07:30" / "Mon, Wed 06:00" */
export function scheduleLabel(days, time) {
    const d = [...days].sort((a, b) => a - b);
    const run = d.length > 2 && d.every((x, i) => i === 0 || x === d[i - 1] + 1);
    return `${run ? `${DAY[d[0]]}–${DAY[d[d.length - 1]]}` : d.map((x) => DAY[x]).join(', ')} ${time}`;
}
export const STALE_AFTER = 24 * 3600;
/** F16: how old the last check is - stale over 24 h ("Last check 3 days ago (Oct 16, 15:27) — data is stale"),
 *  else just "checked 2 h ago". `at` and `now` in seconds. */
export function freshness(at, now, stamp = defaultStamp) {
    const age = Math.max(0, now - at);
    if (age <= STALE_AFTER)
        return { stale: false, text: `checked ${age < 60 ? 'just now' : age < 3600 ? `${Math.floor(age / 60)} min ago` : `${Math.floor(age / 3600)} h ago`}` };
    const days = Math.floor(age / 86400);
    const rel = days >= 2 ? `${days} days ago` : `${Math.floor(age / 3600)} h ago`;
    return { stale: true, text: `Last check ${rel} (${stamp(at)}) — data is stale` };
}
function defaultStamp(at) {
    return new Date(at * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}
/** F16: the quiet second line of a row - project · count / 24 h · place (fingerprint) · since · trend; an
 *  error needs no kind word (almost all are errors), an alert / a rate jump says so */
export function rowFacts(it) {
    return {
        kind: it.kind === 'error' ? '' : KIND_LABEL[it.kind],
        facts: [it.title_project ?? it.project, it.count ? `${it.count}× / 24 h` : '', it.first_seen ? `since ${it.first_seen}` : '', it.trend].filter(Boolean),
        place: it.fingerprint && it.fingerprint !== it.title ? it.fingerprint : '',
    };
}
/** only an https link is ever an href (piperd already drops the rest) */
export function safeUrl(u) {
    return /^https:\/\/[^\s"'<>]+$/.test(u) ? u : '';
}
export function createProd(deps) {
    let data = null;
    let summary;
    let lastV = null;
    let fetching = false;
    let loadErr = ''; // the last GET /api/prod failed: said in the status line (never swallowed)
    let prevNew = -1;
    let prevReport;
    const busy = new Set();
    const el = h('section', { class: 'inbox prod hidden', 'aria-label': 'Production' });
    const title = h('h3', { class: 'ibx-title' }, 'Production');
    const when = h('span', { class: 'ibx-updated mut small', role: 'status' });
    const runBtn = h('button', { class: 'small', title: 'run the morning production check now', onclick: () => void run('check') }, 'Check now');
    const head = h('div', { class: 'ibx-head' }, title, when, h('span', { style: 'flex:1' }), runBtn);
    // F16: the last check is older than 24 h - said in amber, Check now becomes the primary action
    const staleText = h('span', { class: 'prod-stale-t' });
    const stale = h('div', { class: 'prod-stale hidden', role: 'status' }, h('span', { class: 'prod-stale-dot', 'aria-hidden': 'true' }), staleText);
    const statusEl = h('div', { class: 'ibx-status' });
    const list = h('div', { class: 'ibx-list' });
    const foot = h('p', { class: 'ibx-keys mut small' });
    el.append(head, stale, statusEl, list, foot);
    async function load() {
        if (fetching)
            return;
        fetching = true;
        try {
            const r = await call('GET', '/api/prod');
            if (r.data)
                data = r.data;
            loadErr = '';
        }
        catch (e) {
            loadErr = e instanceof Error ? e.message : String(e); // the next /api/state change refetches; Try again now
        }
        finally {
            fetching = false;
        }
        draw();
    }
    async function post(op, body, key) {
        busy.add(key);
        draw();
        try {
            const r = await call('POST', `/api/prod/${op}`, body);
            return r.data;
        }
        catch (e) {
            deps.toast(`Production: ${e.message}`);
            return null;
        }
        finally {
            busy.delete(key);
            deps.kick();
            void load();
        }
    }
    async function run(mode) {
        const r = await post('run', { mode }, 'run');
        if (r?.session)
            deps.toast(mode === 'check' ? 'Production check running' : 'Looking for project mappings');
    }
    async function investigate(it) {
        const r = await post('investigate', { key: it.key }, it.key);
        if (r?.id)
            deps.openSession(r.id);
    }
    function openMute(anchor, it) {
        toggleMenu(anchor, () => [
            { label: 'For a week', hint: 'not new for 7 days', run: () => void post('mute', { key: it.key, days: 7 }, it.key) },
            { label: 'Forever', hint: 'never new again', run: () => void post('mute', { key: it.key, days: 0 }, it.key) },
        ], { label: `mute ${it.title}`, side: 'right' });
    }
    function row(it) {
        const link = safeUrl(it.url);
        const f = rowFacts(it);
        const l2 = h('div', { class: 'irow-l2' }, f.kind ? h('span', { class: `prod-kind k-${it.kind}` }, f.kind) : null, ...f.facts.map((x, i) => h('span', { class: i === 0 ? 'prod-proj' : 'prod-fact', title: i === 0 ? `observability: ${it.obs}` : undefined }, markBidi(x))), f.place ? h('span', { class: 'prod-place', title: 'fingerprint' }, markBidi(f.place)) : null);
        const main = h('div', { class: 'irow-main', title: it.detail || it.title }, h('span', { class: `prod-dot k-${it.kind}`, 'aria-hidden': 'true' }), h('div', { class: 'irow-text' }, h('div', { class: 'irow-l1' }, h('span', { class: 'ititle' }, it.title)), l2), h('span', { class: 'prod-n', title: 'occurrences in the last 24 h' }, it.count ? `${it.count}×` : ''));
        const dis = busy.has(it.key);
        const muteBtn = h('button', { class: 'small', disabled: dis, 'aria-haspopup': 'menu', title: 'not shown as new: for a week or forever',
            onclick: (e) => openMute(e.currentTarget, it) }, 'Mute', fi('chev-d'));
        const act = h('div', { class: 'irow-act row-actions' }, h('button', { class: 'small trunc prod-inv', disabled: dis, title: 'open a session in the project: find the cause, a fix and a regression test', onclick: () => void investigate(it) }, 'Investigate'), it.muted ? h('button', { class: 'small', disabled: dis, onclick: () => void post('unmute', { key: it.key }, it.key) }, 'Unmute') : muteBtn, link ? h('a', { class: 'btn small ghost prod-graf', href: link, target: '_blank', rel: 'noopener noreferrer', title: 'open in Grafana', 'aria-label': 'open in Grafana' }, fi('ext')) : null);
        return h('div', { class: `irow ra-row${it.new && !it.muted ? ' fresh' : ''}${it.muted ? ' muted' : ''}${dis ? ' busy' : ''}`, role: 'listitem' }, main, act);
    }
    function group(label, items, open, extra) {
        if (!items.length)
            return null;
        const rows = h('div', { class: 'ibx-rows', role: 'list' }, ...items.map(row));
        const headKids = [label, ' · ', h('span', { class: 'n' }, String(items.length)), extra ? h('span', { style: 'flex:1' }) : null, extra];
        if (open)
            return h('div', { class: 'ibx-group g-new' }, h('div', { class: 'ibx-ghead sec-h' }, ...headKids), rows);
        return h('details', { class: 'ibx-group' }, h('summary', { class: 'ibx-ghead sec-h' }, ...headKids), rows);
    }
    function statusLine(d) {
        clear(statusEl);
        statusEl.className = 'ibx-status';
        if (d.status === 'running') {
            statusEl.append(h('span', {}, d.mode === 'suggest' ? 'Looking for project mappings to observability… ' : 'Production check running… '), d.session ? h('button', { class: 'link small', onclick: () => deps.select(d.session) }, 'show session') : '');
        }
        else if (d.status === 'waiting') {
            statusEl.classList.add('info');
            statusEl.append(h('span', {}, 'The check is waiting for free slots (it needs 2) - it starts when a session ends.'));
        }
        else if (d.status === 'error') {
            statusEl.classList.add('bad');
            statusEl.append(h('span', {}, 'The check failed: '), h('span', {}, d.error ?? ''), ' ', h('button', { class: 'small', disabled: busy.has('run'), onclick: () => void run(d.mode === 'suggest' ? 'suggest' : 'check') }, 'Try again'));
        }
        if (loadErr)
            statusEl.append(h('p', { class: 'mut small' }, `Not refreshed: ${loadErr} `, h('button', { class: 'link small', onclick: () => void load() }, 'try again')));
        if (d.config_error)
            statusEl.append(h('p', { class: 'mut small' }, `The settings cannot be used, the defaults apply: ${d.config_error}`));
        const toSettings = (label, filter) => h('button', { class: 'link small', onclick: () => deps.openSettings('projects', filter) }, label);
        if (!d.mapping.length) {
            statusEl.classList.add('info');
            statusEl.append(h('p', {}, 'The morning check is off: no project has an observability mapping. ', toSettings('Set it up in Settings → Projects')));
        }
        // F16: the suggestions are a link in the first section heading; only without any section a line here
        if (d.suggestions.length && !(d.report?.items.length))
            statusEl.append(h('p', { class: 'small' }, suggLink(d)));
    }
    /** "9 mapping suggestions →": the list itself is in Settings → Projects (Use / Don't map per project) */
    function suggLink(d) {
        const n = d.suggestions.length;
        return h('button', { class: 'link small prod-sugg', title: 'review in Settings → Projects', onclick: (e) => { e.preventDefault(); deps.openSettings('projects', 'suggested'); } }, `${n} mapping ${n === 1 ? 'suggestion' : 'suggestions'}`, fi('chev-r'));
    }
    function draw() {
        el.classList.toggle('hidden', !summary);
        if (!summary)
            return;
        if (!data) { // never loaded: the failure, not an empty view
            clear(statusEl);
            statusEl.className = 'ibx-status' + (loadErr ? ' bad' : '');
            if (loadErr)
                statusEl.append(h('span', {}, `Production could not be loaded: ${loadErr} `), h('button', { class: 'small', onclick: () => void load() }, 'Try again'));
            return;
        }
        const d = data;
        runBtn.disabled = d.status === 'running' || busy.has('run') || !d.mapping.length;
        const rep = d.report;
        const fr = rep ? freshness(rep.at, Date.now() / 1000) : null;
        const isStale = !!fr?.stale && d.status !== 'running' && d.status !== 'waiting';
        when.textContent = markBidi(fr && !isStale ? fr.text : '');
        staleText.textContent = markBidi(isStale ? fr.text : '');
        stale.classList.toggle('hidden', !isStale);
        runBtn.classList.toggle('primary', isStale);
        (isStale ? stale : head).append(runBtn); // the bar's action when stale, else the header's
        const g = prodGroups(rep?.items ?? []);
        statusLine(d);
        clear(list);
        const errs = (rep?.errors ?? []).map((e) => h('p', { class: 'mut small' }, `${e.project}: query failed - ${e.error}`));
        const sugg = d.suggestions.length ? suggLink(d) : null;
        const first = g.fresh.length ? 'new' : g.known.length ? 'known' : 'muted';
        const groups = [group('New', g.fresh, true, first === 'new' ? sugg : null), group('Known', g.known, false, first === 'known' ? sugg : null),
            group('Muted', g.muted, false, first === 'muted' ? sugg : null)].filter(Boolean);
        if (rep && !rep.items.length)
            list.append(h('div', { class: 'ibx-empty mut' }, fi('check'), 'No errors or alerts in the last check.'));
        else if (!rep && d.mapping.length)
            list.append(h('div', { class: 'ibx-empty mut' }, 'No check yet.'));
        list.append(...groups, ...errs);
        foot.textContent = markBidi(d.mapping.length
            ? `${d.enabled ? `Automatic ${scheduleLabel(d.days, d.time)}` : 'The automatic check is off'} · ${d.mapping.map((m) => m.title).join(', ')}`
            : '');
    }
    /** /api/state `prod`: refetch when `v` moved; a notification when today's check brought new items */
    function sync(s) {
        summary = s;
        if (!s) {
            draw();
            return;
        }
        if (prevNew >= 0 && s.new > prevNew && s.report_at !== prevReport) {
            const n = s.new;
            deps.notify('Production', `${n} ${n === 1 ? 'new error or alert' : 'new errors or alerts'} from the morning check`, 'piper-prod', () => deps.showBoard());
        }
        prevNew = s.new;
        prevReport = s.report_at;
        if (s.v !== lastV) {
            lastV = s.v;
            void load();
        }
        else {
            draw();
        }
    }
    return { el, sync, redraw: () => void load(), run: (mode = 'check') => void run(mode) };
}
//# sourceMappingURL=prod.js.map