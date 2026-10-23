// The one registry of Piper's keys: the Keys dialog is drawn from it and the global key
// handler asks it (keyGate) whether a key is live - so a module that hides a row also lets the key through.
import { GRID_MAX } from './gridlayout.js';
export const SHORTCUTS = [
    { title: 'Navigation', rows: [
            { k: '⌘K', d: 'search & commands' },
            { k: '⌘B', d: 'queue: whole ↔ strip' },
            { k: '⌘⇧L', d: 'keyboard into the queue' },
            { k: '⌘J', d: 'next item that needs you' },
            { k: '⌘1–9', d: 'session 1–9 (the number on its tile)' },
            { k: '⌘⇧↑ / ⌘⇧↓', d: 'move a session in Running' },
            { k: 'Mouse ◀ ▶ · ⌘[ ⌘]', d: 'back / forward', gate: 'nav' },
            { k: '⌘0', d: 'Global agent', gate: 'agent' },
            { k: '⌘.', d: 'chat with the Global agent', gate: 'feed' },
            { k: '⌘G', d: `Single ↔ the grid's panes (up to ${GRID_MAX})`, gate: 'grid' },
            { k: '⌘\\', d: 'add the previous session to the grid', gate: 'grid' },
            { k: '⇧⌘⏎', d: 'the focused grid pane alone (again: the grid)', gate: 'grid' },
            { k: '⌘I', d: 'context panel' },
            { k: '⇧⌘F12', d: 'hide all panels (again: back)' },
            { k: '⌥F12', d: 'host shells (⌥⇧T new, ⌥⇧W close)', gate: 'host' },
            { k: '⌘,', d: 'Settings' },
            { k: 'right click on a session', d: 'its actions' }
        ] },
    { title: 'Queue (when it has the focus)', rows: [
            { k: 'j k / ↑↓', d: 'move' },
            { k: 'Enter / o', d: 'open the detail' },
            { k: 'r', d: 'reply without leaving the queue' },
            { k: 'y / n', d: 'allow / deny a confirmation' },
            { k: 'e', d: 'dismiss the row' },
            { k: 'm', d: 'mute the subject for an hour', gate: 'agent' },
            { k: 'a', d: 'hand it to the Global agent', gate: 'agent' },
            { k: '/', d: 'search the queue' },
            { k: '?', d: 'these shortcuts' },
            { k: 'Esc', d: 'back to the terminal' }
        ] },
    { title: 'Session', rows: [
            { k: '⌘E', d: 'Composer: open, full, collapse', gate: 'composer' },
            { k: '⌘F', d: 'find in the terminal / the open file' },
            { k: 'click on a path', d: 'menu: Files, beside, IntelliJ, copy (⌘/⇧-click: IntelliJ)' },
            { k: 'Shift/⌥+Enter', d: 'new line in Claude Code' },
            { k: 'Esc, Ctrl+C, Ctrl+K', d: 'belong to the terminal' }
        ] },
    { title: 'Files', rows: [
            { k: '⌘P', d: 'open a file (Foo.java:120, #Symbol)' },
            { k: '⌘O', d: 'Files pane in the grid (again: out)' },
            { k: '⌘D', d: 'Database pane in the grid (again: out)', gate: 'db' },
            { k: '⌘⇧F', d: 'search the project' },
            { k: '⌘L', d: 'go to line', ctrl: true },
            { k: '⌘⇧O', d: 'outline' },
            { k: '⌘+click / ⌥+click', d: 'go to definition' },
            { k: '⌥F7', d: 'find usages (F4 / ⇧F4 step)' },
            { k: '⌘⌥↓', d: 'tool panel', ctrl: true },
            { k: '⌥⏎', d: 'path:line into the prompt' },
            { k: '⌘⇧D', d: 'diff against HEAD', ctrl: true },
            { k: '⌘⌥A', d: 'actions for the selected code', ctrl: true },
            { k: '⌘[ / ⌘]', d: 'back / forward', ctrl: true },
            { k: '⌘S', d: 'save', ctrl: true },
            { k: '⌘D · ⌘/ · ⌥⇧↑↓', d: 'next occurrence · comment · move line', ctrl: true },
            { k: '⌘⌥[ / ⌘⌥]', d: 'fold / unfold all', ctrl: true },
            { k: '⌘⇧E', d: 'keyboard to the tree (typing filters)' },
            { k: '⌥N · F6 · ⇧F6 · ⌫', d: 'tree: new · move · rename · delete' },
            { k: '⌥W', d: 'close tab (outside the editor)' },
            { k: 'click in the gutter', d: 'actions for the line' }
        ] },
    { title: 'HTTP tab', gate: 'http', ctrl: true, rows: [
            { k: 'click / Enter on a request', d: 'fold / unfold' },
            { k: '↑↓', d: 'move between requests' },
            { k: 'r · ⌘⏎', d: 'run the request' },
            { k: 'o · double-click', d: 'open it in Files' },
            { k: '▶', d: 'run one · Run all runs the file' }
        ] },
    { title: 'Inbox view', gate: 'inbox', rows: [
            { k: '↑↓ / j k', d: 'move' },
            { k: 'Enter', d: 'main action' },
            { k: 'Space / o', d: 'preview' },
            { k: 'e · g · /', d: 'mark read · open in the browser · search' },
            { k: 'Esc', d: 'close the preview, else back' }
        ] },
    { title: 'Database', gate: 'db', ctrl: true, rows: [
            { k: '⌘⇧L', d: 'new SQL console' },
            { k: '⌘P', d: 'find a table' },
            { k: '⌘↩', d: 'run the statement / the selection' },
            { k: '⌘E', d: 'explain' },
            { k: '⌘⇧↩', d: 'commit the transaction' },
            { k: '⌃Space', d: 'complete' },
            { k: '⌘S', d: 'submit the table\'s edits' },
            { k: '⌘⌫ · ⌘⌥N', d: 'delete the row · set NULL' }
        ] },
];
/** the Navigation row of a global key - the handler's gate (undefined = always live) */
export function keyGate(k) {
    return SHORTCUTS[0].rows.find((r) => r.k === k)?.gate;
}
/** N4: a row's keys as chips - alternatives (" / ", " · ", ", ") with their separator between them, short keys
 *  pressed in turn ("j k", "⌘[ ⌘]") as a chip each; a phrase ("right click on a session") stays words, not a chip */
export function keyChips(k) {
    const out = [];
    const parts = k.split(/( \/ | · |, )/);
    for (const p of parts) {
        if (/^( \/ | · |, )$/.test(p)) {
            out.push({ t: p.trim() === ',' ? '·' : p.trim(), kind: 'sep' });
            continue;
        }
        const words = p.split(' ');
        if (words.length > 1 && /\b[a-z]{4,}\b/.test(p))
            out.push({ t: p, kind: 'text' });
        else if (words.length > 1 && words.every((w) => w.length <= 3))
            for (const w of words)
                out.push({ t: w, kind: 'key' });
        else
            out.push({ t: p, kind: 'key' });
    }
    return out;
}
/** N4: the Keys filter - a row matches on its description or its keys, a group's title shows all its rows */
export function filterShortcuts(groups, q) {
    const s = q.trim().toLowerCase();
    if (!s)
        return groups;
    return groups.map((g) => g.title.toLowerCase().includes(s) ? g
        : { ...g, rows: g.rows.filter((r) => r.d.toLowerCase().includes(s) || r.k.toLowerCase().includes(s)) })
        .filter((g) => g.rows.length);
}
/** the groups and rows whose gate is on; a group without a live row goes */
export function shownShortcuts(isOn) {
    const live = (g) => !g || isOn(g);
    return SHORTCUTS.filter((g) => live(g.gate)).map((g) => ({ ...g, rows: g.rows.filter((r) => live(r.gate)) }))
        .filter((g) => g.rows.length);
}
//# sourceMappingURL=shortcuts.js.map