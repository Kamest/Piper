// Turn snapshots: piperd snapshots the working tree when a prompt
// is submitted and when Claude stops; the GUI diffs one turn instead of the whole session.
// Pure helpers only - the UI lives in gitpanel.ts (panel, modal) and board.ts (card line).
import { cutText } from './text.js';
/** "turn 3 · first line of the prompt…" (the picker and the modal title) */
export function turnLabel(t, max = 60) {
    const first = (t.prompt.split('\n').find((l) => l.trim()) ?? '').trim();
    const prompt = first.length > max ? cutText(first, max - 1) + '…' : first;
    return `turn ${t.n}${prompt ? ' · ' + prompt : ''}${t.to === null ? ' (running)' : ''}`;
}
/** the newest turn (highest n), or null */
export function lastTurn(turns) {
    let best = null;
    for (const t of turns)
        if (!best || t.n > best.n)
            best = t;
    return best;
}
/** the chosen turn, falling back to the newest when n is null or no longer listed */
export function pickTurn(turns, n) {
    return (n === null ? null : turns.find((t) => t.n === n)) ?? lastTurn(turns);
}
/** the diff range of a turn: an open turn is diffed against the working tree now ('') */
export function turnRange(t) {
    return { from: t.from, to: t.to ?? '' };
}
/** 1 file, 0/2+ files */
export function filesLabel(n) {
    return `${n} ${n === 1 ? 'file' : 'files'}`;
}
function unquote(p) {
    return p.length > 1 && p.startsWith('"') && p.endsWith('"') ? p.slice(1, -1).replace(/\\"/g, '"') : p;
}
/**
 * The file each line of a (multi-file) unified diff belongs to, from the headers:
 * `diff --git a/x b/y` starts a file, `+++ b/y` names it (`--- a/x` when the new side
 * is /dev/null). Only header (meta) lines are read, so an added `++` line inside a hunk
 * is never taken for a header.
 */
export function diffPaths(lines) {
    const out = [];
    let cur = '';
    let minus = '';
    for (const d of lines) {
        if (d.cls === 'meta') {
            const g = /^diff --git (?:"?a\/)(.+?)"? (?:"?b\/)(.+?)"?$/.exec(d.text);
            if (g) {
                cur = unquote(g[2]);
                minus = '';
            }
            else if (d.text.startsWith('--- ')) {
                const p = unquote(d.text.slice(4));
                minus = p.startsWith('a/') ? p.slice(2) : '';
            }
            else if (d.text.startsWith('+++ ')) {
                const p = unquote(d.text.slice(4));
                cur = p.startsWith('b/') ? p.slice(2) : minus || cur;
            }
        }
        out.push(cur);
    }
    return out;
}
//# sourceMappingURL=turns.js.map