// node --test: F28 - one session menu with icons, Stop / Remove last in red; alerts in groups.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const dist = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const menu = readFileSync(new URL('../../piper/web/src/menu.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const fn = (src, name) => src.slice(src.indexOf(`function ${name}(`), src.indexOf('\n}\n', src.indexOf(`function ${name}(`)) + 2);

test('F28: the session menu - icons, groups View / Order, Continue / Stop… / Remove last (Stop and Remove red); Z items kept', () => {
  const m = fn(main, 'openRailMenu');
  const order = ["label: 'Open', icon: 'fi-open'", "label: 'Side by side', icon: 'fi-columns'", "'Remove from grid' : 'Add to grid'", "{ group: 'View' }", "label: 'Files', icon: 'fi-dir'",
    "label: 'HTTP requests', icon: 'fi-http'", "label: 'Conversation', icon: 'fi-chat'", "label: 'History', icon: 'fi-history'", "label: 'Terminal beside this session', icon: 'fi-term'", "{ group: 'Order' }",
    "'Unpin' : 'Pin to top', icon: 'fi-pin'", "label: 'Move up', icon: 'fi-up'", "label: 'Move down', icon: 'fi-down'", "label: 'Rename…', icon: 'fi-pen'", 'null,',
    "label: continueLabel(s), icon: 'fi-refresh'", "label: 'Stop…', icon: 'fi-square', run: () => confirmStop(s), danger: true", "label: 'Remove', icon: 'fi-x'"];
  const at = order.map((k) => m.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at);
  assert.match(m, /title: sshOnly\(s\.id\) \? 'remove the ended session from SSH session records' : 'from the list - transcript and audit stay', danger: true \}/);
  assert.match(m, /on\('http'\) && !sshOnly\(s\.id\) \? \{ label: 'HTTP requests'/, 'Z: HTTP with the module');
  assert.match(m, /hasTerminal\(s\) && on\('grid'\) \? \{ label: inGrid/, 'Z: grid rows with the grid module');
});

test('F28: menu.ts keeps an icon column for every item once one has an icon', () => {
  assert.match(menu, /const icons = rows\.some\(\(r\) => r\.kind === 'item' && r\.item\.icon !== undefined\);/);
  assert.match(menu, /icons \? h\('span', \{ class: 'micon', 'aria-hidden': 'true' \}, it\.icon\?\.startsWith\('fi-'\) \? h\('i', \{ class: `fi \$\{it\.icon\}` \}\) : it\.icon \?\? ''\) : null,/);
});

test('F28: alerts - Needs you (with an action) above Earlier, Clear all in the head, the desktop notification state + Kept 24 h', () => {
  const desktopPermission = new Function(`${fn(dist, 'desktopPermission')}; return desktopPermission;`)();
  assert.equal(desktopPermission('granted').text, 'Desktop notifications on');
  assert.equal(desktopPermission('denied').cls, 'warn');
  assert.match(desktopPermission('denied').title, /site settings/);
  assert.equal(desktopPermission('default').ask, true);
  assert.equal(desktopPermission('granted').empty, 'Desktop notifications are on.');
  const o = fn(main, 'openNotices');
  assert.match(o, /const needs = list\.filter\(\(n\) => n\.action\);/);
  assert.match(o, /needs\.length \? h\('div', \{ class: 'nt-sec' \}, 'Needs you'\) : null, \.\.\.needs\.map\(row\),/);
  assert.match(o, /h\('b', \{\}, 'Alerts'\)/);
  assert.match(o, /'Kept 24 h'/);
  assert.match(o, /const emptyText = mobilePushClient\(\) \? 'Nothing new\.' : `Nothing new\. \$\{perm\.empty\}`;/);
  assert.doesNotMatch(o, /cleared on close/);
  const block = css.slice(css.indexOf('/* ==== 9.80 F28'), css.indexOf('/* ==== end F28 ==== */'));
  assert.match(block, /\.nt-sec \{[^}]*text-transform: uppercase/);
});
