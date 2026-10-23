// node --test: 9.46 Settings › Security - the tree of guards Piper adds to hole.
// Pure helpers from web/dist; wiring over web/src and app.css.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { secItems, secCount, secStateName, secMatchesPreset, secMatches, secTag, secKindLabel, secColumns, searchSettings,
  secNearest, secLevelName, secAskSentence, secAlways, SEC_LEVELS } from '../../piper/web/dist/settings.js';

const src = readFileSync(new URL('../../piper/web/src/settings.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const TREE = [
  { id: 'start', title: 'Sandbox start', help: '', items: [
    { id: 'settings_review', label: 'Review .hole/settings.json changes', help: 'Waits for you (L1).', risk: 'Off: starts as written' },
    { id: 'attestation', label: 'Verify the running sandbox', help: 'Inspects the container.', risk: 'Off: ready at once' }] },
  { id: 'approvals', title: 'Approvals', help: '', groups: [
    { id: 'git', title: 'Git', items: [{ id: 'ask.git_push', label: 'Push (git_push)', kind: 'git_push' }] },
    { id: 'db', title: 'Databases', items: [{ id: 'ask.db_unmask', label: 'Unmask values', kind: 'db_unmask' }] }] },
];

test('9.46 secItems / secCount: sub-groups flatten, only true counts as on', () => {
  assert.deepEqual(secItems(TREE[1]).map((i) => i.id), ['ask.git_push', 'ask.db_unmask']);
  const all = TREE.flatMap(secItems);
  assert.deepEqual(secCount(all, {}), [0, 4]);
  assert.deepEqual(secCount(all, { attestation: true, 'ask.git_push': 'yes', nope: true }), [1, 4]);
});

test('9.46 plain hole preset matches the full selected default state', () => {
  const ids = ['db_prod_where', 'ask.gitlab_comment', 'ask.http_test'];
  const preset = { db_prod_where: true, 'ask.gitlab_comment': true, 'ask.http_test': true };
  assert.equal(secMatchesPreset(preset, preset, ids), true);
  assert.equal(secMatchesPreset({ ...preset, extra: true }, preset, ids), true);
  assert.equal(secMatchesPreset({}, preset, ids), false);
  assert.equal(secStateName(3, 64, true), 'Minimal');
  assert.equal(secStateName(0, 64, false), 'Plain hole');
  assert.equal(secStateName(64, 64, false), 'Strict');
  assert.equal(secStateName(9, 64, false), 'Custom');
});

test('9.46 secMatches: label, help, risk, kind, a (sub-)group title = all its items', () => {
  assert.deepEqual([...secMatches(TREE, 'unmask')], ['ask.db_unmask']);
  assert.deepEqual([...secMatches(TREE, 'git_push')], ['ask.git_push']);
  assert.deepEqual([...secMatches(TREE, 'ready at once')], ['attestation']);
  assert.deepEqual([...secMatches(TREE, 'sandbox start')], ['settings_review', 'attestation']);
  assert.deepEqual([...secMatches(TREE, 'databases')], ['ask.db_unmask']);
  assert.equal(secMatches(TREE, '  ').size, 0);
});

test('9.46 searchSettings finds the Security section by an item inside the tree', () => {
  const secs = [{ id: 'security', title: 'Security', fields: [{ key: 'security', label: 'Security tree', type: 'security', tree: TREE }] }];
  assert.deepEqual(searchSettings(secs, 'unmask')?.get('security'), ['security']);
  assert.equal(searchSettings(secs, 'zzz')?.get('security'), undefined);
});

test('9.46 secTag / secKindLabel / secColumns', () => {
  assert.deepEqual(secTag('Waits for you (L1).'), { help: 'Waits for you.', tag: 'L1' });
  assert.deepEqual(secTag('No port (K17).'), { help: 'No port.', tag: 'K17' });
  assert.deepEqual(secTag('(L5, Settings -> Sandbox)'), { help: '(L5, Settings -> Sandbox)', tag: '' });
  assert.equal(secKindLabel({ id: 'a', label: 'Push (git_push)', kind: 'git_push' }), 'Push');
  assert.equal(secKindLabel({ id: 'b', label: 'Push (x)' }), 'Push (x)');
  const ids = (cols) => cols.map((c) => c.map((x) => x.id));
  const subs = ['git', 'gitlab', 'github', 'projects', 'http', 'db', 'agents'].map((id) => ({ id }));
  assert.deepEqual(ids(secColumns(subs)), [['git', 'projects'], ['gitlab'], ['github', 'http'], ['db', 'agents']]);
  assert.deepEqual(ids(secColumns([{ id: 'db' }, { id: 'new' }])), [['db', 'new']], 'empty columns go, an unknown group joins the last');
});

test('9.46 wiring: the page, presets save, governed note, nav count, kept scroll', () => {
  assert.match(src, /f\.type === 'security' \? securityEl\(s, f, q\)/);
  assert.match(src, /function presetAsk[\s\S]*?setVal\(s\.id, f\.key[\s\S]*?void save\(s, \[\], \[f.key\]\)/);
  assert.match(src, /Apply \$\{label\} protection\?/);
  assert.match(src, /Protect the host and production data/);
  assert.match(src, /class: 'st-description'[\s\S]*?governedNote\(f\)\),\s*h\('div', \{ class: 'st-ctl'/,
    'the governing security link stays with the field explanation, before its control');
  assert.match(src, /s\.id === 'security' \? secNavCount\(s\)/);
  assert.match(src, /box\.scrollTop = top;/);
  assert.match(src, /writeStore\('piper\.settings\.secopen'/);
  assert.match(src, /role: 'switch', 'aria-checked': state === 'mixed' \? 'mixed'/);
  for (const c of ['.sc-sw[aria-checked="mixed"]', '.sc-summary', '.sc-items', '.sc-subs', '.sc-governed', '.st-field.governed']) assert.ok(css.includes(c), c);
});


test('F26 Security overview: three level rows, the groups in words, Always protected, Show / Reset (changes 9.46)', () => {
  assert.match(src, /if \(!secDetails && !q && !secFocus\) return securityOverview\(s, f, v\)/);
  assert.match(src, /'Claude asks you before'/);
  assert.match(src, /'Always protected: '/);
  assert.match(src, /'Change', fi\('chev-r'\)/);   // pages polish: the chevron is the mask icon
  assert.match(src, /onclick: \(\) => presetAsk\(s, f, id, label\)/, 'a level is still confirmed first');
  assert.match(src, /'Show'\)/);
  assert.match(src, /'Reset'\)/);
  assert.match(src, /g\.id !== 'github' \|\| github/, 'GitHub only with its module');
  assert.doesNotMatch(src, /Customize details…/);
  assert.doesNotMatch(src, /guards on`/, 'no "n of N guards on"');
  assert.doesNotMatch(src, /`\$\{n\} of \$\{t\} on`/);
  assert.doesNotMatch(src, /mono sc-k/, 'no tool ids under the card kinds');
  assert.match(src, /\.\.\.its.filter\(shown\).map\(kindRow\)/);
  assert.doesNotMatch(src, /sc-subfold/);
  assert.match(src, /start: 'next start \/ L5 now', sandbox: 'next start \/ tools now'/);
  assert.match(src, /Tool rules, cards and L5 apply at once/);
  assert.match(src, /Saved: tools, cards and L5 now; sandbox at next start/);
  assert.match(src, /'Turn off all additional Piper guards…'/, 'plain hole stays in Details');
  for (const c of ['.sc-levels .sc-level.on', '.sc-ask {', '.sc-always {', '.sc-custom {']) assert.ok(css.includes(c), c);
  assert.deepEqual(SEC_LEVELS.map((l) => l[1]), ['Minimal', 'Recommended', 'Strict']);
});

// a slice of security.py: Git and HTTP card kinds with their short phrases, four guards
const T26 = [
  { id: 'tools', title: 'Agent tools', help: '', items: [
    { id: 'protected_mounts', label: 'Protect', short: 'Piper itself' }, { id: 'piper_port', label: 'Port', short: 'Piper itself' },
    { id: 'db_prod_where', label: 'WHERE', short: 'production without WHERE' }, { id: 'db_masking', label: 'Mask', short: 'personal data' }] },
  { id: 'approvals', title: 'Approvals', help: '', groups: [
    { id: 'git', title: 'Git', short: 'Git', items: [
      { id: 'ask.git_commit', label: 'Commit (git_commit)', kind: 'git_commit', short: 'commits' },
      { id: 'ask.git_push', label: 'Push (git_push)', kind: 'git_push', short: 'pushing' },
      { id: 'ask.git_run', label: 'Full git', kind: 'git_run', short: 'git outside the sandbox' },
      { id: 'ask.git_run_project', label: 'git_run in another project', kind: 'git_run_project', short: 'git outside the sandbox' }] },
    { id: 'http', title: 'HTTP requests', short: 'HTTP', items: [
      { id: 'ask.http_test', label: 'Run on test', kind: 'http_test', short: 'test' },
      { id: 'ask.http_dev', label: 'Run on dev', kind: 'http_dev', short: 'dev' },
      { id: 'ask.http_prod', label: 'Run on production', kind: 'http_prod', short: 'production' },
      { id: 'ask.http_host', label: 'Outside', kind: 'http_host', short: 'unknown hosts' }] }] },
];
const IDS = T26.flatMap(secItems).map((i) => i.id);
const on = (...ids) => Object.fromEntries(ids.map((i) => [i, true]));
const MIN = on('protected_mounts', 'piper_port', 'db_prod_where', 'ask.http_test', 'ask.http_prod', 'ask.http_host');
const REC = { ...MIN, ...on('db_masking', 'ask.git_push', 'ask.git_run', 'ask.git_run_project') };
const STRICT = on(...IDS);
const PRESETS = { hole: MIN, recommended: REC, guarded: STRICT, plain: {} };
const git = T26[1].groups[0].items, http = T26[1].groups[1].items;

test('F26 secAskSentence: the groups in words for Minimal / Recommended / Strict / Custom', () => {
  assert.deepEqual(secAskSentence(git, MIN), { asks: 'nothing - Claude does all of it on its own', own: 'commits, pushing, git outside the sandbox' });
  assert.deepEqual(secAskSentence(http, MIN), { asks: 'test, production, unknown hosts', own: 'dev' }, "the board's HTTP row");
  assert.deepEqual(secAskSentence(git, REC), { asks: 'pushing, git outside the sandbox', own: 'commits' }, "the board's Git row");
  assert.deepEqual(secAskSentence(git, STRICT), { asks: 'commits, pushing, git outside the sandbox', own: '' });
  // Custom: git_run off but git_run_project on - a shared phrase asks while any of its kinds asks
  assert.deepEqual(secAskSentence(git, { ...REC, 'ask.git_run': false }), { asks: 'pushing, git outside the sandbox', own: 'commits' });
  assert.deepEqual(secAskSentence(git, { ...REC, 'ask.git_push': false }), { asks: 'git outside the sandbox', own: 'commits, pushing' });
  for (const v of [MIN, REC, STRICT]) for (const g of [git, http]) {
    const t = Object.values(secAskSentence(g, v)).join(' ');
    assert.doesNotMatch(t, /_/, 'no tool ids');
    assert.doesNotMatch(t, /\d+ of \d+/, 'no "n of N"');
  }
});

test('F26 secNearest / secLevelName: Custom is "<level> + N changes", not a fourth level', () => {
  assert.equal(secLevelName(REC, PRESETS, IDS), 'Recommended');
  assert.equal(secLevelName(MIN, PRESETS, IDS), 'Minimal');
  assert.equal(secLevelName(STRICT, PRESETS, IDS), 'Strict');
  assert.equal(secLevelName({ ...REC, 'ask.git_commit': true }, PRESETS, IDS), 'Recommended + 1 change');
  assert.equal(secLevelName({ ...MIN, 'ask.git_commit': true, db_masking: true }, PRESETS, IDS), 'Minimal + 2 changes', 'a tweaked Minimal stays Minimal');
  assert.equal(secLevelName({}, PRESETS, IDS), 'Plain hole');
  const n = secNearest({ ...REC, 'ask.git_commit': true }, PRESETS, IDS);
  assert.equal(n.id, 'recommended');
  assert.deepEqual(n.changed, ['ask.git_commit'], 'Show lists exactly the differing switches');
  assert.equal(secNearest(REC, {}, IDS).label, 'Custom', 'an older piperd without presets');
});

test('F26 secAlways: the guards that are on, each phrase once', () => {
  assert.deepEqual(secAlways(T26, MIN), ['Piper itself', 'production without WHERE']);
  assert.deepEqual(secAlways(T26, REC), ['Piper itself', 'production without WHERE', 'personal data']);
  assert.deepEqual(secAlways(T26, {}), []);
});

test('disabled L5 and attestation hide stored active warnings', () => {
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /state\.gui_flags\?\.l5_watch !== false && s\.watch\.length/);
  assert.match(main, /state\.gui_flags\?\.attestation === false \|\| s\.attest\?\.skipped \? null : s\.attest/);
  assert.match(main, /gitlab: \(id\) => on\('gitlab'\)/);
  assert.match(main, /ciEnabled: \(forge\) => on\(forge\)/);
  const git = readFileSync(new URL('../../piper/web/src/gitpanel.ts', import.meta.url), 'utf8');
  assert.match(git, /!deps\.ciEnabled\('gitlab'\) && !deps\.ciEnabled\('github'\)/);
  const http = readFileSync(new URL('../../piper/web/src/httpview.ts', import.meta.url), 'utf8');
  assert.match(http, /if \(bar\.isConnected && bar\.offsetParent !== null\) void load\(\)/);
});

test('Minimal confirm says what Minimal now does (host protections since 2026-10-16), a draft drops the Saved note', () => {
  assert.doesNotMatch(src, /Additional sandbox isolation stays off/);
  assert.match(src, /hole: 'Keep production safeguards, host protections and a few approvals\. Nothing blocks a start\.'/);
  const body = src.slice(src.indexOf('function setVal('), src.indexOf('function clearErr('));
  assert.match(body, /notes\.delete\(sid\)/, 'an edit after Save clears the "Saved:" note');
});
