// The queue: one list of everything that wants you, beside the detail
// of what you picked. Sections: Needs you (blocking first, then the oldest) · New (GitLab,
// production, CI) · Running (the live sessions, ⌘1–9) · Recently (the Feed's events, when it is
// on) · Ended. The open actions come from piperd (/api/state `queue`, feed.derive_actions) with or
// without the Feed; the sessions, the Feed's events and the board cards (the last answer / prompt)
// fill in the rest. Keys work only while the queue has the focus (never in the terminal):
// j k ↑↓ move · Enter open · r reply · y n decide · e dismiss · m mute · a to the agent · / search.
// While you move through it the order freezes (30 s), so an item never jumps from under the key.
import { decisionButtons } from './decision.js';
import { kbd } from './clipkeys.js';
import { ago, clear, fi, h } from './dom.js';
import { dangerWord, KIND_TEXT, LEVEL, SOURCE_ICON } from './feed.js';
import { interruptedStatus, sectionTitle, UNFINISHED } from './restorenotice.js';
import { cutText, showBidi } from './text.js';
import { filesLabel } from './turns.js';
/** F17: how long a card that went unanswered stays a line in Needs you (piperd sessions.GONE_S) */
export const GONE_S = 10;
export const NEEDS_LEVELS = ['security', 'permission', 'confirm', 'blocked', 'waiting'];
/** sessions.ACTIVE: the states piperd stops (Stop / Cancel) and refuses to forget ("Remove") */
export const ACTIVE = new Set(['preflight', 'preparing', 'spawning', 'trust', 'starting', 'attesting', 'ready', 'working',
    'waiting', 'attention', 'stopping']);
/** Stop / Cancel starts teardown; while stopping, piperd already retries and escalates it. */
export const canStop = (s) => ACTIVE.has(s.state) && s.state !== 'stopping';
/** "Remove" (forget) is offered: piperd's forget() refuses only an ACTIVE session */
export const canForget = (s) => !ACTIVE.has(s.state);
/** the one rule for an ended session's button (sessions.retry resumes an exited session's conversation) */
export const continueLabel = (s) => (s.state === 'exited' && s.claude_session ? 'Continue' : 'Retry');
/** ⌘1–9 and the strip's tiles: one stable order of the sessions - pinned first, the Global agent, the user's
 *  order (drag, ⌘⇧↑↓), then the oldest. Waiting for you never moves a session (it is only a mark). */
export function sessionOrder(sessions, pins, order) {
    const rank = (s) => { const i = order.indexOf(s.id); return i < 0 ? Infinity : i; };
    return sessions.filter((s) => !s.hidden && s.kind !== 'prodcheck'
        && (ACTIVE.has(s.state) || (s.state === 'exited' && pins.has(s.id)))) // a pinned one stays up even when it ends
        .sort((a, b) => Number(!pins.has(a.id)) - Number(!pins.has(b.id)) || Number(b.kind === 'advisor') - Number(a.kind === 'advisor')
        || (rank(a) - rank(b) || 0) || a.created - b.created);
}
/** N10-A-8: the index `id` swaps with when moved by d in `list` (sessionOrder), -1 = the order cannot change: pinned
 *  sessions and the Global agent stay in front whatever the user's order says, so a swap across them did nothing */
export function moveTarget(list, pins, id, d) {
    const i = list.findIndex((s) => s.id === id);
    const a = list[i];
    const b = list[i + d];
    if (i < 0 || !a || !b)
        return -1;
    const group = (s) => `${pins.has(s.id)}:${s.kind === 'advisor'}`;
    return group(a) === group(b) ? i + d : -1;
}
/** B-N8-1: an allowed card whose new session could not start - piperd's own row (not the Feed's), shown and
 *  dismissable with or without the Feed; N10: the same for git's hidden files the net could not put back */
export const allowedFailed = (a) => !!a && (a.key.startsWith('allowed-failed:') || a.key.startsWith('net-failed:'));
/** 2026-10-10: the originals a net-failed row can restore from Piper's trash (over what git left there) */
export function netRestorable(a) {
    return a && a.key.startsWith('net-failed:') && a.project ? (a.trash ?? []).filter((t) => t && t.id && t.path) : [];
}
/** the toast after the row's Restore: what is back, what was refused and why (piperd's reason, e.g. the session still runs) */
export function restoreToast(done, failed) {
    const ok = done.length ? `Restored ${done.map(showBidi).join(', ')} from Piper's trash` : '';
    const bad = failed.map((f) => `${showBidi(f.path)} not restored: ${showBidi(f.error)}`).join(' · ');
    return [ok, bad].filter(Boolean).join(' · ') || 'Nothing to restore';
}
/** the Dismiss button and the `e` key: only where something can act on it - the Feed hides any open
 *  item; without it only GitLab can (marked read in the inbox) and piperd's failed allowed start */
export function canDismiss(it, feedOn) {
    const a = it.action;
    return !!a && ['blocked', 'ci', 'gitlab', 'prod'].includes(a.level) && (feedOn || a.source === 'gitlab' || allowedFailed(a));
}
const IDLE = ['ready', 'waiting', 'attention'];
/** piperd sends KILL this long after the first TERM (sessions._stop_nudge) */
export const KILL_AFTER_S = 30;
/** stopping longer than this is shown as stuck */
export const STUCK_S = 10;
export const FREEZE_MS = 30000;
export const sessionName = (s) => (s.kind === 'advisor' ? 'Global agent' : s.title);
/** F01: the project's last segment - a row's title starts with it (the Global agent: its name) */
export const projectShort = (s) => (s.kind === 'advisor' ? 'Global agent' : s.project.split('/').pop() || s.project);
/** F01: a session row's title "project — sentence": the sentence Claude ended its turn with (waiting), else the issue,
 *  the session's own title, the last prompt or the branch; nothing of these = the session's name */
export function rowTitle(s, sentence = '') {
    // N19b: piperd's summaries also lead with "Session grp/demo wants to …"
    for (const lead of [`Session ${s.project}`, `Session ${projectShort(s)}`, projectShort(s)])
        sentence = dropLead(sentence, lead);
    const issue = s.issue ? `${s.issue.type === 'mr' ? '!' : '#'}${s.issue.iid} ${s.issue.title}` : '';
    const generic = !s.title || s.title === s.project || s.title.split(' · ')[0] === projectShort(s) || s.title.split(' · ')[0] === s.project;
    if (s.kind === 'advisor')
        return sentence ? `Global agent — ${sentence}` : 'Global agent';
    const what = sentence || issue || (generic ? '' : s.title) || firstLine(s.last_prompt || s.first_prompt || s.task || '', 90) || s.branch || '';
    return what ? `${projectShort(s)} — ${what}` : sessionName(s);
}
/** N19: a card's summary that starts with the name the row already leads with ("Global agent wants to
 *  start a session") loses it - not "Global agent — Global agent wants…" */
export function dropLead(sentence, lead) {
    if (!lead || !sentence.startsWith(lead))
        return sentence;
    const rest = sentence.slice(lead.length).replace(/^:?\s+/, '');
    return rest && rest !== sentence.slice(lead.length) ? rest : sentence;
}
/** N19: "resolved: shop: wants to push" (feed.py) reads "shop: wants to push · resolved" in Recently */
export function recentTitle(title) {
    return title.startsWith('resolved: ') ? `${title.slice(10)} · resolved` : title;
}
/** "5 min" from `ts` to `now` (ago() of a pure model) */
export function agoAt(ts, now) {
    const t = Math.max(0, now - ts);
    return t < 60 ? 'now' : t < 3600 ? `${Math.floor(t / 60)} min` : t < 86400 ? `${Math.floor(t / 3600)} h` : `${Math.floor(t / 86400)} d`;
}
/** F01: two letters of a project's name - the first letters of its first two words (acme-shop AS), else its first two (shop2024 SH) */
export function monogram(name) {
    const words = name.split(/[^a-z0-9]+/i).filter((w) => /[a-z]/i.test(w));
    const two = words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? name).replace(/[^a-z]/gi, '').slice(0, 2);
    return (two || '?').toUpperCase();
}
/** F01: Your turn = a finished turn (level waiting); every other Needs you item blocks the agent (Blocked) */
export const isTurn = (it) => it.action?.level === 'waiting';
export function endedSessionName(s) {
    const generic = !s.title || s.title === s.project || s.title === s.project.split('/').at(-1);
    return s.kind === 'advisor' ? 'Global agent' : generic ? firstLine(s.first_prompt || s.task || s.last_prompt || '') || sessionName(s) : sessionName(s);
}
export function sessionDate(timestamp, now) {
    const date = new Date(timestamp * 1000), today = new Date(now * 1000);
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    const day = date.toDateString() === today.toDateString() ? 'Today' : date.toDateString() === yesterday.toDateString() ? 'Yesterday' :
        date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(date.getFullYear() !== today.getFullYear() ? { year: 'numeric' } : {}) });
    return `${day} ${date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })}`;
}
/** the first meaningful line of a text, cut for one row */
export function firstLine(t, n = 140) {
    const line = (t || '').split('\n').map((x) => x.trim()).find((x) => x && !/^\[tool /.test(x)) ?? '';
    const clean = line.replace(/^[#>*\-\s]+/, '').replace(/\*\*|__|`/g, '');
    return clean.length > n ? cutText(clean, n - 1) + '…' : clean;
}
/** Claude's last answer as the item's second line: its last sentence that asks, else its end */
export function askLine(last, n = 140) {
    const lines = (last || '').split('\n').map((x) => x.trim()).filter((x) => x && !/^\[tool /.test(x) && !/^```/.test(x));
    const ask = [...lines].reverse().find((x) => /\?\s*$/.test(x));
    return firstLine(ask ?? lines.at(-1) ?? '', n);
}
/** 12 s · 4 min · 2 h (a stop, a wait) */
export function duration(t) {
    return t < 120 ? `${t} s` : t < 7200 ? `${Math.floor(t / 60)} min` : `${Math.floor(t / 3600)} h`;
}
function glyphOf(level) {
    // F01: every Blocked level is the red mark (a card, a permission too); Your turn (waiting) the amber one
    return level === 'security' || level === 'blocked' || level === 'ci' || level === 'confirm' || level === 'permission' ? 'stuck' : level === 'gitlab' || level === 'prod' ? 'new' : 'wait';
}
/** the whole queue of this moment (pure: queue.test.mjs) */
export function buildQueue(q) {
    const out = { needs: [], review: [], new: [], interrupted: [], running: [], recent: [], ended: [] };
    const byId = new Map(q.sessions.map((s) => [s.id, s]));
    const subjectSession = (a) => String(a.subject.session ?? (a.subject.type === 'session' ? a.subject.key : '') ?? '');
    const l5 = (a) => a.source === 'session' && a.key === 'l5:' + subjectSession(a);
    const blocked = new Set(q.actions.filter(a => NEEDS_LEVELS.includes(a.level) && a.level !== 'waiting' && !l5(a)).map(subjectSession));
    const reviewing = new Set(q.sessions.filter(s => s.review_pending && s.kind === 'project' && !s.hidden
        && ['waiting', 'ready', 'attention'].includes(s.state) && s.at_prompt !== false && !blocked.has(s.id)
        && !Object.keys(s.subagents ?? {}).length && !Object.keys(s.background_tasks ?? {}).length).map(s => s.id));
    const acted = new Set();
    for (const a of q.actions) {
        if (l5(a))
            continue; // a flag is shown on the session, independently of agent activity
        if (a.source === 'gitlab')
            continue; // D11: a forge item is the Inbox view's, the queue links to it
        const sid = subjectSession(a);
        if (a.level === 'waiting' && reviewing.has(sid))
            continue;
        if (a.level === 'waiting' && sid && blocked.has(sid))
            continue;
        const s = byId.get(sid);
        const card = q.board.get(sid);
        const needs = NEEDS_LEVELS.includes(a.level);
        if (needs && sid)
            acted.add(sid);
        const it = {
            key: 'a:' + a.key, section: needs ? 'needs' : 'new', glyph: glyphOf(a.level), source: a.source,
            title: a.title, sub: firstLine(a.body), since: a.since, chips: [], session: s ? s.id : '',
            project: s?.project ?? a.subject.project ?? '', action: a, unread: false,
        };
        const age = agoAt(a.since, q.now);
        if (a.level === 'waiting' && s) {
            // F01: "project — the sentence it ended with"; the second line says whether it asks or is done
            const ask = askLine(card?.last ?? '', 90);
            it.title = rowTitle(s, ask);
            it.sub = ask || 'waiting for you';
            it.status = `${/\?\s*$/.test(ask) ? 'question' : 'turn done'} ${age}`;
        }
        else if (a.level === 'permission' && s) {
            it.title = rowTitle(s, firstLine(s.detail, 90) || 'asks for permission');
            it.sub = firstLine(s.detail) || 'answer in its terminal';
            it.status = `asks ${age} · answer in its terminal`;
        }
        else if (a.level === 'confirm') {
            const fact = firstLine(a.body.split('\n').filter((l) => !l.startsWith('⚠')).join('\n'), 90);
            it.title = s ? rowTitle(s, fact || a.title) : a.title;
            it.sub = fact;
            it.status = `asks ${age} · ${kbd('Allow / Deny ⌘J')}`;
            // F01: its ⚠ lines are the card's (⌘J), the row stays two lines; a session-less card keeps the first as a chip
            const warn = a.body.split('\n').find((l) => l.startsWith('⚠'));
            if (warn && !s)
                it.chips.push({ label: warn.slice(0, 60), cls: 'bad' });
        }
        else if (a.quote) {
            it.sub = `${a.quote.author || 'GitLab'}: ${firstLine(a.quote.text)}`;
        }
        if (a.level === 'gitlab' || a.subject.type === 'issue' || a.subject.type === 'mr')
            it.chips.push({ label: a.subject.label, cls: 'link' });
        // F01: the project is the title's first word - no project chip under it
        if (s)
            it.chips.push(...rowChips(s, card, it.chips, it.title).filter((c) => !(c.proj && it.status)));
        (needs ? out.needs : out.new).push(it);
    }
    for (const s of q.sessions.filter(s => reviewing.has(s.id))) {
        const summary = s.review_summary;
        const it = runningItem(s, q);
        it.section = 'review';
        it.glyph = 'idle';
        it.status = summary ? `${filesLabel(summary.files)} · +${summary.add} −${summary.del} · ${summary.turns} ${summary.turns === 1 ? 'turn' : 'turns'}${summary.tests ? ' · tests ' + summary.tests.status : ''}` : 'Ready to review';
        it.sub = '';
        it.chips = [];
        out.review.push(it);
    }
    out.review.sort((a, b) => {
        const x = byId.get(a.session)?.review_summary, y = byId.get(b.session)?.review_summary;
        return (x ? x.add + x.del : 0) - (y ? y.add + y.del : 0) || (x?.files ?? 0) - (y?.files ?? 0) || a.since - b.since;
    });
    const live = sessionOrder(q.sessions, q.pins, q.order).filter((s) => !acted.has(s.id) && !reviewing.has(s.id));
    for (const s of live) {
        const it = runningItem(s, q);
        if (s.state === 'exited') {
            it.glyph = 'off';
            it.sub = it.status = [q.label(s.state), 'pinned'].join(' · ');
        }
        if (it.glyph === 'stuck')
            out.needs.push({ ...it, section: 'needs' }); // a stop that hangs: you see it, piperd escalates
        else
            out.running.push(it);
    }
    // Ended is the complete list: pins and attention rows must not hide an ended session here.
    const ended = q.sessions.filter((s) => !s.hidden && s.kind !== 'prodcheck'
        && ['exited', 'error', 'blocked'].includes(s.state))
        .sort((a, b) => (b.since || b.created) - (a.since || a.created));
    for (const s of ended) {
        if (s.interrupted && s.state === 'exited' && s.kind === 'project') { // 9.141: its own section, the unfinished work first
            out.interrupted.push({
                key: 'i:' + s.id, section: 'interrupted', glyph: 'intr', source: 'session', title: rowTitle(s),
                sub: '', status: interruptedStatus(s.interrupted, s.since || s.created), since: s.since || s.created,
                chips: rowChips(s, q.board.get(s.id), [], rowTitle(s)).filter((c) => !c.proj), session: s.id, project: s.project, unread: false,
                resume: continueLabel(s), find: ['interrupted restart', s.branch, s.first_prompt, s.last_prompt].filter(Boolean).join(' '),
            });
            continue;
        }
        out.ended.push({
            key: 'ended:' + s.id, section: 'ended', glyph: s.state === 'exited' ? 'off' : 'stuck', source: 'session', title: endedSessionName(s),
            sub: ['error', 'blocked'].includes(s.state) ? firstLine(s.detail) || q.label(s.state) : firstLine(s.last_prompt || s.task || '') || q.label(s.state),
            since: s.since || s.created, chips: [
                { label: sessionDate(s.since || s.created, q.now), title: 'Last active: ' + new Date((s.since || s.created) * 1000).toLocaleString() },
                ...(s.project ? [{ label: s.project }] : []),
                ...(s.branch ? [{ label: s.branch, title: 'Current checkout branch' }] : []),
            ], session: s.id, project: s.project, unread: false, resume: continueLabel(s),
            find: [s.first_prompt, s.last_prompt, s.task, s.branch, s.id, s.claude_session,
                new Date((s.since || s.created) * 1000).toLocaleDateString(), new Date((s.since || s.created) * 1000).toLocaleDateString('sv-SE'),
                sessionDate(s.since || s.created, q.now)].filter(Boolean).join(' '),
        });
    }
    const unfinished = (it) => Number(!UNFINISHED.has(byId.get(it.session)?.interrupted?.was ?? ''));
    out.interrupted.sort((a, b) => unfinished(a) - unfinished(b) || b.since - a.since);
    for (const e of [...q.events].sort((a, b) => b.ts - a.ts).slice(0, 200)) {
        const sid = String(e.subject.session ?? (e.subject.type === 'session' ? e.subject.key : '') ?? '');
        out.recent.push({
            key: 'e:' + e.id, section: 'recent', glyph: 'off', source: e.source, title: recentTitle(e.title) + (e.n > 1 ? ` ×${e.n}` : ''),
            sub: firstLine(e.body), since: e.ts, chips: [], session: byId.has(sid) ? sid : '', project: e.subject.project ?? '',
            event: e, unread: !!q.seen && e.ts > q.seen,
        });
    }
    out.gone = (q.gone ?? []).filter((g) => q.now - g.at < GONE_S).map((g) => ({
        key: 'g:' + g.id, title: `${g.title}: ${KIND_TEXT[g.kind] ?? g.kind}`, sub: 'cancelled - nothing was sent'
    }));
    return out;
}
/** the session whose menu (Stop, Remove…) a row offers: only a row about that session -
 *  a CI / GitLab / production item merely links some session of its project, Stop there would hit it */
export function menuSession(it) {
    const subj = it.action?.subject ?? it.event?.subject;
    return !subj || subj.type === 'session' ? it.session : '';
}
function runningItem(s, q) {
    const card = q.board.get(s.id);
    const t = Math.max(0, Math.round(q.now - (s.since || s.created)));
    const stuck = s.state === 'stopping' && t >= STUCK_S;
    const it = {
        key: 's:' + s.id, section: 'running', source: s.kind === 'advisor' ? 'agent' : 'session', title: sessionName(s),
        // attention / trust in Running (the Global agent: piperd keeps it out of Needs you) still says it waits for you
        glyph: stuck ? 'stuck' : s.state === 'stopping' ? 'off' : ['attention', 'trust'].includes(s.state) ? 'wait' : IDLE.includes(s.state) ? 'idle' : 'run',
        sub: '', since: s.since || s.created, chips: [], session: s.id, project: s.project, unread: q.unseen.has(s.id),
    };
    const age = agoAt(s.since || s.created, q.now);
    if (stuck) {
        it.title = `${sessionName(s)} — stopping ${duration(t)}`;
        it.sub = t < KILL_AFTER_S ? `the sandbox ignores TERM · piperd sends KILL in ${KILL_AFTER_S - t} s` : 'KILL sent · waiting for the sandbox to go';
    }
    else {
        // F01: "project — what it does", the second line its state + time (working: the time alone - the section says it)
        // working: what it was asked; idle: the sentence it ended with
        it.title = rowTitle(s, s.state === 'working' ? firstLine(card?.asked || s.last_prompt || '', 90) : askLine(card?.last ?? '', 90));
        it.sub = [s.state === 'working' ? 'working' : q.label(s.state), s.state === 'working' ? '' : firstLine(card?.last ?? '', 90)].filter(Boolean).join(' · ');
        it.status = s.state === 'working' ? age : `${q.label(s.state)} ${age}`;
    }
    it.chips.push(...rowChips(s, card, [], it.title).filter((c) => !(c.proj && it.status)));
    if (s.branch)
        it.find = s.branch;
    return it;
}
/** W17: a session's chips in one order in every section - project · issue · L5 · subagents ·
 *  shells / monitors · terminal · text held (pure: queue-w17.test.mjs); `have` = the row's chips so far (an
 *  issue chip of a GitLab item is not repeated) */
export function rowChips(s, card, have = [], title = sessionName(s)) {
    const out = [];
    out.push(s.kind === 'advisor' ? { label: 'all projects', title: 'the Global agent sees every project', proj: true } : { label: s.project, proj: true });
    const issue = card?.issue ?? s.issue ?? null;
    const ref = issue ? `${issue.type === 'mr' ? '!' : '#'}${issue.iid}` : '';
    const inTitle = ref && new RegExp(`(^|[^\\w#!])${ref}(?!\\w)`).test(title);
    if (issue && !inTitle && ![...have, ...out].some((c) => c.label.includes(ref)))
        out.push({ label: ref, cls: 'link', title: issue.title });
    const watched = s.watch?.length ?? 0;
    if (watched)
        out.push({ label: `L5 · ${watched}`, cls: 'warn', title: 'New sensitive paths in this session\'s mounted project or libraries; see the context panel' });
    const agents = Object.keys(s.subagents ?? {}).length;
    if (agents)
        out.push({ label: `${agents} subagent${agents === 1 ? '' : 's'}`, cls: 'ok', title: 'native agent activity: subagents running' });
    const tasks = Object.values(s.background_tasks ?? {});
    for (const kind of ['shell', 'monitor']) {
        const n = tasks.filter((x) => x === kind).length;
        if (n)
            out.push({ label: `${n} ${kind}${n === 1 ? '' : 's'}`, cls: 'ok', title: `Claude background tasks: ${n} ${kind}${n === 1 ? '' : 's'} running` });
    }
    if (s.tty)
        out.push({ label: 'terminal', title: 'attached in a terminal (piper attach)' });
    if (s.hold)
        out.push({ label: 'text held', cls: 'warn', title: s.hold });
    return out;
}
/** W17: at most `max` chips on a row, the rest as `+N` with their names in the tooltip */
export function capChips(chips, max = 4) {
    if (chips.length <= max)
        return chips;
    const rest = chips.slice(max);
    return [...chips.slice(0, max), { label: `+${rest.length}`, title: rest.map((c) => c.label).join(' · ') }];
}
export function matchesText(it, text) {
    const t = text.trim().toLowerCase();
    const haystack = [it.title, it.sub, it.project, it.find ?? '', ...it.chips.map((c) => c.label)].join(' ').toLowerCase();
    return !t || t.split(/\s+/).every(part => haystack.includes(part));
}
/** keep the order the user is moving through: known keys where they were, new ones after them */
export function freeze(prev, items) {
    const at = new Map(prev.map((k, i) => [k, i]));
    return items.map((it, i) => ({ it, i, p: at.get(it.key) ?? Infinity }))
        .sort((a, b) => a.p - b.p || a.i - b.i).map((x) => x.it);
}
/** Only actionable detail belongs below a row title; conversation previews stay in Conversation. */
export function rowSummary(it) {
    if (it.status)
        return it.status; // F01: a session row's second line is its state + time
    if (it.action?.level === 'waiting' || it.action?.level === 'confirm' && it.action.body)
        return '';
    return it.action || it.glyph === 'stuck' ? it.sub : '';
}
// ------------------------------------------------------------------ the component
/** the red Allow's label of a confirmation row ('' = an ordinary green one, `y` allowed): feed.py's mark
 *  (args.danger), else the floating card's own rule for that pending card (main.ts dangerLabel: a protected
 *  branch, a file the host runs, a weakening settings change) - one keystroke never grants what the card guards */
export function allowDanger(a, dangerOf) {
    if (a?.level !== 'confirm')
        return '';
    const allow = a.actions.find((o) => o.op === 'decide' && o.args.ok === true && !o.args.always && !o.args.session_always);
    if (!allow)
        return '';
    const own = dangerOf?.(String(allow.args.id ?? '')) ?? '';
    if (!allow.args.danger)
        return own;
    // feed.py marks a weakening settings_change / a git_run of a hidden file too (no DANGER_LABEL entry): their own words, never "on PROD"
    return dangerWord(String(allow.args.danger), own);
}
const store = {
    get(k) {
        try {
            return window.localStorage.getItem(k);
        }
        catch {
            return null;
        }
    },
    set(k, v) {
        try {
            window.localStorage.setItem(k, v);
        }
        catch { /* private window */ }
    },
};
/** the line of an empty Needs you / Running / Recently section: while searching it is the search that found nothing -
 *  "Nothing is waiting for you" under a query that matched none of four waiting items was false (N7-4) */
export function emptyLine(section, searching, waiting = 0) {
    if (searching)
        return 'Nothing matches.';
    if (section === 'needs')
        return 'Nothing is waiting for you.';
    // N10-A-1: a session with a card / question is listed in Needs you only - "No running session." under three of them was false
    if (section === 'running')
        return !waiting ? 'No running session.' : waiting === 1 ? 'The running session waits for you above.'
            : `All ${waiting} running sessions wait for you above.`;
    // N10-A-5: the GitLab / Production filter with nothing new left the column blank
    return section === 'new' ? 'Nothing new here - the whole view opens from its icon on the left.' : 'Nothing has happened yet.';
}
export function createQueue(deps) {
    const el = h('aside', { class: 'queue', 'aria-label': 'Overview queue' });
    let text = '';
    let runningOpen = store.get('piper.queue.running') !== '0';
    let endedOpen = false; // 9.136: the ended wait folded after every load - a click opens them (no longer remembered)
    let recentOpen = store.get('piper.queue.recent') !== '0';
    let recentAll = false; // Recently shows the newest 12; "show all" the rest of what the Feed keeps here
    let frozenUntil = 0;
    const order = new Map();
    let current = { needs: [], review: [], new: [], interrupted: [], running: [], recent: [], ended: [] };
    let shownItems = [];
    let lastSig = '';
    let dragId = '';
    let seenTimer = 0;
    let seenFor = 0;
    let goneTimer = 0;
    const freezeNow = () => { frozenUntil = Date.now() + FREEZE_MS; };
    const frozen = () => Date.now() < frozenUntil || el.contains(document.activeElement);
    function model() {
        const q = buildQueue({ ...deps.input(), now: Date.now() / 1000 });
        const keep = frozen();
        for (const k of ['needs', 'review', 'new', 'interrupted', 'running', 'recent', 'ended']) {
            if (keep && k !== 'running')
                q[k] = freeze(order.get(k) ?? [], q[k]); // Running keeps the user's own order (⌘1–9)
            order.set(k, q[k].map((x) => x.key));
        }
        return q;
    }
    function render() {
        if (dragId)
            return;
        current = model();
        const vis = (list) => list.filter((x) => matchesText(x, text));
        const sec = { needs: vis(current.needs), review: vis(current.review), new: vis(current.new), interrupted: vis(current.interrupted), running: vis(current.running), recent: vis(current.recent), ended: vis(current.ended), gone: current.gone ?? [] };
        if (sec.gone.length) {
            window.clearTimeout(goneTimer);
            goneTimer = window.setTimeout(render, 1000);
        } // F17: the line goes after GONE_S
        const sig = JSON.stringify([text, sec.gone.map((g) => g.key), runningOpen, endedOpen, recentOpen, recentAll, deps.feedOn(), deps.dismissed(), deps.muted(), [sec.needs, sec.review, sec.new, sec.interrupted, sec.running, sec.recent, sec.ended].map((l) => l.map((x) => [x.key, x.title, x.sub, x.glyph, x.unread, x.chips, x.status, deps.selected(x), ago(x.since), x.action?.trash?.map((t) => t.id)]))]);
        if (sig === lastSig)
            return;
        lastSig = sig;
        const f = document.activeElement instanceof HTMLElement && el.contains(document.activeElement) ? document.activeElement : null;
        const focusKey = f?.closest('[data-key]')?.dataset.key ?? '';
        const focusIdx = focusKey ? rows().findIndex((x) => x.dataset.key === focusKey) : -1;
        const inSearch = f instanceof HTMLInputElement && f.classList.contains('q-search');
        const caret = f instanceof HTMLTextAreaElement || f instanceof HTMLInputElement ? [f.selectionStart, f.selectionEnd] : null;
        const top = el.querySelector('.q-scroll')?.scrollTop ?? 0;
        const next = h('div', { class: 'q-wrap' }, head(), body(sec), deps.extra?.() ?? null);
        clear(el);
        el.append(next);
        const scroller = el.querySelector('.q-scroll');
        if (scroller)
            scroller.scrollTop = top;
        shownItems = [...sec.needs, ...sec.review, ...sec.new, ...sec.interrupted, ...(runningOpen ? sec.running : []), ...(recentOpen ? sec.recent : []), ...sec.ended];
        if (inSearch) {
            const s = el.querySelector('.q-search');
            s?.focus();
            if (s && caret && caret[0] !== null)
                s.setSelectionRange(caret[0], caret[1]);
        }
        else if (focusKey) {
            // the item went (answered, dismissed): the keyboard goes on with the one now in its place
            const list = rows();
            const back = el.querySelector(`[data-key="${CSS.escape(focusKey)}"]`) ?? list[Math.min(focusIdx, list.length - 1)];
            back?.focus({ preventScroll: true });
        }
        armSeen(sec.recent);
    }
    const queueVisible = () => el.isConnected && el.offsetParent !== null;
    function head() {
        return h('div', { class: 'q-head' }, h('div', { class: 'q-title' }, h('h2', {}, 'Overview'), h('span', { class: 'grow' }), h('button', { class: 'ghost q-cmdk', title: kbd('search & commands (⌘K)'), 'aria-label': 'search and commands',
            onclick: () => deps.palette() }, h('kbd', {}, kbd('⌘K'))), h('button', { class: 'ghost q-new', title: 'new session in a project', 'aria-label': 'new session', onclick: () => deps.newSession() }, '+ Session'), deps.fold ? h('button', { class: 'ghost q-collapse', title: kbd('collapse to the strip (⌘B)'), 'aria-label': 'collapse to the strip',
            onclick: () => deps.fold?.() }, deps.foldIcon?.() ?? '«') : null), h('div', { class: 'q-searchbox' }, searchGlyph(), h('input', { type: 'search', class: 'q-search', placeholder: 'Find a session…', value: text, autocomplete: 'off',
            spellcheck: 'false', 'aria-label': 'search the queue', title: 'Search title, project, issue, branch or conversation (/)' })), deps.legacy() ? h('p', { class: 'q-legacy' }, 'piperd is older than this GUI: restart it (task piper:restart) - until then confirmations float bottom right and Needs you stays empty.') : null);
    }
    function secHead(title, n, hint, toggle, tip) {
        const label = h('span', { class: 'q-sectitle' }, title);
        const count = h('span', { class: 'q-count' }, String(n));
        return h('div', { class: 'q-sechead' + (toggle ? ' fold' : ''), title: tip }, toggle ? h('button', { class: 'q-fold', 'aria-expanded': String(toggle.open), onclick: toggle.run }, label, count) : h('div', { class: 'q-heading' }, label, count), hint ? h('span', { class: 'q-hint' }, hint) : null);
    }
    function body(sec) {
        const scroll = h('div', { class: 'q-scroll' });
        const gone = (sec.gone ?? []).map((g) => h('div', { class: 'q-gone', 'data-gone': g.key, role: 'status' }, h('span', { class: 'q-gone-t' }, showBidi(g.title)), h('span', { class: 'q-gone-s' }, g.sub)));
        // F01: Needs you in two - Blocked (the agent cannot go on: a card, a permission, a failed start) and Your turn (it finished)
        const blocked = sec.needs.filter((x) => !isTurn(x)), turn = sec.needs.filter(isTurn);
        if (blocked.length || gone.length)
            scroll.append(h('section', { class: 'q-sec needs blocked' }, secHead('Blocked', blocked.length, null, undefined, 'the agent waits for your decision · ⌘J the card'), ...blocked.map(item), ...gone));
        if (sec.review.length)
            scroll.append(h('section', { class: 'q-sec to-review' }, secHead('To review', sec.review.length, null), ...sec.review.map(item)));
        if (turn.length)
            scroll.append(h('section', { class: 'q-sec needs yourturn' }, secHead('Your turn', turn.length, null, undefined, 'finished a turn or asks a question, the oldest first'), ...turn.map(item)));
        if (!sec.needs.length && !sec.review.length && !gone.length)
            scroll.append(h('section', { class: 'q-sec needs yourturn' }, secHead('Your turn', 0, null), h('p', { class: 'q-empty' }, emptyLine('needs', !!text))));
        if (sec.new.length)
            scroll.append(h('section', { class: 'q-sec' }, secHead('New', sec.new.length, null), ...sec.new.map(item)));
        if (sec.interrupted.length) { // 9.141: until restored, removed or dismissed
            const ids = new Set(sec.interrupted.map((x) => x.session));
            const causes = deps.input().sessions.filter((x) => ids.has(x.id) && x.interrupted).map((x) => x.interrupted.cause);
            const acts = h('span', { class: 'q-intr-acts' }, deps.restoreInterrupted ? h('button', { class: 'link small', 'data-intr': 'restore', title: 'pick the ones to restore',
                onclick: () => deps.restoreInterrupted?.() }, 'Restore…') : null, deps.dismissInterrupted ? h('button', { class: 'link small mut', 'data-intr': 'dismiss', title: 'drop the marks - the sessions stay among the ended ones',
                onclick: () => deps.dismissInterrupted?.() }, 'Dismiss') : null);
            scroll.append(h('section', { class: 'q-sec interrupted' }, secHead(sectionTitle(causes), sec.interrupted.length, acts, undefined, 'a restart stopped them; Continue brings one back, Restore… several'), ...sec.interrupted.map(item)));
        }
        {
            const idle = sec.running.filter((x) => x.glyph === 'idle').length;
            const unread = sec.running.filter((x) => x.unread).length;
            const hint = [idle ? `${idle} idle` : '', unread ? `${unread} unread` : ''].filter(Boolean).join(' · ');
            const box = h('section', { class: 'q-sec running' }, secHead('Running', sec.running.length, null, { open: runningOpen, run: () => {
                    runningOpen = !runningOpen;
                    store.set('piper.queue.running', runningOpen ? '1' : '0');
                    render();
                } }, [kbd('switch sessions (⌘1–9)'), hint].filter(Boolean).join(' · ')));
            if (runningOpen) {
                if (!sec.running.length)
                    box.append(h('p', { class: 'q-empty' }, emptyLine('running', !!text, new Set(sec.needs.filter((x) => x.session && x.glyph !== 'stuck' && deps.canReply(x.session)).map((x) => x.session)).size)));
                box.append(...sec.running.map(item));
            }
            scroll.append(box);
        }
        if (deps.feedOn()) {
            const d = deps.dismissed();
            const muted = deps.muted();
            const hint = d || muted.length ? h('span', {}, d ? `dismissed ${d} ` : '', d ? h('button', { class: 'link small', onclick: () => deps.undismiss() }, 'restore') : null, muted.length ? h('span', { title: muted.map((m) => m.label).join(', ') }, `${d ? ' · ' : ''}muted ${muted.length} `) : null, muted.length ? h('button', { class: 'link small', onclick: () => deps.unmute(muted.map((m) => m.key)) }, 'unmute') : null) : null;
            const box = h('section', { class: 'q-sec recent' }, secHead('Recently', sec.recent.length, hint, { open: recentOpen, run: () => {
                    recentOpen = !recentOpen;
                    store.set('piper.queue.recent', recentOpen ? '1' : '0');
                    render();
                } }));
            if (recentOpen) {
                if (!sec.recent.length)
                    box.append(h('p', { class: 'q-empty' }, emptyLine('recent', !!text)));
                box.append(...sec.recent.slice(0, recentAll ? sec.recent.length : 12).map(eventRow));
                if (sec.recent.length > 12)
                    box.append(h('button', { class: 'link small q-more', onclick: () => { recentAll = !recentAll; lastSig = ''; render(); } }, recentAll ? 'show fewer' : `show ${sec.recent.length - 12} more`));
            }
            scroll.append(box);
        } // No Feed, no Recently - no placeholder
        if (sec.ended.length) {
            // F01: one line "61 ended · Find & resume" - the count unfolds the list (Retry of a failed start), the link opens the picker
            const heading = h('div', { class: 'q-sechead q-endline' }, h('button', { class: 'q-fold', 'aria-expanded': String(endedOpen || !!text.trim()), title: endedOpen ? 'fold the ended sessions' : 'list the ended sessions here',
                onclick: () => { endedOpen = !endedOpen; render(); } }, h('span', { class: 'q-endcount' }, `${sec.ended.length} ended`)), deps.resumeConversations ? h('button', { class: 'link q-resume', 'aria-label': 'Find ended conversations',
                title: 'Search, resume or fork saved conversations', onclick: () => deps.resumeConversations?.() }, 'Find & resume') : null);
            const box = h('section', { class: 'q-sec ended' }, heading);
            // folded: the one on screen stays in reach
            box.append(...(endedOpen || text.trim() ? sec.ended : sec.ended.filter((x) => deps.selected(x))).map(item));
            scroll.append(box);
        }
        return scroll;
    }
    function item(it) {
        const a = it.action;
        const summary = rowSummary(it);
        const sel = deps.selected(it);
        const ops = [];
        if (a?.level === 'confirm') {
            const allow = a.actions.find((o) => o.op === 'decide' && o.args.ok === true);
            const deny = a.actions.find((o) => o.op === 'decide' && o.args.ok === false);
            const always = a.actions.find((o) => o.op === 'decide' && o.args.always === true);
            const session = a.actions.find((o) => o.op === 'decide' && o.args.session_always === true);
            // W13: decision.ts - the floating card's order and words, here with the keys written (they work here)
            const run = { deny: () => decide(it, false), always: () => decide(it, true, true), session: () => decide(it, true, false, true), allow: () => decide(it, true) };
            const has = { deny: !!deny, always: !!always, session: !!session, allow: !!allow };
            for (const d of decisionButtons({ always: always?.label, sessionAlways: session?.label, danger: allowDanger(a, deps.dangerOf), keys: true })) {
                if (has[d.role])
                    ops.push(h('button', { class: 'mini' + (d.cls ? ' ' + d.cls : ''), title: d.title, onclick: (e) => { e.stopPropagation(); void run[d.role](); } }, d.label));
            }
        }
        else if (canDismiss(it, deps.feedOn())) {
            // the level's icon (✕ for CI) is no button: dismissing gets its own
            ops.push(h('button', { class: 'mini', title: 'dismiss (e)', onclick: (e) => { e.stopPropagation(); deps.dismiss(it); } }, 'Dismiss e'));
        }
        else if ((it.section === 'ended' || it.section === 'interrupted') && it.session) {
            const resume = it.resume ?? 'Retry';
            ops.push(h('button', { class: 'mini', title: (resume === 'Retry' ? 'start it again' : 'resume the conversation in a new sandbox') +
                    (deps.rebuildPending?.() ? ' · rebuilds the image first (minutes)' : ''),
                onclick: (e) => { e.stopPropagation(); deps.continueSession(it.session); } }, resume), h('button', { class: 'mini ghost', title: 'Remove: from the list (transcript and audit stay)', 'aria-label': 'Remove',
                onclick: (e) => { e.stopPropagation(); deps.forget(it.session); } }, fi('x', 'gi')));
        }
        const restorable = deps.restoreTrash ? netRestorable(a) : [];
        if (restorable.length) {
            const go = (ents) => (e) => { e.stopPropagation(); void restore(it, ents); };
            const base = (p) => p.split('/').pop() || p;
            ops.unshift(...(restorable.length > 1 ? [h('button', { class: 'mini primary', title: showBidi(`put the originals back from Piper's trash: ${restorable.map((t) => t.path).join(', ')}`),
                    onclick: go(restorable) }, 'Restore all')] : []), ...restorable.slice(0, 3).map((t) => h('button', { class: 'mini' + (restorable.length > 1 ? '' : ' primary'),
                title: showBidi(`put the original of ${t.path} back from Piper's trash, over what git left there`), 'aria-label': showBidi(`Restore ${t.path}`),
                onclick: go([t]) }, restorable.length > 1 ? `Restore ${showBidi(base(t.path))}` : 'Restore')));
        }
        const icon = !it.status && it.source !== 'session' && it.source !== 'agent' ? h('span', { class: 'qi-src', 'aria-hidden': 'true', title: it.source }, a ? LEVEL[a.level]?.icon ?? SOURCE_ICON[it.source] ?? '' : SOURCE_ICON[it.source] ?? '') : null;
        // ⌘1–9: the session's place in sessionOrder (the strip's numbers too), on its Needs you row as well
        const runIdx = it.session && (it.section === 'running' || it.section === 'needs') ? (deps.numberOf?.(it.session) ?? -1) : -1;
        const row = h('div', {
            class: `qi g-${it.glyph}` + (sel ? ' sel' : '') + (it.unread ? ' unread' : '') + (a?.level === 'confirm' ? ' confirm ra-row' : '') + (it.status ? ' q2' : ''), tabindex: '0', role: 'button',
            'data-key': it.key, 'data-sid': it.session || undefined,
            draggable: it.section === 'running' && it.session ? 'true' : undefined,
            title: showBidi([it.title, glyphWord(it), runIdx >= 0 && runIdx < 9 ? kbd(`⌘${runIdx + 1}`) : '', it.session ? 'right click = actions' : ''].filter(Boolean).join(' · ')),
            onclick: () => { freezeNow(); deps.open(it); },
        }, h('span', { class: `st ${it.glyph}`, role: 'img', 'aria-label': glyphWord(it), title: glyphWord(it) }), h('span', { class: 'qi-title' }, icon, showBidi(it.title)), 
        // F01: a session row has its time in the second line (state + time), the others an age column
        it.status ? null : h('span', { class: 'qi-age', title: new Date(it.since * 1000).toLocaleString() }, ago(it.since)), summary ? h('span', { class: 'qi-sub', title: it.status && it.sub ? showBidi(it.sub) : undefined }, showBidi(summary)) : null, // actionable detail only
        // F17/F01: the whole card (diff, command) is the floating card's (⌘J); the row's y / n and its buttons on hover stay
        it.chips.length || ops.length ? h('span', { class: 'qi-chips' }, ...capChips(it.chips).map((c) => h('span', { class: 'qi-meta' + (c.cls ? ' ' + c.cls : ''), title: c.title && showBidi(c.title) }, showBidi(c.label))), ops.length ? h('span', { class: 'qi-ops' + (a?.level === 'confirm' ? ' row-actions' : '') }, ...ops) : null) : null);
        return row;
    }
    function eventRow(it) {
        return h('div', { class: 'qe' + (it.unread ? ' unread' : ''), tabindex: '0', role: 'button', 'data-key': it.key, title: new Date(it.since * 1000).toLocaleString(),
            onclick: () => { freezeNow(); deps.open(it); } }, h('span', { class: 'qe-text' }, showBidi(it.title)), h('span', { class: 'qe-time' }, clockOf(it.since)));
    }
    const restoring = new Set();
    async function restore(it, ents) {
        const project = it.action?.project;
        if (!project || !deps.restoreTrash || restoring.has(it.key))
            return;
        restoring.add(it.key);
        freezeNow();
        const row = el.querySelector(`[data-key="${CSS.escape(it.key)}"]`);
        for (const b of row?.querySelectorAll('button') ?? [])
            b.disabled = true;
        try {
            await deps.restoreTrash(project, ents);
        }
        finally {
            restoring.delete(it.key);
            for (const b of row?.querySelectorAll('button') ?? [])
                b.disabled = false;
        }
    }
    const deciding = new Set(); // one decision per request, whatever the keyboard repeats
    async function decide(it, ok, always = false, sessionAlways = false) {
        const op = it.action?.actions.find((o) => o.op === 'decide' && o.args.ok === ok && (o.args.always === true) === always && (o.args.session_always === true) === sessionAlways);
        if (!op || deciding.has(it.key))
            return;
        deciding.add(it.key);
        freezeNow();
        const row = el.querySelector(`[data-key="${CSS.escape(it.key)}"]`);
        for (const b of row?.querySelectorAll('button') ?? [])
            b.disabled = true; // one decision, one POST
        try {
            await deps.decide(String(op.args.id), ok, always, sessionAlways);
        }
        catch {
            deciding.delete(it.key);
            for (const b of row?.querySelectorAll('button') ?? [])
                b.disabled = false;
        }
    }
    function openSearch() {
        el.querySelector('.q-search')?.focus();
    }
    function closeSearch() {
        text = '';
        lastSig = '';
        render();
        focusFirst();
    }
    function rows() {
        return [...el.querySelectorAll('.qi, .qe')];
    }
    function focusFirst() {
        const r = el.querySelector('.qi.sel') ?? rows()[0];
        r?.focus();
        return !!r;
    }
    const itemOf = (node) => {
        const key = node instanceof HTMLElement ? node.closest('[data-key]')?.dataset.key : undefined;
        return key ? shownItems.find((x) => x.key === key) : undefined;
    };
    el.addEventListener('input', (e) => {
        if (!e.target.classList.contains('q-search'))
            return;
        text = e.target.value;
        render();
    });
    el.addEventListener('keydown', (e) => {
        const t = e.target;
        if (t.classList.contains('q-search')) {
            if (e.key === 'Escape')
                closeSearch();
            else if (e.key === 'ArrowDown' || e.key === 'Enter') {
                const first = rows()[0];
                if (e.key === 'Enter') {
                    const it = itemOf(first ?? null);
                    if (it)
                        deps.open(it);
                }
                else
                    first?.focus();
            }
            else
                return;
            e.preventDefault();
            return;
        }
        if (t instanceof HTMLTextAreaElement || t instanceof HTMLInputElement || e.metaKey || e.ctrlKey || e.altKey)
            return;
        // a held key repeats: moving may repeat, deciding / dismissing never (the next item would get it)
        if (e.repeat && !['j', 'k', 'ArrowDown', 'ArrowUp'].includes(e.key)) {
            e.preventDefault();
            return;
        }
        const list = rows();
        const at = list.findIndex((x) => x === document.activeElement || x.contains(document.activeElement));
        const it = itemOf(document.activeElement);
        const feedOn = deps.feedOn();
        let done = true;
        if (e.key === 'j' || e.key === 'ArrowDown') {
            freezeNow();
            list[Math.min(list.length - 1, at + 1)]?.focus();
        }
        else if (e.key === 'k' || e.key === 'ArrowUp') {
            freezeNow();
            list[Math.max(0, at - 1)]?.focus();
        }
        else if (e.key === 'Home')
            list[0]?.focus();
        else if (e.key === 'End')
            list.at(-1)?.focus();
        else if ((e.key === 'Enter' || e.key === 'o') && it && t.tagName !== 'BUTTON') {
            freezeNow();
            deps.open(it);
        }
        else if (e.key === 'r' && it?.session && deps.canReply(it.session))
            deps.reply(it.session);
        else if (e.key === 'y' && it?.action?.level === 'confirm' && !allowDanger(it.action, deps.dangerOf))
            void decide(it, true);
        else if (e.key === 'n' && it?.action?.level === 'confirm')
            void decide(it, false);
        else if (e.key === 'e' && it && canDismiss(it, feedOn))
            deps.dismiss(it);
        else if (e.key === 'm' && it?.action && it.action.level !== 'security' && feedOn)
            deps.mute(it);
        else if (e.key === 'a' && it && feedOn)
            deps.handOver((it.action?.subject ?? it.event?.subject ?? { type: 'session', key: it.session, label: it.title }));
        else if (e.key === '/')
            openSearch();
        else if (e.key === '?')
            deps.shortcuts();
        else if (e.key === 'Escape')
            deps.back();
        else if (e.key === 'ContextMenu' && it && menuSession(it)) {
            const r = document.activeElement.getBoundingClientRect();
            deps.menu(menuSession(it), r.left + 12, r.bottom);
        }
        else
            done = false;
        if (done)
            e.preventDefault();
    });
    el.addEventListener('contextmenu', (e) => {
        const it = itemOf(e.target);
        const sid = it ? menuSession(it) : '';
        if (!sid)
            return;
        e.preventDefault();
        deps.menu(sid, e.clientX, e.clientY);
    });
    // drag & drop: the user's order of Running (the same order as ⌘⇧↑↓, it numbers ⌘1–9)
    const runIds = () => current.running.map((x) => x.session);
    el.addEventListener('dragstart', (e) => {
        const row = e.target.closest('.qi[draggable="true"]');
        if (!row || !e.dataTransfer)
            return;
        dragId = row.dataset.sid ?? '';
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', dragId);
        row.classList.add('dragging');
    });
    el.addEventListener('dragover', (e) => {
        const row = e.target.closest('.qi[draggable="true"]');
        if (!dragId || !row)
            return;
        e.preventDefault();
        const r = row.getBoundingClientRect();
        for (const x of el.querySelectorAll('.drop-before, .drop-after'))
            x.classList.remove('drop-before', 'drop-after');
        if (row.dataset.sid !== dragId)
            row.classList.add(e.clientY > r.top + r.height / 2 ? 'drop-after' : 'drop-before');
    });
    el.addEventListener('drop', (e) => {
        const row = e.target.closest('.qi[draggable="true"]');
        if (!dragId || !row)
            return;
        e.preventDefault();
        const target = row.dataset.sid ?? '';
        const ids = deps.orderIds?.() ?? runIds();
        if (target && target !== dragId && ids.includes(target)) {
            const rest = ids.filter((x) => x !== dragId);
            rest.splice(rest.indexOf(target) + (row.classList.contains('drop-after') ? 1 : 0), 0, dragId);
            deps.saveOrder(rest);
        }
    });
    el.addEventListener('dragend', () => {
        dragId = '';
        lastSig = '';
        render();
    });
    /** Recently counts as seen after it has been on screen for a few seconds */
    function armSeen(recent) {
        const top = Math.max(0, ...recent.filter((x) => x.unread).map((x) => x.since));
        if (!top || !recentOpen || document.hidden || !el.isConnected || el.offsetParent === null)
            return;
        if (seenFor === top)
            return;
        seenFor = top;
        window.clearTimeout(seenTimer);
        seenTimer = window.setTimeout(() => { if (!document.hidden && el.offsetParent !== null)
            deps.markSeen(top); }, 4000);
    }
    window.setInterval(() => { if (el.isConnected && !document.hidden)
        render(); }, 5000); // the ages and a stuck stop's countdown (render skips an unchanged queue)
    return {
        el,
        render,
        /** ⌘⇧L / ⌘B: the keyboard into the queue */
        focus() {
            render();
            return focusFirst();
        },
        /** Running in the order shown: ⌘1–9 */
        running: () => current.running.map((x) => x.session),
        /** the Needs you items in order: ⌘J goes through them; the one "needs you" count (title, badges, strip) */
        needs: () => current.needs,
        /** New (GitLab, production, CI): counted apart from Needs you */
        fresh: () => current.new,
        hasFocus: () => el.contains(document.activeElement),
        /** the item is on screen now: in the queue, matching the search (else its card floats) */
        shows: (key) => shownItems.some((x) => x.key === key) && queueVisible(),
        /** an item with this key is in the queue now */
        has: (key) => Object.values(current).some((l) => l.some((x) => x.key === key)),
    };
}
/** the magnifier of the title row's search button (the fold's svg style) */
function searchGlyph() {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    for (const d of ['M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14z', 'M20 20l-4-4']) {
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', d);
        svg.append(p);
    }
    return svg;
}
function glyphWord(it) {
    return it.glyph === 'intr' ? 'interrupted by a restart' : it.glyph === 'wait' ? 'waiting for you' : it.glyph === 'run' ? 'working' : it.glyph === 'idle' ? 'idle' : it.glyph === 'stuck' ? 'blocked or stuck'
        : it.glyph === 'new' ? 'new' : 'ended';
}
function clockOf(ts) {
    const d = new Date(ts * 1000);
    return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
}
//# sourceMappingURL=queue.js.map