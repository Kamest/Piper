// node --test: F03 session header - one 36 px bar, everything beside the terminal in "Beside ▾".
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const dist = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const fn = (src, name) => src.slice(src.indexOf(`function ${name}(`), src.indexOf('\n}\n', src.indexOf(`function ${name}(`)) + 2);

test('F03: the name is the project\'s last segment when the title only names the project', () => {
  const body = fn(dist, 'headTitle');
  const headTitle = new Function(`${body}; return headTitle;`)();
  const s = (title, project = 'java/acme-app') => ({ title, project });
  assert.equal(headTitle(s('java/acme-app')), 'java/acme-app');
  assert.equal(headTitle(s('java/acme-app · 2')), 'java/acme-app · 2');
  assert.equal(headTitle(s('fix the PDF export')), 'fix the PDF export', 'an own title stays');
  assert.equal(headTitle(s('java/acme-appx')), 'java/acme-appx', 'only the project itself, not a longer name');
  assert.equal(headTitle(s('demo', 'demo')), 'demo');
});

test('F03: one bar - no project chip, no Files / DB pane toggles, no + Add pane, no Single | Grid; Stop is an icon', () => {
  const head = fn(main, 'header');
  assert.match(head, /h\('header', \{ class: 'head detail one', 'data-state': s\.state \},\n\s+h\('div', \{ class: 'dh-top' \},/);
  assert.ok(!/dh-bot/.test(head), 'no second row');
  assert.doesNotMatch(head, /'Files pane'|'DB pane'|'Composer'\)|class: 'chip proj'/);
  assert.match(head, /title: s\.kind === 'advisor' \? 'all projects' : `\$\{name\} · \$\{rawProjectKey\(s\.project\)\} · \$\{s\.path\} · double-click = rename`/, 'the path in the title\'s tooltip');
  assert.match(head, /h\('button', \{ class: 'icon dh-stop', title: 'Stop the session', 'aria-label': 'Stop', onclick: \(\) => confirmStop\(s\) \}, h\('i', \{ class: 'fi fi-square', 'aria-hidden': 'true' \}\)\)/, '9.104 an icon, not the ■ glyph');
  assert.match(head, /att\.ok \? '✓ isolated' : '✗ isolation'/);
  assert.match(head, /proj && httpTab\(s\.project\) && shown\('http'\)/, 'F09: HTTP only with .http files');
  const ctl = fn(main, 'layoutControl');
  assert.match(ctl, /class: 'beside' \+ \(open \? ' on' : ''\), 'aria-haspopup': 'menu'/);
  assert.doesNotMatch(ctl, /seg\(|addpane|layoutbtn/);
});

test('F03: Beside ▾ - tools, Another session…, separator, Restore previous layout ⌘G with caption, then Composer and Layout', () => {
  const m = fn(main, 'besideMenu');
  const order = ["row('dir', 'Files', '⌘O'", "row('db', 'Database', '⌘D'", "row('http', 'HTTP', ''", "row('term', 'Terminal', '⌥⇧T'", "'Another session…'", "role: 'separator'", "row('history', 'Restore previous layout', '⌘G'", "row('pen', 'Composer', '⌘E'", "'Layout'"];
  const at = order.map((k) => m.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at, 'the board\'s order');
  assert.match(m, /role: 'menuitemcheckbox', class: 'bitem', 'aria-checked': String\(onNow\)/, 'a tick = it is beside now');
  assert.match(m, /on\('grid'\) \? row\('grid', 'Another session…', '', false, full \? `\$\{GRID_MAX\} panes at most` : '',\n\s+'Choose a session to add without restoring a previous layout', \(\) => addPaneMenu\(anchor\)\) : null,/, 'adding a session opens the chooser without restoring the old layout');
  assert.match(m, /on\('grid'\) && !open \? h\('div', \{ class: 'lsec', role: 'separator' \}\) : null,/);
  const restore = m.split('\n').find((line) => line.includes("row('history', 'Restore previous layout', '⌘G'"));
  assert.ok(restore, 'restoring has its own row');
  assert.match(restore, /on\('grid'\) && !open \?/);
  assert.match(restore, /false, rest > 1 \? '' : 'No saved layout', 'Restore the previous pane selection', \(\) => toggleGrid\(true\), restoreCaption\) : null,/);
  assert.match(m, /caption \? h\('span', \{ class: 'small mut', style: 'display: block' \}, caption\) : null/, 'the saved panes are secondary text inside the button');
  assert.doesNotMatch(m, /class: 'sec-h lsec' \}, 'Restore previous layout'/, 'Restore is an actionable label, not a section heading');
  assert.match(m, /proj \? row\('term', 'Terminal'/, 'terminal remains discoverable when the module is off');
  assert.match(m, /proj && on\('db'\) \? row\('db', 'Database'/);
  assert.match(m, /'Choose panes…'[^]*kbd\('Single ⌘G'\)/, 'the open grid: Choose panes… and back to Single');
  assert.match(m, /else if \(e\.key === 'Escape'\) \{ close\(\); anchor\.focus\(\); \}/);
});

test('W02: the restore caption names saved tools and the grid count, with an explicit empty state', () => {
  const m = fn(main, 'besideMenu');
  assert.match(m, /restoreMembers\(layoutCtx\(\)\)\.filter\(id => paneKind\(id\) !== 'session'\)/, 'the implicit session does not crowd the tool caption');
  const expression = m.match(/const restoreCaption = ([^;]+);/);
  assert.ok(expression);
  const caption = new Function('rest', 'restoredTools', `return ${expression[1]};`);
  assert.equal(caption(2, ['Files']), 'Files · Grid 2');
  assert.equal(caption(3, []), 'Grid 3', 'sessions only has no redundant Session label');
  assert.equal(caption(4, ['Files', 'Database', 'Database']), 'Files · Database · Grid 4');
  assert.equal(caption(1, []), 'No saved layout');
  assert.equal(caption(0, []), 'No saved layout');
});

test('F03: right-click still hides tabs; the pane toggles left the list, Composer is hidden from Beside ▾', () => {
  const items = fn(main, 'tabItems');
  assert.doesNotMatch(items, /'beside'|'db'/);
  assert.match(items, /\['composer', 'Composer in Beside ▾', '⌘E', on\('composer'\)\]/);
  assert.match(fn(main, 'besideMenu'), /on\('composer'\) && !hiddenTabs\.has\('composer'\)/);
});

test('F03: CSS - a 36 px bar, 36 px tabs with a 1 px underline, the flow strip only with room, a neutral selected preset', () => {
  const block = css.slice(css.indexOf('/* ==== 9.69 F03'), css.indexOf('/* ==== end F03 ==== */'));
  assert.ok(block.length > 200);
  assert.match(block, /\.head\.detail\.one \.dh-top \{[^}]*height: 36px/);
  assert.match(block, /@container dh \(max-width: 1400px\) \{ \.head\.detail\.one \.cflow \{ display: none; \} \}/);
  assert.match(block, /\.besidemenu \.lcard\.on \.lthumb \{ outline: 2px solid var\(--fg\)/);
  assert.match(block, /\.besidemenu \.lgrid \{ grid-template-columns: repeat\(5, 1fr\)/);
  assert.match(block, /\.head\.detail\.one \.tabs \{[^}]*flex: 0 1 auto; min-width: 0;[^}]*overflow-x: auto/,
    'the tabs shrink and scroll before ⋯ or Stop leave the bar (1024 with the queue open)');
  assert.match(block, /@container dh \(max-width: 670px\) \{\n  \.head\.detail\.one \.dh-top \{ height: auto; flex-wrap: wrap; row-gap: 0; \}\n  \.head\.detail\.one \.dh-top \.tabs \{ order: 10; flex: 1 1 100%;/,
    'a narrow detail: the tabs in their own row, never over the terminal');
});
