// node --test: 9.46 Settings › Security - the tree of guards Piper adds to hole.
// Pure helpers from web/dist; wiring over web/src and app.css.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { secItems, secCount, secStateName, secMatchesPreset, secMatches, secTag, secKindLabel, secColumns, searchSettings,
  secSelectedProfile, secNearest, secLevelName, secAskSentence, secAlways, secSetItems, secChangedCount, secVisibleTree, SEC_LEVELS } from '../../piper/web/dist/settings.js';

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

test('Security redesign keeps one panel, shared rows and section saving', () => {
  assert.match(src, /f\.type === 'security' \? securityEl\(s, f, q\)/);
  assert.match(src, /function presetAsk[\s\S]*?setVal\(s\.id, f\.key[\s\S]*?void save\(s, \[\], \[f.key\], id\)/);
  assert.match(src, /Apply the \$\{label\} profile\?/);
  assert.match(src, /writeStore\('piper\.settings\.secopen'/);
  assert.match(src, /'data-sec': i.id/);
  assert.doesNotMatch(src, /secDetails|securityOverview|sc-back/);
  assert.match(src, /class: 'sc-profiles'/);
  assert.match(src, /class: 'switch'/);
  assert.match(src, /secOpen.has\(id\) \|\| items.some/);
  assert.match(src, /g\.id !== 'github' \|\| github/);
  assert.match(src, /Saved: tools, cards and L5 now; sandbox at next start/);
  assert.deepEqual(SEC_LEVELS.map(l => l[1]), ['Minimal', 'Recommended', 'Strict']);
  assert.ok(css.includes('.sc-panel .sc-options'));
  assert.doesNotMatch(css, /\.sc-overview \{/);
});

test('Security profiles clearly apply to the whole page, with Custom as a status', () => {
  assert.match(src, /Start from a ready-made profile\. Applying one /);
  assert.match(src, /approvals and protections on this page/);
  assert.match(src, /class: 'sn-choice sc-level sc-profile-card'/);
  assert.match(src, /'sn-own sc-profile-status on'/);
  assert.doesNotMatch(src, /sc-level-segment|aria-disabled.*Custom/);
  assert.match(src, /secCount\(visibleItems, f.presets/);
});

test('Security group updates preserve mandatory approvals, other areas and the source draft', () => {
  const items = [{id: 'required', required: true}, {id: 'optional'}, {id: 'other_optional'}];
  const value = { required: true, optional: true, unrelated: true };
  assert.deepEqual(secSetItems(value, items, false), {required: true, optional: false, other_optional: false, unrelated: true});
  assert.deepEqual(value, {required: true, optional: true, unrelated: true}, 'do not mutate the saved snapshot');
  assert.deepEqual(secSetItems({}, items, true), {required: true, optional: true, other_optional: true});
  assert.deepEqual(secSetItems({required: false}, [items[0]], false), {required: true}, 'even a stale value cannot disable required approvals');
  assert.deepEqual(secSetItems(value, [], false), value);
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
  assert.deepEqual(secAskSentence(git, MIN), { asks: 'nothing', own: 'commits, pushing, git outside the sandbox' });
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
  assert.match(main, /policy\?\.l5_watch !== false && s\.watch\.length/);
  assert.match(main, /policy\?\.attestation === false \|\| s\.attest\?\.skipped \? null : s\.attest/);
  assert.match(main, /gitlab: \(id\) => on\('gitlab'\)/);
  assert.match(main, /ciEnabled: \(forge\) => on\(forge\)/);
  const git = readFileSync(new URL('../../piper/web/src/gitpanel.ts', import.meta.url), 'utf8');
  assert.match(git, /!deps\.ciEnabled\('gitlab'\) && !deps\.ciEnabled\('github'\)/);
  const http = readFileSync(new URL('../../piper/web/src/httpview.ts', import.meta.url), 'utf8');
  assert.match(http, /if \(bar\.isConnected && bar\.offsetParent !== null\) void load\(\)/);
});

test('Minimal confirm says what Minimal now does (host protections since 2026-10-16), a draft drops the Saved note', () => {
  assert.doesNotMatch(src, /Additional sandbox isolation stays off/);
  assert.match(src, /hole: "Apply Minimal\? This sets the Minimal switches and profile rules/);
  const body = src.slice(src.indexOf('function setVal('), src.indexOf('function clearErr('));
  assert.match(body, /notes\.delete\(sid\)/, 'an edit after Save clears the "Saved:" note');
});

test('Required approvals use disabled shared switches; mixed state counts editable items', () => {
  assert.match(src, /items.filter\(i => !i.required\)/);
  assert.match(src, /disabled: !editable.length/);
  assert.match(src, /input.indeterminate = on > 0 && on < count/);
  assert.match(src, /Production DB and production or unknown HTTP always require your authorization/);
});

test('Plain hole label reflects required approvals that remain enforced', () => {
  const mandatory = ['production', 'unknown-host'];
  const ids = [...mandatory, 'optional'];
  const presets = {plain: {production: true, 'unknown-host': true}, hole: {production: true, 'unknown-host': true, optional: true}};
  assert.equal(secLevelName(presets.plain, presets, ids), 'Plain hole');
  assert.equal(secLevelName(presets.hole, presets, ids), 'Minimal');
});

test('Security counts each changed switch and ignores absent false values', () => {
  const before = {attestation: true, 'ask.git_push': true};
  assert.equal(secChangedCount(TREE, before, {attestation: false, 'ask.git_push': false}), 2);
  assert.equal(secChangedCount(TREE, {}, {attestation: false, unrelated: true}), 0);
  assert.equal(secChangedCount(TREE, before, {...before}), 0);
});

test('Disabled GitHub is absent from the Security search without mutating the full schema', () => {
  const tree = [{id: 'approvals', title: 'Approvals', help: '', groups: [
    {id: 'github', title: 'GitHub', items: [{id:'ask.github_comment', label:'GitHub comment'}]},
    {id: 'git', title: 'Git', items: [{id:'ask.git_push', label:'Push'}]}
  ]}];
  const sections = [{id:'security', title:'Security', fields:[{key:'security', label:'Security tree', help:'', type:'security', tree}]}];
  assert.equal(searchSettings(sections, 'GitHub', false).size, 0);
  assert.equal(searchSettings(sections, 'GitHub', true).get('security').length, 1);
  assert.deepEqual(secVisibleTree(tree, false)[0].groups.map(g => g.id), ['git']);
  assert.equal(tree[0].groups.length, 2);
});

 test('matching switches do not silently opt in to profile semantics', () => {
  const ids = ['one', 'two'];
  const value = {one: true, two: false};
  const presets = {recommended: value};
  assert.equal(secSelectedProfile(value, value, 'custom', presets, ids), undefined);
  assert.equal(secSelectedProfile(value, value, undefined, presets, ids), undefined);
  assert.equal(secSelectedProfile(value, value, 'recommended', presets, ids), 'recommended');
  assert.equal(secSelectedProfile({...value, two: true}, value, 'recommended', presets, ids), undefined);
});
