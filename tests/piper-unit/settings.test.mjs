// node --test: the Settings page's pure helpers (web/src/settings.ts)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyNote, changedKeys, filterProjects, listText, localError, parseList, searchSettings, settingsFieldCopy, settingsFieldGroups } from '../../piper/web/dist/settings.js';

test('Settings A preserves every field exactly once, including new server fields and search subsets', () => {
  const fields = ['idea_mcp_url', 'language', 'default_unlocks', 'future_option', 'max_sessions'].map(key => ({ key, label: key, help: '', type: 'string' }));
  const grouped = settingsFieldGroups('sessions', fields);
  assert.deepEqual(grouped.map(g => g.title), ['General', 'Tools', 'Access & approvals', 'Other settings']);
  const flattened = grouped.flatMap(g => g.fields);
  assert.equal(new Set(flattened).size, fields.length);
  assert.ok(fields.every(f => flattened.includes(f)), 'preserve original schema objects');
  assert.deepEqual(settingsFieldGroups('sessions', fields.filter(f => f.key === 'idea_mcp_url')).map(g => g.title), ['Tools']);
  assert.deepEqual(settingsFieldGroups('future_section', fields), [{ title: '', fields }]);
});

test('dry-run is a Databases setting under Security, separate from the guard tree', () => {
  const fields = [{ key: 'security', type: 'security' }, { key: 'db_dry_run', type: 'bool', value: true }];
  const groups = settingsFieldGroups('security', fields);
  assert.deepEqual(groups.map(g => [g.title, g.fields.map(f => f.key)]), [
    ['', ['security']], ['Databases', ['db_dry_run']],
  ]);
  assert.equal(groups[1].fields[0], fields[1]);
});

test('Settings A exposes an explanation and preserves complete technical details and warnings', () => {
  const f = { key: 'git_hidden_server_content', label: 'Old label', help: 'Verified contents only. Hidden files become readable until restart.' };
  const copy = settingsFieldCopy(f);
  assert.match(copy.summary, /sandbox may then read/);
  assert.equal(copy.detail, f.help);
  const path = { key: 'future', label: 'Future', help: 'Use ~/.m2/settings.xml at http://localhost:1234. Changes apply later.' };
  assert.equal(settingsFieldCopy(path).summary, 'Use ~/.m2/settings.xml at http://localhost:1234.');
  assert.equal(settingsFieldCopy(path).detail, path.help);
  assert.deepEqual(settingsFieldCopy({ key: 'unknown', label: 'Unknown', help: '' }), { label: 'Unknown', summary: '', detail: '' });
});

test('Settings search finds the new visible copy as well as legacy labels and technical details', () => {
  const sections = [{ id: 'sessions', title: 'New sessions', fields: [{ key: 'max_sessions', label: 'Concurrent sessions at most', help: 'Legacy help', type: 'int' }] }];
  for (const query of ['active sessions', 'at most', 'Legacy help', 'max_sessions']) {
    assert.deepEqual(searchSettings(sections, query).get('sessions'), ['max_sessions']);
  }
});

test('parseList: one item per line, trimmed, no blanks or duplicates, ports as numbers', () => {
  assert.deepEqual(parseList(' **/.git \n\n**/*.pem\n**/.git\n'), ['**/.git', '**/*.pem']);
  assert.deepEqual(parseList('2375\n 8080\nabc', true), [2375, 8080, 'abc']);   // piperd rejects "abc"
  assert.equal(listText(['a', 2]), 'a\n2');
  assert.equal(listText(undefined), '');
});

test('changedKeys compares by value, lists and project rows included', () => {
  const orig = { a: ['x'], b: 1, p: [{ key: 'k', gitlab: ['g/p'] }], s: '' };
  assert.deepEqual(changedKeys(orig, structuredClone(orig)), []);
  assert.deepEqual(changedKeys(orig, { ...structuredClone(orig), a: ['x', 'y'], s: 'tok' }), ['a', 's']);
  const rows = structuredClone(orig);
  rows.p[0].gitlab = ['g/q'];
  assert.deepEqual(changedKeys(orig, rows), ['p']);
});

test('localError: ints in range, HH:MM, secrets without spaces', () => {
  assert.equal(localError({ type: 'int', min: 1, max: 200 }, 40), '');
  assert.equal(localError({ type: 'int', min: 1, max: 200 }, 0), 'range 1–200');
  assert.equal(localError({ type: 'int' }, ''), 'a whole number');
  assert.equal(localError({ type: 'time' }, '07:30'), '');
  assert.equal(localError({ type: 'time' }, '7:30'), 'time HH:MM');
  assert.equal(localError({ type: 'time', default: '' }, ''), '');   // optional (quiet hours)
  assert.equal(localError({ type: 'time', default: '07:30' }, ''), 'time HH:MM');
  assert.equal(localError({ type: 'secret' }, 'a b'), 'no spaces');
  assert.equal(localError({ type: 'string' }, 'anything'), '');
});

test('applyNote deduplicates', () => {
  assert.equal(applyNote(['takes effect immediately', 'takes effect immediately', 'after a restart']), 'takes effect immediately · after a restart');
});

test('filterProjects: search over key, path, GitLab, observability; the filters', () => {
  const rows = [
    { key: 'acme/shop', path: '/w/acme/shop', gitlab: ['grp/acme.shop'], profile: '', title: '', obs: 'shop', explicit: true, source: 'projects.json' },
    { key: 'beta/webstore', path: '/w/beta/webstore', gitlab: [], profile: '', title: '', obs: '', explicit: false, source: 'discovered' },
    { key: 'outdoor/outdoor', path: '/w/outdoor/outdoor', gitlab: [], profile: '', title: '', obs: '', explicit: false, source: 'discovered' },
  ];
  assert.deepEqual(filterProjects(rows, '', 'all'), [0, 1, 2]);
  assert.deepEqual(filterProjects(rows, 'ACME', 'all'), [0]);            // case-insensitive, any column
  assert.deepEqual(filterProjects(rows, 'acme.shop', 'all'), [0]);      // the GitLab path
  assert.deepEqual(filterProjects(rows, '', 'gitlab'), [0]);
  assert.deepEqual(filterProjects(rows, '', 'obs'), [0]);
  assert.deepEqual(filterProjects(rows, '', 'explicit'), [0]);
  const sugg = { 'beta/webstore': { obs: 'shop-prod' } };
  assert.deepEqual(filterProjects(rows, '', 'suggested', sugg), [1]);
  assert.deepEqual(filterProjects(rows, '', 'unmapped', sugg, ['outdoor/outdoor']), [1]);   // "Don't map" is not "unmapped"
});

test('searchSettings: label, help, key, env name and choice labels; a section title takes all its fields', () => {
  const sections = [
    { id: 'db', title: 'Databases', fields: [
      { key: 'statement_timeout_s', label: 'Statement timeout (s)', help: '' },
      { key: 'max_rows', label: 'Rows to an agent per call', help: 'cut after this' },
      { key: 'prod_read', label: 'Production reads', help: '', choices: [['off', 'Never'], ['ask', 'Ask on a card']] }] },
    { id: 'env', title: 'Environment', fields: [{ key: 'prod_timeout', label: 'PIPER_PROD_TIMEOUT (s)', help: '', env: 'PIPER_PROD_TIMEOUT' }] },
    { id: 'mm', title: 'Mattermost', fields: [{ key: 'webhook', label: 'Webhook URL', help: '' }, { key: 'channel', label: 'Channel', help: '' }] },
  ];
  assert.equal(searchSettings(sections, '  '), null);
  assert.deepEqual([...searchSettings(sections, 'TIMEOUT')], [['db', ['statement_timeout_s']], ['env', ['prod_timeout']]]);
  assert.deepEqual([...searchSettings(sections, 'max_rows')], [['db', ['max_rows']]]);   // the policy.json key
  assert.deepEqual([...searchSettings(sections, 'cut after')], [['db', ['max_rows']]]);  // help
  assert.deepEqual([...searchSettings(sections, 'on a card')], [['db', ['prod_read']]]); // a choice label
  assert.deepEqual([...searchSettings(sections, 'matter')], [['mm', ['webhook', 'channel']]]);
  assert.equal(searchSettings(sections, 'kubelogin').size, 0);
});

// ---- N10 pass D (GUI): Settings page closures - asserted on the source (no DOM in node --test)
const settingsSrc = async () => (await import('node:fs')).readFileSync(new URL('../../piper/web/src/settings.ts', import.meta.url), 'utf8');

test('N10-D-1: "default" clears the field\'s error (999 -> range error -> default kept Save disabled)', async () => {
  const src = await settingsSrc();
  const btn = src.slice(src.indexOf("title: `back to the default"), src.indexOf("'Reset to default')"));
  assert.match(btn, /clearErr\(s\.id, f\.key\)/, 'the default button drops the stale local / server error');
  assert.match(src, /function clearErr\(sid: string, key: string\): void \{[^}]*delete m\[key\][^}]*srvErrs\.delete\(sid\)/s);
});

test('N10-D-2: after Save the keyboard goes back to the saved field (the Save button is rebuilt, focus fell to <body>)', async () => {
  const src = await settingsSrc();
  const body = src.slice(src.indexOf('async function save('), src.indexOf('function askConfirm('));
  assert.match(body, /const back = \[\.\.\.keys, \.\.\.resets\.get\(s\.id\)!, \.\.\.clears\.get\(s\.id\)!\]\[0\]/);
  assert.match(body, /await load\(true\);\s*refocus\(s\.id, back\)/);
  assert.match(src, /function refocus\(sid: string, key: string \| undefined\): void \{[\s\S]*?document\.activeElement !== document\.body[\s\S]*?\[data-nav="\$\{sid\}"\]/);
});

test('N10-D-3: Code review - the action list follows the edited number / name (it kept the old "1 Tests")', async () => {
  const src = await settingsSrc();
  const upd = src.slice(src.indexOf('const update = () =>'), src.indexOf('for (const input of [label, key, kind, prompt])'));
  assert.match(upd, /menu\.children\[selected\]\?\.replaceChildren\(h\('kbd', \{\}, key\.value\), markBidi\(label\.value\)\)/);
  assert.match(src, /h\('kbd', \{\}, String\(a\.key\)\), markBidi\(a\.label\)\)/, 'a raw RLO in a name stays visible as [U+202E]');
});

test('N10-D-4: a preset applied in Settings decides the Advanced fold at once (it flipped on the next click)', async () => {
  const src = await settingsSrc();
  assert.match(src, /presetNow = String\(r\.json\.preset\)/);
  assert.match(src, /advOpen \?\? \(presetNow \? presetNow !== 'basic' : \(deps\.advanced\?\.\(\) \?\? true\)\)/);
});
