// node --test: W14 - the Settings tree by meaning: group titles from piperd, the crumb over a
// section, the New session dialog's link to the template. Pure helper from web/dist; wiring over web/src.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { settingsCrumb } from '../../piper/web/dist/settings.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const GROUPS = [['general', 'Appearance & Behavior'], ['sessions', 'Sessions'], ['vcs', 'Version Control']];
const SECS = [
  { id: 'features', title: 'Features', group: 'general' }, { id: 'interface', title: 'Interface', group: 'general' },
  { id: 'sessions', title: 'New sessions', group: 'sessions' }, { id: 'approvals', title: 'Approvals', group: 'sessions' },
  { id: 'git', title: 'Git', group: 'vcs' }, { id: 'old', title: 'Old' }, { id: 'x', title: 'X', group: 'later' },
];

test('W14 settingsCrumb: the group title, the section, the group\'s first section', () => {
  assert.deepEqual(settingsCrumb(GROUPS, SECS, 'approvals'), { group: 'Sessions', title: 'Approvals', first: 'sessions' });
  assert.deepEqual(settingsCrumb(GROUPS, SECS, 'git'), { group: 'Version Control', title: 'Git', first: 'git' });
  assert.deepEqual(settingsCrumb(GROUPS, SECS, 'old'), { group: 'Appearance & Behavior', title: 'Old', first: 'features' },
    'no group = general');
  assert.equal(settingsCrumb(GROUPS, SECS, 'x').group, 'later', 'an unknown group shows its id');
  assert.equal(settingsCrumb([], SECS, 'git').group, 'vcs', 'an older piperd without groups');
  assert.equal(settingsCrumb(GROUPS, SECS, 'nope'), null);
});

test('W14 wiring: titles from the response, the crumb above the h2, no own group map', () => {
  const s = read('settings.ts');
  assert.ok(!/GROUP_TITLE/.test(s), 'the titles come from piperd\'s GROUPS');
  assert.match(s, /groups = \(r\.json\.groups as \[string, string\]\[\]\) \?\? \[\];/);
  assert.match(s, /const g = s\.group \?\? 'general';/);
  assert.match(s, /h\('div', \{ class: 'st-crumb' \}, h\('button', \{ class: 'link'[^]*?onclick: \(\) => select\(crumb\.first\) \}, crumb\.group\)/);
  assert.match(s, /crumb\.title\),\n\s+h\('h2'/, 'the crumb sits right over the title');
  assert.match(css, /\.settings \.st-crumb \{/);
});

test('Project options replace the global template link; the Isolation panel names the Sandbox', () => {
  const m = read('main.ts');
  assert.doesNotMatch(m, /Defaults: Settings › Sessions › New sessions/);
  assert.match(m, /'Remember', h\('span', \{ class: 'so-rem-long' \}, ' for this project'\)/);
  assert.ok(!/Settings → Sessions/.test(m), 'no path to the old Sessions section');
  assert.match(m, /ownConfig \? prop\(s\.claude_mode === 'none' \? '~\/\.codex' : '~\/\.claude'/);
  assert.match(m, /Settings › Sessions › Sandbox → Unfiltered network/);
});

test('W14 board: the isolation badge under the label, the header names the tree', () => {
  assert.match(css, /\.settings \.st-label \.st-badge \{ display: table;/);
  assert.match(read('main.ts'), /settings: \['Settings', 'features, projects, sessions, version control, tools, isolation and Piper’s environment'\]/);
});
