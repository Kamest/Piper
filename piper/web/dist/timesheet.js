// Timesheet: the user's attention per issue and day, from
// GET /api/timesheet (piperd builds the blocks). Overview only: nothing is sent to GitLab or
// elsewhere - "Copy" puts text on the clipboard for the company's own system.
// Raw time is always shown; rounding is applied only to what is copied.
// The focus beat (startFocusBeat) tells piperd which session the user looks at while active.
import { call } from './api.js';
import { clear, fi, h } from './dom.js';
import { markBidi } from './text.js';
const ROUND_LABEL = { none: 'no rounding', up: 'up to ¼ h', down: 'down to ¼ h', nearest: 'nearest ¼ h' };
const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
/** minutes rounded to a step (15 min) - only for what is copied */
export function roundMinutes(min, mode, step = 15) {
    if (mode === 'none' || min <= 0)
        return Math.max(0, min);
    const q = min / step;
    const n = mode === 'up' ? Math.ceil(q - 1e-9) : mode === 'down' ? Math.floor(q + 1e-9) : Math.round(q);
    return n * step;
}
/** "1,25 h" (Czech decimal comma); "0 h" for nothing */
/** why a manual row cannot be added ('' = it can): the same limits as piperd's ts_manual */
export function manualError(label, minutes) {
    if (!label.trim())
        return 'Say what the row is for (a meeting, a call…)';
    const m = Number(minutes);
    if (!Number.isInteger(m) || m < 1 || m > 1440)
        return 'Minutes: a whole number from 1 to 1440';
    return '';
}
/** N9: the minutes of a cell's own edit - 0..1440 whole (0 = no time that day); an empty / out-of-range field used to be
 *  saved silently clamped (empty or letters → 0:00, the day's computed time gone; 99999 → 24:00) */
export function cellError(minutes) {
    const m = Number(minutes);
    if (!minutes.trim() || !Number.isInteger(m) || m < 0 || m > 1440)
        return 'Minutes: a whole number from 0 to 1440 (Restore computed brings back the computed time)';
    return '';
}
/** N9: Enter in a field of a timesheet form does what its button does (it did nothing) */
function enterRuns(fields, run) {
    for (const f of fields)
        f.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) {
            e.preventDefault();
            run();
        } });
}
export function fmtHours(min) {
    const hrs = Math.round((min / 60) * 100) / 100;
    return `${String(hrs).replace('.', ',')} h`;
}
/** "0:45" for the raw time next to a value */
export function fmtClock(min) {
    const m = Math.round(min);
    return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`;
}
/** the value of a cell: the user's own minutes win over the computed ones */
export function cellMinutes(c) {
    return c.minutes ?? c.raw_min;
}
export function rowLabel(r) {
    if (r.key.startsWith('p:'))
        return `${r.key.slice(2)} · no issue`;
    const [gl, kind, iid] = r.key.split('#');
    const ref = `${gl}${kind === 'mr' ? '!' : '#'}${iid}`;
    return r.title ? `${r.title} (${ref})` : ref;
}
/** text for the company's system: one line per issue per day, rounded as chosen */
export function copyText(rep, days, mode) {
    const out = [];
    for (const day of days) {
        const lines = [];
        let total = 0;
        for (const r of rep.rows) {
            const c = r.days[day];
            if (!c)
                continue;
            const min = roundMinutes(cellMinutes(c), mode);
            if (min <= 0 && !(c.text ?? '').trim())
                continue;
            total += min;
            lines.push(`  ${rowLabel(r)}\t${fmtHours(min)}\t${(c.text ?? c.auto_text).trim()}`);
        }
        for (const m of rep.manual.filter((x) => x.day === day)) {
            const min = roundMinutes(m.minutes, mode);
            total += min;
            lines.push(`  ${m.label}\t${fmtHours(min)}\t${m.text.trim()}`);
        }
        if (lines.length)
            out.push(`${day} (${fmtHours(total)})`, ...lines);
    }
    return out.join('\n');
}
// ------------------------------------------------------------ focus beat
let lastActivity = 0;
const BEAT_MS = 60000;
const ACTIVE_MS = 120000;
/** POST /api/timesheet/focus about the shown session while the window has focus and the
 * user moved the mouse or typed within 2 min - never for an idle, open window */
export function startFocusBeat(shown, enabled) {
    const mark = () => { if (enabled())
        lastActivity = Date.now(); };
    for (const ev of ['keydown', 'pointerdown', 'wheel', 'pointermove'])
        window.addEventListener(ev, mark, { passive: true, capture: true });
    const beat = () => {
        if (!enabled())
            return;
        const sid = shown();
        if (!sid || document.visibilityState !== 'visible' || !document.hasFocus() || Date.now() - lastActivity > ACTIVE_MS)
            return;
        void call('POST', '/api/timesheet/focus', { session: sid }).catch(() => undefined);
    };
    window.setInterval(beat, BEAT_MS);
}
function loadMode() {
    try {
        const v = window.localStorage.getItem('piper.ts.round');
        if (v === 'none' || v === 'up' || v === 'down' || v === 'nearest')
            return v;
    }
    catch { /* no storage */ }
    return 'up';
}
export function createTimesheet(deps) {
    const el = h('div', { class: 'usage tsheet' });
    const status = h('span', { class: 'mut small' });
    const weekLabel = h('strong', {});
    const mode = h('select', { title: 'rounding only for the copied text; the table always shows the raw time' }, ...Object.keys(ROUND_LABEL).map((m) => h('option', { value: m }, ROUND_LABEL[m])));
    mode.value = loadMode();
    const body = h('div', {});
    let week = '';
    let data = null;
    let loading = false;
    let open = ''; // "day|key" of the expanded cell
    mode.addEventListener('change', () => {
        try {
            window.localStorage.setItem('piper.ts.round', mode.value);
        }
        catch { /* no storage */ }
        draw();
    });
    el.append(h('div', { class: 'bhead' }, h('button', { class: 'icon', title: 'previous week', 'aria-label': 'previous week', onclick: () => void go(data?.prev ?? '') }, h('i', { class: 'fi fi-chev-l', 'aria-hidden': 'true' })), weekLabel, h('button', { class: 'icon', title: 'next week', 'aria-label': 'next week', onclick: () => void go(data?.next ?? '') }, h('i', { class: 'fi fi-chev-r', 'aria-hidden': 'true' })), h('button', { onclick: () => void go(data?.current ?? '') }, 'This week'), h('label', { class: 'small nowrap' }, 'copy: ', mode), h('button', { onclick: () => data && copy(data.days, 'week') }, 'Copy week'), status, h('button', { title: 'recompute', 'aria-label': 'recompute', onclick: () => void load() }, fi('refresh', 'gi'))), body);
    async function go(w) {
        if (!w)
            return;
        week = w;
        open = '';
        await load();
    }
    async function load() {
        status.textContent = '';
        loading = true;
        draw();
        try {
            data = (await call('GET', `/api/timesheet?week=${encodeURIComponent(week)}`)).data;
            if (data) {
                week = data.week;
            }
        }
        catch (e) {
            status.textContent = markBidi(String(e instanceof Error ? e.message : e));
        }
        loading = false;
        draw();
    }
    async function post(op, payload) {
        try {
            await call('POST', `/api/timesheet/${op}`, payload);
            await load();
        }
        catch (e) {
            deps.toast(String(e instanceof Error ? e.message : e));
        }
    }
    function copy(days, what) {
        if (!data)
            return;
        const text = copyText(data, days, mode.value);
        if (!text) {
            deps.toast('Nothing to copy.');
            return;
        }
        const show = () => {
            const ta = h('textarea', { class: 'mono', rows: '14', readonly: true });
            ta.value = text;
            deps.modal(`Timesheet - ${what}`, h('p', { class: 'mut small' }, 'The clipboard is not available: select the text and copy it yourself.'), ta, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Close')));
            ta.select();
        };
        if (navigator.clipboard)
            void navigator.clipboard.writeText(text).then(() => deps.toast(`Timesheet (${what}) is on the clipboard.`), show);
        else
            show();
    }
    function settingsLine(st) {
        return h('p', { class: 'mut small' }, `${st.enabled ? 'Recording of prompts and focus on' : 'Recording of prompts and focus off (the timesheet is computed from turns, audit and commits)'}`
            + ` · joins events with a gap up to ${st.gap_min} min · block lead-in and lead-out ${st.pad_min} min. `, deps.openSettings ? h('button', { class: 'link small', onclick: () => deps.openSettings?.('timesheet') }, 'Change in Settings') : 'Changed in Settings.');
    }
    function detail(r, day, c) {
        const min = h('input', { type: 'number', min: '0', max: '1440', step: '5', class: 'num-in', value: String(Math.round(cellMinutes(c))) });
        const text = h('input', { type: 'text', value: c.text ?? c.auto_text, placeholder: 'description for the timesheet' });
        const save = () => { const why = cellError(min.value); if (why)
            return void deps.toast(why); void post('edit', { week, day, key: r.key, minutes: Number(min.value), text: text.value }); };
        enterRuns([min, text], save);
        const fmt = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
        return h('div', { class: 'ts-detail' }, h('div', { class: 'small' }, h('strong', {}, `${rowLabel(r)} · ${day}`)), h('ul', { class: 'small mono' }, ...c.blocks.map((b) => h('li', {}, `${fmt(b.start)}–${fmt(b.end)} · ${fmtClock(b.min)} · ${b.n} ${b.n === 1 ? 'event' : 'events'}`)), c.blocks.length ? null : h('li', { class: 'mut' }, 'a single event only (e.g. a commit): fill in the time yourself')), h('div', { class: 'row small' }, 'minutes ', min, text, h('button', { onclick: save }, 'Save'), c.minutes !== undefined || c.text !== undefined
            ? h('button', { class: 'link', onclick: () => void post('edit', { week, day, key: r.key, minutes: null, text: null }) }, 'Restore computed') : null), h('p', { class: 'mut small' }, `Computed: ${fmtClock(c.raw_min)} · automatic description: ${c.auto_text || '—'}`));
    }
    function manualForm(days) {
        const day = h('select', {}, ...days.map((d, i) => h('option', { value: d }, `${DAY_NAMES[i]} ${d.slice(5)}`)));
        const now = new Date(); // a row added this week is most often for today, not Monday
        const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        if (days.includes(today))
            day.value = today;
        const label = h('input', { type: 'text', placeholder: 'what (a meeting, a call with the client…)' });
        const min = h('input', { type: 'number', min: '1', max: '1440', step: '15', value: '30', class: 'num-in' });
        const text = h('input', { type: 'text', placeholder: 'description' });
        const add = () => {
            const why = manualError(label.value, min.value);
            if (why)
                return void deps.toast(why);
            void post('manual', { week, day: day.value, label: label.value, minutes: Number(min.value), text: text.value });
        };
        enterRuns([label, min, text], add);
        return h('div', { class: 'row small' }, day, label, min, 'min', text, h('button', { onclick: add }, 'Add row'));
    }
    function assignBox(u) {
        const key = h('input', { type: 'text', placeholder: 'group/project#123' });
        const assign = () => { if (key.value.trim())
            void post('assign', { session: u.id, key: key.value.trim() }); };
        enterRuns([key], assign);
        return h('li', { class: 'ts-assign' }, h('span', { class: 'ts-aname' }, u.name, u.project && u.project !== u.name ? h('span', { class: 'mut' }, ` ${u.project}`) : null), key, h('button', { onclick: assign }, 'Assign'));
    }
    function draw() {
        clear(body);
        weekLabel.textContent = markBidi(data ? `${data.week} · ${data.days[0]} – ${data.days[6]}` : week);
        if (!data) {
            body.append(h('p', { class: 'mut' }, ...(loading ? [h('span', { class: 'spinner' }), ' Computing blocks from turns, audit and commits…'] : ['Could not load - the refresh button tries again.'])));
            return;
        }
        const d = data;
        body.append(settingsLine(d.settings));
        const t = h('table', { class: 'utable tstable' }, h('tr', {}, h('th', {}, 'issue'), ...d.days.map((day, i) => h('th', { class: 'num' }, h('button', { class: 'link', title: `copy ${day}`, onclick: () => copy([day], day) }, `${DAY_NAMES[i]} ${day.slice(8)}.`))), h('th', { class: 'num' }, 'Σ')));
        const dayTotal = d.days.map(() => 0);
        const opened = { el: null };
        for (const r of d.rows) {
            let sum = 0;
            const tr = h('tr', {}, h('td', { title: r.sessions.map((s) => s.name).join(', ') || r.key }, rowLabel(r)));
            d.days.forEach((day, i) => {
                const c = r.days[day];
                if (!c) {
                    tr.append(h('td', { class: 'num mut' }, ''));
                    return;
                }
                const v = cellMinutes(c);
                sum += v;
                dayTotal[i] = (dayTotal[i] ?? 0) + v;
                const edited = c.minutes !== undefined;
                const k = `${day}|${r.key}`;
                tr.append(h('td', { class: 'num' + (open === k ? ' on' : '') }, h('button', { class: 'link' + (edited ? ' ts-edited' : ''), title: edited ? `edited, computed ${fmtClock(c.raw_min)}` : 'block detail and edit',
                    onclick: () => { open = open === k ? '' : k; draw(); } }, fmtClock(v)), edited ? h('div', { class: 'faint' }, fmtClock(c.raw_min)) : null));
                if (open === k)
                    opened.el = detail(r, day, c);
            });
            tr.append(h('td', { class: 'num' }, fmtClock(sum)));
            t.append(tr);
        }
        for (const m of d.manual) {
            const i = d.days.indexOf(m.day);
            if (i >= 0)
                dayTotal[i] = (dayTotal[i] ?? 0) + m.minutes;
        }
        t.append(h('tr', { class: 'utotal' }, h('td', {}, 'total (incl. manual rows)'), ...dayTotal.map((x) => h('td', { class: 'num' }, x ? fmtClock(x) : '')), h('td', { class: 'num' }, fmtClock(dayTotal.reduce((a, b) => a + b, 0)))));
        body.append(t);
        if (opened.el)
            body.append(opened.el);
        if (!d.rows.length)
            body.append(h('p', { class: 'mut' }, 'No work this week - no session turns, audit entries or commits of yours.'));
        body.append(h('h3', {}, 'Manual rows'));
        if (d.manual.length)
            body.append(h('ul', { class: 'small' }, ...d.manual.map((m) => h('li', {}, `${m.day} · ${m.label} · ${fmtClock(m.minutes)} ${m.text ? '· ' + m.text : ''} `, h('button', { class: 'link', onclick: () => void post('manual', { week, id: m.id, delete: true }) }, 'delete')))));
        body.append(manualForm(d.days));
        if (d.unassigned.length)
            body.append(h('h3', {}, 'Sessions without an issue'), h('ul', { class: 'small ts-assigns' }, ...d.unassigned.map(assignBox)));
        body.append(h('p', { class: 'mut small' }, 'Time = your attention (prompts, replies, confirmations, commits, comments, GUI focus), not the agents\' running time. Concurrent issues are split by their event counts. Nothing is sent - "Copy" puts the text on the clipboard.'));
    }
    return {
        el,
        show() {
            draw();
            void load();
        },
    };
}
//# sourceMappingURL=timesheet.js.map