// node --test: W16 - the context panel's Isolation / Network / Session sections as property
// rows (label · value · action), headers that say the state, Network open on a refusal, Last turn folded
// at the bottom, nested <details> folds kept by key. Pure helpers from web/dist; wiring over web/src.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { isoStatus, keepOpen, netStatus, unlockRows } from '../../piper/web/dist/inspector.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');

test('commit footer wraps actions and preserves readable hint space in a narrow inspector', () => {
  assert.match(css, /\.gfoot \{[^}]*flex-wrap: wrap;/);
  const hint = css.match(/\.gfoot > \.grow \{([^}]+)\}/)?.[1] ?? '';
  assert.match(hint, /flex-basis: 8em;/);
  assert.match(hint, /white-space: normal;/);
  assert.match(hint, /overflow-wrap: anywhere;/);
});

test('W16 isoStatus: the check, then what is unlocked in amber', () => {
  assert.deepEqual(isoStatus(null, undefined), [], 'before the check ran and nothing unlocked: no status');
  assert.deepEqual(isoStatus({ ok: true, checks: 3 }, {}).map((b) => [b.text, b.tone]), [['✓ 3 passed', 'ok']]);
  assert.deepEqual(isoStatus({ ok: false, checks: 3 }, {}).map((b) => [b.text, b.tone]), [['✗ failed', 'bad']]);
  assert.deepEqual(isoStatus({ ok: true, checks: 3 }, { projects: true }).map((b) => [b.text, b.tone]),
    [['✓ 3 passed', 'ok'], ['· projects unlocked', 'warn']]);
  assert.deepEqual(isoStatus(null, { git: true, projects_write: true, db_write_other: true }).map((b) => b.text),
    ['git + projects + databases unlocked'], 'no leading dot without the check');
});

test('W16 netStatus: filtered with the count or amber unfiltered, amber refused opens the section', () => {
  assert.deepEqual(netStatus({ allow: ['a', 'b'], denied: [] }), { bits: [{ text: 'filtered · 2 allowed', tone: 'mut' }], open: false });
  const r = netStatus({ allow: ['a'], denied: ['x:443', 'y:443'] });
  assert.deepEqual(r.bits.map((b) => [b.text, b.tone]), [['filtered · 1 allowed', 'mut'], ['· 2 refused', 'warn']]);
  assert.equal(r.open, true);
  assert.deepEqual(netStatus({ unrestricted: true, denied: null }).bits.map((b) => [b.text, b.tone]), [['unfiltered', 'warn']]);
  assert.equal(netStatus({ denied: null }).open, false, 'refused not known yet: folded');
});

test('W16 unlockRows: four rows in order, locked/unlocked with the action, git as in hole links Settings', () => {
  const rows = unlockRows({ projects: true }, false);
  assert.deepEqual(rows.map((r) => r.key), ['projects', 'projects_write', 'git', 'db_write_other']);
  assert.deepEqual(rows.map((r) => r.label), ['Reading other projects', 'Editing other projects', 'Git', 'Other projects\' DB writes']);
  assert.deepEqual(rows[0], { key: 'projects', label: 'Reading other projects', value: 'unlocked', tone: 'warn', action: 'lock' });
  assert.deepEqual(rows[1], { key: 'projects_write', label: 'Editing other projects', value: 'locked', tone: 'mut', action: 'unlock' });
  assert.deepEqual(unlockRows(undefined, true).map((r) => r.key), ['projects', 'projects_write', 'db_write_other']);
  assert.deepEqual(unlockRows({ projects: true, git: true }, false, false), [], 'disabled restrictions expose no ineffective controls');
  assert.equal(unlockRows({ git: true }, false)[2].action, 'lock');
});

test('W16 keepOpen: the fold outlives a rebuild by key, whatever the summary says', () => {
  const fake = () => {
    const ls = [];
    return { open: false, addEventListener: (ev, f) => { if (ev === 'toggle') ls.push(f); }, toggle(v) { this.open = v; ls.forEach((f) => f()); } };
  };
  const a = keepOpen(fake(), 'refused:s1');
  assert.equal(a.open, false, 'nothing remembered: as built');
  a.toggle(true);
  assert.equal(keepOpen(fake(), 'refused:s1').open, true, 'the rebuilt one opens');
  assert.equal(keepOpen(fake(), 'refused:s2').open, false, 'another key is its own');
  const b = keepOpen(fake(), 'refused:s1');
  b.toggle(false);
  const c = fake(); c.open = true;
  assert.equal(keepOpen(c, 'refused:s1').open, false, 'folded by the user stays folded even if built open');
});

test('W16 wiring: three folds iso · net · info of .prop rows; no text-based <details> restore', () => {
  const m = read('main.ts');
  assert.match(m, /fold\('iso', 'Isolation'\)/);
  assert.match(m, /fold\('net', 'Network'\)/);
  assert.match(m, /fold\('info', 'Session'\)/);
  assert.ok(!/Isolation & network/.test(m), 'the heap is gone');
  assert.ok(!/openDetails|closedDetails/.test(m), 'folds by key, not by the summary text');
  assert.ok(!/h\('h4', \{\}, '(Network|GitLab|Session|Unlock)'\)/.test(m), 'no sub-headings in prose');
  assert.match(m, /iso\.auto\(\(!!att && !att\.ok\) \|\| s\.kind === 'advisor'\)/);
  assert.match(m, /nf\.auto\(ns\.open\)/, 'Network opens on a refusal');
  assert.match(m, /info\.auto\(false\)/);
  assert.match(m, /unfold\('iso'\)/, 'the header chip still opens Isolation');
  assert.match(m, /`✗ \$\{att\.failures\.length\} of \$\{att\.checks\} failed`/);
  assert.match(m, /onclick: \(\) => confirmAllow\(s, dom\) \}, 'Allow…'\)/);
  assert.match(m, /stopped \? h\('button', \{ class: 'mini', onclick: \(\) => \(on \? void setStartOpt\(s, k, false\) : confirmStartOpt\(s, k\)\) \}, on \? 'Turn off' : 'Turn on…'\) : null/,
    'start options change only while stopped');
  assert.match(m, /r\.action === 'lock' \? 'Lock' : 'Unlock…'/);
  assert.match(m, /unlockRows\(s\.unlock, !!state\.gui_flags\?\.git_hole, state\.gui_flags\?\.unlocks_required !== false\)/);
  assert.match(m, /keepOpen\(h\('details', \{ class: 'prop-list' \}[\s\S]{0,300}`allow:\$\{s\.id\}`\)/);
  assert.match(m, /keepOpen\(h\('details', \{ class: 'watch-more' \}[\s\S]{0,250}`watch:\$\{s\.id\}`\)/);
  assert.match(read('stack.ts'), /keepOpen\(h\('details', \{ class: 'oneshots' \}[\s\S]{0,300}`oneshots:\$\{project\}`\)/);
});

test('Last turn stays folded by default even with content and follows Session at the bottom', () => {
  const g = read('gitpanel.ts');
  assert.match(g, /: s\.turns === null \? '' : 'no turn yet'\);/);
  assert.match(g, /has: \(id: string\) => !!of\(id\)\.turns\?\.length/);
  const m = read('main.ts');
  const turn = m.slice(m.indexOf("const sec = fold('turn', 'Last turn')"), m.indexOf('const git = createGitPanel'));
  assert.match(turn, /sec\.auto\(false\)/);
  assert.doesNotMatch(turn, /sec\.auto\(!!/);
  const inspector = m.slice(m.indexOf('function renderInspector()'), m.indexOf('function issueForge('));
  assert.match(inspector, /insp\.append\(info\.el\);[\s\S]*insp\.append\(turn\)/);
});

test('W16 CSS: property rows, sub-labels, the allowed-domains list', () => {
  assert.match(css, /\.prop \{ display: grid; grid-template-columns: minmax\(0, 1fr\) auto auto;/);
  assert.match(css, /\.prop-v\.warn \{ color: var\(--warn\); \}/);
  assert.match(css, /\.prop-h \{[^}]*text-transform: uppercase;/);
  assert.match(css, /\.prop-list > summary \{/);
  assert.ok(!/\.unlock-row/.test(css), 'the old prose rows are gone');
});
