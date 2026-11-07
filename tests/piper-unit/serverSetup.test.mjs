import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

class Element extends EventTarget {
  constructor(tag) {
    super(); this.tagName = tag; this.children = []; this.attrs = new Map(); this.className = ''; this.value = ''; this.disabled = false;
    this.classList = { contains: key => this.className.split(' ').includes(key), toggle: (key, on) => {
      on ??= !this.classList.contains(key);
      this.className = [...this.className.split(' ').filter(x => x && x !== key), ...(on ? [key] : [])].join(' ');
    } };
  }
  setAttribute(key, value) {
    this.attrs.set(key, String(value));
    if (key === 'value') this.value = String(value);
    if (key === 'disabled') this.disabled = true;
    if (key === 'hidden') this.hidden = true;
    if (key === 'id') this.id = value;
  }
  getAttribute(key) { return this.attrs.get(key) ?? null; }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(x => x !== node);
      node.parentNode = this; this.children.push(node);
    }
  }
  replaceChildren(...nodes) { for (const node of this.children) node.parentNode = null; this.children = []; this.append(...nodes); }
  get textContent() { return this.children.map(x => x.textContent).join(''); }
  set textContent(text) { this.replaceChildren({ textContent: text }); }
  get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
  getClientRects() { return this.isConnected && !this.hidden ? [{}] : []; }
  querySelector(selector) { return elements(this).slice(1).find(x => matches(x, selector)) ?? null; }
  closest(selector) { return matches(this, selector) ? this : this.parentNode?.closest?.(selector) ?? null; }
  focus() {}
  click() { if (!this.disabled) this.dispatchEvent(new Event('click')); }
}
const elements = node => node instanceof Element ? [node, ...node.children.flatMap(elements)] : [];
const matches = (node, selector) => selector.startsWith('.') ? node.className.split(' ').includes(selector.slice(1))
  : selector.startsWith('#') ? node.id === selector.slice(1)
  : selector.startsWith('[') ? node.attrs.has(selector.slice(1, -1)) : node.tagName === selector;
const button = (node, text) => elements(node).find(x => x.tagName === 'button' && x.textContent === text);
const observers = new Set();
globalThis.MutationObserver = class {
  constructor(fn) { this.fn = fn; }
  observe() { observers.add(this); }
  disconnect() { observers.delete(this); }
};
const mutations = () => { for (const o of [...observers]) o.fn(); };
globalThis.document = Object.assign(new EventTarget(), {
  createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }),
  createElementNS: (_ns, tag) => new Element(tag), hidden: false,
});
document.body = new Element('body');
let timerId = 0;
const timers = new Map(), intervals = new Map(), stored = [];
globalThis.window = {
  localStorage: { getItem: () => 'fixture', setItem: (...args) => stored.push(args), removeItem: (...args) => stored.push(args) },
  setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout: id => timers.delete(id),
  setInterval: (fn, ms) => { const id = ++timerId; intervals.set(id, { fn, ms }); return id; }, clearInterval: id => intervals.delete(id),
};
const copied = [];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => copied.push(text) } } });
const { createServerSetup, setupSummary } = await import('../../piper/web/dist/serverSetup.js');
const { configureMobileComputers } = await import('../../piper/web/dist/transport.js');
const { hosts } = await import('../../piper/web/dist/environment.js');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const base = '/api/environments/build/setup';
const server = { id: 'build', label: 'Server', host: 'dev@build.example.com', mode: 'service', project_path: '/srv/projects' };
const ids = ['service', 'hole', 'git', 'extensions', 'tools', 'projects', 'configuration', 'agents', 'secrets', 'phone'];
const snapshot = (overrides = {}) => ({ environment: 'build', status: 'done', phase: 'done', updated: 1, projects: ['acme/shop'],
  steps: ids.map((id, i) => ({ id, number: i + 1, title: id, state: 'ok', detail: 'Checked ' + id })), ...overrides });
const action = (kind, step = 'hole', body = {}) => ({ kind, method: 'POST', path: `${base}/${step}`, body });
function harness(state = snapshot(), responder, mode = 'service') {
  document.body.replaceChildren(); document.hidden = false; timers.clear(); intervals.clear(); stored.length = copied.length = 0;
  for (const o of [...observers]) o.disconnect();
  hosts.clear(); configureMobileComputers(null);
  let visible = true, modal = null;
  const requests = [], messages = [];
  globalThis.fetch = async (path, init) => {
    const req = { path, method: init.method, body: init.body ? JSON.parse(init.body) : undefined };
    requests.push(req);
    return await responder?.(req) ?? new Response(JSON.stringify(state));
  };
  const closeModal = () => { if (modal) modal.replaceChildren(); document.body.replaceChildren(ui.el); mutations(); };
  const ui = createServerSetup({ ...server, mode }, {
    modal(title, ...content) { if (modal) modal.replaceChildren(); modal = new Element('div'); modal.title = title; modal.append(...content); document.body.append(modal); mutations(); },
    closeModal, changed() {}, toast: text => messages.push(text),
  }, () => visible);
  document.body.append(ui.el);
  return { ui, requests, messages, closeModal, set visible(v) { visible = v; }, get modal() { return modal; } };
}

// Deliberately use the built modules, like environments/move: Claude builds before running this suite.
test('board A renders all five row states, ten rows, header and running progress', async () => {
  const s = snapshot({ status: 'running' });
  ['ok', 'running', 'todo', 'needs_user', 'failed'].forEach((state, i) => s.steps[i].state = state);
  const h = harness(s); h.ui.show('linux'); await tick();
  assert.match(h.ui.el.textContent, /dev@build.example.com · Piper service · linux/);
  assert.match(h.ui.el.textContent, /Step 2 of 10 · hole · 6 done, 1 need you/);
  assert.equal(button(h.ui.el, 'Updating server…').disabled, true);
  assert.equal(elements(h.ui.el).filter(x => x.getAttribute('role') === 'listitem').length, 10);
  for (const state of ['ok', 'running', 'todo', 'needs_user', 'failed']) assert.ok(h.ui.el.querySelector('.server-setup-' + state));
  assert.match(h.ui.el.textContent, /Never copied: logins/);
  assert.match(h.ui.el.textContent, /\.env files and Maven settings move only when you choose Copy in Secrets/);
});

test('board H: the setup log lists what ran newest first and opens after a failure, keeping a user toggle', async () => {
  const s = snapshot({ status: 'halted', log: [
    { at: 1, step: 'service', state: 'running', text: 'Checking server…' },
    { at: 2, step: 'service', state: 'failed', text: 'Python 3.10 or newer is required on the server (found 3.9).' }] });
  s.steps[0].state = 'failed';
  const h = harness(s); h.ui.show('darwin'); await tick();
  const log = h.ui.el.querySelector('.server-setup-log');
  assert.ok(log && log.open, 'opens after a failure');
  assert.match(log.textContent, /Log · 2 entries/);
  const lines = elements(log).filter(x => x.tagName === 'li');
  assert.deepEqual(lines.map(x => x.className), ['server-setup-log-failed', 'server-setup-log-running']);
  assert.match(lines[0].textContent, /service.*failed · Python 3\.10 or newer is required/);
  log.open = false; log.dispatchEvent(new Event('toggle'));
  h.ui.show('darwin'); await tick();
  assert.equal(h.ui.el.querySelector('.server-setup-log').open, false, 'a closed log stays closed on redraw');
  const quiet = harness(snapshot()); quiet.ui.show('linux'); await tick();
  assert.equal(quiet.ui.el.querySelector('.server-setup-log'), null, 'no log, no section');
});

test('fix dispatches its path/body exactly once and never replays a lost response', async () => {
  const s = snapshot(); s.steps[1].state = 'failed'; s.steps[1].action = action('fix', 'hole', { selected: true });
  let reject;
  const h = harness(s, req => req.method === 'POST' ? new Promise((_resolve, fail) => { reject = fail; }) : undefined);
  h.ui.show(); await tick(); const fix = button(h.ui.el, 'Fix'); fix.click(); fix.click();
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
  reject(new Error('Connection lost')); await tick(); await tick();
  assert.deepEqual(h.requests.find(r => r.method === 'POST'), { method: 'POST', path: base + '/hole', body: { selected: true } });
  assert.match(h.ui.el.textContent, /Connection lost/);
  assert.equal(button(h.ui.el, 'Fix').disabled, true);
  assert.ok(button(h.ui.el, 'Check result'));
  assert.equal(timers.size, 0);
});

test('unknown outcomes expose only explicit Check result then Retry actions', async () => {
  const s = snapshot({ status: 'halted', unknown: { step: 'hole', checked: false } });
  s.steps[1].state = 'needs_user'; s.steps[1].action = action('reconcile', 'hole', { reconcile: true });
  const h = harness(s, req => {
    if (req.method === 'POST' && req.body.reconcile) { s.unknown.checked = true; s.steps[1].action = action('retry', 'hole', { retry_unknown: true }); }
  });
  h.ui.show(); await tick(); assert.equal(h.requests.length, 1);
  // Regression: the header kept a disabled "Update server" with no way forward; it now resolves the lost answer.
  assert.equal(button(h.ui.el, 'Update server'), undefined);
  const header = elements(h.ui.el).find(x => x.className === 'server-setup-header');
  assert.equal(button(header, 'Check result').disabled, false);
  const summary = h.ui.summary(); await tick(); assert.ok(button(summary, 'Check result'));
  button(header, 'Check result').click(); await tick();
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
  assert.ok(button(h.ui.el, 'Retry')); button(h.ui.el, 'Retry').click(); await tick();
  assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => r.body), [{ reconcile: true }, { retry_unknown: true }]);
});

test('setup polling uses 2 seconds only while running and visible, without duplicate in-flight reads', async () => {
  const s = snapshot({ status: 'running' }); const h = harness(s); h.ui.show(); await tick();
  assert.equal(timers.size, 1); assert.equal([...timers.values()][0].ms, 2000);
  const fire = async () => { const [id, timer] = [...timers][0]; timers.delete(id); timer.fn(); await tick(); };
  await fire(); assert.equal(h.requests.length, 2);
  h.visible = false; await fire(); assert.equal(h.requests.length, 2); assert.equal(timers.size, 0);
  h.visible = true; document.dispatchEvent(new Event('visibilitychange')); assert.equal(timers.size, 1);
  document.hidden = true; document.dispatchEvent(new Event('visibilitychange')); assert.equal(timers.size, 0);
  document.hidden = false; document.dispatchEvent(new Event('visibilitychange'));
  s.status = 'done'; await fire(); assert.equal(timers.size, 0);
  h.ui.show(); h.ui.show(); await tick(); assert.equal(h.requests.length, 4, 'one read for concurrent show calls');
});

test('collapsed board D reads once without polling and reports readiness or explicit reasons', async () => {
  const s = snapshot(); const h = harness(s); const summary = h.ui.summary(); await tick();
  assert.match(summary.textContent, /Ready · service ✓ · Hole ✓ · extensions ✓ · 1 projects · config current · phone backup ✓/);
  assert.equal(timers.size, 0);
  s.steps[6].state = 'needs_user';
  assert.match(setupSummary(s, true), /Out of date · new Piper version, configuration/);
  hosts.set(server.id, { snapshot: { code_outdated: true } });
  h.ui.refresh(); assert.match(summary.textContent, /Out of date · new Piper version/);
  button(summary, 'Update server').click(); await tick();
  assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => r.path), [base]);
});

test('collapsed board D follows a running pass until it halts, then stops polling', async () => {
  // Regression: the list row read once and stayed on "Updating server…" after the pass halted.
  const s = snapshot({ status: 'running' }); const h = harness(s);
  h.ui.el.replaceChildren(); document.body.replaceChildren(); const summary = h.ui.summary(); document.body.append(summary); await tick();
  assert.match(summary.textContent, /Updating server…/);
  assert.equal(timers.size, 1);
  s.status = 'halted'; s.steps[3].state = 'failed';
  const [id, t] = [...timers][0]; timers.delete(id); t.fn(); await tick();
  assert.match(summary.textContent, /Out of date · extensions/);
  assert.equal(timers.size, 0);
  assert.equal(h.requests.filter(r => r.method === 'GET').length, 2);
});

test('Git fallback shows public key, forge hosts, Copy and exactly one explicit recheck', async () => {
  const s = snapshot(); s.steps[2].action = { kind: 'public_key', public_key: 'ssh-ed25519 synthetic-public-key', hosts: ['forge.example.com'], fix: action('fix', 'git') };
  const h = harness(s); h.ui.show(); await tick(); button(h.ui.el, 'Show key…').click();
  assert.match(h.modal.textContent, /forge.example.com/); assert.match(h.modal.textContent, /ssh-ed25519 synthetic-public-key/);
  button(h.modal, 'Copy').click(); await tick(); assert.deepEqual(copied, ['ssh-ed25519 synthetic-public-key']);
  button(h.modal, 'Check again').click(); await tick();
  assert.equal(h.requests.filter(r => r.method === 'POST' && r.path.endsWith('/git')).length, 1);
});

test('projects picker defaults to active local main projects and posts only chosen keys', async () => {
  const s = snapshot({ updated: null, projects: [] });
  const local = { pending: [], projects: [{ key: 'acme/shop', exists: true, title: 'Shop' }, { key: 'acme/api', exists: true, title: 'API' }, { key: 'acme/shop@work', exists: true }], sessions: [{ project: 'acme/shop@work', state: 'working' }, { project: 'acme/api', state: 'exited' }] };
  const h = harness(s, req => req.path === '/api/state' ? new Response(JSON.stringify(local)) : undefined);
  h.ui.show(); await tick(); button(h.ui.el, 'Choose…').click(); await tick();
  const inputs = elements(h.modal).filter(x => x.tagName === 'input');
  assert.deepEqual(inputs.map(x => [x.value, x.checked]), [['acme/shop', true], ['acme/api', false]]);
  const save = button(h.modal, 'Save projects'); save.click(); save.click(); await tick();
  assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => [r.path, r.body]), [[base + '/projects', { projects: ['acme/shop'] }]]);
});

test('configuration review lists counts, samples, server edits and exclusions; stale apply requires Review again', async () => {
  const s = snapshot(); s.steps[6].action = action('configuration', 'configuration/plan');
  const plan = { plan_id: 'plan-one', added: ['skills/new.md'], changed: ['settings.json'], removed_on_server: ['skills/old.md'], edited_on_server: ['settings.json'], left_out: [{ path: 'development/acme-shop/.env', reason: 'secret or unsafe name' }] };
  let plans = 0;
  const h = harness(s, req => {
    if (req.path.endsWith('/plan')) { plans++; return new Response(JSON.stringify(plan)); }
    if (req.path.endsWith('/apply')) return new Response('{"error":"Configuration plan is stale. Review again."}', { status: 400 });
  });
  const timeouts = [], timeout = AbortSignal.timeout;
  AbortSignal.timeout = ms => { timeouts.push(ms); return timeout.call(AbortSignal, ms); };
  try {
    h.ui.show(); await tick(); timeouts.length = 0; button(h.ui.el, 'Review…').click();
    assert.ok(button(h.ui.el, 'Comparing…'), 'a slow comparison is visible, not a silent disabled button');
    await tick();
  } finally { AbortSignal.timeout = timeout; }
  // Regression: a 24 s comparison over SSH hit the 15 s request limit and the review never opened.
  assert.ok(timeouts[0] >= 120000, String(timeouts));
  assert.match(h.modal.textContent, /added/); assert.match(h.modal.textContent, /removed on server/);
  assert.match(h.modal.textContent, /Edited on the server/); assert.match(h.modal.textContent, /Left out \(1\)/); assert.match(h.modal.textContent, /secret or unsafe name/);
  const copy = button(h.modal, 'Copy 3 files'); copy.click(); copy.click(); await tick(); await tick();
  assert.equal(h.requests.filter(r => r.path.endsWith('/apply')).length, 1);
  assert.deepEqual(h.requests.find(r => r.path.endsWith('/apply')).body, { plan_id: 'plan-one' });
  assert.match(h.modal.textContent, /plan is stale/); assert.equal(copy.disabled, true);
  button(h.modal, 'Review again').click(); await tick(); assert.equal(plans, 2);
});

test('secrets use password fields and set/unset only, clear immediately and never retry or retain values', async () => {
  const s = snapshot(); s.steps[8].action = { kind: 'secrets', method: 'GET', path: base + '/secrets' };
  const data = { secrets: [{ id: 'one', key: 'ACCESS_KEY', label: 'Access key', file: '~/.config/tool.env', state: 'unset' }, { id: 'two', key: 'DATABASE_PASSWORD', label: 'Database password', file: '/srv/projects/acme/shop/.env', state: 'set' }] };
  let reject;
  const h = harness(s, req => req.path.endsWith('/secrets') ? req.method === 'GET' ? new Response(JSON.stringify(data)) : new Promise((_ok, fail) => { reject = fail; }) : undefined);
  h.ui.show(); await tick(); button(h.ui.el, 'Enter…').click(); await tick();
  const inputs = elements(h.modal).filter(x => x.tagName === 'input');
  assert.deepEqual(inputs.map(x => x.getAttribute('type')), ['password', 'password']);
  assert.match(h.modal.textContent, /ACCESS_KEY · unset/); assert.match(h.modal.textContent, /DATABASE_PASSWORD · set/);
  assert.match(h.modal.textContent, /Existing values on the server are kept unless you type a new one/);
  inputs[0].value = 'synthetic-value';
  const form = h.modal.querySelector('form'); form.dispatchEvent(new Event('submit', { cancelable: true })); form.dispatchEvent(new Event('submit', { cancelable: true }));
  assert.deepEqual(inputs.map(x => x.value), ['', '']);
  assert.deepEqual(h.requests.find(r => r.method === 'POST').body, { values: { one: 'synthetic-value' } });
  reject(new Error('synthetic-value must never be echoed')); await tick(); await tick();
  assert.doesNotMatch(h.modal.textContent, /synthetic-value/); assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
  assert.deepEqual(stored, []);
  inputs[0].value = 'discard-me'; h.closeModal(); assert.equal(inputs[0].value, '');
});

test('secrets found in the local .env copy by id only and keep inputs for the rest', async () => {
  const s = snapshot(); s.steps[8].action = { kind: 'secrets', method: 'GET', path: base + '/secrets' };
  const data = { secrets: [{ id: 'one', key: 'ACCESS_KEY', label: 'Access key', file: '~/.config/tool.env', state: 'unset', local: true },
    { id: 'two', key: 'REPO_PASSWORD', label: 'Repository password', file: '~/.m2/settings.xml', state: 'unset', local: false },
    { id: 'three', key: 'PRODUCTION_KEY', label: 'PRODUCTION_KEY', file: '/srv/projects/acme/shop/.env', state: 'unset', local: false, optional: true }] };
  const h = harness(s, req => req.path.endsWith('/secrets') ? new Response(JSON.stringify(req.method === 'GET' ? data : { secrets: [] })) : undefined);
  h.ui.show(); await tick(); button(h.ui.el, 'Enter…').click(); await tick();
  assert.match(h.modal.textContent, /1 value found on this computer/);
  assert.match(h.modal.textContent, /ACCESS_KEY · unset · found on this computer/);
  assert.equal(elements(h.modal).filter(x => x.tagName === 'input').length, 2, 'values missing locally are typed');
  assert.match(h.modal.textContent, /PRODUCTION_KEY · unset · optional, not set on this computer either/);
  const copy = button(h.modal, 'Copy 1 to Server'); copy.click(); copy.click(); await tick(); await tick();
  const posts = h.requests.filter(r => r.method === 'POST');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { values: {}, local: ['one'] });
});

test('server phone QR is transient, counts down, expires, renews explicitly and clears on dismissal', async () => {
  const s = snapshot(); s.steps[9].action = action('pair_phone', 'phone/pair');
  const now = Date.now; let seconds = 1000; Date.now = () => seconds * 1000;
  try {
    const h = harness(s, req => req.path.endsWith('/pair') ? new Response(JSON.stringify({ id: 'invitation', url: 'https://phone.example.com/#single-use', expires: seconds + 60 })) : undefined);
    h.ui.show(); await tick(); const pair = button(h.ui.el, 'Pair phone…'); pair.click(); pair.click(); await tick();
    assert.equal(h.requests.filter(r => r.path.endsWith('/pair')).length, 1);
    assert.ok(h.modal.querySelector('svg')); assert.match(h.modal.textContent, /Expires in 1:00/);
    assert.doesNotMatch(h.modal.textContent, /single-use/);
    assert.match(h.modal.textContent, /Settings › Mobile access › Scan backup code/);
    assert.match(h.modal.textContent, /Not the Camera app/);
    seconds += 60; for (const timer of intervals.values()) timer.fn();
    assert.equal(h.modal.querySelector('svg'), null); assert.match(h.modal.textContent, /QR code expired/);
    assert.equal(h.requests.filter(r => r.path.endsWith('/pair')).length, 1);
    button(h.modal, 'New code').click(); await tick(); assert.ok(h.modal.querySelector('svg'));
    assert.equal(h.requests.filter(r => r.path.endsWith('/pair')).length, 2);
    const qr = h.modal.querySelector('.mobile-qr'); h.closeModal();
    assert.equal(intervals.size, 0); assert.equal(qr.isConnected, false); assert.deepEqual(stored, []);
  } finally { Date.now = now; }
});

test('SSH-only and limited mobile never fetch or expose the setup panel', async () => {
  const ssh = harness(snapshot(), undefined, 'ssh'); ssh.ui.show(); await tick();
  assert.equal(ssh.requests.length, 0); assert.equal(ssh.ui.el.textContent, '');
  const h = harness(); configureMobileComputers({ primary: 'Laptop', fallback: 'Server', limited: true, connected: true, forgetBackup() {} });
  try { h.ui.show(); await tick(); assert.equal(h.requests.length, 0); assert.equal(h.ui.el.textContent, ''); }
  finally { configureMobileComputers(null); }
});

test('phone setup uses the supplied POST action once; pairing is never generated by setup rendering', async () => {
  const s = snapshot(); s.steps[9].state = 'todo'; s.steps[9].action = action('set_up', 'phone');
  const h = harness(s); h.ui.show(); await tick();
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  const start = button(h.ui.el, 'Set up'); start.click(); start.click(); await tick();
  assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => [r.path, r.body]), [[base + '/phone', {}]]);
});

test('a primary setup request starts once, then observes the running snapshot', async () => {
  const s = snapshot({ status: 'idle', updated: null });
  const h = harness(s, req => { if (req.method === 'POST') s.status = 'running'; });
  h.ui.show(); await tick(); const start = button(h.ui.el, 'Set up server'); start.click(); start.click(); await tick();
  assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => [r.path, r.body]), [[base, {}]]);
  assert.equal(button(h.ui.el, 'Updating server…').disabled, true);
  assert.equal(timers.size, 1);
});

test('dismissing a pairing modal during its request discards the late invitation and never renews it', async () => {
  const s = snapshot(); s.steps[9].action = action('pair_phone', 'phone/pair');
  let resolve;
  const h = harness(s, req => req.path.endsWith('/pair') ? new Promise(ok => { resolve = ok; }) : undefined);
  h.ui.show(); await tick(); button(h.ui.el, 'Pair phone…').click();
  const modal = h.modal; h.closeModal();
  resolve(new Response(JSON.stringify({ id: 'late', url: 'https://phone.example.com/#late', expires: Date.now() / 1000 + 60 })));
  await tick();
  assert.equal(modal.querySelector('svg'), null); assert.equal(intervals.size, 0);
  assert.equal(h.requests.filter(r => r.path.endsWith('/pair')).length, 1); assert.deepEqual(stored, []);
});

test('configuration cancel and secret cancel dispatch no writes and secret cancel clears the detached inputs', async () => {
  const s = snapshot();
  s.steps[6].action = action('configuration', 'configuration/plan');
  s.steps[8].action = { kind: 'secrets', method: 'GET', path: base + '/secrets' };
  const h = harness(s, req => {
    if (req.path.endsWith('/plan')) return new Response(JSON.stringify({ plan_id: 'cancel', added: ['skills/new.md'], changed: [], removed_on_server: [], edited_on_server: [], left_out: [] }));
    if (req.path.endsWith('/secrets')) return new Response(JSON.stringify({ secrets: [{ id: 'one', file: '~/.config/tool.env', key: 'ACCESS_KEY', label: 'Access key', state: 'unset' }] }));
  });
  h.ui.show(); await tick(); button(h.ui.el, 'Review…').click(); await tick(); button(h.modal, 'Cancel').click();
  button(h.ui.el, 'Enter…').click(); await tick(); const input = h.modal.querySelector('input'); input.value = 'discard-me';
  button(h.modal, 'Cancel').click();
  assert.equal(input.value, '');
  assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => r.path), [base + '/configuration/plan']);
});

test('agent sign-in presents backend commands without running any of them automatically', async () => {
  const s = snapshot(); s.steps[7].action = { kind: 'terminal', environment: 'build', commands: { claude: ['claude', '/login'], codex: ['codex', 'login', '--device-auth'] } };
  const h = harness(s, req => req.path === '/api/state' ? new Response(JSON.stringify({ sessions: [], pending: [], projects: [] })) : undefined);
  hosts.set('build', { host: server, online: true, checkedAt: 1 });
  h.ui.show(); await tick(); button(h.ui.el, 'Sign in…').click();
  assert.ok(button(h.modal, 'Claude')); assert.ok(button(h.modal, 'Codex'));
  assert.equal(h.requests.length, 1, 'opening the choice never runs a sign-in command');
  button(h.modal, 'Codex').click(); await tick();
  assert.match(h.messages.at(-1), /Choose and set up a project/);
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
});

test('successful configuration apply sends only the reviewed plan and closes after acknowledgement', async () => {
  const s = snapshot(); s.steps[6].action = action('configuration', 'configuration/plan');
  const h = harness(s, req => {
    if (req.path.endsWith('/plan')) return new Response(JSON.stringify({ plan_id: 'reviewed-plan', added: ['skills/new.md'], changed: [], removed_on_server: [], edited_on_server: [], left_out: [] }));
    if (req.path.endsWith('/apply')) return new Response('{"applied":true,"plan_id":"reviewed-plan"}');
  });
  h.ui.show(); await tick(); button(h.ui.el, 'Review…').click(); await tick();
  const copy = button(h.modal, 'Copy 1 files'); copy.click(); copy.click(); await tick(); await tick();
  assert.deepEqual(h.requests.filter(r => r.path.endsWith('/apply')).map(r => r.body), [{ plan_id: 'reviewed-plan' }]);
  assert.equal(h.modal.isConnected, false);
});

test('a detached panel stops polling, and re-entry reads the durable result without a write', async () => {
  const s = snapshot({ status: 'running' }); const h = harness(s); h.ui.show(); await tick();
  document.body.replaceChildren();
  const [id, timer] = [...timers][0]; timers.delete(id); timer.fn(); await tick();
  assert.equal(h.requests.length, 1); assert.equal(timers.size, 0);
  s.status = 'done'; document.body.append(h.ui.el); h.ui.show(); await tick();
  assert.equal(h.requests.length, 2); assert.equal(timers.size, 0);
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
});

test('a newer local GUI build marks a ready snapshot out of date without running setup', async () => {
  const h = harness(); window.__PIPER__ = { guiBuild: 'new-build' };
  hosts.set('build', { host: server, online: true, snapshot: { gui_build: 'old-build' } });
  try {
    const summary = h.ui.summary(); await tick();
    assert.match(summary.textContent, /Out of date · new Piper version/);
    assert.ok(button(summary, 'Update server'));
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  } finally { delete window.__PIPER__; }
});
