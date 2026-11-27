// Generated terminal layouts for wrapped references: one source for the unit matrix (terminal-wrap.test.mjs) and
// the real xterm matrix (test_cross_project_reference_browser.py runs `node termwrap-cases.mjs` for its JSON).
// Claude Code and Codex lay text out with wrap-ansi: words wrap at spaces, a word longer than the width is cut
// hard (filling the current row first when that needs fewer breaks), rows continue under a hanging indent.
import { fileURLToPath } from 'node:url';

/** A port of wrap-ansi 9.0.0 `exec(text, columns, {hard: true})` (trim on) for plain one-cell characters. See NOTICE. */
export function wrapAnsi(text, columns) {
  let rows = [''];
  for (const [index, word] of text.split(' ').entries()) {
    rows[rows.length - 1] = rows.at(-1).trimStart();
    let rowLength = rows.at(-1).length;
    if (index !== 0 && rowLength > 0) { rows[rows.length - 1] += ' '; rowLength++; }
    if (word.length > columns) {
      const remaining = columns - rowLength;
      const thisLine = 1 + Math.floor((word.length - remaining - 1) / columns);
      const nextLine = Math.floor((word.length - 1) / columns);
      if (nextLine < thisLine) rows.push('');
      // wrapWord
      let visible = rows.at(-1).length;
      for (let i = 0; i < word.length; i++) {
        if (visible + 1 <= columns) rows[rows.length - 1] += word[i];
        else { rows.push(word[i]); visible = 0; }
        visible++;
        if (visible === columns && i < word.length - 1) { rows.push(''); visible = 0; }
      }
      if (!visible && rows.at(-1).length > 0 && rows.length > 1) rows[rows.length - 2] += rows.pop();
      continue;
    }
    if (rowLength + word.length > columns && rowLength > 0 && word.length > 0) rows.push('');
    rows[rows.length - 1] += word;
  }
  return rows.map(row => row.replace(/ +$/, ''));
}

const PATHS = [
  { path: '/Users/u/www/acme/shop/development/1234-terminal-note.md', line: 0 },
  { path: 'development/gitlab-1234-note.md', line: 0 },
  { path: 'piper/web/src/term.ts', line: 706, shown: 'piper/web/src/term.ts:706' },
  { path: 'development/all-projects-session/', line: 0 },
  { path: 'src/main/java/com/example/shop/checkout/OrderSummaryController.java', line: 42, shown: 'src/main/java/com/example/shop/checkout/OrderSummaryController.java:42' },
  { path: 'README.md', line: 0 },
];
// the wider sweep (`cases({extended: true})`): links, quoted names, accents, more sentence starts
const MORE_PATHS = [
  { url: 'https://gitlab.example.com/acme/shop/-/merge_requests/4321' },
  { url: 'https://example.com/search?q=terminal&page=2' },
  { path: 'development/gitlab-1234-note.md', line: 0, shown: '`development/gitlab-1234-note.md`' },
  { path: 'development/café-résumé-preview.md', line: 0 },
  { path: '~/www/acme/shop/development/plan.md', line: 0 },
  { path: 'src/components/checkout/AddressForm.tsx', line: 12, shown: 'src/components/checkout/AddressForm.tsx:12:5' },
];
const MORE_NEXT = ['3 files remain.', 'see above.', 'MR !4321 is open.', '(see below)', 'https://example.com/x is a link.'];
const AFTER = ['.', ',', ':', ';', ')', '', '.)'];
const NEXT = ['Do not share the draft yet.', 'a copy may be opened.', 'Étape finale.', '— done', 'src/other.ts is the second.'];
const BEFORE = ['I prepared a draft review note for app:', 'See', 'I also left a note about progress', 'Source', ''];
const LAYOUTS = [
  { first: '● ', rest: '  ' },          // Claude Code answer
  { first: '  1. ', rest: '     ' },    // numbered list
  { first: '- ', rest: '  ' },          // bullet
  { first: '', rest: '' },              // plain
];

/** Every combination at every width: rows (as displayed) and the expected reference on each row of the path. */
export function cases({ widths = [], margins = [0, 1, 2], extended = false } = {}) {
  const out = [];
  const paths = extended ? [...PATHS, ...MORE_PATHS] : PATHS, nexts = extended ? [...NEXT, ...MORE_NEXT] : NEXT;
  for (const cols of widths) for (const margin of margins) for (const layout of LAYOUTS) for (const p of paths)
    for (const after of AFTER) for (const next of nexts) for (const before of BEFORE) {
      const shown = p.shown ?? p.url ?? p.path;
      // "dir/" + " " + "src/b.ts" wrapped at the space reads exactly like one path a TUI cut after its '/'
      if (shown.endsWith('/') && !after && next.split(' ')[0].includes('/')) continue;
      const open = after.startsWith('.)') ? '(' : after === ')' ? '(' : '';
      const close = after === '.)' ? ').' : after;
      const sentence = [before, open + shown + close, next].filter(Boolean).join(' ');
      const width = cols - margin - layout.first.length;
      if (width < 12) continue;
      const rows = wrapAnsi(sentence, width).map((row, i) => (i ? layout.rest : layout.first) + row);
      // locate the path in the displayed rows: offsets of its characters, row by row
      const flat = rows.join('\n');
      // walk the displayed text skipping only row breaks and the hanging indent (or the space a wrap replaced)
      const walk = pos => {
        const where = [];
        for (let i = pos, k = 0; i < flat.length && k < shown.length; i++) {
          if (flat[i] === shown[k]) { where.push(i); k++; continue; }
          if (flat[i] === '\n' || flat[i] === ' ' && where.length && /\n *$/.test(flat.slice(0, i + 1))) continue;
          return null;
        }
        return where.length === shown.length ? where : null;
      };
      let where = null;
      for (let i = 0; i < flat.length && !where; i++) if (/[\s(]/.test(flat[i - 1] ?? ' ')) where = walk(i);
      if (!where) throw new Error('layout lost the path: ' + JSON.stringify(rows));
      const rowOf = i => flat.slice(0, i).split('\n').length - 1;
      const q = shown.startsWith('`') ? 1 : 0;          // a code quote is not part of the name
      const pathRows = [...new Set(where.slice(q, where.length - q).map(rowOf))];
      // Unknowable from the screen: the name ends exactly at a full row (bare or before ':') and the next row starts
      // with what could be its cut rest: a link with any ASCII word or path, a line number with digits.
      const last = where.at(-1), lastRow = rows[rowOf(last)];
      const edge = flat[last + 1] === '\n' || after === ':' && flat[last + 2] === '\n';
      const ambiguous = edge && lastRow.trimEnd().length >= cols - 2 && (after === '' || after === ':')
        && (p.url ? /^[a-z0-9/]/.test(next) : /^\d/.test(next) && (after === ':' || p.line > 0));
      out.push({ cols, rows, path: p.path, line: p.line, url: p.url, shown: shown.replace(/`/g, ''), pathRows, ambiguous, span: [where[0], where.at(-1) + 1], name: `${cols}/${margin}/${JSON.stringify(layout.first)}/${shown}/${JSON.stringify(after)}/${next.split(' ')[0]}/${before.split(' ')[0]}` });
    }
  return out;
}

/** The reference a click on the path must open: itself, or a tentative join whose fallback is itself. */
export function opens(ref, c) {
  if (c.url) return ref.kind === 'url' && ref.url === c.url;
  return ref.kind === 'file' && (ref.path === c.path || ref.fallback === c.path) && ref.line === c.line;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const widths = process.argv.slice(2).map(Number);
  process.stdout.write(JSON.stringify(cases({ widths, extended: true })));
}
