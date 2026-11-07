import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

class Element extends EventTarget {
  constructor(tag) {
    super(); this.tagName = tag; this.children = []; this.attrs = new Map(); this.className = ''; this.value = ''; this.disabled = false;
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      add: name => this.classList.toggle(name, true),
      remove: name => this.classList.toggle(name, false),
      toggle: (name, on = !this.classList.contains(name)) => {
        this.className = [...this.className.split(' ').filter(n => n && n !== name), ...(on ? [name] : [])].join(' ');
      },
    };
  }
  setAttribute(name, value) { this.attrs.set(name, String(value)); if (name === 'disabled') this.disabled = true; if (name === 'value') this.value = value; }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  get textContent() { return this.children.map(n => n.textContent).join(''); }
  set textContent(text) { this.children = [{ textContent: text }]; }
  get childNodes() { return this.children; }
  click() { if (!this.disabled) this.dispatchEvent(new Event('click')); }
  focus() {}
}
const descendants = node => node instanceof Element ? [node, ...node.children.flatMap(descendants)] : [];
const elements = roots => roots.flatMap(descendants);
const button = (roots, text) => elements(roots).find(n => n.tagName === 'button' && n.textContent === text);
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
globalThis.window = { localStorage: { getItem: () => 'fixture', setItem() {}, removeItem() {} } };
const { createMoveUI, moveView, moveDisabled, moveServers, movedReason } = await import('../../piper/web/dist/move.js');
const { hosts, scoped } = await import('../../piper/web/dist/environment.js');
const { refreshHost } = await import('../../piper/web/dist/api.js');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const transfer = (overrides = {}) => ({ id: 'transfer', environment: 'build', mode: 'wait', project: 'acme/shop', phase: 'requested', status: 'running', at: { requested: 1 }, ...overrides });
const session = (overrides = {}) => ({ id: 'local', kind: 'project', project: 'acme/shop', title: 'Shop', branch: 'feature', ...overrides });
const server = (id = 'build', overrides = {}) => ({ host: { id, mode: 'service', label: id, host: 'build.example.com', project_path: '/srv/projects' }, online: true, checkedAt: 1, etag: '', error: '', capabilities: { transfer: true }, ...overrides });
function harness(s = session()) {
  hosts.clear(); hosts.set('build', server());
  const modals = [], errors = [], opened = [], cards = [];
  let closed = 0, changed = 0;
  const ui = createMoveUI({ modal: (title, ...content) => modals.push({ title, content }), closeModal: () => closed++, toast: text => errors.push(text), changed: () => changed++, session: id => id === s.id ? s : undefined, open: id => opened.push(id), showCard: id => cards.push(id) });
  return { ui, s, modals, errors, opened, cards, get closed() { return closed; }, get changed() { return changed; }, get content() { return modals.at(-1)?.content ?? []; } };
}

test('every backend transfer phase maps to the authored tone, step and recovery action', () => {
  const cases = [
    ['requested', 'running', 'run', 0, undefined, 'waiting for the agent to finish its turn'],
    ['handoff_sent', 'running', 'run', 0, undefined, 'the agent is writing HANDOFF.md'],
    ['waiting_send', 'running', 'warn', 0, 'send', 'you are typing in its prompt – the request waits'],
    ['blocked_on_card', 'running', 'warn', 0, 'card', 'answer its card first – the request goes right after'],
    ...['stopped', 'committed', 'pushed'].map(phase => [phase, 'running', 'run', 3, undefined, 'pushing wip: handoff local – confirm the push card']),
    ['remote_sending', 'running', 'run', 4, undefined, 'starting the session on Server…'],
    ['remote_unknown', 'halted', 'warn', 4, 'check', 'Server did not answer – it may have started the session'],
    ['committed', 'halted', 'bad', 3, 'continue', 'stopped: the push was declined'],
    ['pushed', 'halted', 'bad', 4, 'continue', 'Server refused: push declined'],
  ];
  for (const [phase, status, tone, step, action, text] of cases) {
    const v = moveView(transfer({ phase, status, error: 'push declined' }), 'local');
    assert.deepEqual([v.tone, v.step, v.action, v.text], [tone, step, action, text], phase + '/' + status);
  }
  assert.match(moveView(transfer({ phase: 'stopped', status: 'halted', error: 'hook failed' }), 'local').text, /hook failed/);
  assert.doesNotMatch(moveView(transfer({ phase: 'committed', status: 'halted', error: 'network failed' }), 'local').text, /declined/);
});

test('the menu distinguishes locality, capability, an existing move, and the moved lock', () => {
  assert.equal(moveDisabled(session(), [server()]), '');
  assert.equal(moveDisabled(session(), [server('old', { capabilities: {} })]), 'Update service first');
  assert.equal(moveDisabled(session(), [server('loading', { capabilities: undefined })]), 'Checking server capabilities…');
  assert.match(moveDisabled(session(), []), /Add a Piper service/);
  assert.match(moveDisabled(session({ id: scoped('build', 'session', 'local') }), [server()]), /Only local project/);
  assert.match(moveDisabled(session({ kind: 'advisor' }), [server()]), /Only local project/);
  assert.match(moveDisabled(session({ project: '' }), [server()]), /Only local project/);
  assert.match(moveDisabled(session({ transfer: transfer() }), [server()]), /already/);
  assert.equal(moveDisabled(session({ transfer: transfer({ phase: 'remote_unknown', status: 'halted' }) }), [server()]), 'Check on Server first');
  const moved = session({ moved: { environment: 'build', session: 'remote', at: 1 } });
  assert.equal(moveDisabled(moved, []), movedReason(moved));
  assert.match(movedReason(moved), /Locked here/);
  assert.equal(movedReason(session()), '');
  assert.deepEqual(moveServers([server(), server('old', { capabilities: {} }), server('ssh', { host: { mode: 'ssh' } })]).map(r => r.host.id), ['build']);
});

test('remote capability discovery uses the explicit host and does not block state refresh', async () => {
  hosts.clear(); hosts.set('build', server('build', { capabilities: {} }));
  const paths = [];
  globalThis.fetch = async (path, init) => {
    paths.push(path); assert.equal(init.headers['X-Piper-Environment'], 'build');
    return new Response(JSON.stringify(path === '/api/ping' ? { capabilities: { transfer: true } } : { sessions: [], pending: [], projects: [] }));
  };
  await refreshHost('build'); await tick();
  assert.deepEqual(paths, ['/api/state', '/api/ping']);
  assert.equal(hosts.get('build').capabilities.transfer, true);
  await refreshHost('build');
  assert.equal(paths.filter(p => p === '/api/ping').length, 1);
});

test('dialog defaults to wait, omits a single server picker and loads real git facts', async () => {
  const h = harness();
  globalThis.fetch = async () => new Response(JSON.stringify({ branch: 'feature', files: [{ path: 'app.ts' }, { path: 'test.ts' }] }));
  await h.ui.confirm(h.s);
  assert.equal(h.modals[0].title, 'Move to Server?');
  assert.equal(elements(h.content).filter(n => n.tagName === 'select').length, 0);
  const radio = elements(h.content).filter(n => n.getAttribute('role') === 'radio');
  assert.deepEqual(radio.map(n => n.getAttribute('aria-checked')), ['true', 'false']);
  assert.match(elements(h.content).map(n => n.textContent).join(' '), /Branch feature · 2 uncommitted files/);
  assert.equal(button(h.content, 'Move to Server').disabled, false);
});

test('starting accepts the current backend raw record and never replays a refused request', async () => {
  const h = harness(); let posts = 0;
  globalThis.fetch = async (path, init) => {
    if (init.method === 'GET') return new Response('{"branch":"feature","files":[]}');
    posts++; return new Response(JSON.stringify(transfer()));
  };
  await h.ui.confirm(h.s); button(h.content, 'Move to Server').click(); await tick();
  assert.equal(h.s.transfer.id, 'transfer'); assert.equal(posts, 1);
  const refused = harness();
  globalThis.fetch = async (path, init) => {
    if (init.method === 'GET') return new Response('{"branch":"feature","files":[]}');
    posts++; return new Response('{"error":"not allowed"}', { status: 400 });
  };
  await refused.ui.confirm(refused.s); button(refused.content, 'Move to Server').click(); await tick();
  assert.deepEqual(refused.errors, ['not allowed']); assert.equal(posts, 2);
  assert.equal(refused.modals.length, 1);
});

test('409 names hidden paths and only Move anyway repeats the exact selected server/timing with consent', async () => {
  const h = harness(); hosts.set('second', server('second'));
  const writes = [];
  globalThis.fetch = async (path, init) => {
    if (init.method === 'GET') return new Response('{"branch":"feature","files":[]}');
    const body = JSON.parse(init.body); writes.push([path, body]);
    return body.confirm_hidden ? new Response(JSON.stringify({ transfer: transfer({ environment: body.environment, mode: body.mode }) }))
      : new Response('{"error":"hidden conversation","hidden_paths":["private/settings.txt",".env"]}', { status: 409 });
  };
  await h.ui.confirm(h.s);
  const select = elements(h.content).find(n => n.tagName === 'select');
  select.value = 'second'; select.dispatchEvent(new Event('change'));
  elements(h.content).find(n => n.getAttribute('role') === 'radio' && n.textContent.startsWith('Now')).click();
  button(h.content, 'Move to Server').click(); await tick();
  assert.equal(writes.length, 1);
  assert.match(h.content[0].textContent, /Their content may be in the handoff Server gets:/);
  assert.deepEqual(elements(h.content).filter(n => n.tagName === 'code').map(n => n.textContent), ['private/settings.txt', '.env']);
  button(h.content, 'Move anyway').click(); await tick();
  assert.deepEqual(writes, [
    ['/api/sessions/local/move', { environment: 'second', mode: 'now' }],
    ['/api/sessions/local/move', { environment: 'second', mode: 'now', confirm_hidden: true }],
  ]);
  assert.equal(h.errors.length, 0);
});

test('cancel hidden consent, an ordinary error and a detached HEAD never submit a move', async () => {
  const h = harness(); let posts = 0;
  globalThis.fetch = async (path, init) => {
    if (init.method === 'GET') return new Response('{"branch":"feature","files":[]}');
    posts++; return new Response('{"error":"hidden","hidden_paths":[]}', { status: 409 });
  };
  await h.ui.confirm(h.s); button(h.content, 'Move to Server').click(); await tick();
  assert.match(h.content[0].textContent, /cannot be named/);
  button(h.content, 'Cancel').click(); await tick(); assert.equal(posts, 1);
  globalThis.fetch = async () => new Response('{"branch":"","files":[]}');
  await h.ui.confirm(h.s); assert.equal(button(h.content, 'Move to Server').disabled, true);
  globalThis.fetch = async () => new Response('{"error":"git unavailable"}', { status: 503 });
  await h.ui.confirm(h.s); assert.match(h.content[0].textContent, /git unavailable/);
  assert.equal(button(h.content, 'Move to Server').disabled, true);
});

test('Send now only clears input, Show card reveals its session and progress has five steps', async () => {
  const h = harness(session({ transfer: transfer({ phase: 'waiting_send' }) })); const writes = [];
  globalThis.fetch = async (path, init) => { writes.push([path, init.method]); return new Response('{"ok":true}'); };
  let bar = h.ui.bar(h.s);
  assert.equal(elements([bar]).filter(n => n.tagName === 'li').length, 5);
  button([bar], 'Send now').click(); await tick();
  assert.deepEqual(writes, [['/api/sessions/local/input-clear', 'POST']]);
  h.s.transfer.phase = 'blocked_on_card'; bar = h.ui.bar(h.s);
  button([bar], 'Show card').click(); assert.deepEqual(h.cards, ['local']);
  h.s.transfer = transfer({ phase: 'committed', at: { stopped: 1, committed: 2 } });
  bar = h.ui.bar(h.s);
  assert.deepEqual(elements([bar]).filter(n => n.tagName === 'li').map(n => n.className), ['done', 'done', 'done', 'current', '']);
});

test('unknown outcome only checks and reads the result; it never replays a move', async () => {
  const h = harness(session({ transfer: transfer({ phase: 'remote_unknown', status: 'halted' }) })); const calls = [];
  globalThis.fetch = async (path, init) => {
    calls.push([init.method, path]);
    return new Response(JSON.stringify(init.method === 'GET' ? { transfer: transfer({ phase: 'moved', status: 'done' }), moved: { environment: 'build', session: 'remote', at: 123 } } : { found: true }));
  };
  const bar = h.ui.bar(h.s); assert.equal(button([bar], 'Continue move'), undefined);
  button([bar], 'Check on Server').click(); await tick();
  assert.deepEqual(calls, [['POST', '/api/sessions/local/move/check'], ['GET', '/api/sessions/local/move']]);
  assert.equal(h.s.moved.session, 'remote');
  button([h.ui.bar(h.s)], 'Open on Server').click();
  assert.deepEqual(h.opened, [scoped('build', 'session', 'remote')]);
});

test('a check finding nothing exposes only an explicit Continue move, with the recorded destination', async () => {
  const h = harness(session({ transfer: transfer({ phase: 'remote_unknown', status: 'halted', mode: 'now', project: 'acme/remote' }) })); const writes = [];
  globalThis.fetch = async (path, init) => {
    if (init.method === 'GET') return new Response(JSON.stringify({ transfer: transfer({ phase: 'pushed', status: 'halted', mode: 'now', project: 'acme/remote' }), moved: null }));
    writes.push([path, JSON.parse(init.body)]); return new Response('{}');
  };
  button([h.ui.bar(h.s)], 'Check on Server').click(); await tick();
  assert.equal(writes.length, 1);
  button([h.ui.bar(h.s)], 'Continue move').click(); await tick();
  assert.deepEqual(writes[1], ['/api/sessions/local/move', { environment: 'build', mode: 'now', remote_project: 'acme/remote' }]);
});

test('duplicate recovery clicks send once and a failed check never starts a transfer', async () => {
  const h = harness(session({ transfer: transfer({ phase: 'remote_unknown', status: 'halted' }) }));
  let resolve, calls = 0;
  globalThis.fetch = () => { calls++; return new Promise(r => { resolve = r; }); };
  const check = button([h.ui.bar(h.s)], 'Check on Server');
  check.click(); check.click(); assert.equal(calls, 1);
  resolve(new Response('{"error":"unavailable"}', { status: 502 })); await tick();
  assert.deepEqual(h.errors, ['unavailable']); assert.equal(calls, 1);
  assert.ok(button([h.ui.bar(h.s)], 'Check on Server'));
});

test('an unrecognized move response leaves the last known session intact without a retry', async () => {
  const h = harness(); let writes = 0;
  globalThis.fetch = async (path, init) => {
    if (init.method === 'GET') return new Response('{"branch":"feature","files":[]}');
    writes++; return new Response('{}');
  };
  await h.ui.confirm(h.s); button(h.content, 'Move to Server').click(); await tick();
  assert.equal(writes, 1); assert.equal(h.s.transfer, undefined);
  assert.match(h.errors[0], /outcome is not confirmed/);
});

test('incoming manual-stop warning dismisses only client state', () => {
  const incoming = { id: 'transfer', source: 'local', computer: 'Workstation', commit: 'abcdef handoff', manual_stop: true };
  const h = harness(session({ id: scoped('build', 'session', 'remote'), transferred_from: incoming }));
  const bar = h.ui.bar(h.s);
  assert.match(bar.textContent, /Moved here from Workstation · abcdef · HANDOFF.md/);
  assert.match(bar.textContent, /stopped by hand: no HANDOFF.md/);
  assert.ok(bar.classList.contains('move-run'), 'a quiet strip');
  assert.ok(!bar.classList.contains('move-warn'));
  assert.ok(elements([bar]).some(n => n.className === 'warn small' && /stopped by hand/.test(n.textContent)), 'only the manual-stop line warns');
  button([bar], 'Dismiss').click(); assert.equal(h.ui.bar(h.s), null);
  assert.equal(h.s.transferred_from, incoming);
  assert.ok(harness(h.s).ui.bar(h.s), 'a new client can still see the server provenance');
});

test('a moved Composer target disables Send, preserves the draft and refuses keyboard delivery', async () => {
  const { createComposer } = await import('../../piper/web/dist/composer.js');
  window.setInterval = () => 1;
  const sent = [], errors = [];
  let locked = false;
  const composer = createComposer({ current: () => 'local', title: () => 'Shop', others: () => [],
    sendTo: async (...args) => { sent.push(args); return true; }, busy: () => false, live: () => !locked,
    blocked: () => locked ? 'Locked here. This session continues on Server.' : '', confirm() {}, toast: text => errors.push(text) });
  composer.put('Keep this draft');
  const send = button([composer.el], 'Send');
  const ta = elements([composer.el]).find(n => n.tagName === 'textarea');
  locked = true; composer.sync();
  assert.equal(send.disabled, true);
  assert.match(send.title, /Locked here/);
  // A keyboard handler or retained button cannot bypass the current lock.
  send.dispatchEvent(new Event('click')); await tick();
  assert.equal(ta.value, 'Keep this draft'); assert.deepEqual(sent, []);
  assert.match(errors[0], /Locked here/);
});

test('the header menu reads the current session: a kept header must not offer a stale move or switch', () => {
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  const header = main.slice(main.indexOf('function header('), main.indexOf('function layoutControl('));
  assert.match(header, /'aria-label': 'more actions'[\s\S]*?openRailMenu\(session\(s\.id\) \?\? s, /);
  assert.match(main, /if \(nextHead\.outerHTML !== headEl\.outerHTML\) \{/, 'the reason: the header is replaced only when its HTML changes');
});
