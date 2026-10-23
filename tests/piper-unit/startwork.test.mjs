// node --test: W15 - one Start work dialog: the kinds (New session · From a GitLab issue ·
// Resume conversation, ⌥1-3), the folded Options summary, every entry on startWork, Resume mounted in the
// dialog, one fixed size. Pure helpers from web/dist; wiring over web/src.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { kindKey, optsSummary, parseForgeUrl, projectForRef, projectRow, projectsRoot, rebuildSummary, refChip, startOptionsDrafts, workKinds } from '../../piper/web/dist/startwork.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const key = (o) => ({ altKey: false, metaKey: false, ctrlKey: false, shiftKey: false, ...o });

test('W15 workKinds: the issue kind only with the gitlab module, Resume always last', () => {
  assert.deepEqual(workKinds(true).map((k) => k.id), ['new', 'issue', 'resume']);
  assert.deepEqual(workKinds(false).map((k) => k.id), ['new', 'resume']);
  assert.equal(workKinds(true)[1].label, 'From a GitLab issue');
  assert.equal(workKinds(true, true)[1].label, 'From an issue');        // W23: GitLab and GitHub
  assert.equal(workKinds(false, true)[1].label, 'From a GitHub issue');
  assert.deepEqual(workKinds(false, false).map((k) => k.id), ['new', 'resume']);
  for (const k of workKinds(true)) assert.ok(k.label && k.sub && k.icon, `${k.id} has a label, a sub line and an icon`);
});

test('W15 kindKey: ⌥1-3 by e.code (⌥digit types a character on a Mac), nothing else', () => {
  assert.equal(kindKey(key({ altKey: true, code: 'Digit1' })), 0);
  assert.equal(kindKey(key({ altKey: true, code: 'Digit3' })), 2);
  assert.equal(kindKey(key({ altKey: true, code: 'Digit4' })), null, 'only three kinds');
  assert.equal(kindKey(key({ code: 'Digit1' })), null, 'a plain digit types into the task');
  assert.equal(kindKey(key({ altKey: true, metaKey: true, code: 'Digit1' })), null);
  assert.equal(kindKey(key({ altKey: true, ctrlKey: true, code: 'Digit2' })), null);
  assert.equal(kindKey(key({ altKey: true, shiftKey: true, code: 'Digit2' })), null);
  assert.equal(kindKey(key({ altKey: true, code: 'Numpad1' })), null);
  assert.equal(kindKey(key({ altKey: true })), null, 'no code');
});

test('F02 optsSummary: the options that are on, else "sandbox default"; any option on is a warning (the agent has its segment)', () => {
  const L = { docker: 'Docker', unfiltered: 'Unfiltered network' };
  assert.deepEqual(optsSummary([], L), { text: 'sandbox default', risky: false });
  assert.deepEqual(optsSummary(['unfiltered'], L), { text: 'Unfiltered network', risky: true });
  assert.deepEqual(optsSummary(['docker', 'x'], L), { text: 'Docker · x', risky: true }, 'an unknown key shows as is');
});

test('W15 wiring: every entry opens startWork on its kind; the inbox prefill keeps its single pane', () => {
  const m = read('main.ts');
  assert.match(m, /function showResume\(initial = ''\): void \{\s*startWork\('resume', \{ initial \}\);/);
  assert.match(m, /if \(!prefill\) return startWork\(fromIssue \? 'issue' : 'new', \{ pick \}\);/);
  assert.match(m, /modal\(`New session: \$\{prefill\.title\}`, \.\.\.pane\.nodes\)/);
  assert.match(m, /modal\('Start work', root\)/);
  assert.match(m, /const i = kindKey\(e\);[\s\S]{0,240}e\.preventDefault\(\);\s*e\.stopPropagation\(\);/, '⌥N is not typed into the task');
  assert.match(m, /\(\['new', 'issue', 'resume'\] as WorkKind\[\]\)\[i\]/, '⌥1 new · ⌥2 the issue picker · ⌥3 resume, also without an issue module');
  assert.ok(!/'From a GitLab issue…'/.test(m), 'the button inside the form is gone - the kind replaces it');
  assert.match(m, /class: 'hub-start'[\s\S]{0,200}onclick: \(\) => showResume\(\) \}, 'Resume a conversation…'/, 'the hub knows Resume');
});

test('W15 wiring: New ↔ Issue is one form, Resume is built once and mounted without its own head', () => {
  const m = read('main.ts');
  assert.match(m, /body\.replaceChildren\(formEl\);\s*if \(project\) form\.choose\(project\);\s*form\.issue\(k === 'issue'\);/);
  assert.match(m, /if \(resumeEl\) \{\s*body\.replaceChildren\(resumeEl\);/, 'the Resume pane keeps its search and selection');
  assert.match(m, /mount: \(r\) => pane\.append\(r\),\s*newSession: \(p\) => show\('new', p\)/);
  assert.match(m, /chosen\.includes\('@'\) \? 'worktree' : 'not in projects\.json'/, 'a chosen worktree from Resume shows as chosen');
  const c = read('convo.ts');
  assert.match(c, /mount\?\(root: HTMLElement\): void;/);
  assert.match(c, /deps\.mount \? \[\] : \[h\('div', \{ class: 'resume-head' \}/);
  assert.match(c, /if \(!deps\.mount\) deps\.closeModal\(\); deps\.newSession\(project \|\| rows\.find\(\(r\) => r\.session === selected\)\?\.project \|\| ''\)/,
    'mounted: New conversation… only switches the kind, with the selected row\'s project');
  assert.match(c, /if \(deps\.mount\) deps\.mount\(root\);/);
  assert.ok(!/h\('kbd', \{\}, 'Enter'\)/.test(c), '⏎ like the rest of the GUI');
  assert.match(c, /↑↓ select · ⏎ resume · Esc close/);
});

test('Options: folded summary names the project; the global defaults link is gone', () => {
  const m = read('main.ts');
  assert.match(m, /h\('details', \{ class: 'startopts' \}/);
  assert.doesNotMatch(m, /Defaults: Settings › Sessions › New sessions/);
  assert.match(m, /class: 'so-project mut small'/);
  assert.match(m, /optsSummary\(/);
  assert.match(css, /\.startopts > summary/);
  assert.match(css, /\.so-sum\.warn \{[^}]*var\(--warn\)/);
});

test('Project options: A → B → A preserves unsaved drafts and never mutates saved preferences', () => {
  const saved = { a: { agent: 'codex', opts: ['docker'] }, b: { agent: 'claude', opts: [] } };
  const load = startOptionsDrafts(saved, ['idea']);
  const a = load('a');
  assert.deepEqual(a, { agent: 'codex', opts: { docker: true }, remember: true });
  a.opts.unrestricted = true;
  a.remember = false;
  const b = load('b');
  assert.deepEqual(b, { agent: 'claude', opts: {}, remember: true }, 'explicit empty beats legacy defaults');
  b.opts.idea = true;
  assert.equal(load('a'), a, 'returning or redrawing keeps the actual draft');
  assert.deepEqual(load('a@topic'), a, 'worktrees share their base project');
  assert.deepEqual(load('new'), { agent: 'claude', opts: { idea: true }, remember: true }, 'legacy fallback for untouched project');
  assert.deepEqual(saved, { a: { agent: 'codex', opts: ['docker'] }, b: { agent: 'claude', opts: [] } }, 'Cancel has nothing to undo');
  assert.deepEqual(startOptionsDrafts(saved, [])('a'), { agent: 'codex', opts: { docker: true }, remember: true }, 'new dialog discards drafts');
});

test('Project options: a fresh project starts with Claude, all options off and Remember on', () => {
  assert.deepEqual(startOptionsDrafts({}, [])('fresh'), { agent: 'claude', opts: {}, remember: true });
});

test('F02 CSS: one fixed size, tabs on top, the N9/N10 heights on .wk-pane, Options open above the foot', () => {
  const block = css.slice(css.indexOf('/* ==== 9.82 F02'), css.indexOf('/* ==== end F02 ==== */'));
  assert.match(block, /\.modal:has\(\.workdlg\) \{ width: min\(760px, [^;]+; height: min\(680px, [^;]+;/);
  assert.match(block, /\.modal:has\(\.workdlg\) > h2 \{ display: none; \}/);
  assert.match(block, /\.workdlg \{ flex: 1; display: flex; flex-direction: column;/);
  assert.match(block, /\.wk-pane > \.plist, \.wk-pane > textarea, \.wk-pane > \.issues \{ flex-shrink: 0; \}/);
  assert.match(block, /\.wk-pane > \.wk-foot \{ margin-top: auto;/);
  assert.match(block, /\.wk-foot \.startopts\[open\] > \.so-body \{ position: absolute; left: 0; bottom: calc\(100% \+ 4px\);/);
  assert.ok(!/\.wk-rail/.test(css), 'the left column is gone');
  assert.ok(!/\.modal:has\(\.issues\.open\)/.test(css), 'the issue picker no longer widens the dialog');
});

test('9.54 the Options summary names a pending image rebuild', () => {
  assert.equal(rebuildSummary(false), '');
  assert.match(rebuildSummary(true), /image rebuild at this start/);
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /so-rebuild/);   // the summary line shows it and offers "cancel"
});

test('Start work: no image rebuild button; a pending command-palette rebuild can still be cancelled', () => {
  const main = read('main.ts');
  const options = main.slice(main.indexOf('function startOptsBox()'), main.indexOf('const TIPS:'));
  assert.doesNotMatch(options, /so-rb|Rebuild image at this start|rbThis|rbBtn/);
  assert.doesNotMatch(main, /snapshot\.rebuild|opts\.rebuild\(\)/);
  assert.doesNotMatch(css, /\.so-rb/);
  assert.match(options, /if \(on\('codex'\)\) body\.append\(codexNote\)/);
  assert.match(options, /const armed = rebuildArmed\(\)/);
  assert.match(options, /setRebuild\(false\)\.then\(syncRb\)/);
});

test('F02 parseForgeUrl: GitLab issues / MRs on any host, GitHub only with its module, anything else is task text', () => {
  assert.deepEqual(parseForgeUrl(' https://gitlab.com/acme/shop/-/issues/412 ', false),
    { forge: 'gitlab', host: 'gitlab.com', path: 'acme/shop', kind: 'issue', iid: 412 });
  assert.deepEqual(parseForgeUrl('https://gitlab.example.com/a/b/c/-/merge_requests/7#note_1', false),
    { forge: 'gitlab', host: 'gitlab.example.com', path: 'a/b/c', kind: 'mr', iid: 7 });
  assert.equal(parseForgeUrl('https://github.com/o/r/issues/3', false), null, 'GitHub off');
  assert.deepEqual(parseForgeUrl('https://github.com/o/r/pull/3', true), { forge: 'github', host: 'github.com', path: 'o/r', kind: 'mr', iid: 3 });
  assert.equal(parseForgeUrl('fix https://gitlab.com/acme/shop/-/issues/412 please', false), null, 'a URL inside a sentence stays text');
  assert.equal(parseForgeUrl('https://gitlab.com/acme/shop/-/pipelines/9', false), null);
  assert.equal(parseForgeUrl('', false), null);
});

test('F02 projectForRef: the project linked to the GitLab path, else the one with the repository\'s name; ambiguous = none', () => {
  const ps = [{ key: 'projects/shop', gitlab: ['acme/shop'] }, { key: 'projects/shop2024' }, { key: 'x/webstore' }, { key: 'y/webstore' }, { key: 'gh/tool', gitlab: ['gh:o/tool'] }];
  const ref = (path, forge = 'gitlab') => ({ forge, host: 'h', path, kind: 'issue', iid: 1 });
  assert.equal(projectForRef(ref('acme/shop'), ps), 'projects/shop');
  assert.equal(projectForRef(ref('ACME/Shop'), ps), 'projects/shop', 'GitLab paths ignore case');
  assert.equal(projectForRef(ref('other/shop2024'), ps), 'projects/shop2024', 'by name');
  assert.equal(projectForRef(ref('z/webstore'), ps), '', 'two projects are called webstore');
  assert.equal(projectForRef(ref('o/tool', 'github'), ps), 'gh/tool');
  assert.equal(refChip(ref('acme/shop')), '#1');
  assert.equal(refChip({ ...ref('a'), kind: 'mr', iid: 7 }, 'Split'), '!7 Split');
});

test('F02 project rows: the name, the folder under the projects\' root grey, never the absolute path', () => {
  const paths = ['/Users/dev/www/projects/acme/shop', '/Users/dev/www/projects/acme/shop2024', '/Users/dev/www/java/shop2'];
  const root = projectsRoot(paths);
  assert.equal(root, '/Users/dev/www');
  assert.deepEqual(projectRow('shop', paths[0], root), { name: 'shop', dir: 'projects/acme' });
  assert.deepEqual(projectRow('java/shop2', paths[2], root), { name: 'shop2', dir: 'java' });
  assert.equal(projectsRoot(['/a/b/c']), '/a/b', 'one project: its parent');
  assert.equal(projectsRoot([]), '');
});

test('F02 wiring: tabs on top, agent segment by the search, chip under the task, Where segment, warning only on the main checkout', () => {
  const m = read('main.ts');
  const sw = m.slice(m.indexOf('function startWork('), m.indexOf('function resumableFor('));
  assert.match(sw, /h\('nav', \{ class: 'wk-tabs', role: 'tablist'/);
  assert.match(sw, /const tabs = kinds\.filter\(\(k\) => k\.id !== 'issue'\);/, 'the issue kind is no tab');
  assert.match(sw, /'issue \/ MR URL → paste it as the task'/);
  const pane = m.slice(m.indexOf('function sessionPane('), m.indexOf('function whereBox('));
  assert.match(pane, /h\('div', \{ class: 'wk-search' \}, h\('span', \{ class: 'wk-sico', 'aria-hidden': 'true' \}, h\('i', \{ class: 'fi fi-search' \}\)\), filter, opts\.seg\)/);
  assert.match(pane, /const row = projectRow\(p\.key, p\.path, root\);/);
  assert.match(pane, /title: p\.path, 'data-project': p\.key/, 'the full path in the tooltip');
  assert.match(pane, /const found = projectForRef\(ref, projects\);\s*if \(found && found !== chosen\) \{ chosen = found;/);
  assert.match(pane, /if \(d\.web_url && !url\.startsWith\(d\.web_url\)\) return;/, 'another repository\'s #N never names the chip');
  assert.match(pane, /const show = !!sib\.length && where\.main\(\);/);
  assert.match(pane, /h\('div', \{ class: 'row wk-foot' \}, opts\.el, h\('span', \{ class: 'grow' \}\), button\('Cancel', closeModal\),/);
  assert.match(pane, /h\('button', \{ class: 'primary wk-start'/);
  const where = m.slice(m.indexOf('function whereBox('), m.indexOf('\n}\n', m.indexOf('function whereBox(')));
  assert.match(where, /h\('div', \{ class: 'wseg seg' \}, cMain, cOld, cNew\), base\)/);
  assert.match(where, /main: \(\) => !info \|\| rMain\.checked,/);
  const opts = m.slice(m.indexOf('function startOptsBox('), m.indexOf('const TIPS:'));
  assert.match(opts, /const seg = on\('codex'\) \? h\('div', \{ class: 'wk-agent seg', role: 'radiogroup', 'aria-label': 'Agent' \}\) : null;/);
  assert.doesNotMatch(opts, /name: 'agent'/, 'no agent radios inside Options');
});
