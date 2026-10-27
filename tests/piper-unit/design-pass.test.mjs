// node --test: the design pass - one surface under the session bar, aligned lines, icons instead of glyphs.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { auditTime } from '../../piper/web/dist/board.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const block = (start, end) => css.slice(css.indexOf(start), css.indexOf(end));

test('9.104: the session tabs fill the area under the bar like the terminal (no inset card); other pages keep theirs', () => {
  const b = block('/* ==== 9.104', '/* ==== end 9.104 ==== */');
  assert.match(b, /#main:has\(> \.head\.detail\.one\) > \.body:not\(\.grid\) \{ padding: 0; background: var\(--well\); \}/);
  assert.match(b, /> \.view > :is\(\.convo, \.files, \.audit, \.board\) \{ border-radius: 0; \}/);
  assert.match(css, /\.board, \.audit \{ overflow: auto; background: var\(--well\); border-radius: 10px;/, 'the History page keeps its card');
});

test('9.104: the queue title sits on the 44 px line of the session bar', () => {
  const b = block('/* ==== 9.104', '/* ==== end 9.104 ==== */');
  assert.match(b, /\.q-title \{ height: 44px; margin: 0 -12px 4px; padding: 0 8px 0 16px; border-bottom: 1px solid var\(--line\); \}/);
  assert.match(b, /\.q-head \{ padding: 0 12px 12px; \}/);
});

test('9.104: Stop and Search all are icons; the transcript says 24 h times like the rest', () => {
  assert.match(src('main.ts'), /class: 'icon dh-stop'[^\n]*h\('i', \{ class: 'fi fi-square', 'aria-hidden': 'true' \}\)/);
  const convo = src('convo.ts');
  assert.match(convo, /h\('i', \{ class: 'fi fi-search', 'aria-hidden': 'true' \}\)/);
  assert.ok(!convo.includes("'⌕'"));
  assert.match(convo, /h\('span', \{ class: 'mut', title: new Date\(t\.ts\)\.toLocaleString\(\) \}, ' ' \+ auditTime\(t\.ts\)\)/);
  assert.match(css, /\.fi-search \{ --fic: currentColor; -webkit-mask-image: url\(/);
  const today = new Date(2026, 9, 20, 9, 0);
  assert.equal(auditTime(new Date(2026, 9, 20, 15, 7).toISOString(), today), '15:07');
  assert.match(auditTime(new Date(2026, 9, 8, 12, 1).toISOString(), today), / 12:01$/);
  assert.doesNotMatch(auditTime(new Date(2026, 9, 8, 14, 1).toISOString(), today), /PM|AM/);
});

test('9.105: page heads and the Agent drawer head are one 44 px row (title + subtitle inline); one search icon everywhere', () => {
  const b = block('/* ==== 9.105', '/* ==== end 9.105 ==== */');
  assert.match(b, /#main > \.head:not\(\.detail\) \{ min-height: 44px; height: 44px; padding: 0 16px; box-sizing: border-box; \}/);
  assert.match(b, /#main > \.head:not\(\.detail\) \.titles \{ display: flex; align-items: baseline;/);
  assert.match(b, /\.drawer-head \{ height: 44px; box-sizing: border-box;/);
  for (const [f, cls] of [['inbox.ts', 'ibx-sglyph'], ['main.ts', 'wk-sico'], ['convo.ts', 'csearch']]) {
    const s = src(f);
    assert.match(s, new RegExp(`class: '[^']*${cls}'[^\\n]*h\\('i', \\{ class: 'fi fi-search'`), f);
  }
  for (const f of ['inbox.ts', 'main.ts', 'convo.ts']) assert.ok(!src(f).includes("'⌕'"), `${f}: no ⌕ glyph`);
});

test('9.106: the palette keeps command names whole; Files drops the empty tab strip and the second tree toggle', () => {
  const b = block('/* ==== 9.106', '/* ==== end 9.106 ==== */');
  assert.match(b, /\.palette \.pitem > \.name > \.plabel \{ flex: none; max-width: 100%; \}/);
  assert.match(b, /\.files:not\(\.narrow\) \.ftabs > button\.ftree\.on \{ display: none; \}/);
  assert.match(b, /\.files:not\(\.narrow\) \.ftabs:not\(:has\(> :not\(button\.ftree\.on\)\)\) \{ display: none; \}/);
  assert.match(b, /\.layoutmenu \.lthumb\.row \{ margin: 0; justify-content: normal; \}/, 'Beside ▾ thumbs: the generic .row margin');
  assert.match(css, /^\.row \{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; \}/m, 'what it collides with');
  assert.match(src('files.ts'), /h\('button', \{ class: 'icon tree-hide', title: 'hide tree'/, 'the tree keeps its own « to hide it');
});

test('9.107: motion - one set of durations; modals, drawer, menus, the approval card and toasts arrive; reduced motion stays off', () => {
  const b = block('/* ==== 9.107', '/* ==== end 9.107 ==== */');
  assert.match(b, /:root \{ --dur-fast: 120ms; --dur: 180ms; --ease-out: cubic-bezier\(\.2, \.8, \.2, 1\); \}/);
  for (const sel of ['#overlay.open', '.olayer > .modal', '.drawer', '.cmenu, #notices', '#pending > .pending', '.toast'])
    assert.ok(b.includes(`${sel} { animation: pi-`), sel);
  assert.match(b, /@keyframes pi-toast \{ from \{ opacity: 0; transform: translate\(-50%, 8px\); \} \}/, 'keeps the toast centred');
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{ \*, \*::before, \*::after \{ animation-duration: \.01ms !important;[^}]*transition: none !important; \} \}/);
});

test('9.107: "Desktop notifications are blocked" toasts once per browser, not on every reload', () => {
  const m = src('main.ts');
  assert.match(m, /Notification\.permission === 'denied' && firstTimeHere\('piper\.notification-help'\)\) \{/);
  assert.match(m, /function firstTimeHere\(key: string\): boolean \{\n  try \{\n    if \(localStorage\.getItem\(key\)\) return false;\n    localStorage\.setItem\(key, '1'\);\n  \} catch/);
});

test('9.109: the Files tab at phone width is the narrow pane; the tree slides over the editor and closes on a pick', () => {
  const m = src('main.ts');
  assert.match(m, /slotEl\.remove\(\);\n    files\.setNarrow\(phoneWidth\(\)\);/);
  assert.match(m, /matchMedia\('\(max-width: 640px\)'\)\.addEventListener\('change', \(\) => \{ if \(!bodyEl\.classList\.contains\('grid'\)\) files\.setNarrow\(phoneWidth\(\)\); \}\);/);
  assert.match(m, /function phoneWidth\(\): boolean \{\n  return matchMedia\('\(max-width: 640px\)'\)\.matches;/);
  assert.match(src('files.ts'), /onclick: \(\) => \{ if \(narrow && matchMedia\('\(max-width: 640px\)'\)\.matches\) setTree\(false\); void open\(child\); \}/);
  const b = block('/* ==== 9.109', '/* ==== end 9.109 ==== */');
  assert.match(b, /^\/\* ==== 9\.109[^\n]*\n@media \(max-width: 640px\) \{/);
  assert.match(b, /#main \.files\.narrow:not\(\.tree-off\) > \.fleft \{ position: absolute;/);
});

test('9.110: menus open under their anchor; the menu search draws the search icon; Add pane rows have room', () => {
  assert.match(src('menu.ts'), /if \(!\('bottom' in at\)\) return \{ left: clampX\(at\.x\), top: clampY\(at\.y\) \};/);
  const b = block('/* ==== 9.110', '/* ==== end 9.110 ==== */');
  assert.match(b, /\.cmenu \.msearch::before \{ content: ""; flex: none; width: 13px; height: 13px; background: var\(--mut\); -webkit-mask: url\(/);
  assert.match(b, /\.cmenu\.addpane \.ap-row \{ display: inline-flex; align-items: center; gap: 8px; min-width: 0; \}/);
});

test('9.111: the Git window lane graph is cells - a node, a vertical line, a merge glyph - in lane colours; dates in 24 h', async () => {
  const { laneCells } = await import('../../piper/web/dist/history.js');
  assert.deepEqual(laneCells('* '), [{ t: '', lane: 0, kind: 'node' }, { t: ' ', lane: -1, kind: 'sp' }]);
  assert.deepEqual(laneCells('| *').map((c) => [c.kind, c.lane]), [['v', 0], ['sp', -1], ['node', 1]]);
  assert.deepEqual(laneCells('|\\').map((c) => [c.kind, c.t]), [['v', ''], ['g', '\\']]);
  const g = src('gitwindow.ts');
  assert.match(g, /laneCells\(r\.graph\)\.map\(\(x\) => h\('span', \{ class: `gc \$\{x\.kind\}`/);
  assert.match(g, /toLocaleString\(undefined, \{ dateStyle: 'medium', timeStyle: 'short', hourCycle: 'h23' \}\)/);
  const b = block('/* ==== 9.111', '/* ==== end 9.111 ==== */');
  assert.match(b, /\.gc\.node::before, \.gw-rows \.gw-g \.gc\.v::before \{ content: ""; position: absolute;/);
  assert.ok(!b.includes('.gc.dot'), 'the global .dot (10 px status dot) would size the cell');
});

test('9.112: Push… next to Commit is a full-size button; Review lists whole file names; Generate draws the sparkles icon', () => {
  assert.match(src('gitpanel.ts'), /=== 'push' \? 'primary' : '', onclick: \(\) => confirmPush\(\) \}, 'Push…'\)/);
  assert.ok(!src('gitpanel.ts').includes("'primary mini' : 'mini', onclick: () => confirmPush()"));
  const g = src('gitwindow.ts');
  assert.ok(!g.includes('✦'), 'no ✦ glyph in the Git window');
  assert.match(g, /function generateLabel\(\): Node\[\] \{\n  return \[h\('i', \{ class: 'fi fi-sparkles', 'aria-hidden': 'true' \}\), h\('span', \{\}, 'Generate'\)\];/);
  const b = block('/* ==== 9.112', '/* ==== end 9.112 ==== */');
  assert.match(b, /@media \(min-width: 1101px\) \{ \.reviewpane \.review-list \{ width: var\(--rv-w, 248px\); \} \}/);
  assert.match(css, /^\.reviewpane \.review-list \{ width: var\(--rv-w, 190px\);/m, 'what it widens');
  assert.match(b, /\.pd-mrbody button\.link:has\(> \.fi\) \{ display: inline-flex; align-items: center; gap: 4px; \}/);
});

test('9.113: green only for state - the Git window toggles and the list focus rings use the app colours', () => {
  const b = block('/* ==== 9.113', '/* ==== end 9.113 ==== */');
  assert.match(b, /\.gw-fhead button\.mini\.on, \.gw-dhead button\.mini\.on \{ border-color: var\(--mut\); background: var\(--tile\); \}/);
  assert.match(css, /^button\.on \{ background: var\(--tile\); border-color: var\(--mut\);/m, 'the same as every other toggle');
  assert.match(b, /\.hlist:focus-visible, \.gw-rows:focus-visible \{ box-shadow: inset 0 0 0 1px var\(--link\); \}/);
  assert.match(b, /button\.tnode:focus-visible \{ outline-color: var\(--link\); \}/);
});

test('9.114: the editor toolbar draws line icons; a toggle that is on is tile-grey, @ is a plain button', () => {
  const f = src('files.ts');
  for (const g of ["'@'", "'◧'", "'ⓑ'", "'⏱'", "'☰'", "'±'", "ib('←'", "ib('→'"]) assert.ok(!f.includes(`ib(${g.startsWith('ib(') ? g.slice(3) : g}`), g);
  for (const n of ['back', 'fwd', 'at', 'diff', 'blame', 'history', 'outline']) assert.ok(f.includes(`ib(tbIcon('${n}')`), n);
  assert.match(f, /function tbIcon\(name: string\): HTMLElement \{\n  return h\('i', \{ class: `fi fi-\$\{name\}`, 'aria-hidden': 'true' \}\);/);
  assert.match(f, /markdownModeControl\(t.mode/);
  const b = block('/* ==== 9.114', '/* ==== end 9.114 ==== */');
  for (const n of ['back', 'fwd', 'at', 'diff', 'blame', 'outline']) assert.match(b, new RegExp(`^\\.fi-${n} \\{ -webkit-mask-image: url\\("data:image/svg\\+xml,`, 'm'), n);
  for (const n of ['eye', 'history']) assert.match(css, new RegExp(`^\\.fi-${n} \\{`, 'm'), `${n} already exists`);
  assert.match(b, /\.fhead \.ib > \.fi \{ --fic: currentColor; \}/);
  assert.match(b, /\.fhead \.ib\.on \{ color: var\(--fg\); background: var\(--tile\); border-color: var\(--line\); \}/);
  assert.match(b, /\.fhead \.ib\.at \{ color: inherit; background: none; font-weight: normal; \}/);
});

test('9.115: beside the context panel the editor header wraps only when the path would get under 160 px', () => {
  const b = block('/* ==== 9.115', '/* ==== end 9.115 ==== */');
  assert.match(b, /@container fright \(max-width: 520px\) \{\n  \.fright \.fhead \.crumbs \{ flex-basis: 160px; \}/);
  assert.match(css, /\.fright \.fhead \.crumbs \{ flex-basis: 100%; min-height: 24px; \}/, 'the N10-B-3 rule it relaxes');
});

test('9.116: search fields read like the rest - capitalised, short (the detail in the tooltip)', () => {
  assert.match(src('httppanel.ts'), /placeholder: 'Filter requests', title: 'filter files, requests, URLs', class: 'hpfilter'/);
  assert.match(src('board.ts'), /placeholder: 'Filter events'/);
  for (const f of ['board.ts', 'main.ts', 'settings.ts', 'stack.ts', 'convo.ts', 'httppanel.ts', 'inbox.ts', 'gitwindow.ts', 'dbview.ts']) {
    const lower = [...src(f).matchAll(/type: 'search'[^)\n]*?placeholder: '([a-z][^']*)'/g)].map((m) => m[1]);
    assert.deepEqual(lower, [], f);
  }
});

test('9.117: a narrow session bar (tabs on their own row) keeps its first row 44 px, on the line of the queue title', () => {
  const b = block('/* ==== 9.117', '/* ==== end 9.117 ==== */');
  assert.match(b, /@container dh \(max-width: 670px\) \{\n  \.head\.detail\.one \.dh-top \{ row-gap: 0; \}/);
  assert.match(b, /\.head\.detail\.one \.dh-top > :is\(\.layoutctl, \.actions\) \{ height: 44px; display: flex; align-items: center; \}/);
  assert.match(css, /\.head\.detail\.one \.dh-top \{ height: 44px; min-height: 44px; gap: 10px; \}/, 'the gap shorthand that reset row-gap to 10 px');
});

test('9.118: the context panel does not repeat a waiting card in a yellow "Needs you" box (old name, ⚑ glyph)', () => {
  const m = src('main.ts');
  assert.ok(!m.includes('the card in Needs you'));
  assert.match(m, /h\('h3', \{\}, att && !att\.ok \|\| attention \? 'Needs you' : 'Isolation warning'\)/, 'the box stays for a failed isolation check and attention');
});

test('9.119: Beside ▾ rows carry their icons (as the rail and the other menus); the layout thumbnails have contrast', () => {
  const m = src('main.ts');
  assert.match(m, /h\('span', \{ class: 'bcheck', 'aria-hidden': 'true' \}, onNow \? '✓' : ''\), h\('i', \{ class: `fi fi-\$\{icon\}`, 'aria-hidden': 'true' \}\)/);
  const b = block('/* ==== 9.119', '/* ==== end 9.119 ==== */');
  for (const [icon] of [["dir"], ["db"], ["http"], ["term"], ["grid"], ["pen"]]) assert.match(b + css, new RegExp(`\\.fi-${icon} \\{`), icon);
  assert.match(b, /\.besidemenu \.lthumb \.lcell \{ background: color-mix\(in srgb, var\(--mut\) 18%, transparent\); border-color: color-mix\(in srgb, var\(--mut\) 45%, transparent\); \}/);
});

test('9.120: context menus draw line icons - the session menu and Add pane; a glyph icon still works', () => {
  const m = src('main.ts');
  const menuFn = m.slice(m.indexOf("label: 'Open', icon:"), m.indexOf("label: 'Remove', icon:") + 40);
  const icons = [...menuFn.matchAll(/icon: (?:inGrid \? )?'([^']+)'(?: : '([^']+)')?/g)].flatMap((x) => [x[1], x[2]]).filter(Boolean);
  assert.ok(icons.length >= 15, String(icons.length));
  for (const i of icons) {
    assert.match(i, /^fi-[a-z-]+$/, i);
    assert.match(css, new RegExp(`^\\.${i} \\{`, 'm'), `${i} is defined`);
  }
  assert.match(m, /const ic = \(name: string\) => h\('i', \{ class: `fi fi-\$\{name\}`, 'aria-hidden': 'true' \}\);/);
  assert.ok(!m.includes("'>_')"), 'no >_ glyph for a host shell');
  assert.match(src('menu.ts'), /it\.icon\?\.startsWith\('fi-'\) \? h\('i', \{ class: `fi \$\{it\.icon\}` \}\) : it\.icon \?\? ''/);
  const b = block('/* ==== 9.120', '/* ==== end 9.120 ==== */');
  assert.match(b, /\.cmenu \.micon > \.fi \{ --fic: currentColor; \}/, 'a danger row keeps its red');
});

test('9.121: a plan-usage row with no numbers says why instead of empty bars', async () => {
  const { noteText } = await import('../../piper/web/dist/limits.js');
  assert.equal(noteText({ status: 'expired', error: '' }), 'login expired');
  assert.equal(noteText({ status: 'error', error: 'HTTP 503' }), 'HTTP 503');
  assert.equal(noteText({ status: 'error', error: '' }), 'usage unavailable');
  const l = src('limits.ts');
  assert.match(l, /const unknown = s\.status !== 'ok' && values\[0\] === null && values\[1\] === null;/);
  assert.match(l, /for \(const c of p\.cells\) c\.hidden = unknown;/);
  const b = block('/* ==== 9.121', '/* ==== end 9.121 ==== */');
  assert.match(b, /\.ql-provider > \.ql-note \{ grid-column: 2 \/ -1;/);
  assert.match(b, /color: var\(--warn\)/);
});

test('9.123: the collapsed usage tile without numbers shows a warning icon and says why', () => {
  const l = src('limits.ts');
  assert.match(l, /h\('i', \{ class: 'fi fi-alert qml-warn', 'aria-hidden': 'true' \}\)/);
  assert.match(l, /p\.mini\.classList\.toggle\('unknown', unknown\);/);
  assert.match(l, /const description = unknown \? `\$\{p\.name\} · \$\{noteText\(s\)\}` :/);
  const b = block('/* ==== 9.123', '/* ==== end 9.123 ==== */');
  assert.match(b, /#actbar \.qm-lim\.unknown :is\(\.qml-pct, \.qml-bar\) \{ display: none; \}/);
  assert.match(b, /--fic: var\(--warn\)/);
});

test('9.124: History and Audit records draw line icons, not text glyphs; the footer drops its dot beside the note', async () => {
  const { auditIcon } = await import('../../piper/web/dist/board.js');
  assert.equal(auditIcon({ event: 'git-push', ts: '' }), 'up');
  assert.equal(auditIcon({ event: 'confirm', ts: '' }), 'ask');
  assert.equal(auditIcon({ event: 'gui-read-hidden', ts: '' }), 'eye');
  assert.equal(auditIcon({ event: 'session-created', ts: '' }), 'dot');
  assert.equal(auditIcon({ event: 'tool', ts: '', env: 'prod' }), 'prod');
  const b = block('/* ==== 9.124', '/* ==== end 9.124 ==== */');
  assert.match(b, /^\.fi-ask \{ -webkit-mask-image: url\(/m);
  assert.match(b, /\.audit \.arow\.risk-prod \.aicon > \.adot \{[^}]*background: var\(--bad\);/);
  assert.ok(!/'[↑⎇·?◉●]'/.test(src('board.ts').slice(src('board.ts').indexOf('export function auditIcon'), src('board.ts').indexOf('function auditIconEl'))));
  assert.match(src('limits.ts'), /p\.indicator\.hidden = !stale \|\| unknown;/);
});

test('9.125 (N3): a card of an off module does not float over the content until its row is picked', async () => {
  const { cardModule } = await import('../../piper/web/dist/decision.js');
  assert.equal(cardModule('spawn_session'), 'agent');
  assert.equal(cardModule('propose_session'), 'agent');
  assert.equal(cardModule('gitlab_mr_merge'), 'gitlab');
  assert.equal(cardModule('github_pr_write'), 'github');
  assert.equal(cardModule('http_prod'), 'http');
  assert.equal(cardModule('db_write_dev'), 'db');
  assert.equal(cardModule('allow_domain'), null);
  assert.equal(cardModule('git_push'), null);
  const m = src('main.ts');
  assert.match(m, /const list = state\.pending\.filter\(\(p\) => cardVisible\(p,.*p\.id === picked\)\);/);
  assert.ok(m.indexOf("const picked = pendingPick ||") < m.indexOf('const list = state.pending.filter'), 'picked is known before the filter');
});

test('9.126: a long session-bar chip is a block that ends in an ellipsis, not an inline-flex cut mid-letter', () => {
  const b = block('/* ==== 9.126', '/* ==== end 9.126 ==== */');
  assert.match(b, /\.head\.detail\.one \.dh-chips span\.chip \{ display: inline-block;/);
  assert.match(css, /\.head\.detail\.one \.dh-chips \.chip \{[^}]*text-overflow: ellipsis;/);
});

test('9.127: the Resume filters are one 30 px control set; the project button has a chevron icon, not ▾', () => {
  const c = src('convo.ts');
  assert.ok(!c.includes("' ▾'") && !c.includes("'All projects ▾'"));
  assert.match(c, /h\('span', \{ class: 'rp-label' \}, 'All projects'\), h\('i', \{ class: 'fi fi-chev-d', 'aria-hidden': 'true' \}\)/);
  assert.match(c, /projectButton\.querySelector\('\.rp-label'\)!\.textContent = markBidi\(value \? rawProjectKey\(value\) : 'All projects'\);/);
  const b = block('/* ==== 9.127', '/* ==== end 9.127 ==== */');
  assert.match(b, /\.resume-filters > input, \.resume-filters > select, \.resume-project \{ height: 30px;/);
  assert.match(b, /\.resume-project \{[^}]*border: 1px solid var\(--line\); background: var\(--well\);/);
});

test('9.128: Start work search icon inside the field, Options chevron icon, no hole flags in the visible option text', () => {
  const b = block('/* ==== 9.128', '/* ==== end 9.128 ==== */');
  assert.match(b, /\.wk-search > \.wk-sico \{ position: absolute; left: 9px;/);
  assert.match(b, /\.wk-search > input\[type=search\] \{ height: 32px; box-sizing: border-box; padding-left: 29px; \}/);
  assert.match(b, /\.startopts\[open\] > summary::before \{ transform: rotate\(90deg\); \}/);
  assert.match(b, /prefers-reduced-motion: reduce/);
  const m = src('main.ts');
  assert.match(m, /choices\.append\(h\('label', \{ class: 'so-choice'/);
  assert.ok(!m.includes("'hole --rebuild for this start"));
});

test('9.129: Conversation without text glyphs - a turning chevron on tool rows, expand / shrink icons, "To <session>"', () => {
  const c = src('convo.ts');
  assert.ok(!/'▾' : '▸'/.test(c));
  assert.match(c, /h\('span', \{ class: 'tcaret', 'aria-hidden': 'true' \}\),/);
  const k = src('composer.ts');
  assert.match(k, /sizeBtn\.replaceChildren\(h\('i', \{ class: `fi fi-\$\{on \? 'expand' : 'shrink'\}`, 'aria-hidden': 'true' \}\), on \? markBidi\(kbd\('⌘E'\)\) : ''\);/);
  assert.ok(!k.includes('⤢') && !k.includes('`→ $'));
  assert.match(k, /target\.textContent = markBidi\(`To \$\{deps\.title\(bound\)\}/);
  const b = block('/* ==== 9.129', '/* ==== end 9.129 ==== */');
  for (const i of ['expand', 'shrink']) assert.match(b, new RegExp(`^\\.fi-${i} \\{ -webkit-mask-image: url\\(`, 'm'));
  assert.match(b, /\.convo \.trow\.open \.tcaret \{ transform: rotate\(90deg\); \}/);
});

test('9.130: palette sub-action chevron is an icon; rebuild hints and notes in plain words', async () => {
  const p = src('palette.ts');
  assert.match(p, /enter\(c\); \} \}, h\('i', \{ class: 'fi fi-chev-r', 'aria-hidden': 'true' \}\)\) : null\)\);/);
  const m = src('main.ts');
  assert.ok(!m.includes("hint: 'hole --rebuild") && !m.includes("hint: 'the next start runs without hole --rebuild'"));
  const { rebuildNote } = await import('../../piper/web/dist/setup.js');
  assert.ok(!rebuildNote().includes('hole --') && !rebuildNote('x').includes('hole --'));
  assert.match(block('/* ==== 9.130', '/* ==== end 9.130 ==== */'), /\.pitem \.psub > \.fi \{ width: 12px; height: 12px; --fic: currentColor; \}/);
});

test('9.131 the commit split caret is an icon and dims with a disabled Commit', () => {
  const g = src('gitpanel.ts');
  assert.ok(!/'More commit actions'[^\n]*'▾'\)/.test(g));
  assert.match(g, /'More commit actions'[^\n]*h\('i', \{ class: 'fi fi-chev-d' \}\)/);
  const b = block('/* ==== 9.131', '/* ==== end 9.131 ==== */');
  assert.match(b, /\.split:has\(> button:first-child:disabled\) > \.split-more:not\(:hover\)/);
});

test('9.131 a reload restoring a removed session does not toast "unknown: <id>"', () => {
  const c = src('convo.ts');
  assert.match(c, /const r = await api\.conversation\(id\);[\s\S]{0,600}if \(!\(e instanceof ApiError && e\.status === 404\)\) deps\.toast\(/);
});

test('9.131 a changed setting resets with "Reset to default" and an icon, not a bare "default" chip', () => {
  const st = src('settings.ts');
  assert.match(st, /h\('button', \{ class: 'st-reset', title: `back to the default[\s\S]{0,400}\}, fi\('history'\), 'Reset to default'\)/);
  const b = block('/* ==== 9.131', '/* ==== end 9.131 ==== */');
  assert.match(b, /\.settings \.st-ctl > \.st-reset \{ display: inline-flex;/);
});

test('9.131 the GUI names no company host or brand (Piper is to be open-sourced)', () => {
  for (const f of ['dbview.ts', 'settings.ts', 'main.ts', 'theme.ts']) {
    const code = src(f).split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n');
    assert.ok(!/\.cz\b/i.test(code), `${f}: a company host`);
  }
  assert.match(src('theme.ts'), /const INDIGO = 'indigo';/, 'the theme is keyed by its own name in Piper\'s themes file');
});
