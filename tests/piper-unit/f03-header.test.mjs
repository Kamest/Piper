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
  assert.match(head, /title: s\.kind === 'advisor' \? 'all projects' : `\$\{rawProjectKey\(s\.project\)\} · \$\{s\.path\} · double-click = rename`/, 'the path in the title\'s tooltip');
  assert.match(head, /h\('button', \{ class: 'icon dh-stop', title: 'Stop the session', 'aria-label': 'Stop', onclick: \(\) => confirmStop\(s\) \}, h\('i', \{ class: 'fi fi-square', 'aria-hidden': 'true' \}\)\)/, '9.104 an icon, not the ■ glyph');
  assert.match(head, /att\.ok \? '✓ isolated' : '✗ isolation'/);
  assert.match(head, /proj && httpTab\(s\.project\) && shown\('http'\)/, 'F09: HTTP only with .http files');
  const ctl = fn(main, 'layoutControl');
  assert.match(ctl, /class: 'beside' \+ \(open \? ' on' : ''\), 'aria-haspopup': 'menu'/);
  assert.doesNotMatch(ctl, /seg\(|addpane|layoutbtn/);
});

test('F03: Beside ▾ - Files ⌘O, Database ⌘D, HTTP, Terminal, Another session… / Grid ⌘G, Composer ⌘E, then Layout', () => {
  const m = fn(main, 'besideMenu');
  const order = ["row('dir', 'Files', '⌘O'", "row('db', 'Database', '⌘D'", "row('http', 'HTTP', ''", "row('term', 'Terminal', '⌥⇧T'", "'Another session…'", "row('pen', 'Composer', '⌘E'", "'Layout'"];
  const at = order.map((k) => m.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at, 'the board\'s order');
  assert.match(m, /role: 'menuitemcheckbox', class: 'bitem', 'aria-checked': String\(onNow\)/, 'a tick = it is beside now');
  assert.match(m, /`Another session… \/ Grid\$\{rest > 1 \? ` \$\{rest\}` : ''\}`/, '9.45\'s Grid n moved into the row');
  assert.match(m, /proj \? row\('term', 'Terminal'/, 'terminal remains discoverable when the module is off');
  assert.match(m, /proj && on\('db'\) \? row\('db', 'Database'/);
  assert.match(m, /'Choose panes…'[^]*kbd\('Single ⌘G'\)/, 'the open grid: Choose panes… and back to Single');
  assert.match(m, /else if \(e\.key === 'Escape'\) \{ close\(\); anchor\.focus\(\); \}/);
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
