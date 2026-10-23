// 9.141: sessions a restart interrupted. piperd marks what ptyhost lost (`interrupted`
// {at, was, cause, resumes}); the queue keeps them in their own section until restored or dismissed, and the
// first GUI after the restart offers once which to restore (/api/state `interrupted_notice`, POST
// /api/interrupted {seen}). Restore = Continue (sessions.retry) of each checked one, one after another; no
// queue over the session limit - a row without a free slot stays marked.
import { call } from './api.js';
import { h } from './dom.js';
import { markBidi } from './text.js';
/** unfinished work - what the card checks: it was working or waited for you */
export const UNFINISHED = new Set(['working', 'waiting', 'attention', 'trust']);
/** the state it had, in the user's words */
export function wasWord(was) {
    if (was === 'working')
        return 'was working';
    if (UNFINISHED.has(was))
        return 'waited for you';
    if (was === 'ready')
        return 'idle';
    return 'was starting';
}
/** what its restore does: Continue resumes the conversation, else it starts again with its task */
export const howWord = (resumes) => (resumes ? 'resumes' : 'starts again with its task');
/** the queue row's second line: "was working · 08:14 · resumes its conversation" */
export function interruptedStatus(i, at) {
    const d = new Date(at * 1000);
    const clock = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return [wasWord(i.was), clock, i.resumes ? 'resumes its conversation' : 'starts again with its task'].join(' · ');
}
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
/** the card's and the section's words for the cause */
export function causeTitle(cause, n) {
    if (cause === 'reboot')
        return `Your computer restarted — ${plural(n, 'session')} stopped`;
    if (cause === 'piperd')
        return `Piper restarted — ${plural(n, 'session')} did not start`;
    return `Piper's terminal host restarted — ${plural(n, 'session')} stopped`;
}
/** the section title in the queue */
export const sectionTitle = (causes) => (causes.length && causes.every((c) => c === 'reboot') ? 'Interrupted by restart' : 'Interrupted');
/** the checked rows: unfinished work, as many as there are free slots (the card's order) */
export function preselect(rows, free) {
    return new Set(rows.filter((r) => UNFINISHED.has(r.was)).slice(0, Math.max(0, free)).map((r) => r.id));
}
export const restoreLabel = (n) => (n ? `Restore ${plural(n, 'session')}` : 'Restore');
export function slotsLine(free, limit) {
    return free > 0
        ? `${free} of ${limit} session slots free (Settings › Sessions). Each restore starts a new sandbox; Docker must be running.`
        : `0 of ${limit} slots free. Stop a session or raise the limit in Settings › Sessions; the rest wait under Interrupted.`;
}
/** whether to open it now: once per notice id and page, never over the setup or another dialog */
export function restoreWanted(n, shownId, busy) {
    return !!n && !n.seen && n.sessions.length > 0 && n.id !== shownId && !busy;
}
const ROTATE = ['M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5'];
function icon(paths, cls) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', cls);
    for (const d of paths) {
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', d);
        svg.append(p);
    }
    return svg;
}
/** marks the notice seen at once (one time, also when closed with Esc or in another tab) and opens the card;
 *  `again`: opened from the section's Restore… (not marked, the kicker says nothing about once) */
export function showRestoreNotice(n, deps, again = false) {
    if (!again)
        void call('POST', '/api/interrupted', { seen: n.id }).catch(() => { });
    const free = Math.max(0, n.limit - deps.running());
    const chosen = preselect(n.sessions, free);
    const primary = h('button', { class: 'primary', 'data-restore': 'go' });
    const sync = () => {
        primary.textContent = markBidi(restoreLabel(chosen.size));
        primary.toggleAttribute('disabled', !chosen.size);
        for (const box of list.querySelectorAll('input[type=checkbox]')) {
            if (!box.checked)
                box.disabled = chosen.size >= free;
        }
    };
    const status = new Map();
    const list = h('div', { class: 'rn-list' }, ...n.sessions.map((r) => {
        const box = h('input', { type: 'checkbox', checked: chosen.has(r.id), 'data-sid': r.id, 'aria-label': `Restore ${r.title}`,
            onchange: () => { if (box.checked)
                chosen.add(r.id);
            else
                chosen.delete(r.id); sync(); } });
        const how = h('span', { class: 'rn-how' + (r.resumes ? '' : ' warn') }, howWord(r.resumes));
        status.set(r.id, how);
        const issue = r.issue ? `${r.issue.type === 'mr' ? '!' : '#'}${r.issue.iid}` : '';
        return h('label', { class: 'rn-row', 'data-sid': r.id }, box, h('span', { class: 'rn-text' }, h('b', {}, markBidi(r.title)), h('span', { class: 'rn-proj' }, markBidi([r.project.split('/').pop() || r.project, issue].filter(Boolean).join(' · ')))), h('span', { class: 'rn-was' + (UNFINISHED.has(r.was) ? ' warn' : '') }, wasWord(r.was)), how);
    }));
    const slots = h('p', { class: 'rn-slots' + (free ? '' : ' warn') }, slotsLine(free, n.limit));
    const later = h('button', { 'data-restore': 'later', onclick: deps.closeModal }, 'Later');
    primary.addEventListener('click', () => void (async () => {
        primary.setAttribute('disabled', '');
        later.textContent = 'Close';
        const ids = [...chosen];
        let ok = 0;
        for (const id of ids) { // one after another: each start reserves its slot before the next asks
            const how = status.get(id);
            if (how) {
                how.textContent = 'restoring…';
                how.className = 'rn-how';
            }
            const err = await deps.restore(id);
            if (!err)
                ok += 1;
            if (how) {
                how.textContent = markBidi(err || 'restored');
                how.className = 'rn-how ' + (err ? 'bad' : 'ok');
                how.title = markBidi(err);
            }
            const box = list.querySelector(`input[data-sid="${CSS.escape(id)}"]`);
            if (box)
                box.disabled = true;
            chosen.delete(id);
        }
        if (ok === ids.length) {
            deps.closeModal();
            deps.toast(`${plural(ok, 'session')} restored`);
        }
    })());
    sync();
    const title = causeTitle(n.cause, n.sessions.length);
    const at = new Date(n.at * 1000);
    deps.modal(title, h('div', { class: 'sn rn' }, h('div', { class: 'sn-head' }, h('span', { class: 'sn-tile rn-tile' }, icon(ROTATE, 'sn-ic')), h('span', { class: 'sn-head-text' }, h('b', { class: 'sn-title' }, title), h('span', { class: 'sn-kicker' }, [at.toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }),
        again ? '' : 'shown once', 'they stay under Interrupted until you decide'].filter(Boolean).join(' · ')))), h('p', { class: 'mut' }, 'Their sandboxes are gone. Worktrees, branches and conversations are kept, so a restored session continues where it was. Pick the ones to bring back now.'), list, slots, h('div', { class: 'row sn-actions' }, later, h('kbd', { class: 'rn-esc' }, 'Esc'), h('span', { class: 'grow' }), primary)));
}
//# sourceMappingURL=restorenotice.js.map