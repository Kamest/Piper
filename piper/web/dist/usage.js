// Token usage: per day (last 30) and per project over all
// visible sessions' transcripts (GET /api/usage, piperd counts each message id once).
// A global view; bars are plain divs scaled to the busiest day.
import { api } from './api.js';
import { fmtTokens } from './convo.js';
import { clear, fi, h } from './dom.js';
import { markBidi } from './text.js';
export const PARTS = ['input', 'output', 'cache_write', 'cache_read'];
const PART_LABEL = { input: 'input', output: 'output', cache_write: 'cache write', cache_read: 'cache read' };
/** the tokens of a row, optionally without cache reads (they dwarf everything else) */
export function usageTotal(r, withCacheRead = true) {
    return r.input + r.output + r.cache_write + (withCacheRead ? r.cache_read : 0);
}
/** bar segments of a row in % of `max` (0 when max is 0) */
export function barParts(r, max, withCacheRead = true) {
    return PARTS.filter((p) => withCacheRead || p !== 'cache_read')
        .map((p) => ({ part: p, pct: max > 0 ? (r[p] / max) * 100 : 0 }));
}
const zero = () => ({ input: 0, output: 0, cache_read: 0, cache_write: 0 });
function ymd(t) {
    return new Date(t).toISOString().slice(0, 10);
}
/** today as the local YYYY-MM-DD (piperd buckets by local date) */
export function localDay(d = new Date()) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
/** the last n days ending with `today`, newest first, days without usage as zero rows */
export function fillDays(days, n, today) {
    const by = new Map(days.map((d) => [d.day, d]));
    const end = Date.parse(today + 'T00:00:00Z');
    const out = [];
    for (let i = 0; i < n; i++) {
        const day = ymd(end - i * 86400000);
        out.push(by.get(day) ?? { ...zero(), day });
    }
    return out;
}
/** F20: the days newest first, every run of days without a token as one "gap" (from = the oldest day) */
export function dayRuns(days) {
    const out = [];
    for (const d of days) {
        const prev = out[out.length - 1];
        if (usageTotal(d))
            out.push({ kind: 'day', d });
        else if (prev?.kind === 'gap') {
            prev.from = d.day;
            prev.n++;
        }
        else
            out.push({ kind: 'gap', from: d.day, to: d.day, n: 1 });
    }
    return out;
}
/** "Oct 19" (a YYYY-MM-DD read as that calendar day, whatever the time zone) */
export function dayLabel(day) {
    return new Date(day + 'T12:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}
/** "Oct 14 · no use", "Oct 9 – Oct 14 · 6 days without use" */
export function gapText(g) {
    return g.n === 1 ? `${dayLabel(g.from)} · no use` : `${dayLabel(g.from)} – ${dayLabel(g.to)} · ${g.n} days without use`;
}
/** the heading's summary: "≈ 408 M over 6 days of use" */
export function usageSummary(days) {
    const used = days.filter((d) => usageTotal(d) > 0);
    const sum = used.reduce((a, d) => a + usageTotal(d), 0);
    return used.length ? `≈ ${fmtTokens(sum)} over ${used.length} ${used.length === 1 ? 'day' : 'days'} of use` : 'no use in 30 days';
}
/** the exact numbers of a day (the bar's tooltip) */
export function partsTitle(r) {
    return [...PARTS.map((p) => `${PART_LABEL[p]} ${r[p].toLocaleString()}`), `total ${usageTotal(r).toLocaleString()}`].join(' · ');
}
/** nothing to chart yet: no day with usage and no project with a token - sessions that have not answered
 *  yet are listed with zeros, and 30 rows of zeros are not a report (N7-1) */
export function noUsage(r) {
    return !r.days.length && !r.projects.some((p) => usageTotal(p) > 0);
}
/** D16: what an empty Usage page says instead of a lone checkbox */
export const USAGE_EMPTY = [
    'No token usage yet.',
    'Usage counts the tokens of every Piper session from its transcript: per day for the last 30 days and per project for all time. The numbers appear once a session has answered.',
    'The Claude plan limits (5 h / week) are under the sessions while this module is on.',
];
export function createUsage() {
    const el = h('div', { class: 'usage' });
    const withCache = h('input', { type: 'checkbox' });
    const status = h('span', { class: 'mut small' });
    const body = h('div', {});
    const cacheLabel = h('label', { class: 'small nowrap', title: 'cache reads are an order of magnitude bigger than the rest: without them the other parts are visible' }, withCache, 'cache reads in the chart');
    const reload = h('button', { class: 'ghost icon', title: 'count again', 'aria-label': 'count again', onclick: () => void load() }, fi('refresh'));
    const head = h('div', { class: 'bhead' }, status, reload); // F20: the cache switch sits right of the legend, refresh in the heading
    el.append(head, body);
    let data = null;
    let loading = false;
    withCache.addEventListener('change', () => draw());
    async function load() {
        status.textContent = '';
        loading = true;
        draw();
        try {
            data = (await api.usage()).data;
        }
        catch (e) {
            status.textContent = markBidi(String(e instanceof Error ? e.message : e));
        }
        loading = false;
        draw();
    }
    const num = (n) => h('td', { class: 'num', title: n.toLocaleString() }, fmtTokens(n));
    const cells = (r) => [num(r.input), num(r.output), num(r.cache_write), num(r.cache_read), num(usageTotal(r))];
    const headRow = (first, ...extra) => h('tr', {}, h('th', {}, first), ...extra.map((x) => h('th', {}, x)), ...PARTS.map((p) => h('th', { class: 'num' }, PART_LABEL[p])), h('th', { class: 'num' }, 'total'));
    function bar(r, max) {
        const b = h('div', { class: 'ubar', title: partsTitle(r) });
        for (const x of barParts(r, max, withCache.checked)) {
            const seg = h('span', { class: `u-${x.part}` });
            seg.style.width = `${x.pct.toFixed(2)}%`;
            b.append(seg);
        }
        return b;
    }
    function draw() {
        clear(body);
        head.append(reload); // back in the head until there is a heading to sit in
        cacheLabel.hidden = !data || noUsage(data); // nothing to chart: no chart option
        head.hidden = !!data && noUsage(data); // the empty state carries its own Load again
        if (!data) {
            body.append(h('p', { class: 'mut' }, ...(loading ? [h('span', { class: 'spinner' }), ' Going through the transcripts of all sessions - a long history takes a few seconds.']
                : ['Could not load - the refresh button tries again.'])));
            return;
        }
        if (noUsage(data)) { // not 30 rows of zeros
            body.append(h('div', { class: 'uempty' }, h('p', {}, h('b', {}, USAGE_EMPTY[0])), h('p', { class: 'mut' }, USAGE_EMPTY[1]), h('p', { class: 'mut' }, USAGE_EMPTY[2]), h('button', { onclick: () => void load() }, 'Load again')));
            return;
        }
        const wc = withCache.checked;
        const days = fillDays(data.days, 30, localDay());
        const maxDay = Math.max(0, ...days.map((d) => usageTotal(d, wc)));
        // F20: a day = day · stacked bar · total (the exact parts in the tooltip); days without use folded into one line
        const legend = h('div', { class: 'ulegend small' }, ...PARTS.filter((p) => wc || p !== 'cache_read')
            .map((p) => h('span', {}, h('span', { class: `usw u-${p}` }), PART_LABEL[p])));
        const t = h('div', { class: 'udays', role: 'list' });
        for (const r of dayRuns(days)) {
            if (r.kind === 'gap') {
                t.append(h('div', { class: 'ugap', role: 'listitem' }, markBidi(gapText(r))));
                continue;
            }
            const d = r.d;
            t.append(h('div', { class: 'uday', role: 'listitem', title: partsTitle(d) }, h('span', { class: 'uday-d', title: d.day }, markBidi(dayLabel(d.day))), bar(d, maxDay), h('span', { class: 'uday-n' }, fmtTokens(usageTotal(d)))));
        }
        const projects = [...data.projects].sort((a, b) => usageTotal(b) - usageTotal(a));
        const maxProject = Math.max(0, ...projects.map((p) => usageTotal(p, wc)));
        const pt = h('table', { class: 'utable' }, headRow('project', 'sessions', 'chart'));
        for (const p of projects) {
            pt.append(h('tr', {}, h('td', { class: 'nowrap' }, p.project || '(Global agent)'), h('td', { class: 'num' }, String(p.sessions)), h('td', { class: 'ubarcell' }, bar(p, maxProject)), ...cells(p)));
        }
        if (projects.length)
            pt.append(h('tr', { class: 'utotal' }, h('td', {}, 'all time'), h('td', {}), h('td', { class: 'mut small' }, 'every transcript of Piper sessions'), ...cells(data.total)));
        head.hidden = !status.textContent; // only an error to say: the heading carries refresh
        body.append(h('div', { class: 'usec-h' }, h('h3', { class: 'sec-h' }, 'Usage · 30 days'), h('span', { class: 'usum' }, markBidi(usageSummary(days))), reload), h('div', { class: 'ulegrow' }, legend, cacheLabel), t, h('h3', {}, 'By project (all time)'), pt);
        if (!projects.length)
            body.append(h('p', { class: 'mut' }, 'No data.'));
    }
    return {
        el,
        show() {
            draw();
            void load();
        },
    };
}
//# sourceMappingURL=usage.js.map