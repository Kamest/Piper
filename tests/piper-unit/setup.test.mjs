// node --test: feature modules and the setup card's pure parts
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { checkSummary, imageLine, keepOn, rebuildNote, rebuildWhere, moduleOn, offModules, presetChips, presetDiff, readiness, recheckDelay, rowAction, setupWanted, setupUsesHole, splitRows } from '../../piper/web/dist/setup.js';

const feat = (modules, onboarded = true) => ({ features: { modules, onboarded, preset: 'custom', list: [], presets: [] } });
const row = (key, ok, level = 'fail', title = key) => ({ key, ok, level, title, text: '', fix: '', settings: '', action: '' });
const report = (rows, deep = false) => ({ rows, at: 0, deep, fail: rows.filter((r) => !r.ok && r.level === 'fail').length,
  warn: rows.filter((r) => !r.ok && r.level === 'warn').length });

test('an older piperd without features: everything on; a module piperd does not list: off', () => {
  assert.equal(moduleOn({}, 'gitlab'), true);
  assert.equal(moduleOn(feat({ gitlab: false }), 'gitlab'), false);
  assert.equal(moduleOn(feat({ gitlab: false }), 'grid'), false);   // not listed (an extension's, not loaded) = off
});

test('commands and rows of a module that is off are left out', () => {
  const st = feat({ grid: false, http: true });
  const cmds = [{ label: 'a' }, { label: 'grid', module: 'grid' }, { label: 'http', module: 'http' }];
  assert.deepEqual(keepOn(cmds, (m) => moduleOn(st, m)).map((c) => c.label), ['a', 'http']);
});

test('the setup card: a new install, or a check that blocks sessions', () => {
  assert.equal(setupWanted(feat({}, false), null), true);
  assert.equal(setupWanted(feat({}, true), null), false);
  assert.equal(setupWanted(feat({}, true), report([row('docker', false)])), true);
  assert.equal(setupWanted({}, report([row('login', false, 'warn')])), false);
});

test('readiness: one green line with the facts, or the problems counted', () => {
  const ok = report([row('hole', true, 'fail', 'hole 2.1.0'), row('docker', true, 'fail', 'Docker 29.8.1'), row('git', true),
    row('projects', true, 'warn', '55 projects in ~/www')]);
  assert.deepEqual(readiness(ok), { tone: 'ok', title: 'This machine is ready', detail: '4 of 4 checks passed · hole 2.1.0 · Docker 29.8.1 · 55 projects in ~/www' });
  const bad = report([row('hole', true), row('docker', false, 'fail', 'Docker is not running'), row('login', false, 'warn'), row('service', false, 'warn')]);
  assert.deepEqual(readiness(bad), { tone: 'fail', title: '1 blocker, 2 warnings', detail: '1 of 4 checks passed' });
  const warn = report([row('hole', true), row('login', false, 'warn')]);
  assert.equal(readiness(warn).tone, 'warn');
  assert.equal(readiness(warn).title, 'This machine works, with 1 warning');
});

test('splitRows: failures before warnings; an info row goes to its module tile, not the problems', () => {
  const rep = report([row('a', true), row('b', false, 'warn'), row('c', false, 'info'), row('d', false, 'fail'), row('e', true)]);
  const { problems, passed, info } = splitRows(rep);
  assert.deepEqual(problems.map((r) => r.key), ['d', 'b']);
  assert.deepEqual(passed.map((r) => r.key), ['a', 'e']);
  assert.deepEqual(info.map((r) => r.key), ['c']);
});

test('an optional module that is not set up (dev stack down) leaves the machine ready', () => {
  const rep = report([row('hole', true, 'fail', 'hole 2.1.0'), row('stack', false, 'info', 'the dev stack is not running')]);
  assert.equal(readiness(rep).tone, 'ok');
  assert.match(readiness(rep).detail, /^1 of 1 checks passed/);
});

test('preset chips are the labels of its modules; Everything is one chip', () => {
  const f = { modules: {}, onboarded: true, preset: 'basic', presets: [],
    list: [['diag', 'Diagnostics', ''], ['stack', 'Dev stack', ''], ['grid', 'Grid', '']],
    preset_modules: { basic: ['diag', 'stack'], all: ['diag', 'stack', 'grid'] } };
  assert.deepEqual(presetChips(f, 'basic'), ['Diagnostics', 'Dev stack']);
  assert.deepEqual(presetChips(f, 'all'), ['all 3 modules']);
  assert.deepEqual(presetChips({ ...f, preset_modules: undefined }, 'basic'), []);   // an older piperd: no chips
});

test('offModules counts the modules that are off', () => {
  const f = { modules: { diag: true, grid: false, http: false }, onboarded: true, preset: 'custom', presets: [],
    list: [['diag', 'Diagnostics', ''], ['grid', 'Grid', ''], ['http', 'HTTP', '']] };
  assert.equal(offModules(f), 2);
  assert.equal(offModules(undefined), 0);
});

test('N10-D-5: the setup card keeps the keyboard on the switch / preset / button it redraws (focus fell to <body>)', async () => {
  const src = (await import('node:fs')).readFileSync(new URL('../../piper/web/src/setup.ts', import.meta.url), 'utf8');
  const draw = src.slice(src.indexOf('  function draw(): void {'), src.indexOf('  return {\n    el,'));
  assert.match(draw, /const fk = \(el\.contains\(document\.activeElement\) \? \(document\.activeElement as HTMLElement\)\.dataset\.fk : undefined\)\s*\?\? \(document\.activeElement === document\.body \? keepFk : undefined\)/);
  assert.match(draw, /if \(back && !back\.disabled\) back\.focus\(\{ preventScroll: true \}\);\s*keepFk = back\?\.disabled \? fk \?\? '' : '';/);
  for (const k of ["'data-fk': `sw:${id}`", "'data-fk': `preset:${id}`", "'data-fk': 'check'", "'data-fk': 'deep'"]) assert.ok(src.includes(k), k);
});

test('9.54 image row: the newest agent image, its use, and the arm / cancel action', () => {
  const img = (ref, age, in_use = 0) => ({ ref, size: '6.1GB', age, created: '', in_use });
  const info = { ok: true, in_use: 2, images: [img('hole-sandbox/agent-global:f3a82d025ae6', '3 weeks ago', 2), img('hole-sandbox/agent-global:63db1184bb43', '5 weeks ago')] };
  assert.deepEqual(imageLine(info, false), { text: 'agent-global:f3a82d02 · 6.1GB · built 3 weeks ago · in use by 2 sandboxes · +1 older', tone: 'ok', action: '' });   // information only: armed in Start work / ⌘K
  assert.match(imageLine({ ok: true, in_use: 1, images: [img('hole-sandbox/agent-global:f3a82d025ae6', '1 day ago', 1)] }, false).text, /in use by 1 sandbox$/);
  assert.deepEqual(imageLine(info, true), { text: 'rebuild pending - the next start of any session · agent-global:f3a82d02 · 6.1GB', tone: 'warn', action: 'cancel' });
  assert.match(imageLine(info, true, 'shop').text, /^rebuilding now in shop/);
  assert.equal(imageLine(info, true, 'shop').action, '');   // a build under way: no Cancel that stops nothing
  assert.equal(imageLine(null, false).action, '');
  assert.deepEqual(imageLine({ ok: false, error: 'Cannot connect' }, false), { text: 'docker: Cannot connect', tone: 'warn', action: '' });
  assert.equal(imageLine({ ok: true, in_use: 0, images: [] }, false).action, '');   // nothing to rebuild yet
});

test('9.54 the armed note: the next start, then the start that builds', () => {
  assert.match(rebuildNote(), /^The next start of any session rebuilds the image: /);
  assert.match(rebuildNote('shop'), /^The image is rebuilt in shop:.*clears when that session starts its agent\.$/);
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /JSON\.stringify\(\[state\.features \?\? null, state\.image_rebuild \?\? \{\}\]\)/);   // redraw on claim / done
});

test('9.54 while a start builds the image, the GUI offers no arm / cancel and no "at this start"', () => {
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /if \(!rebuildState\(\)\.by\) cmds\.push\(rebuildArmed\(\)/);
  assert.match(main, /function rebuildArmed\(\): boolean \{[^}]*return r\.pending && !r\.by;/);
  assert.match(main, /const armed = rebuildArmed\(\)/);   // the dialog's summary
  assert.match(main, /rebuildPending: \(\) => rebuildArmed\(\)/);
});

test('9.54 Setup names where the rebuild is armed', () => {
  assert.match(rebuildWhere(), /^Rebuild the image from (⌘K|Alt\+K) \(the next start\)\.$/);
});

test('F19 presetDiff: the matching preset, else the nearest one and what differs (labels, list order)', () => {
  const f = { modules: {}, onboarded: true, preset: 'basic', presets: [['basic', 'Basic', ''], ['dev', 'Developer', ''], ['all', 'Everything', '']],
    list: [['db', 'Databases', ''], ['grid', 'Grid', ''], ['http', 'HTTP', ''], ['host', 'Host shells', '']],
    preset_modules: { basic: ['db'], dev: ['db', 'grid'], all: ['db', 'grid', 'http'] } };
  assert.deepEqual(presetDiff(f, { db: true, grid: true, host: true }), { preset: 'dev', base: 'dev', label: 'Developer', added: [], removed: [] });
  assert.deepEqual(presetDiff(f, { db: true, http: true }), { preset: 'custom', base: 'basic', label: 'Basic', added: ['HTTP'], removed: [] });
  assert.deepEqual(presetDiff(f, { grid: true, http: true }), { preset: 'custom', base: 'all', label: 'Everything', added: [], removed: ['Databases'] });
  assert.equal(presetDiff({ ...f, presets: [] }, {}).preset, 'custom');
});

test('F19 Settings › Features: segment + module rows, the sticky Save bar only while unsaved, ⌘S, the Projects count', () => {
  const setup = readFileSync(new URL('../../piper/web/src/setup.ts', import.meta.url), 'utf8');
  const st = readFileSync(new URL('../../piper/web/src/settings.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  // a row: the small sketch, text, a switch that does not open the row; a click / Enter opens the large sketch
  assert.match(setup, /export function moduleRow\(id: string, label: string, text: string, on: boolean, flip: \(\) => void,/);
  assert.match(setup, /onclick: \(e: Event\) => \{ e\.stopPropagation\(\); flip\(\); \}/);
  assert.match(setup, /e\.key === 'Enter' \|\| e\.key === ' '\)\) \{ e\.preventDefault\(\); o\.toggle\?\.\(\); \}/);
  assert.match(setup, /big = h\('div', \{ class: 'fm-big' \}, img\)/);
  // Settings: segment of presets + Custom, the line from the draft, rows
  assert.match(st, /h\('div', \{ class: 'fm-seg', role: 'radiogroup'/);
  assert.match(st, /`Custom = \$\{diff\.label\}`/);
  assert.match(st, /return h\('div', \{ class: 'st-feat' \}, seg, line, h\('div', \{ class: 'fm-list' \}, \.\.\.rows\)\);/);
  // Save / Revert: one sticky bar, hidden until dirty, with the count; ⌘S clicks the open section's Save
  assert.match(st, /h\('div', \{ class: 'st-savebar', hidden: true \}/);
  assert.match(st, /bar\.hidden = !dirty;/);
  assert.match(st, /`\$\{n\} unsaved change\$\{n === 1 \? '' : 's'\}`/);
  assert.match(st, /section\[data-sid="\$\{CSS\.escape\(selected\)\}"\] button\.st-save/);
  assert.match(st, /: s\.id === 'security' \? secNavCount\(s\) : navCount\(s\),/);
  assert.match(css, /\/\* ==== 9\.(?:XX|\d+) F19 [^\n]*==== \*\/[\s\S]*\.settings \.st-savebar \{[^}]*position: sticky[\s\S]*\/\* ==== end F19 ==== \*\//);
  assert.match(css, /\.fm-thumb \{[^}]*width: 64px/);
});

test('F25 checkSummary: blockers on top, the optional ones one folded line, passed only counted', () => {
  const blocked = report([row('docker', false, 'fail', 'Docker is not running'), row('login', false, 'warn', 'No Claude login'),
    row('service', false, 'warn', 'Not started at login'), row('hole', true), row('git', true), row('stack', false, 'info')]);
  const s = checkSummary(blocked);
  assert.equal(s.tone, 'fail');
  assert.equal(s.title, '1 thing blocks the first session');
  assert.deepEqual(s.blockers.map((r) => r.key), ['docker']);
  assert.equal(s.fold, '2 optional — No Claude login, Not started at login');
  assert.equal(s.passed, 2);   // an info row (an optional module) is neither a problem nor passed
  assert.equal(checkSummary(report([row('a', false), row('b', false)])).title, '2 things block the first session');
  const ready = checkSummary(report([row('hole', true), row('git', true)]));
  assert.deepEqual([ready.tone, ready.title, ready.fold], ['ok', 'This machine is ready · 2/2', 'Passed checks']);
  assert.equal(checkSummary(report([row('hole', true), row('service', false, 'warn')])).title, 'This machine is ready · 1/2');
});

test('F25 rowAction: one action per blocker - run, a short / long command to copy, Settings, or none', () => {
  const r = (fix, action = '', settings = '') => ({ ...row('x', false), fix, action, settings });
  assert.deepEqual(rowAction(r('open -a Docker')), { kind: 'copy', label: 'Copy: open -a Docker', value: 'open -a Docker' });
  assert.deepEqual(rowAction(r('check that `hole version` works in a shell')), { kind: 'copy', label: 'Copy: hole version', value: 'hole version' });
  assert.deepEqual(rowAction(r('task piper:install-service --with-a-long-flag')).label, 'Copy command');
  assert.deepEqual(rowAction(r('install them', 'db-install', 'databases')), { kind: 'run', label: 'Install', value: 'db-install' });
  assert.equal(rowAction(r('', 'deep')).label, 'Run the mount test');
  assert.deepEqual(rowAction(r('Settings → Mattermost → Webhook URL', '', 'mattermost')), { kind: 'settings', label: 'Settings', value: 'mattermost' });
  assert.equal(rowAction(r('install Docker Desktop (or another engine)')).kind, '');   // words: a line of text, no button
});

test('F25 recheck: only while something blocks and the card is on screen', () => {
  assert.equal(recheckDelay(report([row('docker', false)]), true), 12000);
  assert.equal(recheckDelay(report([row('docker', false)]), false), 0);
  assert.equal(recheckDelay(report([row('service', false, 'warn')]), true), 0);
  assert.equal(recheckDelay(null, true), 0);
});

test('F25 the welcome: hero tiles, a stepper, the fresh re-check, the footer keeps "Later – just close" and "Continue anyway"', () => {
  const src = readFileSync(new URL('../../piper/web/src/setup.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(src, /h\('h1', \{\}, 'Welcome to Piper'\),\s*h\('p', \{ class: 'mut' \}, 'Claude and Codex sessions on your chosen machine\. Three steps to the first one\.'\)/);
  assert.match(src, /const STEPPER = \['Check this machine', 'Pick modules', 'Start a session'\];/);
  assert.match(src, /el\.append\(h\('div', \{ class: 'su-wiz' \}, heroEl\(\), stepperEl\(\),/);
  // the poll: a fresh check (piperd's caches skipped), one timer, gone with the card
  assert.match(src, /pollT = window\.setTimeout\(\(\) => \{ pollT = 0; if \(recheckDelay\(rep, shown\(\)\)\) void check\(false, true\); \}, ms\);/);
  assert.match(src, /dismiss\(\): void \{\s*dismissed = true;\s*unschedule\(\);/);
  assert.match(src, /'Later – just close'/);
  assert.match(src, /next\(fails \? 'Continue anyway' : 'Next: pick modules'\)/);
  assert.match(src, /`Fix the blocker\$\{fails === 1 \? '' : 's'\} first`\)/);
  // the mount test is in Details; the image row stays and says where the rebuild is armed
  assert.match(src, /if \(!rep\.deep\) box\.append\(h\('div', \{ class: 'su-deep small' \}/);
  assert.match(src, /kbd\('Rebuild: ⌘K'\)/);
  assert.match(src, /step === 1 \? \[checkEl\(\), rep && hole\(\) \? imageEl\(\) : null\]/);
  // step 2: the segment, the gallery behind a link
  assert.match(src, /galleryOpen \? 'Hide the modules ‹' : 'What each module adds ›'/);
  assert.match(css, /\/\* ==== 9\.(?:XX|\d+) F25 [^\n]*==== \*\/[\s\S]*\.setup \.su-stepper \{[\s\S]*\/\* ==== end F25 ==== \*\//);
});

test('direct and remote onboarding does not promise Hole isolation or a local Mac', () => {
  const src = readFileSync(new URL('../../piper/web/src/setup.ts', import.meta.url), 'utf8');
  assert.match(src, /native permissions/);
  assert.match(src, /Claude and Codex sessions on your chosen machine/);
  assert.doesNotMatch(src, /Nothing leaks|Excluded files never reach Claude|each in its own sandbox|This Mac|Check this Mac/);
  assert.match(src, /native login and project trust/);
});


test('direct setup neither requests nor displays a Hole image; legacy servers retain it', () => {
  assert.equal(setupUsesHole('direct'), false);
  assert.equal(setupUsesHole('hole'), true);
  assert.equal(setupUsesHole(undefined), true);
  const src = readFileSync(new URL('../../piper/web/src/setup.ts', import.meta.url), 'utf8');
  assert.match(src, /img = hole\(\) \? await call<ImageInfo>/);
  assert.match(src, /rep && hole\(\) \? imageEl\(\) : null/);
  assert.match(src, /if \(rep && hole\(\)\) el.append\(imageEl\(\)\);/);
});
