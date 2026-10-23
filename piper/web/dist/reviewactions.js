import { h } from './dom.js';
export function reviewDigit(event) {
    if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing)
        return null;
    const match = /^(?:Digit|Numpad)(\d)$/.exec(event.code);
    return match ? Number(match[1]) : /^\d$/.test(event.key) ? Number(event.key) : null;
}
export function reviewTargetKey(target) {
    return JSON.stringify([target.path, target.old, target.from, target.to, target.sha, target.code]);
}
export function toggleReviewAction(items, target, action) {
    const key = reviewTargetKey(target);
    const found = items.find(x => x.state === 'open' && reviewTargetKey(x) === key);
    if (!action)
        return items.filter(x => x !== found);
    if (!found)
        return [...items, { ...target, id: crypto.randomUUID(), actions: [{ ...action }], text: '', state: 'open' }];
    const actions = found.actions.some(x => x.id === action.id) ? found.actions.filter(x => x.id !== action.id) : [...found.actions, { ...action }];
    return items.map(x => x === found ? { ...x, actions } : x).filter(x => x.actions.length || x.text.trim() || x.state !== 'open');
}
export function renderReviewInstruction(action, item) {
    const range = item.from === item.to ? String(item.from) : `${item.from}–${item.to}`;
    const values = { target: `${item.path}:${range}${item.old ? ' (original version)' : ''}`, file: item.path,
        range, side: item.old ? 'original' : 'working', code: item.code, note: item.text };
    return action.prompt.replace(/\{\{(target|file|range|side|code|note)\}\}/g, (_, name) => values[name]);
}
export function reviewCodeAppendix(items) {
    return items.filter(x => x.state === 'open' && !x.old && (x.actions.length || x.text.trim()))
        .map(x => `\n\nReviewed working code [${x.id}]:\n${x.code}`).join('');
}
export function reviewPrompt(items, includeCode = false) {
    const open = [...items].filter(x => x.state === 'open' && (x.actions.length || x.text.trim()))
        .sort((a, b) => a.path.localeCompare(b.path) || a.from - b.from || Number(a.old) - Number(b.old));
    const edit = open.some(x => x.actions.some(a => a.kind === 'edit') || (!x.actions.length && !!x.text.trim()));
    const body = ['Review task', edit ? 'Implement the requested edits; answer read/check actions first.' : 'Explain/check only. Do not modify files.',
        'Read project instructions and the referenced files from disk. Verify each working SHA256 before acting; stop for changed targets. Original-side code is reference material. Keep overlapping requests distinct. Report results and verification by item ID. Do not commit or push.'];
    const versions = new Set();
    for (const item of open) {
        const version = JSON.stringify([item.path, item.sha]);
        if (!versions.has(version)) {
            body.push(`\n${item.path} · Working SHA256: ${item.sha ?? '(missing file)'}`);
            versions.add(version);
        }
        body.push(`[${item.id}] Lines ${item.from}–${item.to}${item.old ? ' · ORIGINAL SIDE' : ''}`);
        const overlap = open.filter(x => x !== item && x.path === item.path && x.old === item.old && x.from <= item.to && x.to >= item.from);
        if (overlap.length)
            body.push('Overlaps: ' + overlap.map(x => x.id).join(', '));
        if (item.old)
            body.push('Reviewed original code:\n' + item.code);
        for (const action of [...item.actions].sort((a, b) => Number(a.kind === 'edit') - Number(b.kind === 'edit') || a.key - b.key)) {
            body.push(`${action.label} (${action.kind}): ${renderReviewInstruction(action, item)}`);
        }
        if (item.text.trim() && !item.actions.some(x => x.prompt.includes('{{note}}')))
            body.push('Reviewer instruction: ' + item.text.trim());
    }
    return body.join('\n') + (includeCode ? reviewCodeAppendix(open) : '');
}
export function actionMenu(actions, selected, choose) {
    return h('div', { class: 'review-action-menu', role: 'group', 'aria-label': 'Code review actions' }, ...actions.map(action => h('button', { type: 'button', 'aria-pressed': String(selected.some(x => x.id === action.id)),
        'aria-keyshortcuts': String(action.key), title: action.prompt, onclick: () => choose(action) }, h('kbd', {}, String(action.key)), action.label)), h('button', { type: 'button', onclick: () => choose(null) }, h('kbd', {}, '0'), 'Clear instructions'));
}
export function revertText(left, right, chunk, selection) {
    if (!selection || selection.from === selection.to)
        return right.slice(0, chunk.fromB) + left.slice(chunk.fromA, chunk.toA) + right.slice(chunk.toB);
    if (selection.from < chunk.fromB || selection.to > chunk.toB)
        throw new Error('Select rows within one change block, or restore the whole block.');
    const rows = (text) => text.match(/[^\n]*\n|[^\n]+$/g) ?? [];
    const a = rows(left.slice(chunk.fromA, chunk.toA)), b = rows(right.slice(chunk.fromB, chunk.toB));
    if (a.length !== b.length && chunk.fromA !== Math.min(chunk.toA, left.length))
        throw new Error('This change has different row counts; restore the whole block to avoid guessing.');
    const prefix = right.slice(chunk.fromB, selection.from), selected = right.slice(selection.from, selection.to);
    const start = rows(prefix).length;
    const replacement = chunk.fromA === Math.min(chunk.toA, left.length) ? '' : a.slice(start, start + rows(selected).length).join('');
    return right.slice(0, selection.from) + replacement + right.slice(selection.to);
}
//# sourceMappingURL=reviewactions.js.map