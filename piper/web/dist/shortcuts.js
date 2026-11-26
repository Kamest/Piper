// The one registry of Piper's keys: the Keys dialog is drawn from it and the global key
// handler asks it (keyGate) whether a key is live - so a module that hides a row also lets the key through.
import { GRID_MAX } from './gridlayout.js';
const fk = (id, k, d, keys, editor = false) => ({ id, k, d, keys, editor, ctrl: true });
export const FILE_KEYS = {
    intellij: [
        fk('recent', '⌘E', 'recent files', ['Mod-KeyE']),
        fk('gotoFile', '⌘⇧O', 'go to file (as ⌘P)', ['Mod-Shift-KeyO']),
        fk('outline', '⌘F12', 'file structure', ['Mod-F12']),
        fk('definition', '⌘B / ⌘+click', 'go to declaration', ['Mod-KeyB'], true),
        fk('addCaret', '⌥+click', 'add a caret', [], true),
        fk('duplicateLine', '⌘D', 'duplicate the line or selection', ['Mod-KeyD'], true),
        fk('deleteLine', '⌘⌫', 'delete the line', ['Mod-Backspace'], true),
        fk('moveLineUp', '⌥⇧↑', 'move the line up', ['Alt-Shift-ArrowUp'], true),
        fk('moveLineDown', '⌥⇧↓', 'move the line down', ['Alt-Shift-ArrowDown'], true),
        fk('extendSelection', '⌥↑', 'extend the selection', ['Alt-ArrowUp'], true),
        fk('shrinkSelection', '⌥↓', 'shrink the selection', ['Alt-ArrowDown'], true),
        fk('nextOccurrence', '⌃G', 'add the next occurrence', ['Ctrl-KeyG'], true),
        fk('allOccurrences', '⌃⌘G', 'select all occurrences', ['Ctrl-Mod-KeyG'], true),
        fk('replace', '⌘R', 'replace in the file', ['Mod-KeyR'], true),
        fk('closeTab', '⌘W / ⌥W', 'close the tab (⌘W in Piper Desktop)', ['Mod-KeyW', 'Alt-KeyW']),
        fk('prevTab', '⌘⇧[ / ⌃⇧Tab', 'previous tab', ['Mod-Shift-BracketLeft', 'Ctrl-Shift-Tab']),
        fk('nextTab', '⌘⇧] / ⌃Tab', 'next tab', ['Mod-Shift-BracketRight', 'Ctrl-Tab']),
    ],
    classic: [
        fk('outline', '⌘⇧O', 'outline', ['Mod-Shift-KeyO']),
        fk('definition', '⌘+click', 'go to definition', [], true),
        fk('addCaret', '⌥+click', 'add a caret', [], true),
        fk('nextOccurrence', '⌘D', 'add the next occurrence', ['Mod-KeyD'], true),
        fk('moveLineUp', '⌥⇧↑ / ⌥↑', 'move the line up', ['Alt-Shift-ArrowUp', 'Alt-ArrowUp'], true),
        fk('moveLineDown', '⌥⇧↓ / ⌥↓', 'move the line down', ['Alt-Shift-ArrowDown', 'Alt-ArrowDown'], true),
        fk('closeTab', '⌥W', 'close the tab', ['Alt-KeyW']),
    ],
};
/** a key press as FILE_KEYS writes it: Ctrl (⌃ on a Mac), Mod (⌘, Ctrl elsewhere), Alt, Shift and the physical key
 *  (KeyboardEvent.code); '' for the Windows key off a Mac */
export function keyName(e, mac) {
    if (!mac && e.metaKey)
        return '';
    const mods = [mac && e.ctrlKey ? 'Ctrl' : '', (mac ? e.metaKey : e.ctrlKey) ? 'Mod' : '', e.altKey ? 'Alt' : '', e.shiftKey ? 'Shift' : ''];
    return [...mods.filter(Boolean), e.code].join('-');
}
/** report §4.5 rule 4: ⌥ + a letter is ours only where the layout types no ASCII character with it - US ⌥W is ∑ (ours),
 *  a layout that types | or @ with ⌥ keeps the character */
export function altOwned(e) {
    if (!e.altKey || e.metaKey || e.ctrlKey || e.key.length !== 1)
        return true;
    return !/^[\x21-\x7e]$/.test(e.key) || e.key.toLowerCase() === e.code.replace(/^Key/, '').toLowerCase();
}
/** IntelliJ's keymap is its macOS one (⌃G, ⌃Tab, ⌘⌫ have other meanings off a Mac): elsewhere Files keeps Piper classic */
export const filesKeymapFor = (keymap, mac) => mac ? keymap : 'classic';
/** the Files group of the Keys dialog for a keymap: the keys both keymaps share, then the keymap's own */
function filesRows(keymap) {
    const own = FILE_KEYS[keymap].map(({ k, d }) => ({ k, d, ctrl: true }));
    return [...FILES_BASE.slice(0, 6), ...own, ...FILES_BASE.slice(6)];
}
const FILES_BASE = [
    { k: '⌘P', d: 'open a file (Foo.java:120, #Symbol)' },
    { k: '⌘O', d: 'Files pane in the grid (again: out)' },
    { k: '⌘D', d: 'Database pane in the grid (again: out)', gate: 'db' },
    { k: '⌘⇧F', d: 'search the project' },
    { k: '⌥⇧S', d: 'new scratch file (from anywhere)' },
    { k: '⌘L', d: 'go to line', ctrl: true },
    { k: '⌥F7', d: 'find usages (F4 / ⇧F4 step)' },
    { k: '⌘⌥↓', d: 'tool panel', ctrl: true },
    { k: '⌥⏎', d: 'path:line into the prompt' },
    { k: '⌘⇧D', d: 'diff against HEAD', ctrl: true },
    { k: '⌘⌥A', d: 'actions for the selected code', ctrl: true },
    { k: '⌘[ / ⌘]', d: 'back / forward', ctrl: true },
    { k: '⌘S', d: 'save', ctrl: true },
    { k: '⌘/', d: 'comment', ctrl: true },
    { k: '⌘⌥[ / ⌘⌥]', d: 'fold / unfold all', ctrl: true },
    { k: '⌘⇧E', d: 'keyboard to the tree (typing filters)' },
    { k: '⌥N · F6 · ⇧F6 · ⌫', d: 'tree: new · move · rename · delete' },
    { k: 'click in the gutter', d: 'actions for the line' }
];
export const SHORTCUTS = [
    { title: 'Navigation', rows: [
            { k: '⌘K', d: 'search & commands' },
            { k: '⌘B', d: 'queue: whole ↔ strip (outside the Files editor)' },
            { k: '⌘⇧L', d: 'keyboard into the queue' },
            { k: '⌘J', d: 'next item that needs you' },
            { k: '⌘⇧J', d: 'quick note (a line in notes.md, a pasted file)', gate: 'notes' },
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
            { k: '⌘E', d: 'Composer: open, full, collapse (outside Files)', gate: 'composer' },
            { k: '⌘F', d: 'find in the terminal / the open file' },
            { k: 'click on a path', d: 'menu: Files, beside, IntelliJ, copy (⌘/⇧-click: IntelliJ)' },
            { k: 'Shift/⌥+Enter', d: 'new line in Claude Code' },
            { k: 'Esc, Ctrl+C, Ctrl+K', d: 'belong to the terminal' }
        ] },
    { title: 'Files', rows: filesRows('intellij') },
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
/** the groups and rows whose gate is on, Files with the keymap in use; a group without a live row goes */
export function shownShortcuts(isOn, keymap = 'intellij') {
    const live = (g) => !g || isOn(g);
    return shortcutsFor(keymap).filter((g) => live(g.gate)).map((g) => ({ ...g, rows: g.rows.filter((r) => live(r.gate)) }))
        .filter((g) => g.rows.length);
}
/** the registry with the Files group and the ⌘E / ⌘B notes of a keymap (Piper classic: ⌘E and ⌘B are Piper's in Files too) */
export function shortcutsFor(keymap) {
    if (keymap === 'intellij')
        return SHORTCUTS;
    const plain = (r) => ({ ...r, d: r.d.replace(/ \(outside (the Files editor|Files)\)$/, '') });
    return SHORTCUTS.map((g) => g.title === 'Files' ? { ...g, rows: filesRows(keymap) }
        : g.title === 'Navigation' || g.title === 'Session' ? { ...g, rows: g.rows.map(plain) } : g);
}
//# sourceMappingURL=shortcuts.js.map