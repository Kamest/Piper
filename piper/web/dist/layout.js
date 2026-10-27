// W12: one layout model. The center
// is Single or Grid (D1/D2: the grid is the one multi-pane container - Beside Files / DB are its panes `files` /
// `db:here`, ⌘\\ adds the previous session to the grid); the left is the queue, the strip or hidden;
// the right one column: the context panel, the agent drawer or nothing. ⇧⌘F12 hides both sides and brings them back. One rule for every toggle: a target that is not on screen is shown - a hidden state is never flipped.
// 9.45: the grid is its panes (the pick) and nothing else; Single is a
// zoom on one pane that leaves the pick alone; every add inserts after the focused pane and never replaces one.
// Pure (node --test): main.ts keeps the Layout, dispatches actions and carries out the effects.
import { addPane, besideOnly, GRID_MAX, paneKind, removePane, replaceFocused } from './gridlayout.js';
/** Files / HTTP: the code gets the width - the queue folds to the strip there */
export const FOLD_VIEWS = new Set(['files', 'http']);
/** views with the queue beside them (the whole width goes to Audit, Usage, Timesheet, Settings, Database) */
// one source for main.ts too (R17: it had its own copy with 'host', so the queue in the Host view disagreed)
export const QUEUE_VIEWS = new Set(['quiet', 'term', 'files', 'http', 'convo', 'history', 'gitlab', 'prod', 'host']);
export function defaultLayout(wide) {
    return { v: 1, center: { kind: 'single' }, left: 'strip', leftBack: 'strip', leftFold: false,
        right: wide ? 'context' : 'hidden', rightBack: wide ? 'context' : 'hidden', zen: null };
}
const SINGLE = { kind: 'single' };
const GRID = { kind: 'grid' };
const GRID_OFF = 'The grid is off (Settings > Features)';
const TOAST = {
    perSession: 'The context panel is per session - open a session first',
    zen: 'Panels hidden · ⇧⌘F12 brings them back',
};
export const LAYOUT_TOASTS = TOAST;
/** ⌘\: the first session that can be beside the active one and is not on screen - the previous one first */
function partnerFor(c) {
    return [c.lastActive, ...c.ids].find((id) => id && id !== c.active && !c.splitBlock(id) && !(c.shown ?? []).includes(id)) ?? '';
}
/** Adding beside Single starts with that visible pane. Only an explicit Grid / unzoom restores hidden panes. */
export function editMembers(s, c) {
    if (s.center.kind === 'grid' && (c.gridIds.length > 1 || (c.gridIds[0] && c.shown?.includes(c.gridIds[0]))))
        return c.gridIds;
    if (s.center.kind === 'single' && s.center.pane)
        return [s.center.pane];
    return (c.hasSession ?? c.hasPty)(c.active) ? [c.active] : [];
}
/** Preview and restore use the same panes, including a starting active session, without stale ended panes. */
export function restoreMembers(c) {
    const pick = c.gridIds.filter((id) => paneKind(id) !== 'session' || id === c.active || !c.isEnded?.(id));
    const base = pick.length === c.gridIds.length ? c.gridIds : pick;
    if (!(c.hasSession ?? c.hasPty)(c.active) || base.includes(c.active))
        return base;
    const r = addPane(base, c.active, '', '');
    if (!r.refuse)
        return r.pick;
    if (c.isEnded?.(c.active))
        return base; // an ended session never pushes out a live pane (the grid action refuses)
    // a full grid (9 panes) left for another session: that session takes the last session pane's place instead of a
    // refusal - ⌘G from it never worked again until a pane was closed by hand (user report 2026-10-20)
    const last = [...base].reverse().find((id) => paneKind(id) === 'session');
    return last ? base.map((id) => (id === last ? c.active : id)) : [c.active, ...base.slice(0, GRID_MAX - 1)];
}
/** Insert after the visible focus, without bringing an unseen active terminal into a zoomed tool. */
function addBeside(s, pane, c) {
    const base = editMembers(s, c);
    return addPane(base, pane, c.focus ?? '', base.includes(c.active) ? c.active : '');
}
/** the grid's panes are on screen as a grid (not a zoom, not the terminal of another tab) */
const gridOpen = (s, c) => s.center.kind === 'grid' && c.view === 'term'
    && ((c.shown ?? []).length > 1 || (!!c.adding && (c.shown ?? []).length > 0));
/** an explicit left / right action leaves ⇧⌘F12 (IntelliJ: opening a tool window leaves Hide All) */
function leaveZen(s) {
    return s.zen ? { ...s, zen: null } : s;
}
export function layoutReduce(s, a, c) {
    const term = c.view === 'term';
    const fx = {};
    const done = (state) => ({ state, fx });
    switch (a.t) {
        case 'choose': {
            if (!c.gridOn)
                return { state: s, fx: { refuse: GRID_OFF } };
            if (!a.ids.length)
                return { state: s, fx: { refuse: 'Choose at least one pane' } };
            if (a.ids.length > GRID_MAX)
                return { state: s, fx: { refuse: `The grid has ${GRID_MAX} panes at most` } };
            // a session beside its project tools first, as gridIdsShown() shows it - a stored ['files', s] put the next
            // added pane's agent on the right; a multi-session grid keeps the ticked order
            fx.pick = besideOnly(a.ids) ? [...a.ids.filter((id) => paneKind(id) === 'session'), ...a.ids.filter((id) => paneKind(id) !== 'session')] : [...a.ids];
            fx.add = false;
            fx.next = a.ids.includes(c.active) ? c.active : a.ids.find((id) => paneKind(id) === 'session') ?? a.ids[0];
            if (paneKind(fx.next) === 'session')
                fx.focus = 'detail';
            if (!term)
                fx.toTerm = true;
            const only = a.ids.length === 1 ? a.ids[0] : '';
            return done({ ...s, center: only ? paneKind(only) === 'session' ? SINGLE : { kind: 'single', pane: only } : GRID });
        }
        case 'grid': {
            const on = a.on ?? !(gridOpen(s, c) || (s.center.kind === 'grid' && term && (c.shown ?? []).length > 0));
            if (!on)
                return done(s.center.kind === 'grid' ? { ...s, center: SINGLE } : s);
            if (!c.gridOn && !besideOnly(c.gridIds))
                return { state: s, fx: { refuse: GRID_OFF } };
            if (!c.ids.some(c.hasSession ?? c.hasPty) && !c.gridOther)
                return { state: s, fx: { refuse: 'No live session for the grid' } };
            if (!term)
                fx.toTerm = true;
            // Restore the previewed panes; with fewer than 2 the Add cell shows beside them.
            const pick = restoreMembers(c);
            if ((c.hasSession ?? c.hasPty)(c.active) && !pick.includes(c.active)) {
                return { state: s, fx: { refuse: `The grid has ${GRID_MAX} panes at most and this session has ended. Remove a pane before adding this session.` } };
            }
            const live = c.gridIds.filter((id) => paneKind(id) !== 'session' || id === c.active || !c.isEnded?.(id));
            if (live.length >= GRID_MAX && !live.includes(c.active) && pick.includes(c.active)) {
                fx.toast = `The grid was full (${GRID_MAX} panes): this session took the place of the last one - it stays in the queue`;
            }
            if (pick !== c.gridIds)
                fx.pick = pick;
            if (pick.length < 2 && c.gridOn)
                fx.add = true;
            return done(s.center.kind === 'grid' ? s : { ...s, center: GRID });
        }
        case 'single':
            if (!term)
                fx.toTerm = true;
            return done(s.center.kind === 'single' && !s.center.pane ? s : { ...s, center: SINGLE });
        case 'zoom': {
            if (!term)
                fx.toTerm = true;
            if (!a.pane)
                return layoutReduce(s, { t: 'grid', on: true }, c);
            if (paneKind(a.pane) !== 'session')
                fx.next = a.pane;
            return done({ ...s, center: paneKind(a.pane) === 'session' ? SINGLE : { kind: 'single', pane: a.pane } });
        }
        case 'addCell': {
            if (!c.gridOn)
                return { state: s, fx: { refuse: GRID_OFF } };
            if (!term)
                fx.toTerm = true;
            const base = editMembers(s, c);
            if (base.length >= GRID_MAX)
                return { state: s, fx: { refuse: `The grid has ${GRID_MAX} panes at most` } };
            fx.pick = base;
            fx.add = true;
            return done({ ...s, center: GRID });
        }
        case 'split': {
            // D2/9.45: ⌘\ adds the previous session (else the next live one off screen) next to the active one; never closes
            if (!c.gridOn)
                return { state: s, fx: { refuse: GRID_OFF } };
            const partner = partnerFor(c);
            if (!partner)
                return { state: s, fx: { refuse: c.hasPty(c.active) ? 'No other session with a terminal to put beside this one' : `No split: ${c.splitBlock('')}.` } };
            return reducePick(s, addBeside(s, partner, c), c, fx);
        }
        case 'splitWith': {
            if (!c.gridOn)
                return { state: s, fx: { refuse: GRID_OFF } };
            const why = c.splitBlock(a.id);
            if (why)
                return { state: s, fx: { refuse: `Not beside: ${why}.` } };
            return reducePick(s, addBeside(s, a.id, c), c, fx);
        }
        case 'add': {
            if (!c.gridOn)
                return { state: s, fx: { refuse: GRID_OFF } };
            return reducePick(s, addBeside(s, a.id, c), c, fx);
        }
        case 'remove': {
            const r = removePane(c.gridIds, a.id);
            fx.pick = r.pick;
            if (s.center.kind === 'grid' && (c.focus || c.active) === a.id && r.next) {
                // Files / HTTP / the project's DB belong to the active session: closing them returns the keyboard there.
                fx.next = besideOnly([c.active, a.id]) && r.pick.includes(c.active) ? c.active : r.next;
            }
            // A lone tool must remain visible after its terminal is removed; one session returns to Single.
            if (s.center.kind === 'grid' && r.pick.length < 2) {
                const only = r.pick[0];
                return done({ ...s, center: only && paneKind(only) !== 'session' ? { kind: 'single', pane: only } : SINGLE });
            }
            // a zoom on the removed pane: the active session
            return done(s.center.kind === 'single' && s.center.pane === a.id ? { ...s, center: SINGLE } : s);
        }
        case 'pane': {
            // On screen = closes; Single adds beside just its visible pane. A remembered grid is restored only explicitly.
            // Without the grid module: only the beside pair (one session + Files / HTTP / its Database).
            const why = c.paneBlock(a.pane);
            if (why)
                return { state: s, fx: { refuse: why } };
            if (term && (c.shown ?? []).includes(a.pane))
                return reduceRemove(s, a.pane, c, fx);
            if (s.center.kind === 'grid' && c.gridIds.includes(a.pane)) {
                if (!term)
                    fx.toTerm = true;
                return done(s.center.kind === 'grid' ? s : { ...s, center: GRID });
            }
            const members = editMembers(s, c);
            const base = c.gridOn ? [...members] : members.filter((x) => x === c.active || (paneKind(x) !== 'session' && besideOnly([c.active, x])));
            // A project tool always has its agent beside it, even after exit or from a zoomed tool.
            if (!base.includes(c.active))
                base.unshift(c.active);
            const simple = besideOnly([...base, a.pane]);
            // Beside is one tool slot. Explicit Add / Choose keep the full grid available.
            const r = simple ? { pick: [c.active, a.pane] } : addPane(base, a.pane, c.focus ?? '', c.active);
            if (!c.gridOn && !besideOnly(r.pick))
                return { state: s, fx: { refuse: GRID_OFF } };
            return reducePick(s, r, c, fx);
        }
        case 'strip': {
            const z = leaveZen(s);
            if (z.left === 'hidden' || !QUEUE_VIEWS.has(c.view))
                return reduceQueue(z, true, c, fx);
            // a folded queue (Files / HTTP) opens whole there, the stored mode stays
            if (z.leftFold || z.leftNarrow) {
                fx.focus = 'queue';
                return done({ ...z, leftFold: false, leftNarrow: false, left: 'queue', leftBack: 'queue' });
            }
            const next = z.left === 'strip' ? 'queue' : 'strip';
            fx.focus = next === 'queue' ? 'queue' : 'detail';
            return done({ ...z, left: next, leftBack: next });
        }
        // Overview returns to the session first; only its active icon folds to the strip. The grid stays: there the icon
        // only opens / folds the queue beside it (a zoomed tool still returns to the active session).
        case 'overview': {
            const z = leaveZen(s);
            if (a.on === false) {
                fx.focus = 'detail';
                return done({ ...z, left: 'strip', leftBack: 'strip', leftFold: false, leftNarrow: false });
            }
            fx.focus = 'queue';
            if (!term)
                fx.toTerm = true;
            return done({ ...z, center: z.center.kind === 'grid' ? z.center : SINGLE, right: z.right === 'agent' ? z.rightBack : z.right,
                left: 'queue', leftBack: 'queue', leftFold: false, leftNarrow: false });
        }
        case 'narrow':
            return done(s.left === 'queue' && !s.leftNarrow ? { ...s, leftNarrow: true } : s);
        case 'queue': {
            const shown = s.left !== 'hidden' && QUEUE_VIEWS.has(c.view);
            return reduceQueue(leaveZen(s), a.on ?? !shown, c, fx);
        }
        case 'context': {
            if (!c.sessionView)
                return { state: s, fx: { refuse: TOAST.perSession } };
            const z = leaveZen(s);
            // I7: the drawer gives the column to the context panel
            const right = z.right === 'context' ? 'hidden' : 'context';
            return done({ ...z, right, rightBack: right === 'context' ? 'context' : 'hidden' });
        }
        case 'agent': {
            const on = a.on ?? s.right !== 'agent';
            if (on && !c.feedOn)
                return { state: s, fx: { refuse: 'The Feed is off - the Global agent has its terminal' } };
            const z = leaveZen(s);
            if (!on)
                return done(z.right === 'agent' ? { ...z, right: z.rightBack } : z);
            if (z.right === 'agent')
                return done(z);
            return done({ ...z, right: 'agent', rightBack: z.right === 'context' ? 'context' : 'hidden' });
        }
        case 'zen': {
            if (s.zen)
                return done({ ...s, left: s.zen.left, right: s.zen.right, zen: null });
            fx.toast = TOAST.zen;
            return done({ ...s, left: 'hidden', right: 'hidden', zen: { left: s.left, right: s.right } });
        }
        case 'activate': {
            // 9.45: in the grid = only the focus; a live session not in it takes the focused session pane; one without a
            // terminal shows alone (the pick stays). Single stays Single
            if (!gridOpen(s, c) || c.gridIds.includes(a.id))
                return done(s);
            if (c.hasPty(a.id)) {
                fx.pick = replaceFocused(c.gridIds, a.id, c.focus ?? '', c.active);
                return done(s);
            }
            return done({ ...s, center: SINGLE });
        }
        case 'view': {
            const fold = FOLD_VIEWS.has(a.to) && (s.leftFold || (s.left === 'queue' && !FOLD_VIEWS.has(c.view)));
            // N10: the rail's Inbox / Production / a session tile showed nothing under the whole queue - the move shows its view
            // (Overview resets it after its own move to the terminal)
            const narrow = !!c.narrow && s.left === 'queue' && !s.leftNarrow && a.to !== c.view;
            if (narrow)
                return done({ ...s, leftFold: fold, leftNarrow: true });
            return done(fold === s.leftFold ? s : { ...s, leftFold: fold });
        }
        case 'reconcile': {
            let next = s;
            if (next.center.kind === 'grid' && !c.gridOn && !besideOnly(c.gridIds))
                next = { ...next, center: SINGLE };
            // a zoomed pane gone (its module off, the shell closed): the active session
            if (next.center.kind === 'single' && next.center.pane && !c.gridIds.includes(next.center.pane))
                next = { ...next, center: SINGLE };
            if (next.right === 'agent' && !c.feedOn)
                next = { ...next, right: next.rightBack };
            return done(next);
        }
    }
}
/** an add: the new pick, the grid on screen (a zoom ends, another tab goes to the terminal) */
function reducePick(s, r, c, fx) {
    if (r.refuse)
        return { state: s, fx: { refuse: r.refuse } };
    if (c.view !== 'term')
        fx.toTerm = true;
    if (r.pick !== c.gridIds)
        fx.pick = r.pick;
    const only = r.pick.length === 1 ? r.pick[0] : '';
    const center = only && paneKind(only) !== 'session' ? { kind: 'single', pane: only } : GRID;
    return { state: s.center.kind === 'grid' && center.kind === 'grid' ? s : { ...s, center }, fx };
}
function reduceRemove(s, pane, c, fx) {
    const r = layoutReduce(s, { t: 'remove', id: pane }, c);
    return { state: r.state, fx: { ...fx, ...r.fx } };
}
/** the Overview icon: the left shown / hidden; asked for from a view without it = the hub (Terminal or GitLab) */
function reduceQueue(s, on, c, fx) {
    if (!on) {
        fx.focus = 'detail';
        return { state: { ...s, left: 'hidden', leftFold: false, leftNarrow: false }, fx };
    }
    fx.focus = 'queue';
    if (!QUEUE_VIEWS.has(c.view))
        fx.hub = true;
    return { state: { ...s, left: s.left === 'hidden' ? s.leftBack : s.left }, fx };
}
// ---- what is on screen (main.ts renders from these)
/** the queue (whole or strip) is beside the view */
export function queueShown(s, view) {
    return s.left !== 'hidden' && QUEUE_VIEWS.has(view);
}
/** the strip instead of the whole queue (the user's mode, or folded by Files / HTTP) */
export function stripShown(s) {
    return s.left === 'strip' || s.leftFold || !!s.leftNarrow;
}
/** the grid (not a zoom) is the center of the terminal view; `n` = its panes, `add` = the Add cell shows (9.45: a
 *  grid of one pane is Single) */
export function gridShown(s, view, n = 2, add = false) {
    return view === 'term' && s.center.kind === 'grid' && (n > 1 || add);
}
// ---- persistence: one key, `piper.layout`
export const LAYOUT_KEY = 'piper.layout';
export const LEGACY_KEYS = ['piper.ctx', 'piper.side', 'piper.sidedb', 'piper.queue.hidden', 'piper.queue.mode'];
/** what is stored: the sides always; the center (grid; its panes are piper.grid) only with policy restore_view;
 *  the agent drawer, ⇧⌘F12 and the Files fold never */
export function persisted(s, restoreView) {
    const out = { v: 1, left: s.zen ? s.zen.left : s.left, leftBack: s.leftBack,
        right: (() => { const r = s.zen ? s.zen.right : s.right; return r === 'agent' ? s.rightBack : r; })() };
    if (restoreView && s.center.kind !== 'single')
        out.center = s.center;
    return out;
}
/** `piper.layout`, else the five keys before W12 (ctx, side, sidedb, queue.hidden, queue.mode), else the default.
 *  D1/D2: a stored Beside (`aside`, piper.side) is dropped, a split comes back Single, a grid without its old `back` */
export function migrateLayout(get, width) {
    const d = defaultLayout(width >= 1400);
    try {
        const o = JSON.parse(get(LAYOUT_KEY) ?? 'null');
        if (o && o.v === 1) {
            const left = ['queue', 'strip', 'hidden'].find((x) => x === o.left) ?? d.left;
            const leftBack = ['queue', 'strip'].find((x) => x === o.leftBack) ?? (left === 'hidden' ? 'strip' : left);
            const right = ['context', 'hidden'].find((x) => x === o.right) ?? (d.right === 'context' ? 'context' : 'hidden');
            const c = o.center;
            return { ...d, left, leftBack, right, rightBack: right, center: c?.kind === 'grid' ? GRID : SINGLE };
        }
    }
    catch { /* a bad entry: the old keys */ }
    const ctx = get('piper.ctx');
    const right = ctx === null ? d.right : ctx === '1' ? 'context' : 'hidden';
    const mode = get('piper.queue.mode') === 'full' ? 'queue' : 'strip';
    const left = get('piper.queue.hidden') === '1' ? 'hidden' : mode;
    return { ...d, left, leftBack: mode, right, rightBack: right === 'context' ? 'context' : 'hidden' };
}
/** `add`: the Add cell shows - a grid of one pane + Add is on screen, so ⌘G reads "Grid: off" (as syncLayout draws it) */
export function layoutCommands(s, view, gridMax, n = 2, add = false, restoreN = n) {
    const grid = gridShown(s, view, n, add);
    return [
        { label: grid ? 'Grid: off (Single, the grid keeps its panes)' : restoreN > 1 ? `Grid: its ${restoreN} panes` : `Grid: up to ${gridMax} panes at once`, hint: '⌘G', action: { t: 'grid' }, module: 'grid' },
        { label: 'Grid: add a pane…', hint: '', action: { t: 'addCell' }, module: 'grid' },
        { label: s.center.kind === 'single' ? 'Single terminal' : 'Single terminal (leave the grid)', hint: '', action: { t: 'single' } },
        { label: s.zen ? 'Restore the panels' : 'Hide all panels', hint: '⇧⌘F12', action: { t: 'zen' } },
    ];
}
//# sourceMappingURL=layout.js.map