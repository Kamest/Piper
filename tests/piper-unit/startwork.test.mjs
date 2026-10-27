// node --test: W15 - one Start work dialog: the kinds (New session · From a GitLab issue ·
// Resume conversation, ⌥1-3), visible Options, compact project selection and mounted Resume.
// Pure helpers from web/dist; browser regressions exercise task drafts and launch payloads.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { kindKey, worktreeSuggestion, missingIssueTracker, optsSummary, parseForgeUrl, projectForRef, projectRow, projectsRoot, rebuildSummary, refChip, launchPhaseText, availableAgent, startOptionsDrafts, workKinds } from '../../piper/web/dist/startwork.js';

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
  assert.match(m, /modal\(`New session: \$\{prefill\.title\}`, root\)/);
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
  assert.match(m, /mount: \(r\) => pane\.append\(r\),\s*host: o\.initial \? refHost\(o\.initial\) : form\.host\(\)/);
  assert.match(m, /projectName\.textContent = markBidi\(chosen \? rawProjectKey\(chosen\)/, 'a chosen worktree is named even outside the configured list');
  const c = read('convo.ts');
  assert.match(c, /mount\?\(root: HTMLElement\): void;/);
  assert.match(c, /deps\.mount \? \[\] : \[h\('div', \{ class: 'resume-head' \}/);
  assert.match(c, /if \(!deps\.mount\) deps\.closeModal\(\); deps\.newSession\(project \|\| rows\.find\(\(r\) => r\.session === selected\)\?\.project \|\| '', selectedHost\)/,
    'mounted: New conversation… only switches the kind, with the selected row\'s project');
  assert.match(c, /if \(deps\.mount\) deps\.mount\(root\);/);
  assert.ok(!/h\('kbd', \{\}, 'Enter'\)/.test(c), '⏎ like the rest of the GUI');
  assert.match(c, /↑↓ select · ⏎ resume · Esc close/);
});

test('Options: expanded by default in the form without warning prose', () => {
  const m = read('main.ts');
  assert.match(m, /h\('details', \{ class: 'startopts', open: true \}/);
  const options = m.slice(m.indexOf('function startOptsBox('), m.indexOf('const TIPS:'));
  assert.doesNotMatch(options, /class: 'warn|codexNote|t\.warn/);
  assert.match(options, /class: 'so-choices'/);
  assert.match(css, /\.wk-form > \.startopts/);

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

test('Compact start dialog uses Piper theme tokens, natural height and a footer outside the scroll body', () => {
  const block = css.slice(css.indexOf('/* ==== F02 Start work:'), css.indexOf('/* ==== end F02 ==== */'));
  assert.match(block, /width: min\(680px,[^}]+height: auto;/);
  assert.match(block, /\.wk-form \{[^}]*overflow-y: auto/);
  assert.match(block, /\.wk-foot \{ flex: none;/);
  assert.doesNotMatch(block, /margin-top: auto/, 'no blank spacer');
  assert.doesNotMatch(block, /#[a-f0-9]{6}/i, 'reuse Piper theme tokens');
  assert.doesNotMatch(block, /\.workdlg \{[^}]*font-family:/, 'inherit the application font');
  assert.match(block, /background: var\(--well\)/);
  assert.match(block, /max-width: 560px/);
  assert.match(block, /\.wk-project-pop > \.plist \{ max-height: 198px/);
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
  assert.doesNotMatch(options, /codexNote/);
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
  assert.deepEqual(projectRow('java/shop2', paths[2], root), { name: 'java/shop2', dir: 'java' });
  assert.equal(projectsRoot(['/a/b/c']), '/a/b', 'one project: its parent');
  assert.equal(projectsRoot([]), '');
});

test('Compact launcher preserves Resume, labelled agent, URL inference and worktree choice', () => {
  const m = read('main.ts');
  const pane = m.slice(m.indexOf('function sessionPane('), m.indexOf('function whereBox('));
  assert.match(pane, /class: 'wk-project', type: 'button', role: 'combobox'/);
  assert.match(pane, /class: 'wk-agent-field'[^\n]*'Agent'/);
  assert.match(pane, /if \(found && found !== chosen\) chooseProject\(found, true\)/);
  assert.match(pane, /!url\.startsWith\(d\.web_url\)/, 'another tracker cannot name the chip');
  assert.match(pane, /nodes: \[content, footer\]/);
  assert.match(pane, /draftsByProject\.set\(chosen/);
  assert.match(pane, /Additional task:/, 'issue attachment does not replace the written task');
  assert.match(pane, /Description and comments/);
  assert.doesNotMatch(pane, /wk-shared|syncShared/);
  const where = m.slice(m.indexOf('function whereBox('), m.indexOf('const layers ='));
  assert.match(where, /h\('span', \{ 'aria-hidden': 'true' \}, '⑂ '\), h\('b', \{\}, markBidi\(info\?\.current \|\| 'HEAD'\)\)/);
  // 9.145: a new worktree names its From branch (any local / remote one) and gets a name from the task
  assert.match(where, /const from = branchPicker\(\(\) => sync\(\)\)/, '9.146: no native select that scrolls the dialog');
  assert.doesNotMatch(where, /h\('select', \{ 'aria-label': 'New branch from'/);
  assert.match(where, /start: from\.value\(\)/);
  assert.match(where, /phase\(\{ kind: 'worktree', name: p\.worktree\.name, from: p\.worktree\.start \|\| 'HEAD' \}\)/);
  assert.match(where, /if \(info && !edited\) name\.value = worktreeSuggestion\(source\(\), taken\)/);
  assert.match(where, /Creates \$\{shortDir/);
  assert.doesNotMatch(where, /Name \+ branch as in/);
  assert.match(where, /main: \(\) => !info \|\| rMain\.checked/);
  assert.match(m, /startDialogFocus\(root\)/);
});


test('project rows distinguish equally named repositories without changing their keys', () => {
  const keys = ['globex/eshop', 'initech/eshop'];
  assert.deepEqual(keys.map(key => projectRow(key, '/work/' + key, '/work').name), keys);
});


test('only a missing tracker becomes a neutral empty state; auth and real failures stay visible', () => {
  for (const message of ['no GitLab project is mapped to this project (Settings -> Projects)',
    'no GitLab project and no GitHub repository is mapped to demo/plain', 'no GitHub repository is mapped to this project']) {
    assert.equal(missingIssueTracker(400, message), true);
    assert.equal(missingIssueTracker(503, message), false);
  }
  assert.equal(missingIssueTracker(400, 'project is required'), false);
  assert.equal(missingIssueTracker(401, 'login required'), false);
});

test('F02c (9.143, board Kd1T9): New session speaks the app\'s language - tabs, capitals, one segment style, stable Where, Remember in the footer', () => {
  const m = read('main.ts');
  const pane = m.slice(m.indexOf('function sessionPane('), m.indexOf('function whereBox('));
  const box = m.slice(m.indexOf('function startOptsBox('), m.indexOf('const TIPS:'));
  const where = m.slice(m.indexOf('function whereBox('), m.indexOf('const layers ='));
  // Remember sits in the footer instead of the "Select a project" hint; Start says why it is disabled
  assert.match(pane, /h\('div', \{ class: 'row wk-foot' \}, opts\.rememberEl,/);
  assert.doesNotMatch(pane, /wk-start-hint|'Select a project'/);
  assert.match(pane, /startButton\.title = chosen \? kbd\('⌘⏎ start', 'Ctrl'\) : 'Choose a project first'/);
  assert.doesNotMatch(box, /body\.append\(h\('label', \{ class: 'so-remember'/, 'Remember is not inside Options');
  // Options stay expanded: the summary is a label that never toggles
  assert.match(box, /h\('summary', \{ tabindex: '-1', onclick: \(e: Event\) => e\.preventDefault\(\) \}/);
  // the issue action carries its key; issue rows show the number apart from the title
  assert.match(pane, /h\('kbd', \{\}, fromIssue \? 'Esc' : kbd\('⌥2'\)\)/);
  assert.match(pane, /h\('span', \{ class: 'wk-iid' \}, `#\$\{it\.iid\}`\), h\('span', \{ class: 'name' \}, it\.title\)/);
  assert.match(pane, /class: 'wk-pop-keys'/);
  // Where is in the form from the start (disabled) - the dialog does not grow when a project is chosen
  assert.match(where, /h\('div', \{ class: 'where off' \}/);
  assert.match(where, /idle\('Choose a project first'\);/);
  assert.match(where, /if \(rawProjectKey\(project\)\.includes\('@'\)\) \{ el\.classList\.add\('hidden'\); return; \}/);
  const f02 = css.slice(css.indexOf('/* F02c (9.143'), css.indexOf('/* ==== end F02 ==== */'));
  assert.match(f02, /\.wk-kind\.on \{ color: var\(--fg\); font-weight: 600; border-bottom-color: var\(--fg\); background: transparent; \}/, 'tabs like the session tabs');
  assert.match(f02, /\.wk-kind \.wk-key \{[^}]*font-weight: 400; \}/, 'the keys are shown on desktop');
  assert.match(f02, /\.wk-label \{[^}]*font-size: var\(--t-xs, 11px\); font-weight: 600; text-transform: uppercase;[^}]*color: var\(--faint-t\); \}/);
  assert.match(f02, /\.wk-agent, \.wk-runtime, \.where \.wseg \{ display: flex; height: 32px; padding: 2px; gap: 2px; border: 0;/, 'runtime reuses the native Agent and Where style');
  assert.doesNotMatch(f02, /\.wk-project > \.name \{[^}]*--mono/, 'project keys in the UI font');
  assert.doesNotMatch(f02, /\.so-choice \{[^}]*border: 1px/, 'no bordered option cards');
  assert.match(f02, /\.wk-foot \{[^}]*background: var\(--card\);/);
  assert.match(f02, /\.so-remember \.so-rem-long[^}]*display: none;/, 'phones: Remember stays one line beside Cancel / Start');
});

test('9.145 a new worktree name follows the task: issue number + words, folded, bounded, unique', () => {
  const free = () => false;
  const now = new Date(2026, 9, 23, 14, 5);
  assert.equal(worktreeSuggestion('#2246 Révision du découpage api en frontend / backend et façade', free, now),
    '2246-revision-du-decoupage-api');
  assert.equal(worktreeSuggestion('!12 Refactor the invoice export', free, now), '12-refactor-the-invoice-export');
  assert.equal(worktreeSuggestion('Fix the PDF invoice in checkout\nmore lines here', free, now), 'fix-the-pdf-invoice-in');
  assert.equal(worktreeSuggestion('   ', free, now), 'session-1023-1405', 'nothing to go by: a dated name');
  assert.equal(worktreeSuggestion('Crème brûlée façade', free, now), 'creme-brulee-facade');
  const taken = new Set(['fix-pdf', 'fix-pdf-2']);
  assert.equal(worktreeSuggestion('Fix PDF', (n) => taken.has(n), now), 'fix-pdf-3', 'an existing worktree / branch is skipped');
  for (const s of ['#1 ' + 'x'.repeat(90), 'a'.repeat(200)]) assert.ok(worktreeSuggestion(s, free, now).length <= 40);
});

test('9.146: the From picker is searchable and floats over the dialog; Start session says what it does', () => {
  const m = read('main.ts');
  const picker = m.slice(m.indexOf('function branchPicker('), m.indexOf('function whereBox('));
  assert.match(picker, /role: 'combobox', 'aria-label': 'New branch from'/);
  assert.match(picker, /placeholder: 'Search branches…'/);
  assert.match(picker, /b\.name\.toLowerCase\(\)\.includes\(q\)/);
  assert.match(picker, /const up = below < 260 && above > below/, 'opens upwards when there is no room below');
  assert.match(picker, /e\.key === 'Escape'\) \{\n?\s*e\.preventDefault\(\); e\.stopPropagation\(\); close\(\)/, 'Esc closes the list, not the dialog');
  assert.match(picker, /window\.addEventListener\('scroll', moved, true\)/);
  assert.match(css, /\.wk-from-pop \{ position: fixed;/);
  const pane = m.slice(m.indexOf('function sessionPane('), m.indexOf('function branchPicker('));
  assert.match(pane, /role: 'status', 'aria-live': 'polite'/);
  assert.match(pane, /where\.key\(snapshot\.project, step\)/);
  assert.match(pane, /launching \? 'Starting…' : 'Start session'/);
  assert.match(pane, /opts\.rememberEl\.classList\.toggle\('hidden', launching\)/);
  assert.equal(launchPhaseText({ kind: 'worktree', name: 'fix-x', from: 'origin/dev' }, 0.4), 'Creating worktree fix-x from origin/dev…');
  assert.equal(launchPhaseText({ kind: 'worktree', name: 'fix-x', from: 'HEAD' }, 5.9), 'Creating worktree fix-x from HEAD… 5 s');
  assert.equal(launchPhaseText({ kind: 'start' }, 1.2), 'Starting the session…');
  assert.equal(launchPhaseText({ kind: 'start' }, 12), 'Starting the session… 12 s');
});

test('Remote agents: only untouched drafts choose the sole installed CLI; unknown keeps selection', () => {
  assert.equal(availableAgent('claude', {claude:false,codex:true}, false), 'codex');
  assert.equal(availableAgent('codex', {claude:true,codex:false}, false), 'claude');
  for (const available of [undefined,null,{}, {claude:false,codex:false}, {claude:true,codex:true}])
    assert.equal(availableAgent('codex', available, false), 'codex');
  assert.equal(availableAgent('claude', undefined, false, 'codex'), 'codex', 'late server defaults replace the untouched placeholder');
  assert.equal(availableAgent('claude', {claude:true,codex:false}, false, 'codex'), 'codex', 'saved agent is preserved even if unavailable');
  assert.equal(availableAgent('claude', {claude:false,codex:true}, true, 'codex'), 'claude', 'explicit selection beats newly received defaults');
  assert.equal(availableAgent('claude', {claude:false,codex:true}, true), 'claude', 'saved/explicit/model already chosen never changes');
});
