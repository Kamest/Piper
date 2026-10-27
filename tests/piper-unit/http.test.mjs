// node --test: pure helpers of the .http runner in the file browser (httpview.ts), from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseEnvDocument, httpApi, httpView, httpConfirmationRequests, httpIterationLabel, httpResultGroups, httpSaveTarget, httpFileChanged, newHttpTab, isHttpFile, pickEnv, prettyBody, requestAt, saveName, SECRET_HEADER, shortEnv, tlsDefault, tunnelName, writes } from '../../piper/web/dist/httpview.js';
import { highlightLines, langOf } from '../../piper/web/dist/highlight.js';
import { groupByDir, httpTabWanted, matchesFilter, newestRun, newRequestFiles } from '../../piper/web/dist/httppanel.js';

const ENVS = [{ name: 'prod', cls: 'prod' }, { name: 'test', cls: 'test' }, { name: 'dev', cls: 'dev' }];

test('production confirmation lists actual imported methods and destinations before acknowledging writes', () => {
  const info = {requests:[{index:0,method:'run',url:'./child.http'}],urls:{},previews:{prod:{0:{sha:'snapshot',
    requests:[{method:'GET',url:'https://read.example/a'},{method:'DELETE',url:'https://write.example/b'}]}}}};
  const rows = httpConfirmationRequests(info,'prod',null);
  assert.deepEqual(rows.map(r=>r.url),['https://read.example/a','https://write.example/b']);
  assert.equal(writes(rows).length,1);
  assert.throws(()=>httpConfirmationRequests({...info,previews:{}},'prod',null),/preview/);
});

test('iteration labels cannot mix different leaf requests in the same imported file', () => {
  const a = {name:'Same', root_index:0, source_file:'child.http', source_line:1, iteration:null};
  const b = {...a, source_line:5, iteration:0};
  assert.equal(httpIterationLabel(b, [a,b]), 'Same');
  const c = {...b, iteration:1};
  assert.equal(httpIterationLabel(b, [a,b,c]), 'Same · 1 of 2');
  assert.equal(httpIterationLabel(c, [a,b,c]), 'Same · 2 of 2');
});

test('native results group by original root and occurrence, never by repeated names', () => {
  const info = { sha: 'same', requests: [{ index: 0, name: 'Login', method: 'GET' },
    { index: 1, name: 'Login', method: 'GET' }, { index: 2, name: 'Flow', method: 'run' }] };
  const a = { name: 'Login', root_index: 0, ok: true };
  const b = { name: 'Login', root_index: 1, ok: false };
  const c = { name: 'Child', root_index: 2, source_file: 'api/child.http', ok: true };
  const d = { name: 'Child', root_index: 2, source_file: 'api/child.http', ok: false };
  const run = { sha: 'same', index: null, requests: [a, b, c, d] };
  const groups = httpResultGroups(run, info);
  assert.deepEqual(groups.get(0), [a]);
  assert.deepEqual(groups.get(1), [b]);
  assert.deepEqual(groups.get(2), [c, d]);
  assert.deepEqual(httpSaveTarget(run, info, a), { index: 0 });
  assert.match(httpSaveTarget(run, info, c).why, /does not replay the group/);
  assert.equal(httpFileChanged(run, { ...info, sha: 'changed' }), true);
  assert.equal(httpResultGroups(run, { ...info, sha: 'changed' }).size, 0);
  assert.match(httpSaveTarget(run, { ...info, sha: 'changed' }, a).why, /changed since/);
});

test('legacy result mapping requires exact order; iteration saving cannot replay a group', () => {
  const info = { requests: [{ index: 0, name: 'one', method: 'GET' }, { index: 1, name: 'two', method: 'GET' }] };
  const first = { name: 'one' }, second = { name: 'two' };
  assert.deepEqual([...httpResultGroups({ index: null, requests: [first, second] }, info)], [[0, [first]], [1, [second]]]);
  assert.equal(httpResultGroups({ index: null, requests: [second, first] }, info).size, 0);
  assert.deepEqual(httpSaveTarget({ index: 1, requests: [first] }, info, first), { index: 1 });
  const rows = [{ name: 'one', root_index: 0, iteration: 0 }, { name: 'one', root_index: 0, iteration: 1 }];
  assert.deepEqual(httpResultGroups({ index: null, requests: rows }, info).get(0), rows);
  assert.match(httpSaveTarget({ index: null, requests: rows }, info, rows[0]).why, /does not replay one/);
  assert.match(httpSaveTarget({ index: null, requests: [second, first] }, info, first).why, /cannot be matched/);
});

test('No environment remains an explicit production choice, including files with no envs', async () => {
  const saved = { document: globalThis.document, window: globalThis.window };
  const apiSaved = { ...httpApi };
  class El {
    children = []; attrs = {}; listeners = {}; dataset = {};
    classList = { add() {}, remove() {}, toggle() {} };
    append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
    setAttribute(name, value) { this.attrs[name] = value; if (name === 'selected') this.selected = true; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    get firstChild() { return this.children[0]; }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
    get textContent() { return this.children.map((item) => item.textContent).join(''); }
    focus() {}
  }
  const find = (node, predicate) => predicate(node) ? node : node.children?.map((child) => find(child, predicate)).find(Boolean);
  globalThis.document = { createElement: () => new El(), createElementNS: () => new El(),
    createTextNode: (textContent) => ({ textContent, remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); } }) };
  const stores = [];
  globalThis.window = { localStorage: { getItem: () => null, setItem: (...args) => stores.push(args) } };
  let envs = [...ENVS, { name: 'No environment', cls: 'test' }];
  let modal;
  let executions = 0;
  httpApi.envs = async () => ({ data: { ijhttp: '/ijhttp', envs, requests: [{ index: 0, method: 'POST', name: 'write', url: 'http://localhost/' }] } });
  httpApi.runs = async () => ({ data: { runs: [] } });
  httpApi.envcfg = async () => ({ data: null });
  httpApi.exec = async () => { executions++; };
  try {
    const state = newHttpTab();
    const view = httpView({ project: 'app', path: 'test.http', linesChanged() {}, toast() {}, closeModal() {},
      modal: (title, ...children) => { modal = { title, children }; } }, state);
    await view.load();
    assert.equal(state.env, 'dev', 'normal preselection stays unchanged');
    let select = find(view.bar, (node) => node.className?.startsWith('httpenv'));
    assert.equal(select.children.filter((node) => node.textContent.startsWith('No environment')).length, 2, 'a named env has its own distinct option');
    select.value = '';
    select.listeners.change();
    assert.equal(state.env, '');
    assert.equal(view.cls(), 'prod');
    assert.equal(stores.length, 0, 'No environment is not remembered in local storage');
    await view.load();
    assert.equal(state.env, '', 'an explicit choice survives a reload in this view');
    assert.equal(view.bar.dataset.cls, 'prod');
    assert.equal(Object.hasOwn(find(view.bar, (node) => node.className === 'httprun danger').attrs, 'disabled'), false);
    await view.run(0);
    assert.equal(executions, 0, 'clicking run first confirms production');
    assert.equal(modal.title, 'Production · no environment');
    const button = find(modal, (node) => node.textContent === 'Run without environment');
    assert.ok(Object.hasOwn(button.attrs, 'disabled'), 'writes require the existing acknowledgement');
    const ack = find(modal, (node) => node.attrs?.type === 'checkbox');
    ack.checked = true;
    ack.listeners.change();
    assert.equal(button.disabled, false);
    state.run = { env: 'dev' };
    assert.equal(view.stale(), true);
    state.run = null;
    envs = [];
    await view.load();
    select = find(view.bar, (node) => node.className?.startsWith('httpenv'));
    assert.equal(select.children.length, 1);
    assert.equal(select.children[0].textContent, 'No environment');
    assert.equal(view.cls(), 'prod');
  } finally { Object.assign(globalThis, saved); Object.assign(httpApi, apiSaved); }
});

test('no-log results cannot offer history-backed sharing; saved results still can', async () => {
  const saved = { document: globalThis.document };
  const originalShare = httpApi.share;
  class El {
    children = []; attrs = {}; listeners = {}; dataset = {};
    classList = { add() {}, remove() {}, toggle() {} };
    append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
    setAttribute(name, value) { this.attrs[name] = value; if (name === 'checked') this.checked = true; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    get firstChild() { return this.children[0]; }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
    get textContent() { return this.children.map((item) => item.textContent).join(''); }
  }
  const find = (node, predicate) => predicate(node) ? node : node.children?.map((child) => find(child, predicate)).find(Boolean);
  globalThis.document = {
    createElement: () => new El(),
    createTextNode: (textContent) => ({ textContent, remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); } }),
  };
  const calls = [];
  httpApi.share = async (...args) => { calls.push(args); };
  try {
    for (const no_log of [true, false]) {
      const state = newHttpTab();
      state.env = 'dev';
      state.run = { id: 'fixture', env: 'dev', cls: 'dev', by: 'gui', at: Date.now() / 1000,
        index: null, requests: [], summary: 'fixture', ms: 1, share: true, no_log };
      const view = httpView({ project: 'app', path: 'test.http', linesChanged() {}, toast() {} }, state);
      view.draw();
      const parts = view.resultParts({ tests: [{ name: 'real test', ok: true, message: '' }], error: '',
        headers: [], req_headers: [], status: null, script_log: '<script>literal</script>\nsecond line\n',
        script_log_cut: no_log, script_log_limit: 20 });
      assert.equal(parts.length, 2, 'a log is a separate section after the real tests');
      assert.equal(parts[1].children[0].textContent, no_log ? 'Script log (cut)' : 'Script log (2 lines)');
      assert.equal(parts[1].children[1].textContent, '<script>literal</script>\nsecond line\n');
      if (no_log) assert.match(parts[1].textContent, /20 characters/);
      const label = find(view.res, (node) => node.className === 'small httpshare');
      const checkbox = label.children[0];
      assert.equal(Object.hasOwn(checkbox.attrs, 'disabled'), no_log);
      assert.equal(!!checkbox.checked, !no_log);
      assert.match(label.attrs.title, no_log ? /@no-log.*not saved.*cannot be shared/ : /scrubbed/);
      checkbox.checked = false;
      checkbox.listeners.change(); // A synthetic event must not bypass the disabled control.
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(calls.length, no_log ? 0 : 1);
    }
  } finally {
    Object.assign(globalThis, saved);
    httpApi.share = originalShare;
  }
});

test('installation polling resumes after hiding and returning to the same HTTP view', async () => {
  const saved = { window: globalThis.window, document: globalThis.document };
  const apiSaved = { ...httpApi };
  class El {
    children = [];
    dataset = {};
    isConnected = true;
    offsetParent = {};
    classList = { toggle() {}, add() {}, remove() {} };
    append(...xs) { for (const x of xs) { x.parent = this; this.children.push(x); } }
    setAttribute() {}
    addEventListener() {}
    get firstChild() { return this.children[0]; }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
    get textContent() { return this.children.map((x) => x.textContent).join(''); }
  }
  const timers = [];
  let fetches = 0;
  globalThis.document = {
    createElement: () => new El(),
    createTextNode: (textContent) => ({ textContent, remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); } }),
  };
  globalThis.window = { setTimeout: (fn) => { timers.push(fn); return 1; } };
  httpApi.envs = async () => { fetches++; return { data: { ijhttp: null, installing: true, envs: [], requests: [] } }; };
  httpApi.runs = async () => ({ data: { runs: [] } });
  httpApi.envcfg = async () => ({ data: null });
  try {
    const v = httpView({ project: 'app', path: 'demo.http', linesChanged() {}, toast() {}, modal() {}, closeModal() {} }, newHttpTab());
    await v.load();
    assert.equal(timers.length, 1);
    for (const detached of [false, true]) {
      v.bar.isConnected = !detached;
      v.bar.offsetParent = null;
      await timers.shift()();
      assert.equal(fetches, 1, 'hidden or detached view does not call the API');
      v.bar.isConnected = true;
      v.bar.offsetParent = {};
      v.resume();
      v.resume();
      assert.equal(timers.length, 1, 'returning resumes exactly one polling timer');
    }
    await timers.shift()();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(fetches, 2, 'the same view updates again after returning');
    assert.match(v.bar.textContent, /Installing/);
  } finally {
    Object.assign(globalThis, saved);
    Object.assign(httpApi, apiSaved);
  }
});

test('pickEnv: remembered when offered, else the first dev, else the first', () => {
  assert.equal(pickEnv(ENVS, 'test'), 'test');
  assert.equal(pickEnv(ENVS, 'gone'), 'dev');
  assert.equal(pickEnv([{ name: 'live', cls: 'prod' }], ''), 'live');
  assert.equal(pickEnv(ENVS, 'prod'), 'dev');                                      // never preselected armed on prod
  assert.equal(pickEnv([{ name: 'prod', cls: 'prod' }, { name: 'stage', cls: 'test' }], ''), 'stage');
  assert.equal(pickEnv([], 'x'), '');
});

test('writes: everything but GET/HEAD/OPTIONS changes data', () => {
  const reqs = ['GET', 'post', 'HEAD', 'DELETE', 'OPTIONS'].map((method, index) => ({ index, name: '', method, url: '', line: 0, start: 0 }));
  assert.deepEqual(writes(reqs).map((r) => r.method), ['post', 'DELETE']);
});

test('prettyBody: JSON indented, XML kept, the rest plain', () => {
  assert.deepEqual(prettyBody('{"a":1}', 'application/json'), { text: '{\n  "a": 1\n}', lang: 'json' });
  assert.equal(prettyBody('{broken', 'application/json').lang, 'plain');
  assert.equal(prettyBody('<a/>', '').lang, 'xml');
  assert.equal(prettyBody('ok', 'text/plain').lang, 'plain');
});

test('requestAt maps 1-based line numbers to request indexes', () => {
  const info = { requests: [{ index: 0, line: 2 }, { index: 1, line: 9 }] };
  assert.equal(requestAt(info, 3), 0);
  assert.equal(requestAt(info, 10), 1);
  assert.equal(requestAt(info, 4), -1);
  assert.equal(requestAt(null, 3), -1);
});

test('isHttpFile and secret headers', () => {
  assert.ok(isHttpFile('a/b/Order.http') && isHttpFile('x.rest') && !isHttpFile('x.https.json'));
  assert.ok(SECRET_HEADER.test('Authorization') && SECRET_HEADER.test('set-cookie') && !SECRET_HEADER.test('Content-Type'));
});

test('highlighting of the .http format', () => {
  assert.equal(langOf('http-client/api.http'), 'http');
  const lines = ['### Login', '@base = x', 'POST {{base}}/login', 'Content-Type: application/json', '', '> {%',
    '  client.test("ok", () => {});', '%}', 'GET https://a.example/x'];
  const t = highlightLines(lines, 'http');
  const cls = (i, text) => t[i].find((k) => k.t.includes(text))?.c;
  assert.equal(cls(0, 'Login'), 'h');
  assert.equal(cls(1, '@base'), 'v');
  assert.equal(cls(2, 'POST'), 'k');
  assert.equal(cls(2, '{{base}}'), 'v');
  assert.equal(cls(3, 'Content-Type'), 'p');
  assert.equal(cls(5, '{%'), 'g');
  assert.equal(cls(6, '"ok"'), 's');
  assert.equal(cls(6, 'client'), 'k');
  assert.equal(cls(7, '%}'), 'g');
  assert.equal(cls(8, 'https://a.example/x'), 's');
});

test('the HTTP tab groups files by directory and filters by path, request name or URL', () => {
  const f = (file, reqs = []) => ({ file, requests: reqs.map(([name, url], index) => ({ index, name, method: 'GET', url })), last: null });
  const files = [f('b/z.http'), f('a.http'), f('b/a.http', [['Login', '{{base}}/auth']]), f('c/d/e.http')];
  assert.deepEqual(groupByDir(files).map(([d, g]) => [d, g.map((x) => x.file)]),
    [['', ['a.http']], ['b', ['b/a.http', 'b/z.http']], ['c/d', ['c/d/e.http']]]);
  assert.ok(matchesFilter(files[2], 'login') && matchesFilter(files[2], '/AUTH') && matchesFilter(files[2], 'b/a'));
  assert.ok(!matchesFilter(files[0], 'login') && matchesFilter(files[0], '  '));
});

test('the HTTP tab opens the file run last (else the only one)', () => {
  const f = (file, at) => ({ file, requests: [], last: at ? { at, ok: true } : null });
  assert.equal(newestRun([f('a.http', 5), f('b.http', 9), f('c.http')]), 'b.http');
  assert.equal(newestRun([f('only.http')]), 'only.http');
  assert.equal(newestRun([f('a.http'), f('b.http')]), '');
});

test('shortEnv keeps buttons short', () => {
  assert.equal(shortEnv('test with tunel `ssh -L 8443:192.0.2.1:443 projects@test-careers.example.com`'), 'test with tunel');
  assert.equal(shortEnv('dev'), 'dev');
  assert.equal(shortEnv('a-very-long-environment-name-indeed'), 'a-very-long-environment…');
});

test('saveName: Content-Disposition first, else the request name + the type\'s extension, never a path', () => {
  assert.equal(saveName({ name: 'PDF posudek', headers: [['Content-Type', 'application/pdf']] }), 'pdf-posudek.pdf');
  assert.equal(saveName({ name: 'x', headers: [['content-disposition', 'attachment; filename="Posudek 2026.pdf"']] }), 'Posudek 2026.pdf');
  assert.equal(saveName({ name: 'x', headers: [['Content-Disposition', "attachment; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd"]] }), 'passwd');
  assert.equal(saveName({ name: '', headers: [] }), 'response.bin');
  assert.equal(saveName({ name: 'Export', headers: [['Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']] }), 'export.xlsx');
});

test('tlsDefault: the class policy, then the env file, else checked', () => {
  assert.match(tlsDefault({ cls: 'dev', env_file_tls: null, insecure_by_class: true }), /not checked - dev/);
  assert.match(tlsDefault({ cls: 'test', env_file_tls: false, insecure_by_class: false }), /SSLConfiguration/);
  assert.equal(tlsDefault({ cls: 'test', env_file_tls: null, insecure_by_class: false }), 'checked');
  assert.match(tlsDefault({ cls: 'test', env_file_tls: null, insecure_by_class: false, tunnel: { label: 'ssh x -> y:1' } }), /through a tunnel/);
});

test('tunnelName: the chip names the server, never the user or the ssh port', () => {
  assert.equal(tunnelName({ kind: 'ssh', via: 'projects@test-k.example:2222', to: '192.0.2.1:443' }), 'ssh test-k.example');
  assert.equal(tunnelName({ kind: 'ssh', via: 'test-k.example' }), 'ssh test-k.example');
  assert.equal(tunnelName({ kind: 'stack', via: 'stack', to: 'test/app' }), 'stack test/app');
  assert.equal(tunnelName({}), 'tunnel');
  assert.match(tlsDefault({ cls: 'test', env_file_tls: null, insecure_by_class: false, tunnel: { label: 'ssh x' } }), /tunnel/);
});

test('N4: the HTTP view shows a bidi control in a request name / URL / file / body / test as a mark and names no agent tool', async () => {
  const { readFileSync } = await import('node:fs');
  const js = (f) => readFileSync(new URL(`../../piper/web/dist/${f}`, import.meta.url), 'utf8');
  const panel = js('httppanel.js');
  const view = js('httpview.js');
  for (const s of ["'hpreqname' }, showBidi(r.name)", "'fpath' }, showBidi(file)", 'hpurl\' }, showBidi(', 'const name = showBidi(f.file']) assert.ok(panel.includes(s), s);
  for (const s of ['showBidi(r.name)', 'showBidi(tk.t)', 'showBidi(t.name)', 'showBidi(t.message)', 'showBidi(r.error)']) assert.ok(view.includes(s), s);
  assert.ok(!/title: '[^']*http_last_result/.test(view), 'a tooltip names http_last_result');
});

test('N4: an environment name with a bidi control shows as a mark (select, Run all on…, Production · … title)', () => {
  assert.equal(shortEnv('st‮age'), 'st[U+202E]age');
  assert.equal(shortEnv('prod'), 'prod');
});

test('N10-C-9: Environment settings opens on the chosen tunnel and keeps the focus on a radio it redraws (Enter on SSH dropped it to body)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/httpview.ts', import.meta.url), 'utf8');
  const dlg = src.slice(src.indexOf('async function envDialog('), src.indexOf('const stale = () =>'));
  assert.match(dlg, /const paintMode = \(\) => \{\s*const had = seg\.contains\(document\.activeElement\);/);
  assert.match(dlg, /if \(had\) seg\.querySelector<HTMLElement>\('\[aria-checked=true\]'\)\?\.focus\(\);/);
  assert.match(dlg, /const paintCert = \(\) => \{\s*const had = certEl\.contains\(document\.activeElement\);/);
  assert.match(dlg, /if \(had\) certEl\.querySelector<HTMLElement>\('\[aria-checked=true\]'\)\?\.focus\(\);/);
  assert.match(dlg, /queueMicrotask\(\(\) => seg\.querySelector<HTMLElement>\('\[aria-checked=true\]'\)\?\.focus\(\)\);/, 'the dialog starts on the tunnel choice');
});

test('N10-C-10: the .http list says so when the filter matches nothing (it went blank, only "0 of 1 file" up top)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/httppanel.ts', import.meta.url), 'utf8');
  assert.match(src, /if \(!shown\.length && files\.length\) return void listEl\.append\(h\('p', \{ class: 'mut small' \}, showBidi\(`No file, request or URL matches "\$\{filterEl\.value\.trim\(\)\}"\.`\)\)\);/,
    'only with files loaded - before they arrive (a session switch) it would say No ... matches ""');
});

// ---- F09: the HTTP tab only when it makes sense, one empty state with one action
test('F09 httpTabWanted: the module on and a .http file - or the tab is the one shown (it never vanishes under the user)', () => {
  assert.equal(httpTabWanted(true, true), true);
  assert.equal(httpTabWanted(false, true), false);
  assert.equal(httpTabWanted(false, true, true), true);
  assert.equal(httpTabWanted(true, false), false, 'the module off hides it always');
  assert.equal(httpTabWanted(true, false, true), false);
});

test('F09 newRequestFiles: development/<project>.http with GET {{host}}/ and an env file with an empty dev.host', () => {
  const n = newRequestFiles('grp/acme-app');
  assert.equal(n.dir, 'development');
  assert.equal(n.file, 'development/acme-app.http');
  assert.equal(n.text, 'GET {{host}}/\n');
  assert.equal(n.env, 'development/http-client.env.json');
  assert.deepEqual(JSON.parse(n.envText), { dev: { host: '' } });
  assert.equal(newRequestFiles('java/shop').file, 'development/shop.http');
  assert.equal(newRequestFiles('a/../..').file, 'development/requests.http', 'no path tricks from a project key');
  assert.equal(newRequestFiles('x/we ird name').file, 'development/we-ird-name.http');
});

test('F09 wiring: no .http file = the list hidden and one empty state; the env file is never replaced', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/httppanel.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /'No \.http files in this project\.'/, 'the list has no second empty state');
  assert.doesNotMatch(src, /'Nothing to run yet\.'/);
  assert.match(src, /el\.classList\.toggle\('hp-none', none\)/);
  assert.match(src, /h\('button', \{ class: 'primary'[^]*?'New request file…'\)/);
  const create = src.slice(src.indexOf('async function createFirst('), src.indexOf('/** the left list shows'));
  assert.match(create, /api\.fsCreate\(p, n\.dir, 'dir'\)\.catch\(\(e\) => \{ if \(!exists\(e\)\) throw e; \}\)/, 'development/ may exist');
  assert.match(create, /api\.fsCreate\(p, n\.env, 'file', n\.envText\)\.catch\(\(e\) => \{ if \(!exists\(e\)\) throw e; \}\)/, 'an existing env file stays (fsCreate never replaces)');
  assert.match(create, /picked\.set\(p, n\.file\);\s*await load\(true\);/, 'the new file is shown at once');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const blk = css.slice(css.indexOf('/* ==== 9.66 F09'), css.indexOf('/* ==== end F09 ==== */'));
  assert.match(blk, /\.httppanel\.hp-none > \.hpleft \{ display: none; \}/);
  assert.doesNotMatch(blk, /font-size: \d|border-radius: [1-9]/);
});


test('HTTP references select a request from its heading and body without running it', async () => {
  const {requestForLine} = await import('../../piper/web/dist/httppanel.js');
  const requests = [{index:0,start:0,line:1},{index:1,start:3,line:4}];
  for(const n of [0,1,2,3]) assert.equal(requestForLine(requests,n).index,0);
  for(const n of [4,5,9]) assert.equal(requestForLine(requests,n).index,1);
  assert.equal(requestForLine([],10),undefined);
});

 test('env JSON editor accepts multiple environments and preserves typed values', () => {
  const text = '{"dev":{"port":8080,"enabled":false,"nested":{"host":"example.test"}},"prod":{"token":"placeholder"}}';
  assert.deepEqual(parseEnvDocument(text), JSON.parse(text));
  for (const invalid of ['[]', 'null', '{"dev":12}', '{"dev":[]}', '{secret-text']) {
    assert.throws(() => parseEnvDocument(invalid), error => !error.message.includes('secret-text'));
  }
});
