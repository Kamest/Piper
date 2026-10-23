// Small edits in the GUI: a textarea, not an IDE. Pure helpers for
// the keys a code textarea needs (Tab / ⇧Tab indent by the file's own unit, Enter keeps the
// indentation) and for turning the edited text back into file content.
/** the file's indentation unit: a tab, or 2 / 4 spaces by the most common step between lines */
export function indentUnit(lines, fallback = '    ') {
    let tabs = 0;
    let spaces = 0;
    const steps = new Map();
    let prev = 0;
    for (const l of lines) {
        if (!l.trim())
            continue;
        const m = /^[ \t]*/.exec(l)[0];
        if (m.startsWith('\t')) {
            tabs++;
            prev = 0;
            continue;
        }
        if (m.length)
            spaces++;
        const d = Math.abs(m.length - prev);
        if (d === 2 || d === 4 || d === 3 || d === 8)
            steps.set(d, (steps.get(d) ?? 0) + 1);
        prev = m.length;
    }
    if (tabs > spaces)
        return '\t';
    if (!steps.size)
        return fallback;
    const best = [...steps.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0][0];
    return ' '.repeat(best === 8 ? 4 : best);
}
/** the edited text as file content (read_file splits lines; LF + a final newline assumed) */
export function fileContent(text, eol = '\n', finalNewline = true) {
    const body = text.replace(/\r\n?/g, '\n');
    const joined = eol === '\n' ? body : body.split('\n').join(eol);
    return finalNewline && joined.length ? joined + eol : joined;
}
//# sourceMappingURL=editor.js.map