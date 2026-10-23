// Side-by-side diff of one file: a unified diff paired into two
// columns - a run of removed lines next to the run of added lines that replaces it, context
// on both sides. Pure pairing (tested) + a chunked renderer that highlights each side.
import { h } from './dom.js';
import { highlightLines } from './highlight.js';
import { showBidi } from './text.js';
export const isPair = (r) => r.kind !== 'hunk' && r.kind !== 'file';
/** unified diff → rows of two columns; the headers before the first @@ are dropped */
export function pairDiff(unified) {
    const rows = [];
    let dels = [];
    let adds = [];
    let oldNo = 0;
    let newNo = 0;
    let inHunk = false;
    let files = 0;
    const flush = () => {
        const n = Math.max(dels.length, adds.length);
        for (let k = 0; k < n; k++) {
            const left = dels[k] ?? null;
            const right = adds[k] ?? null;
            rows.push({ kind: left && right ? 'chg' : left ? 'del' : 'add', left, right });
        }
        dels = [];
        adds = [];
    };
    const lines = unified.replace(/\r\n/g, '\n').split('\n');
    if (lines[lines.length - 1] === '')
        lines.pop();
    for (const l of lines) {
        if (l.startsWith('diff --git ') || (!inHunk && l.startsWith('diff '))) {
            flush();
            inHunk = false;
            if (++files > 1 || rows.length)
                rows.push({ kind: 'file', text: l.replace(/^diff --git a\/(.*) b\/.*$/, '$1') });
            continue;
        }
        const hm = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(l);
        if (hm) {
            flush();
            oldNo = Number(hm[1]);
            newNo = Number(hm[2]);
            inHunk = true;
            rows.push({ kind: 'hunk', text: l });
            continue;
        }
        if (!inHunk)
            continue;
        const c = l[0];
        if (c === '-')
            dels.push({ no: oldNo++, text: l.slice(1) });
        else if (c === '+')
            adds.push({ no: newNo++, text: l.slice(1) });
        else if (c === '\\')
            continue; // "\ No newline at end of file"
        else if (c === ' ' || l === '') {
            flush();
            const text = l.slice(1);
            rows.push({ kind: 'ctx', left: { no: oldNo++, text }, right: { no: newNo++, text } });
        }
        else {
            // anything else ends the hunk (e.g. the next file's headers without `diff --git`)
            flush();
            inHunk = false;
        }
    }
    flush();
    return rows;
}
/** +added −removed of paired rows */
export function diffStat(rows) {
    let add = 0;
    let del = 0;
    for (const r of rows) {
        if (!isPair(r))
            continue;
        if (r.kind !== 'ctx') {
            if (r.left)
                del++;
            if (r.right)
                add++;
        }
    }
    return { add, del };
}
/** tokens per side: each column is highlighted as one flowing text (block comments carry over) */
export function sideTokens(rows, lang) {
    const side = (pick) => {
        const idx = [];
        const texts = [];
        rows.forEach((r, i) => {
            const c = pick(r);
            if (c) {
                idx.push(i);
                texts.push(c.text);
            }
        });
        const toks = highlightLines(texts, lang);
        const out = rows.map(() => null);
        idx.forEach((i, k) => { out[i] = toks[k] ?? null; });
        return out;
    };
    const cell = (r, left) => (!isPair(r) ? null : left ? r.left : r.right);
    return { left: side((r) => cell(r, true)), right: side((r) => cell(r, false)) };
}
const CHUNK = 300;
/** the two-column view; clicking a new-side line number calls onLine(n) */
export function renderSideDiff(rows, lang, onLine) {
    const box = h('div', { class: 'sbs mono' });
    const { left, right } = sideTokens(rows, lang);
    const text = (toks, fallback) => {
        const tx = h('span', { class: 'tx' });
        // the code of a turn: a bidi control is a mark, not a reordering (Trojan Source)
        if (toks)
            for (const t of toks)
                tx.append(t.c ? h('span', { class: 'hl-' + t.c }, showBidi(t.t)) : showBidi(t.t));
        else
            tx.append(showBidi(fallback));
        if (!tx.textContent)
            tx.append(' ');
        return tx;
    };
    let i = 0;
    const step = () => {
        const frag = document.createDocumentFragment();
        const end = Math.min(i + CHUNK, rows.length);
        for (; i < end; i++) {
            const r = rows[i];
            if (!isPair(r)) {
                frag.append(h('div', { class: 'sbs-' + r.kind }, showBidi(r.text)));
                continue;
            }
            const l = r.left;
            const rt = r.right;
            const rk = r.kind;
            frag.append(h('div', { class: 'sbs-row ' + rk }, h('span', { class: 'no' + (l && rk !== 'ctx' ? ' del' : '') }, l ? String(l.no) : ''), l ? h('span', { class: 'cell' + (rk === 'ctx' ? '' : ' del') }, text(left[i] ?? null, l.text)) : h('span', { class: 'cell void' }), h('span', {
                class: 'no' + (rt && rk !== 'ctx' ? ' add' : '') + (rt ? ' go' : ''), title: rt ? 'open at the line' : undefined,
                onclick: rt ? () => onLine(rt.no) : undefined,
            }, rt ? String(rt.no) : ''), rt ? h('span', { class: 'cell' + (rk === 'ctx' ? '' : ' add') }, text(right[i] ?? null, rt.text)) : h('span', { class: 'cell void' })));
        }
        box.append(frag);
        if (i < rows.length)
            window.requestAnimationFrame(step);
    };
    step();
    return box;
}
//# sourceMappingURL=sidediff.js.map