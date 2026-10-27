// The code view of Files: CodeMirror 6 instead of the rendered lines + the
// Edit textarea. One mode: a file is always an editor; it is read-only when the file cannot be
// written - the caller decides, this module only shows it. Saving stays files.ts' business (POST /api/fs/write with the sha lock).
// CodeMirror is vendored as one bundle (vendor/codemirror) and loaded lazily: this module only
// imports its types, so node tests can import files.js and the GUI starts without it.
import { langOf } from './highlight.js';
import { revertText } from './reviewactions.js';
import { identChars, identifierAt, importBlock, outline, scopeAt } from './codenav.js';
import { markBidi } from './text.js';
/** CodeMirror's own special characters lack these direction controls (LRE, RLE, PDF, FSI): drawn as a marker too */
const SPECIALS = { addSpecialChars: /[\u202a-\u202c\u2068]/ };
let loading = null;
/** the CodeMirror bundle (one request, then cached) */
export function loadCM() {
    loading ??= import('cm');
    return loading;
}
// ---------------------------------------------------------------- languages
function language(cm, path) {
    const name = (path.split('/').pop() ?? '').toLowerCase();
    const ext = name.includes('.') ? name.split('.').pop() : '';
    const sl = (m) => cm.StreamLanguage.define(m);
    switch (ext) {
        case 'java': return cm.java();
        case 'kt':
        case 'kts': return sl(cm.kotlin);
        case 'scala': return sl(cm.scala);
        case 'groovy':
        case 'gradle': return sl(cm.groovy);
        case 'go': return sl(cm.go);
        case 'ts':
        case 'mts':
        case 'cts': return cm.javascript({ typescript: true });
        case 'tsx': return cm.javascript({ typescript: true, jsx: true });
        case 'js':
        case 'mjs':
        case 'cjs': return cm.javascript();
        case 'jsx': return cm.javascript({ jsx: true });
        case 'json':
        case 'jsonc':
        case 'webmanifest':
        case 'map': return cm.json();
        case 'html':
        case 'htm':
        case 'xhtml':
        case 'vue':
        case 'svelte':
        case 'jsp':
        case 'ftl': return cm.html();
        case 'css':
        case 'scss':
        case 'less': return cm.css();
        case 'yml':
        case 'yaml': return cm.yaml();
        case 'md':
        case 'markdown': return cm.markdown();
        case 'sql': return cm.sql({ dialect: cm.MySQL });
        case 'py': return cm.python();
        case 'toml': return sl(cm.toml);
        case 'diff':
        case 'patch': return sl(cm.diff);
        case 'properties':
        case 'ini':
        case 'conf':
        case 'cfg':
        case 'env':
        case 'http':
        case 'rest': return sl(cm.properties);
        case 'sh':
        case 'bash':
        case 'zsh': return sl(cm.shell);
        default: break;
    }
    if (/\.(xml|xsd|xsl|svg|pom)$/.test(name) || ext === 'xml')
        return cm.xml();
    if (name === 'dockerfile' || name.startsWith('dockerfile.'))
        return sl(cm.dockerFile);
    if (/^\.?env(\..*)?$/.test(name))
        return sl(cm.properties);
    if (/^(makefile|taskfile)$|rc$/.test(name))
        return sl(cm.shell);
    return [];
}
/** the app.css token colours (dark + light come from the variables) */
function theme(cm) {
    const t = cm.tags;
    const style = cm.HighlightStyle.define([
        { tag: [t.keyword, t.modifier, t.controlKeyword, t.operatorKeyword, t.definitionKeyword, t.moduleKeyword, t.self, t.null, t.bool], color: 'var(--hl-k)' },
        { tag: [t.string, t.special(t.string), t.regexp, t.character, t.docString], color: 'var(--hl-s)' },
        { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: 'var(--hl-c)', fontStyle: 'italic' },
        { tag: [t.number, t.integer, t.float, t.atom], color: 'var(--hl-n)' },
        { tag: [t.typeName, t.className, t.namespace, t.standard(t.typeName)], color: 'var(--hl-t)' },
        { tag: [t.function(t.variableName), t.function(t.propertyName), t.function(t.definition(t.variableName))], color: 'var(--hl-f)' },
        { tag: [t.annotation, t.meta, t.attributeName, t.processingInstruction], color: 'var(--hl-a)' },
        { tag: [t.tagName, t.angleBracket], color: 'var(--hl-g)' },
        { tag: [t.propertyName, t.definition(t.propertyName)], color: 'var(--hl-p)' },
        { tag: [t.constant(t.variableName), t.standard(t.variableName), t.special(t.variableName)], color: 'var(--hl-v)' },
        { tag: [t.heading, t.strong], color: 'var(--fg)', fontWeight: '600' },
        { tag: t.emphasis, fontStyle: 'italic' },
        { tag: t.link, color: 'var(--link)', textDecoration: 'underline' },
        { tag: t.invalid, color: 'var(--bad)' },
        { tag: [t.inserted], color: 'var(--ok)' },
        { tag: [t.deleted], color: 'var(--bad)' },
    ]);
    const view = cm.EditorView.theme({
        '&': { height: '100%', fontSize: '12.5px', backgroundColor: 'var(--page)', color: 'var(--fg)' },
        '&.cm-focused': { outline: 'none' },
        '.cm-scroller': { fontFamily: '"JetBrains Mono", "SF Mono", Menlo, monospace', lineHeight: '1.55' },
        '.cm-content': { caretColor: 'var(--fg)', padding: '6px 0' },
        '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--fg)', borderLeftWidth: '1.5px' },
        '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection': { backgroundColor: 'color-mix(in srgb, var(--link) 28%, transparent) !important' },
        '.cm-activeLine': { backgroundColor: 'color-mix(in srgb, var(--link) 7%, transparent)' },
        '.cm-activeLineGutter': { backgroundColor: 'transparent', color: 'var(--fg)' },
        '.cm-gutters': { backgroundColor: 'var(--page)', color: 'var(--faint)', border: 'none' },
        '.cm-lineNumbers .cm-gutterElement': { padding: '0 4px 0 10px', minWidth: '34px' },
        '.cm-foldGutter .cm-gutterElement': { color: 'var(--faint)', cursor: 'pointer', padding: '0 2px' },
        '.cm-foldPlaceholder': { background: 'var(--tile)', border: '1px solid var(--line)', color: 'var(--mut)', borderRadius: '4px',
            padding: '0 6px', margin: '0 4px', fontFamily: 'inherit', fontSize: '11px' },
        '.cm-matchingBracket': { backgroundColor: 'color-mix(in srgb, var(--ok) 25%, transparent)', outline: 'none' },
        '.cm-selectionMatch': { backgroundColor: 'color-mix(in srgb, var(--warn) 18%, transparent)' },
        '.cm-searchMatch': { backgroundColor: 'color-mix(in srgb, var(--warn) 30%, transparent)', outline: '1px solid color-mix(in srgb, var(--warn) 60%, transparent)' },
        '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: 'color-mix(in srgb, var(--warn) 60%, transparent)' },
        '.cm-panels': { backgroundColor: 'var(--well)', color: 'var(--fg)' },
        '.cm-panels.cm-panels-top': { borderBottom: '1px solid var(--line)' },
        '.cm-panels.cm-panels-bottom': { borderTop: '1px solid var(--line)' },
        '.cm-tooltip': { backgroundColor: 'var(--card)', border: '1px solid var(--line)', color: 'var(--fg)', borderRadius: '6px' },
        '.cm-diagnostic': { borderLeftWidth: '3px' },
    }, { dark: document.documentElement.dataset.scheme !== 'light' });
    return [cm.syntaxHighlighting(style), view];
}
// ---------------------------------------------------------------- the editor
export async function createEditor(h) {
    const cm = await loadCM();
    const { EditorView, EditorState, StateField, StateEffect, Decoration, GutterMarker, WidgetType, RangeSetBuilder } = cm;
    class Mark extends GutterMarker {
        kinds;
        constructor(kinds) {
            super();
            this.kinds = kinds;
        }
        eq(o) { return o.kinds === this.kinds; }
        toDOM() {
            const el = document.createElement('div');
            el.className = 'cmk ' + this.kinds.split(' ').map((k) => 'cmk-' + k).join(' ');
            if (this.kinds.includes('http')) {
                el.textContent = '▶';
                el.title = 'run this request';
            }
            else if (this.kinds.includes('note'))
                el.title = 'review note';
            return el;
        }
    }
    const lineSets = { add: new Set(), chg: new Set(), del: new Set(), claude: new Set(), note: new Set(), http: new Set() };
    const setMarksFx = StateEffect.define();
    const markField = StateField.define({
        create: () => new RangeSetBuilder().finish(),
        update(v, tr) {
            if (tr.effects.some((e) => e.is(setMarksFx)))
                return buildMarks(tr.state.doc);
            return tr.docChanged ? v.map(tr.changes) : v;
        },
    });
    function buildMarks(doc) {
        const b = new RangeSetBuilder();
        const lines = new Set();
        for (const s of Object.values(lineSets))
            for (const n of s)
                lines.add(n);
        for (const n of [...lines].sort((a, c) => a - c)) {
            if (n < 1 || n > doc.lines)
                continue;
            const kinds = Object.keys(lineSets).filter((k) => lineSets[k].has(n)).join(' ');
            b.add(doc.line(n).from, doc.line(n).from, new Mark(kinds));
        }
        return b.finish();
    }
    const marksGutter = cm.gutter({
        class: 'cm-marks',
        renderEmptyElements: h.original !== undefined,
        markers: (v) => v.state.field(markField),
        initialSpacer: () => new Mark('add'),
        domEventHandlers: {
            mousedown(view, block, e) {
                const n = view.state.doc.lineAt(block.from).number;
                if (lineSets.http.has(n) && h.httpRun)
                    h.httpRun(n);
                else
                    h.onNote(n);
                e.preventDefault();
                return true;
            },
        },
    });
    // the purple flash of lines the session just changed
    const flashFx = StateEffect.define();
    const flashField = StateField.define({
        create: () => Decoration.none,
        update(v, tr) {
            let d = v.map(tr.changes);
            for (const e of tr.effects) {
                if (!e.is(flashFx))
                    continue;
                const b = new RangeSetBuilder();
                for (const n of [...e.value].sort((a, c) => a - c)) {
                    if (n >= 1 && n <= tr.state.doc.lines)
                        b.add(tr.state.doc.line(n).from, tr.state.doc.line(n).from, Decoration.line({ class: 'cm-claude-flash' }));
                }
                d = b.finish();
            }
            return d;
        },
        provide: (f) => EditorView.decorations.from(f),
    });
    // inline blame of the caret line (ghost text after the line end)
    class Ghost extends WidgetType {
        text;
        constructor(text) {
            super();
            this.text = text;
        }
        eq(o) { return o.text === this.text; }
        toDOM() {
            const s = document.createElement('span');
            s.className = 'cm-blame';
            s.textContent = markBidi(this.text);
            return s;
        }
    }
    const blameFx = StateEffect.define();
    const blameField = StateField.define({
        create: () => Decoration.none,
        update(v, tr) {
            if (!tr.selection && !tr.docChanged && !tr.effects.some((e) => e.is(blameFx)))
                return v;
            const st = tr.state;
            const head = st.selection.main.head;
            const line = st.doc.lineAt(head);
            if (!st.selection.main.empty || st.selection.ranges.length > 1 || isDirtyLine(st, line.number))
                return Decoration.none;
            const txt = h.blame(line.number);
            if (!txt)
                return Decoration.none;
            return Decoration.set([Decoration.widget({ widget: new Ghost(txt), side: 1 }).range(line.to)]);
        },
        provide: (f) => EditorView.decorations.from(f),
    });
    // sticky scope: the class / method the top line is in (outline of the file, cheap regexes)
    let outlineCache = { doc: null, items: [] };
    const outlineOf = (doc) => {
        if (outlineCache.doc !== doc)
            outlineCache = { doc, items: outline(doc.toString().split('\n'), h.path) };
        return outlineCache.items;
    };
    const sticky = cm.ViewPlugin.fromClass(class {
        view;
        dom;
        key = '';
        constructor(view) {
            this.view = view;
            this.dom = document.createElement('div');
            this.dom.className = 'cm-sticky';
            view.dom.appendChild(this.dom);
            this.paint();
        }
        update(u) {
            if (u.docChanged || u.viewportChanged || u.geometryChanged)
                this.paint();
        }
        /** layout is read in CodeMirror's measure phase, never during an update */
        paint() {
            const v = this.view;
            v.requestMeasure({
                key: this,
                read: () => ({ n: v.state.doc.lineAt(v.lineBlockAtHeight(v.scrollDOM.getBoundingClientRect().top - v.documentTop + 4).from).number,
                    top: v.scrollDOM.offsetTop }),
                write: (m) => { this.dom.style.top = `${m.top}px`; this.draw(m.n); },
            });
        }
        draw(n) {
            const v = this.view;
            const chain = n > 1 ? scopeAt(outlineOf(v.state.doc), n).filter((o) => o.line < n) : [];
            const key = chain.map((o) => o.line).join(',');
            if (key === this.key)
                return;
            this.key = key;
            this.dom.replaceChildren();
            this.dom.classList.toggle('on', chain.length > 0);
            for (const o of chain.slice(-3)) {
                const row = document.createElement('div');
                row.className = 'cm-sticky-row';
                const no = document.createElement('span');
                no.className = 'no';
                no.textContent = markBidi(String(o.line));
                const tx = document.createElement('span');
                tx.className = 'tx';
                tx.textContent = markBidi(v.state.doc.line(o.line).text);
                row.append(no, tx);
                row.addEventListener('mousedown', (e) => { e.preventDefault(); api.reveal(o.line, true); });
                this.dom.append(row);
            }
        }
        destroy() { this.dom.remove(); }
    }, { eventHandlers: { scroll() { this.paint(); } } });
    // Java: Lezer leaves annotations and CONSTANTS as plain identifiers - colour them like IntelliJ
    const annoMark = Decoration.mark({ class: 'cm-anno' });
    const constMark = Decoration.mark({ class: 'cm-const' });
    const javaExtras = cm.ViewPlugin.fromClass(class {
        decorations;
        constructor(v) { this.decorations = this.build(v); }
        update(u) {
            if (u.docChanged || u.viewportChanged || cm.syntaxTree(u.startState) !== cm.syntaxTree(u.state))
                this.decorations = this.build(u.view);
        }
        build(v) {
            const b = new RangeSetBuilder();
            const doc = v.state.doc;
            for (const { from, to } of v.visibleRanges) {
                cm.syntaxTree(v.state).iterate({
                    from, to,
                    enter: (n) => {
                        if (n.name === 'MarkerAnnotation' || n.name === 'Annotation') {
                            const id = n.node.getChild('Identifier') ?? n.node.getChild('ScopedIdentifier');
                            b.add(n.from, id ? id.to : n.from + 1, annoMark);
                            return false;
                        }
                        if ((n.name === 'Identifier' || n.name === 'TypeName' || n.name === 'Definition') && n.to - n.from > 1 && /^[A-Z][A-Z0-9_]+$/.test(doc.sliceString(n.from, n.to)))
                            b.add(n.from, n.to, constMark);
                        return undefined;
                    },
                });
            }
            return b.finish();
        }
    }, { decorations: (p) => p.decorations });
    let baseline = h.text;
    let wasDirty = false;
    const isDirtyLine = (st, n) => {
        if (st.doc.toString() === baseline)
            return false;
        const b = baseline.split('\n');
        return n > b.length || st.doc.line(n).text !== b[n - 1];
    };
    const ro = new cm.Compartment();
    const readOnlyExt = (on) => [EditorState.readOnly.of(on), EditorView.contentAttributes.of({ 'aria-readonly': on ? 'true' : 'false' })];
    const caretOf = (st) => {
        const m = st.selection.main;
        const line = st.doc.lineAt(m.head);
        const a = st.doc.lineAt(m.from).number;
        let b = st.doc.lineAt(m.to).number;
        if (b > a && m.to === st.doc.line(b).from)
            b--; // a selection ending at a line start does not take that line
        const id = identifierAt(line.text, m.head - line.from, identChars(h.path));
        return { line: line.number, col: m.head - line.from + 1, sel: [a, b], word: id?.name ?? '',
            selText: m.empty ? '' : st.sliceDoc(m.from, Math.min(m.to, m.from + 200)), cursors: st.selection.ranges.length };
    };
    const wordAt = (view, pos) => {
        const line = view.state.doc.lineAt(pos);
        const id = identifierAt(line.text, pos - line.from, identChars(h.path));
        return { word: id?.name ?? '', line: line.number };
    };
    const keys = [
        { key: 'Mod-s', preventDefault: true, run: () => { h.onSave(); return true; } },
        ...(h.extraKeys ?? []),
    ];
    const state = EditorState.create({
        doc: h.text,
        extensions: [
            cm.Prec.highest(cm.keymap.of(keys)),
            cm.lineNumbers(),
            cm.foldGutter({
                markerDOM: (open) => {
                    const s = document.createElement('span');
                    s.className = 'cm-fold ' + (open ? 'open' : 'closed');
                    s.textContent = open ? '⌄' : '›';
                    return s;
                },
            }),
            markField, marksGutter, flashField, blameField,
            cm.highlightSpecialChars(SPECIALS), cm.history(), cm.drawSelection(), cm.dropCursor(), cm.indentOnInput(),
            cm.bracketMatching(), cm.closeBrackets(), cm.rectangularSelection(), cm.crosshairCursor(),
            cm.highlightActiveLine(), cm.highlightActiveLineGutter(), cm.highlightSelectionMatches(),
            cm.search({ top: true }),
            cm.keymap.of([...cm.closeBracketsKeymap, ...cm.defaultKeymap, ...cm.searchKeymap, ...cm.historyKeymap, ...cm.foldKeymap,
                cm.indentWithTab, { key: 'Mod-/', run: cm.toggleComment }]),
            cm.indentUnit.of(guessIndent(h.text)),
            EditorState.tabSize.of(4),
            ro.of(readOnlyExt(h.readOnly)),
            language(cm, h.path),
            ...(h.original !== undefined ? [cm.unifiedMergeView({ original: h.original, mergeControls: false,
                    highlightChanges: true, syntaxHighlightDeletions: true, collapseUnchanged: { margin: 3, minSize: 4 } })] : []),
            /\.java$/i.test(h.path) ? javaExtras : [],
            theme(cm),
            sticky,
            EditorView.updateListener.of((u) => {
                if (u.docChanged) {
                    const d = u.state.doc.toString() !== baseline;
                    if (d !== wasDirty) {
                        wasDirty = d;
                        h.onDirty(d);
                    }
                    h.onEdit?.();
                }
                if (u.docChanged || u.selectionSet)
                    h.onCaret(caretOf(u.state));
            }),
            EditorView.domEventHandlers({
                mousedown(e, view) {
                    if (!(e.metaKey || e.altKey) || e.button !== 0)
                        return false;
                    const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
                    if (pos === null)
                        return false;
                    const w = wordAt(view, pos);
                    if (!w.word)
                        return false;
                    e.preventDefault();
                    h.onDefinition(w.word, w.line);
                    return true;
                },
                contextmenu(e, view) {
                    const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
                    if (pos === null)
                        return false;
                    const m = view.state.selection.main;
                    // a right click outside the selection moves the caret there first
                    if (pos < m.from || pos > m.to)
                        view.dispatch({ selection: { anchor: pos } });
                    e.preventDefault();
                    h.onMenu(e, caretOf(view.state));
                    return true;
                },
            }),
        ],
    });
    const host = document.createElement('div');
    host.className = 'cmhost';
    const view = new EditorView({ state, parent: host });
    // the leading import block starts folded (Java / Kotlin / TS / Python): the first screen is the code
    const imp = importBlock(h.text.split('\n'), langOf(h.path));
    if (imp) {
        const doc = view.state.doc;
        const from = doc.line(imp[0]).to;
        const to = doc.line(imp[1]).to;
        if (to > from)
            view.dispatch({ effects: cm.foldEffect.of({ from, to }) });
    }
    const api = {
        dom: host,
        text: () => view.state.doc.toString(),
        dirty: () => view.state.doc.toString() !== baseline,
        setBaseline(text) {
            baseline = text;
            const d = view.state.doc.toString() !== baseline;
            if (d !== wasDirty) {
                wasDirty = d;
                h.onDirty(d);
            }
            view.dispatch({ effects: blameFx.of(null) });
        },
        replace(text) {
            const cur = view.state.doc;
            if (cur.toString() === text)
                return;
            const line = Math.min(cur.lineAt(view.state.selection.main.head).number, text.split('\n').length);
            const top = view.scrollDOM.scrollTop;
            // the smallest changed span: CodeMirror keeps folds and history around it
            const old = cur.toString();
            let a = 0;
            while (a < old.length && a < text.length && old[a] === text[a])
                a++;
            let b = 0;
            while (b < old.length - a && b < text.length - a && old[old.length - 1 - b] === text[text.length - 1 - b])
                b++;
            view.dispatch({ changes: { from: a, to: old.length - b, insert: text.slice(a, text.length - b) }, annotations: cm.Transaction.addToHistory.of(false) });
            const ln = view.state.doc.line(Math.max(1, line));
            view.dispatch({ selection: { anchor: ln.from } });
            view.scrollDOM.scrollTop = top;
        },
        setReadOnly(on) {
            view.dispatch({ effects: ro.reconfigure(readOnlyExt(on)) });
            host.classList.toggle('ro', on);
        },
        setMarks(m) {
            for (const k of ['add', 'chg', 'del'])
                lineSets[k].clear();
            if (m) {
                const n = view.state.doc.lines;
                if (m.untracked)
                    for (let i = 1; i <= n; i++)
                        lineSets.add.add(i);
                else {
                    for (const [a, b] of m.added)
                        for (let i = a; i <= b && i <= n; i++)
                            lineSets.add.add(i);
                    for (const [a, b] of m.changed)
                        for (let i = a; i <= b && i <= n; i++)
                            lineSets.chg.add(i);
                    for (const d of m.deleted)
                        lineSets.del.add(Math.max(1, d));
                }
            }
            view.dispatch({ effects: setMarksFx.of(null) });
        },
        setClaude(lines, flash) {
            lineSets.claude = new Set(lines);
            view.dispatch({ effects: [setMarksFx.of(null), ...(flash ? [flashFx.of(lines)] : [])] });
            if (flash)
                window.setTimeout(() => { if (view.dom.isConnected)
                    view.dispatch({ effects: flashFx.of([]) }); }, 2600);
        },
        setNotes(lines) {
            lineSets.note = new Set(lines);
            view.dispatch({ effects: setMarksFx.of(null) });
        },
        setProblems(ps) {
            const doc = view.state.doc;
            const diags = [];
            for (const p of ps ?? []) {
                if (p.line < 1 || p.line > doc.lines)
                    continue;
                const ln = doc.line(p.line);
                const from = Math.min(ln.to, ln.from + Math.max(0, (p.col ?? 1) - 1));
                const id = identifierAt(ln.text, from - ln.from, identChars(h.path));
                const to = id ? ln.from + id.end : Math.max(from, ln.to);
                diags.push({ from: id ? ln.from + id.start : from, to: to > from ? to : ln.to, message: p.message, source: 'IntelliJ',
                    severity: /err/i.test(p.severity) ? 'error' : /warn/i.test(p.severity) ? 'warning' : 'info' });
            }
            view.dispatch(cm.setDiagnostics(view.state, diags));
        },
        setHttpLines(lines) {
            lineSets.http = new Set(lines);
            view.dispatch({ effects: setMarksFx.of(null) });
        },
        reveal(line, select = true) {
            const doc = view.state.doc;
            const ln = doc.line(Math.max(1, Math.min(line, doc.lines)));
            view.dispatch({
                selection: select ? { anchor: ln.from } : undefined,
                effects: [EditorView.scrollIntoView(ln.from, { y: 'center' }), cm.unfoldEffect.of({ from: ln.from, to: ln.to })],
            });
            // a folded range around the line opens
            const folded = cm.foldedRanges(view.state);
            const fx = [];
            folded.between(ln.from, ln.to, (from, to) => { fx.push(cm.unfoldEffect.of({ from, to })); });
            if (fx.length)
                view.dispatch({ effects: fx });
        },
        selectRange(from, to) {
            const doc = view.state.doc;
            const a = doc.line(Math.max(1, Math.min(from, doc.lines)));
            const b = doc.line(Math.max(1, Math.min(to, doc.lines)));
            view.dispatch({ selection: { anchor: a.from, head: b.to }, effects: EditorView.scrollIntoView(a.from, { y: 'center' }) });
        },
        caret: () => caretOf(view.state),
        focus: () => view.focus(),
        find(text) {
            if (text)
                view.dispatch({ effects: cm.setSearchQuery.of(new cm.SearchQuery({ search: text })) });
            cm.openSearchPanel(view);
        },
        foldAll: () => { cm.foldAll(view); },
        unfoldAll: () => { cm.unfoldAll(view); },
        scrollTop: () => view.scrollDOM.scrollTop,
        setScrollTop: (px) => { view.scrollDOM.scrollTop = px; },
        refreshBlame: () => view.dispatch({ effects: blameFx.of(null) }),
        destroy: () => view.destroy(),
    };
    return api;
}
/** Map an original-side caret through whole change chunks into the compared document. */
export function reviewSourcePosition(pos, chunks, length) {
    let offset = 0;
    for (const chunk of chunks) {
        if (pos < chunk.fromA)
            break;
        if (pos < chunk.toA)
            return Math.min(length, chunk.fromB + Math.min(pos - chunk.fromA, Math.max(0, chunk.toB - chunk.fromB - 1)));
        offset = chunk.toB - chunk.toA;
    }
    return Math.min(length, Math.max(0, pos + offset));
}
/** Identify only paired indentation/trailing-space edits; internal text and unequal line counts stay real changes. */
export function reviewWhitespaceLines(left, right) {
    if (left.length !== right.length)
        return [];
    const trim = (line) => line.replace(/^[\t ]+|[\t ]+$/g, '');
    return left.flatMap((line, i) => line !== right[i] && trim(line) === trim(right[i]) ? [i] : []);
}
/** Read-only documents; block controls request guarded working-tree operations through the host. */
export async function createMergeReview(hooks) {
    const cm = await loadCM();
    const host = document.createElement('div');
    host.className = 'review-merge cmhost';
    let active = 'b';
    let operationsEnabled = false;
    const compartments = { a: new cm.Compartment(), b: new cm.Compartment() };
    const activeChunkFx = cm.StateEffect.define();
    const activeChunkField = cm.StateField.define({
        create: () => cm.Decoration.none,
        update(value, transaction) {
            for (const effect of transaction.effects) {
                if (!effect.is(activeChunkFx))
                    continue;
                const doc = transaction.state.doc;
                value = cm.Decoration.set(effect.value.filter(line => line >= 1 && line <= doc.lines)
                    .map(line => cm.Decoration.line({ class: 'cm-review-active-chunk' }).range(doc.line(line).from)));
            }
            return value;
        },
        provide: field => cm.EditorView.decorations.from(field),
    });
    const whitespaceFx = cm.StateEffect.define();
    const whitespaceField = cm.StateField.define({
        create: () => cm.Decoration.none,
        update(value, transaction) {
            for (const effect of transaction.effects)
                if (effect.is(whitespaceFx)) {
                    value = cm.Decoration.set(effect.value.map(({ line, className }) => cm.Decoration.line({ class: className }).range(transaction.state.doc.line(line).from)), true);
                }
            return value;
        },
        provide: field => cm.EditorView.decorations.from(field),
    });
    let highlightedChunk = -1;
    let destroyed = false;
    class NoteMark extends cm.GutterMarker {
        state;
        constructor(state) {
            super();
            this.state = state;
        }
        eq(other) { return this.state === other.state; }
        toDOM() {
            const node = document.createElement('div');
            node.className = this.state === 'add' ? 'cmk cmk-review-add' : 'cmk cmk-note';
            node.title = markBidi(this.state === 'add' ? 'Add a review instruction for this code' : 'Review item · ' + this.state);
            if (this.state === 'add')
                node.textContent = '+';
            return node;
        }
    }
    const noteGutter = (old) => cm.gutter({
        class: 'cm-marks', renderEmptyElements: true,
        initialSpacer: () => new NoteMark('open'),
        lineMarker: (view, line) => {
            const state = hooks.notes(old).get(view.state.doc.lineAt(line.from).number);
            return new NoteMark(state ?? 'add');
        },
        domEventHandlers: {
            mousedown(view, block, event) {
                const line = view.state.doc.lineAt(block.from), selected = view.state.selection.main;
                const first = view.state.doc.lineAt(selected.from).number, last = view.state.doc.lineAt(Math.max(selected.from, selected.to - 1)).number;
                // Clicking the note gutter inside a selection must keep the range the reviewer chose.
                if (selected.empty || line.number < first || line.number > last)
                    view.dispatch({ selection: { anchor: line.from } });
                view.focus();
                hooks.onNote(line.number, old, line.text);
                event.preventDefault();
                return true;
            },
        },
    });
    const extensions = (old) => [
        cm.EditorState.readOnly.of(true), cm.EditorView.contentAttributes.of({ 'aria-readonly': 'true' }),
        cm.lineNumbers(), cm.drawSelection(), cm.highlightSpecialChars(SPECIALS),
        language(cm, old ? hooks.originalPath ?? hooks.path : hooks.path), theme(cm),
        compartments[old ? 'a' : 'b'].of(hooks.plain ? [] : noteGutter(old)), activeChunkField, whitespaceField,
        cm.keymap.of([...cm.defaultKeymap, ...cm.searchKeymap]), cm.search({ top: true }),
        cm.EditorView.updateListener.of((update) => {
            if (update.viewportChanged || update.geometryChanged)
                window.requestAnimationFrame(() => { if (!destroyed)
                    markActiveSpacers(); });
            if (update.view.hasFocus) {
                active = old ? 'a' : 'b';
                if (update.selectionSet || update.focusChanged) {
                    window.requestAnimationFrame(() => { if (!destroyed)
                        highlightChunk(); });
                    hooks.onTarget?.(update.selectionSet);
                }
            }
        }),
    ];
    const merge = new cm.MergeView({
        parent: host,
        a: { doc: hooks.left, extensions: extensions(true) },
        b: { doc: hooks.right, extensions: extensions(false) },
        // MergeView's default scanLimit=500 can paint long identical runs as one replacement.
        // Keep detailed matching bounded in both work and time, without normalizing the reviewed bytes.
        diffConfig: { scanLimit: 10000, timeout: 200 },
        highlightChanges: true, gutter: false, collapseUnchanged: { margin: 3, minSize: 4 },
        ...(hooks.onBlockAction ? { revertControls: 'a-to-b', renderRevertControl: () => {
                const group = document.createElement('div');
                group.className = 'review-chunk-actions';
                group.setAttribute('role', 'group');
                group.setAttribute('aria-label', 'Change block operations');
                // Native MergeView mousedown writes directly to the editor; only the host may change disk.
                group.addEventListener('mousedown', event => { event.preventDefault(); event.stopPropagation(); });
                for (const [action, label, title] of [['revert', '↶', 'Revert this block to the left version'], ['accept', '✓', 'Accept this change: keep the working code and mark it reviewed'], ['delete', '×', 'Delete this block from the working file']]) {
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.textContent = label;
                    button.disabled = !operationsEnabled;
                    button.title = title;
                    button.setAttribute('aria-label', title);
                    button.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); reveal(merge.chunks[Number(group.dataset.chunk)]); hooks.onBlockAction?.(action); });
                    group.append(button);
                }
                window.requestAnimationFrame(() => {
                    group.style.transform = `translateY(${-merge.a.scrollDOM.scrollTop}px)`;
                    const chunk = merge.chunks[Number(group.dataset.chunk)];
                    for (const button of group.querySelectorAll('button')) {
                        button.disabled = !operationsEnabled || button.textContent === '×' && chunk?.fromB === chunk?.endB;
                        if (button.textContent === '✓')
                            button.setAttribute('aria-pressed', String(hooks.accepted?.(Number(group.dataset.chunk) + 1) ?? false));
                    }
                });
                return group;
            } } : {}),
    });
    const changedLines = (chunk, side) => {
        const doc = merge[side].state.doc, lines = new Set();
        for (const change of chunk.changes) {
            const from = side === 'a' ? chunk.fromA + change.fromA : chunk.fromB + change.fromB;
            const to = side === 'a' ? chunk.fromA + change.toA : chunk.fromB + change.toB;
            if (from >= to)
                continue;
            for (let n = doc.lineAt(Math.min(from, doc.length)).number; n <= doc.lineAt(Math.min(to - 1, doc.length)).number; n++)
                lines.add(n);
        }
        return [...lines].sort((a, b) => a - b);
    };
    const whitespaceRows = { a: [], b: [] };
    for (const chunk of merge.chunks) {
        for (const side of ['a', 'b']) {
            const from = side === 'a' ? chunk.fromA : chunk.fromB, to = side === 'a' ? chunk.endA : chunk.endB;
            if (from === to)
                continue;
            const doc = merge[side].state.doc, changed = new Set(changedLines(chunk, side));
            for (let n = doc.lineAt(from).number; n <= doc.lineAt(to).number; n++)
                if (!changed.has(n))
                    whitespaceRows[side].push({ line: n, className: 'cm-review-context' });
        }
        if (chunk.fromA === chunk.endA || chunk.fromB === chunk.endB)
            continue;
        const a = merge.a.state.doc.lineAt(chunk.fromA).number, b = merge.b.state.doc.lineAt(chunk.fromB).number;
        const left = merge.a.state.doc.sliceString(chunk.fromA, chunk.endA).split('\n');
        const right = merge.b.state.doc.sliceString(chunk.fromB, chunk.endB).split('\n');
        for (const i of reviewWhitespaceLines(left, right)) {
            whitespaceRows.a.push({ line: a + i, className: 'cm-review-whitespace' });
            whitespaceRows.b.push({ line: b + i, className: 'cm-review-whitespace' });
        }
    }
    merge.a.dispatch({ effects: whitespaceFx.of(whitespaceRows.a) });
    merge.b.dispatch({ effects: whitespaceFx.of(whitespaceRows.b) });
    host.classList.toggle('review-additions', merge.chunks.length > 0 && merge.chunks.every(chunk => chunk.fromA === chunk.endA));
    host.classList.toggle('review-removals', merge.chunks.length > 0 && merge.chunks.every(chunk => chunk.fromB === chunk.endB));
    // MergeView aligns document heights; bounded panes must share vertical scroll and move its controls.
    const syncScroll = (source, other) => {
        if (Math.abs(other.scrollDOM.scrollTop - source.scrollDOM.scrollTop) > 1)
            other.scrollDOM.scrollTop = source.scrollDOM.scrollTop;
        for (const group of host.querySelectorAll('.review-chunk-actions'))
            group.style.transform = `translateY(${-source.scrollDOM.scrollTop}px)`;
    };
    const scrollA = () => syncScroll(merge.a, merge.b), scrollB = () => syncScroll(merge.b, merge.a);
    merge.a.scrollDOM.addEventListener('scroll', scrollA);
    merge.b.scrollDOM.addEventListener('scroll', scrollB);
    const currentChunk = () => {
        const pos = merge[active].state.selection.main.head;
        return merge.chunks.find(c => pos >= (active === 'a' ? c.fromA : c.fromB) && pos <= (active === 'a' ? c.toA : c.toB))
            ?? merge.chunks.find(c => (active === 'a' ? c.fromA : c.fromB) >= pos) ?? merge.chunks.at(-1);
    };
    function markActiveSpacers() {
        const chunk = currentChunk();
        for (const side of ['a', 'b']) {
            const from = chunk && (side === 'a' ? chunk.fromA : chunk.fromB), to = chunk && (side === 'a' ? chunk.endA : chunk.endB);
            for (const spacer of merge[side].contentDOM.querySelectorAll('.cm-mergeSpacer')) {
                const pos = merge[side].posAtDOM(spacer);
                spacer.classList.toggle('cm-review-active-spacer', from !== undefined && to !== undefined && pos >= from && pos <= to);
            }
        }
    }
    function highlightChunk() {
        const chunk = currentChunk(), index = chunk ? merge.chunks.indexOf(chunk) : -1;
        markActiveSpacers();
        if (!chunk || index === highlightedChunk)
            return;
        highlightedChunk = index;
        host.dataset.activeChunk = String(index + 1);
        for (const side of ['a', 'b']) {
            merge[side].dispatch({ effects: activeChunkFx.of(changedLines(chunk, side)) });
        }
        for (const group of host.querySelectorAll('.review-chunk-actions'))
            group.setAttribute('aria-current', String(Number(group.dataset.chunk) === index));
    }
    const reveal = (chunk) => {
        if (!chunk)
            return false;
        active = chunk.fromB === chunk.endB && chunk.fromA < chunk.endA ? 'a' : 'b';
        const view = merge[active], pos = Math.min(view.state.doc.length, active === 'a' ? chunk.fromA : chunk.fromB);
        view.dispatch({ selection: { anchor: pos }, effects: cm.EditorView.scrollIntoView(pos, { y: 'center' }) });
        view.focus();
        highlightChunk();
        hooks.onTarget?.();
        return true;
    };
    return {
        dom: host, focus: () => merge[active].focus(),
        hasSelection: () => !merge[active].state.selection.main.empty,
        clearSelection: () => {
            const view = merge[active];
            view.dispatch({ selection: { anchor: view.state.selection.main.head } });
            view.focus();
        },
        change: dir => {
            const chunk = currentChunk();
            if (!chunk)
                return false;
            const pos = merge[active].state.selection.main.head;
            const from = active === 'a' ? chunk.fromA : chunk.fromB;
            const to = active === 'a' ? chunk.endA : chunk.endB;
            const index = merge.chunks.indexOf(chunk);
            return reveal(dir > 0 && pos < from || dir < 0 && pos >= to && pos > from ? chunk : merge.chunks[index + dir]);
        },
        edge: (dir) => reveal(dir > 0 ? merge.chunks[0] : merge.chunks.at(-1)),
        operations: enabled => {
            operationsEnabled = enabled;
            for (const group of host.querySelectorAll('.review-chunk-actions')) {
                const index = Number(group.dataset.chunk), chunk = merge.chunks[index];
                for (const button of group.querySelectorAll('button')) {
                    button.disabled = !enabled || button.textContent === '×' && chunk?.fromB === chunk?.endB;
                    if (button.textContent === '✓')
                        button.setAttribute('aria-pressed', String(hooks.accepted?.(index + 1) ?? false));
                }
            }
        },
        position: () => {
            const chunk = currentChunk(), index = Math.max(0, merge.chunks.indexOf(chunk)) + 1, total = merge.chunks.length;
            if (!chunk)
                return { index, total };
            const view = merge[active], start = active === 'a' ? chunk.fromA : chunk.fromB, end = active === 'a' ? chunk.endA : chunk.endB;
            const rows = (side) => {
                const from = side === 'a' ? chunk.fromA : chunk.fromB, to = side === 'a' ? chunk.endA : chunk.endB;
                return from === to ? 0 : merge[side].state.doc.lineAt(Math.max(from, to - 1)).number - merge[side].state.doc.lineAt(from).number + 1;
            };
            const a = rows('a'), b = rows('b');
            return { index, total, from: view.state.doc.lineAt(start).number, to: view.state.doc.lineAt(Math.max(start, end - 1)).number,
                count: Math.max(a, b), kind: a === 0 ? 'added' : b === 0 ? 'removed' : 'changed' };
        },
        anchor: () => {
            // F23: where "Ask Claude ▾" sits - at the right end of the selection's / active block's last line, in the side that holds it
            const view = merge[active], selection = view.state.selection.main, chunk = currentChunk();
            const end = !selection.empty ? selection.to : chunk ? Math.max(active === 'a' ? chunk.fromA : chunk.fromB, (active === 'a' ? chunk.toA : chunk.toB) - 1) : selection.head;
            const at = view.coordsAtPos(view.state.doc.lineAt(Math.min(end, view.state.doc.length)).from);
            const box = host.getBoundingClientRect(), scroller = view.scrollDOM.getBoundingClientRect();
            if (!at || at.top < scroller.top || at.bottom > scroller.bottom)
                return null;
            return { top: at.top - box.top, right: box.right - scroller.right + (view.scrollDOM.offsetWidth - view.scrollDOM.clientWidth) };
        },
        target: (whole = false) => {
            const view = merge[active], selection = view.state.selection.main, chunk = currentChunk();
            const from = whole ? 0 : !selection.empty ? selection.from : chunk ? active === 'a' ? chunk.fromA : chunk.fromB : view.state.doc.lineAt(selection.head).from;
            const to = Math.min(view.state.doc.length, whole ? view.state.doc.length : !selection.empty ? selection.to : chunk ? active === 'a' ? chunk.toA : chunk.toB : view.state.doc.lineAt(selection.head).to);
            return { from: view.state.doc.lineAt(Math.min(from, view.state.doc.length)).number, to: view.state.doc.lineAt(Math.max(from, to - 1)).number,
                code: view.state.doc.sliceString(from, to), old: active === 'a' };
        },
        restore: () => {
            const chunk = currentChunk();
            if (!chunk)
                throw new Error('There is no change block at the caret.');
            const selection = merge[active].state.selection.main;
            if (!selection.empty && active === 'a')
                throw new Error('Select working-side rows for a partial restore, or clear the selection to restore the whole block.');
            const right = merge.b.state.doc.toString(), left = merge.a.state.doc.toString();
            const rows = selection.empty ? undefined : { from: merge.b.state.doc.lineAt(selection.from).from,
                to: Math.min(merge.b.state.doc.length, merge.b.state.doc.lineAt(Math.max(selection.from, selection.to - 1)).to + 1) };
            return { right, text: revertText(left, right, chunk, rows) };
        },
        remove: () => {
            const chunk = currentChunk();
            if (!chunk || chunk.fromB === chunk.endB)
                throw new Error('This block has no working code to delete.');
            const selection = merge[active].state.selection.main;
            if (!selection.empty && active === 'a')
                throw new Error('Select working-side rows to delete code.');
            const doc = merge.b.state.doc, right = doc.toString();
            const from = selection.empty ? chunk.fromB : doc.lineAt(selection.from).from;
            const to = Math.min(doc.length, selection.empty ? chunk.toB : doc.lineAt(Math.max(selection.from, selection.to - 1)).to + 1);
            if (from < chunk.fromB || to > chunk.toB)
                throw new Error('Select working rows within one change block.');
            return { right, text: right.slice(0, from) + right.slice(to) };
        },
        caret: () => {
            const view = merge[active];
            const line = view.state.doc.lineAt(view.state.selection.main.head);
            return { line: line.number, old: active === 'a', excerpt: line.text };
        },
        sourceLine: () => {
            const pos = active === 'a' ? reviewSourcePosition(merge.a.state.selection.main.head, merge.chunks, merge.b.state.doc.length) : merge.b.state.selection.main.head;
            return merge.b.state.doc.lineAt(pos).number;
        },
        notesChanged: () => {
            if (hooks.plain)
                return;
            merge.a.dispatch({ effects: compartments.a.reconfigure(noteGutter(true)) });
            merge.b.dispatch({ effects: compartments.b.reconfigure(noteGutter(false)) });
        },
        destroy: () => { destroyed = true; merge.a.scrollDOM.removeEventListener('scroll', scrollA); merge.b.scrollDOM.removeEventListener('scroll', scrollB); merge.destroy(); },
    };
}
export function guessIndent(text) {
    let tabs = 0;
    let two = 0;
    let four = 0;
    for (const l of text.split('\n', 4000)) {
        const m = /^[ \t]+/.exec(l);
        if (!m || !l.trim())
            continue;
        if (m[0].startsWith('\t'))
            tabs++;
        else if (m[0].length % 4 === 0)
            four++;
        else if (m[0].length % 2 === 0)
            two++;
    }
    if (tabs > two + four)
        return '\t';
    return two > four / 3 ? '  ' : '    ';
}
//# sourceMappingURL=cmeditor.js.map