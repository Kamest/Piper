// node --test: the design pass 9.132 - the remaining text glyphs used as icons are line icons (.fi), visible words lose their glyphs.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const gui = readFileSync(new URL('../piper-e2e/gui.py', import.meta.url), 'utf8');
const block = (start, end) => css.slice(css.indexOf(start), css.indexOf(end));
const G = block('/* ==== 9.132', '/* ==== end 9.132 ==== */');

test('9.132: the grid pane bar draws expand / shrink, more and x icons, named by data-act and aria-label', () => {
  const m = src('main.ts');
  const p = m.slice(m.indexOf("el.append(h('span', { class: 'pane-tools' },"), m.indexOf('/** 9.45: ⤢ / ⇧⌘⏎'));
  assert.ok(!/'[⤢⤡✕⋯]'/.test(p), 'no glyph in the pane bar');
  assert.match(p, /'data-act': z \? 'unzoom' : 'zoom'/);
  assert.match(p, /'aria-label': z \? 'back to the grid' : 'this pane alone', onclick: \(\) => zoomPane\(id\) \}, fi\(z \? 'shrink' : 'expand', 'gi'\)\)/);
  assert.match(p, /'data-act': 'remove', [^\n]*'aria-label': 'remove from grid'/);
  assert.match(m, /'aria-label': 'pane menu', onclick: \(\) => \{[\s\S]*?\} \}, fi\('more', 'gi'\)\);\n {2}el\.append\(h\('span', \{ class: 'pane-tools' \},/);
  assert.match(m, /^import \{ ago, clear, fi, fold, h, unfold, sideSplitter \} from '\.\/dom\.js';$/m);
});

test('9.132: gui.py clicks the pane bar by data-act, never by a glyph', () => {
  assert.ok(!/\.tlabel button"[^)\n]*, "[⤢⤡✕]"\)/.test(gui), 'no glyph text selects a pane button');
  for (const act of ['zoom', 'unzoom', 'remove']) assert.match(gui, new RegExp(`\\.tlabel button"[^\\n]*, "${act}", "data-act"\\)`), act);
});

test('9.132: HTTP - the reload is an icon, the request caret is a chevron mask that turns when open', () => {
  const s = src('httppanel.ts');
  assert.ok(!s.includes("'↻'") && !s.includes("'▾' : '▸'"));
  assert.match(s, /h\('button', \{ title: 'look for \.http files again', 'aria-label': 'look for \.http files again', onclick: \(\) => void load\(true\) \}, fi\('refresh', 'gi'\)\)/);
  assert.match(s, /h\('span', \{ class: 'hpchev', 'aria-hidden': 'true' \}\),/);
  assert.match(G, /\.hpchev, button\.ibx-ghead \.caret \{ width: 10px; height: 10px; background-color: var\(--faint\); -webkit-mask: url\("data:image\/svg\+xml,[^"]*m9 18 6-6-6-6[^"]*"\) center \/ contain no-repeat;/);
  assert.match(G, /\.hpreq-toggle\[aria-expanded="true"\] \.hpchev, button\.ibx-ghead\[aria-expanded="true"\] \.caret \{ transform: rotate\(90deg\); \}/);
  assert.match(G, /@media \(prefers-reduced-motion: reduce\) \{ \.hpchev, button\.ibx-ghead \.caret \{ transition: none; \} \}/);
});

test('9.132: Files, the session bar and the inbox carry the tree / branch / refresh icons and the chevron caret', () => {
  const f = src('files.ts');
  assert.ok(!f.includes("'☰'") && !f.includes("'⎇ '"));
  assert.match(f, /onclick: \(\) => setTree\(!treeOn\(\)\),\n {4}\}, fi\('tree', 'gi'\)\)\);/);
  assert.match(f, /h\('span', \{ class: 'br', title: 'branch' \}, fi\('branch', 'gi'\), showBidi\(br\)\)/);
  const m = src('main.ts');
  assert.ok(!m.includes('`⎇ ${'));
  assert.match(m, /h\('span', \{ class: 'chip dh-branch', title: 'git branch of the working tree' \}, fi\('branch', 'gi'\), showBidi\(s\.branch\)\)/);
  const i = src('inbox.ts');
  assert.match(i, /'aria-label': 'Refresh GitLab', onclick: \(\) => void refresh\(\) \}, fi\('refresh', 'g gi'\)\)/, '.g keeps the spin while it loads');
  assert.match(i, /h\('span', \{ class: 'caret', 'aria-hidden': 'true' \}\)\);/);
  assert.ok(!/'[▸▾↻]'/.test(i));
  assert.ok(!/↻/.test(i.replace(/\/\/[^\n]*/g, '')), 'no ↻ in the inbox words');
  assert.match(G, /\.chip\.dh-branch > \.fi\.gi, \.fstatus \.br > \.fi\.gi \{ width: 11px; height: 11px; margin-right: 4px;/);
});

test('9.132: close / remove ✕ buttons are x icons with a name', () => {
  const cases = [
    ['queue.ts', /'aria-label': 'Remove',\n\s+onclick: \(e: Event\) => \{ e\.stopPropagation\(\); deps\.forget\(it\.session\); \} \}, fi\('x', 'gi'\)\)/],
    ['main.ts', /'aria-label': 'close', onclick: \(\) => toggleDrawer\(false\) \}, fi\('x', 'gi'\)\)/],
    ['feed.ts', /'aria-label': b\.state === 'error' \? 'discard' : 'cancel',\n\s+onclick: \(\) => void cancel\(b\.id!\) \}, fi\('x', 'gi'\)\)/],
    ['feed.ts', /'aria-label': `remove \$\{r\}`[\s\S]*?drawRefs\(\);\n\s+\} \}, fi\('x', 'gi'\)\)\)\);/],
    ['gitwindow.ts', /title: 'close \(Esc\)', 'aria-label': 'close', onclick: deps\.closeModal \}, fi\('x', 'gi'\)\)\);/],
    ['gitwindow.ts', /title: 'close', 'aria-label': 'close', onclick: closeSheet \}, fi\('x', 'gi'\)\)/],
    ['gitwindow.ts', /class: 'icon pd-x', title: 'close \(Esc\)', 'aria-label': 'close', onclick: deps\.closeModal \}, fi\('x', 'gi'\)\)/],
    ['comment.ts', /class: 'icon ghost cx', 'aria-label': 'close', title: 'Esc', onclick: askClose \}, fi\('x', 'gi'\)\)/],
    ['composer.ts', /'aria-label': 'Delete the queued message'[\s\S]*?drawQueue\(\);\n\s+\} \}, fi\('x', 'gi'\)\)\)\);/],
  ];
  for (const [f, re] of cases) assert.match(src(f), re, f);
  for (const f of ['queue.ts', 'gitwindow.ts', 'comment.ts', 'composer.ts']) assert.ok(!src(f).includes("'✕'"), `${f}: no ✕ literal`);
  const feed = src('feed.ts');
  assert.ok(!/\}, '✕'\)/.test(feed), 'feed: no ✕ button');
});

test('9.132: refresh buttons draw the refresh icon and keep it after loading; words lose ↻', () => {
  for (const [f, label] of [['stack.ts', 'refresh the stack'], ['diag.ts', 'refresh the diagnostics']]) {
    const s = src(f);
    assert.match(s, new RegExp(`const reload = h\\('button', \\{ class: 'mini icon', title: 'refresh', 'aria-label': '${label}', [^\\n]*\\}, fi\\('refresh'\\)\\);`), f);
    assert.match(s, /reload\.replaceChildren\(fi\('refresh'\)\);/, `${f}: the icon comes back after '…'`);
    assert.ok(!s.includes("textContent = '↻'"), f);
  }
  assert.match(src('stack.ts'), /'aria-label': 'reload the log', onclick: \(\) => void fetchLog\(\) \}, fi\('refresh', 'gi'\)\)/);
  assert.match(src('timesheet.ts'), /h\('button', \{ title: 'recompute', 'aria-label': 'recompute', onclick: \(\) => void load\(\) \}, fi\('refresh', 'gi'\)\)/);
  for (const f of ['stack.ts', 'timesheet.ts', 'usage.ts', 'board.ts', 'gitpanel.ts', 'dbview.ts']) {
    const code = src(f).replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
    assert.ok(!code.includes('↻'), `${f}: no ↻ outside comments`);
  }
  assert.match(src('usage.ts'), /h\('button', \{ onclick: \(\) => void load\(\) \}, 'Load again'\)/);
  assert.match(src('board.ts'), /ist\.exists \? 'Update issue status' : 'Create issue status'/);
  assert.match(src('gitpanel.ts'), /'aria-label': 'Refresh review' \}, 'Refresh comparison'\)/);
  assert.match(src('gitpanel.ts'), /onclick: \(\) => void regen\(\) \}, 'Draft again'\)/);
});

test('9.132: the chat says "more", not "more ▸"', () => {
  const feed = src('feed.ts');
  assert.ok(!feed.includes('more ▸'));
  assert.equal(feed.match(/\? 'less' : 'more'\) : null/g)?.length, 2);
});

test('9.132: one CSS block - the icon takes the text colour and size; the pane bar centres it', () => {
  assert.ok(css.indexOf('/* ==== 9.132') > css.indexOf('/* ==== end 9.129 ==== */'), 'after 9.129');
  assert.match(G, /\.fi\.gi \{ width: 12px; height: 12px; --fic: currentColor; vertical-align: -2px; \}/);
  assert.match(G, /button:not\(\.mini\) > \.fi\.gi \{ width: 14px; height: 14px; \}/);
  assert.match(G, /\.tlabel \.pane-tools button\.mini \{ display: inline-flex; align-items: center; justify-content: center; min-width: 22px; \}/);
  assert.doesNotMatch(G, /#[0-9a-f]{3,8}\b|rgb\(/i, 'colours only via tokens');
});

test('9.133: no text glyph is left as a close / more / caret / week-arrow button', () => {
  const lit = /\}, '(×|⋯|▾|◀|▶)'\)|\}, t\.dirty \? '●' : '×'\)|ib\('⋯'/;
  for (const f of ['timesheet.ts', 'stack.ts', 'hostterm.ts', 'gitwindow.ts', 'files.ts', 'gitpanel.ts', 'main.ts', 'convo.ts']) {
    assert.doesNotMatch(src(f), lit, f);
  }
  assert.match(src('main.ts'), /h\('i', \{ class: 'chev fi fi-chev-d', 'aria-hidden': 'true' \}\)\);/, 'Beside ▾ caret');
  assert.match(src('timesheet.ts'), /'aria-label': 'previous week'[^\n]*fi-chev-l/);
  const b = css.slice(css.indexOf('/* ==== 9.133'), css.indexOf('/* ==== end 9.133 ==== */'));
  assert.match(b, /\.layoutctl button\.beside i\.chev \{ width: 12px; height: 12px;/);
});

test('9.133: Timesheet - a session without an issue is one row (name, project, field, Assign), no bullet', () => {
  assert.match(src('timesheet.ts'), /h\('li', \{ class: 'ts-assign' \}, h\('span', \{ class: 'ts-aname' \}/);
  const b = css.slice(css.indexOf('/* ==== 9.133'), css.indexOf('/* ==== end 9.133 ==== */'));
  assert.match(b, /\.tsheet ul\.ts-assigns \{ list-style: none;/);
  assert.match(b, /\.tsheet li\.ts-assign \{ display: flex;/);
});

test('9.133: a squeezed button chip in the session bar ends in an ellipsis (inline-block, not flex)', () => {
  const b = css.slice(css.indexOf('/* ==== 9.133'), css.indexOf('/* ==== end 9.133 ==== */'));
  assert.match(b, /\.head\.detail\.one \.dh-chips button\.chip \{ display: inline-block; line-height: 20px; \}/);
});

test('9.133: with the approval card shown the toasts move off it (beside from the rail, else to the top)', () => {
  const b = css.slice(css.indexOf('/* ==== 9.133'), css.indexOf('/* ==== end 9.133 ==== */'));
  assert.match(b, /body\.pending-open #toasts \{ left: calc\(var\(--ab-w, 64px\) \+ 16px\); transform: none;/);
  assert.match(b, /max-width: calc\(100vw - var\(--ab-w, 64px\) - min\(460px, 100vw\) - 64px\)/);
  assert.match(b, /body\.pending-open\.drawer-open #toasts \{[^}]*top: 56px; bottom: auto; \}/);
  assert.match(b, /@media \(max-width: 899px\) \{ body\.pending-open #toasts \{[^}]*top: 56px; bottom: auto; \} \}/);
});
