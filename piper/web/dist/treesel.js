// Files tree selection (IntelliJ's project view): ⇧-click / ⇧↑↓ select a range of the shown rows, ⌘-click toggles one,
// and Cut / Copy / Paste (into dropDir of filedrop.ts) / Delete / drag act on the selected items. Pure helpers, unit-tested in treesel.test.mjs.
/** the shown rows from the anchor to `to` (either direction); an anchor no longer shown selects `to` alone */
export function rangeSel(rows, anchor, to) {
    const a = rows.indexOf(anchor);
    const b = rows.indexOf(to);
    if (b < 0)
        return new Set();
    if (a < 0)
        return new Set([to]);
    return new Set(rows.slice(Math.min(a, b), Math.max(a, b) + 1));
}
/** ⌘-click: one row in or out; the first ⌘-click starts from the row that is already selected (the open file) */
export function toggleSel(sel, path, current = '') {
    const out = new Set(sel.size ? sel : current ? [current] : []);
    if (out.has(path))
        out.delete(path);
    else
        out.add(path);
    return out;
}
/** the items an action works on: a folder once, not again for files inside it; order kept, duplicates dropped */
export function topLevel(paths) {
    const uniq = [...new Set(paths)];
    return uniq.filter((p) => !uniq.some((q) => q !== p && p.startsWith(q + '/')));
}
/** a menu header / toast: the one name, or how many items */
export const selLabel = (paths) => (paths.length === 1 ? paths[0].split('/').pop() ?? paths[0] : `${paths.length} items`);
//# sourceMappingURL=treesel.js.map