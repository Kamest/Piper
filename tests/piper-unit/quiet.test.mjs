import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as Quiet from '../../piper/web/dist/quiet.js';
import { QUIET_SCOPE, createQuiet, quietBadge, quietOwner, quietStatus } from '../../piper/web/dist/quiet.js';
import { moduleOn } from '../../piper/web/dist/setup.js';

const active = (phase, participants = []) => ({ id: 'q1', owner: 'a', title: 'Atlas', minutes: 15, phase, participants });
test('a module enabled by default does not imply an active quiet window', () => {
  assert.equal(moduleOn({}, 'quiet'), true);
  assert.equal(moduleOn({ features: { modules: { quiet: false } } }, 'quiet'), false);
  assert.equal(quietBadge({ active: null }, 'a'), '');
  assert.equal(quietBadge(undefined, 'a'), '');
});
test('waiting counts confirmed sessions; finishing and unknown are never ready', () => {
  const a = active('draining', [{ id: 'b', status: 'waiting' }, { id: 'c', status: 'finishing' }, { id: 'd', status: 'unknown' }]);
  assert.equal(quietStatus(a).label, 'Asking 3 sessions to pause · 1 of 3 waiting');
  assert.equal(quietBadge({ active: a }, 'a'), 'Owner');
  assert.equal(quietBadge({ active: a }, 'b'), 'Waiting');
  assert.equal(quietBadge({ active: a }, 'c'), 'Delivering');
  assert.equal(quietBadge({ active: a }, 'd'), 'Needs checking');
});
test('ready, working and lost supervision have distinct truthful states', () => {
  assert.equal(quietStatus(active('ready')).step, 2);
  assert.equal(quietStatus(active('working')).step, 3);
  assert.equal(quietStatus(active('recovering')).tone, 'warn');
  assert.match(quietStatus(active('recovering')).label, /before your window started/);
  assert.equal(quietStatus(null).step, 4);
  assert.match(QUIET_SCOPE, /outside Piper may keep running/);
});

test('observed background work keeps its actual source in the participant row', () => {
  const value = Quiet.quietParticipant({ id: 'b', status: 'finishing', at: 1000, detail: 'Background work is still running', confirmed_by: 'Piper observed' }, { ...active('draining'), now: 1001 });
  assert.equal(value.label, 'Finishing');
  assert.equal(value.source, 'Piper observed');
});

class Element extends EventTarget {
  constructor(tag) { super(); this.tagName = tag; this.children = []; this.attrs = {}; }
  setAttribute(key, value) { this.attrs[key] = value; if (key === 'value') this.value = value; }
  append(...nodes) { for (const node of nodes) { this.children.push(node); node.parent = this; } }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  get firstChild() { return this.children[0]; }
  get textContent() { return this.children.map(n => n.textContent).join(''); }
}
const descendants = node => node.children.flatMap(child => child instanceof Element ? [child, ...descendants(child)] : []);
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
const find = (root, tag, text) => descendants(root).find(n => n.tagName === tag && (text === undefined || n.textContent === text));

test('menu flow has no duration cap and never pauses until submitted; Cancel never submits', async () => {
  const calls = []; let closed = 0, seen = false;
  const view = createQuiet({ state: () => ({ active: null }), sessions: () => [], seen: () => seen, markSeen: () => { seen = true; },
    act: async body => { calls.push(body); }, close: () => { closed++; }, open() {}, confirm() {}, error() {}, changed() {} });
  view.open('a', true);
  assert.match(view.el.textContent, /Need the machine for heavy work/);
  find(view.el, 'button', 'Request a quiet window').dispatchEvent(new Event('click'));
  const input = find(view.el, 'input'); input.value = 'Full build'; input.dispatchEvent(new Event('input'));
  assert.doesNotMatch(view.el.textContent, /Up to|10 min|15 min|30 min/);
  assert.match(view.el.textContent, /Until you release it/);
  const cancel = find(view.el, 'button', 'Cancel');
  assert.equal(cancel.attrs.type, 'button');
  cancel.dispatchEvent(new Event('click'));
  assert.equal(closed, 1);
  assert.equal(calls.length, 0);
  find(view.el, 'form').dispatchEvent(new Event('submit', { cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls, [{ action: 'request', session: 'a', reason: 'Full build' }]);
});

test('first-use help never masks an already active window', () => {
  const state = { active: active('ready'), queue: [], operations: [] };
  const view = createQuiet({ state: () => state, sessions: () => [], seen: () => false, markSeen() {}, act: async () => {}, close() {}, open() {}, confirm() {}, error() {}, changed() {} });
  view.open('a');
  assert.match(view.el.textContent, /Atlas can start work/);
  assert.doesNotMatch(view.el.textContent, /Need the machine for heavy work/);
  assert.match(view.banner(() => {}).textContent, /Atlas can start work/);
  state.active.phase = 'recovering';
  assert.match(view.banner(() => {}).textContent, /Piper lost track/);
});

test('legacy sessions can be checked manually; renewal is explicit and never revives recovery', async () => {
  const calls = []; let confirmation;
  const state = { active: active('draining', [{ id: 'b', title: 'API', status: 'finishing', stale: true }]), now: 1000, queue: [], operations: [] };
  const view = createQuiet({ state: () => state, sessions: () => [], seen: () => true, markSeen() {},
    act: async body => { calls.push(body); }, close() {}, open() {},
    confirm: (title, body, run, label) => { confirmation = { title, body, run, label }; }, error() {}, changed() {} });
  view.open('a');
  find(view.el, 'button', 'Confirm waiting…').dispatchEvent(new Event('click'));
  assert.equal(calls.length, 0, 'opening confirmation must not acknowledge');
  assert.equal(confirmation.label, 'Confirm waiting');
  assert.match(confirmation.body, /background tasks and subagents/);
  confirmation.run();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls, [{ action: 'ack', id: 'q1', session: 'b', finished: true }]);
  state.active.lease_until = 100; state.active.now = 100; view.update();
  find(view.el, 'button', 'Still needed').dispatchEvent(new Event('click'));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls[1], { action: 'renew', id: 'q1', session: 'a' });
  state.active.phase = 'recovering';
  view.update();
  assert.equal(find(view.el, 'button', 'Still needed'), undefined);
});

test('a dispatching start cannot be cancelled and an uncertain record needs explicit reconciliation', async () => {
  const calls = []; let confirm;
  const state = { active: null, queue: [], starts: [{ id: 'once', project: 'demo', phase: 'checking' }] };
  const view = createQuiet({ state: () => state, sessions: () => [], seen: () => true, markSeen() {},
    act: async body => { calls.push(body); }, close() {}, open() {},
    confirm: (title, body, run) => { confirm = run; }, error() {}, changed() {} });
  view.open('a');
  assert.ok(Object.hasOwn(find(view.el, 'button', 'Remove queued start').attrs, 'disabled'));
  state.starts[0].phase = 'uncertain'; view.update();
  find(view.el, 'button', 'Remove queued start').dispatchEvent(new Event('click'));
  assert.equal(calls.length, 0);
  confirm();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls, [{ action: 'cancel_start', id: 'once', checked: true }]);
});


test('an incomplete running controller cannot accept a reservation from a stale open form', () => {
  const calls = [], errors = [];
  const view = createQuiet({ state: () => null, sessions: () => [], seen: () => true, markSeen() {},
    act: async body => calls.push(body), close() {}, open() {}, confirm() {}, error: message => errors.push(message), changed() {} });
  view.open('a', true);
  const input = find(view.el, 'input'); input.value = 'Full build'; input.dispatchEvent(new Event('input'));
  assert.equal(find(view.el, 'button', 'Request quiet window').disabled, true);
  find(view.el, 'form').dispatchEvent(new Event('submit', { cancelable: true }));
  assert.deepEqual(calls, []);
  assert.match(errors[0], /Restart Piper/);
});

test('every active phase exposes the requesting session and reason outside Details and opens that session', () => {
  for (const phase of ['draining', 'settling', 'ready', 'working', 'recovering']) {
    const opened = [], state = { active: { ...active(phase), reason: 'Full build' }, queue: [], operations: [] };
    const view = createQuiet({ state: () => state, sessions: () => [{ id: 'a', title: 'Atlas', state: 'working' }],
      seen: () => true, markSeen() {}, act: async () => {}, close() {}, open: id => opened.push(id), confirm() {}, error() {}, changed() {} });
    view.open('b');
    const context = descendants(view.el).find(node => node.attrs['data-quiet-owner'] === 'a');
    assert.ok(context, phase + ': owner context missing');
    assert.match(context.textContent, /Atlas/);
    assert.match(context.textContent, /Full build/);
    for (let node = context.parent; node; node = node.parent) assert.notEqual(node.tagName, 'details');
    const link = find(view.el, 'button', 'Open Atlas');
    assert.ok(link, phase + ': navigation missing');
    link.dispatchEvent(new Event('click'));
    assert.deepEqual(opened, ['a']);
    assert.match(view.banner(() => {}).textContent, /Atlas/);
  }
});

test('owner navigation follows renames, keeps ended sessions inspectable and preserves removed-owner context', () => {
  const opened = [], owner = { id: 'a', title: 'Atlas', state: 'working' };
  let rows = [owner];
  const state = { active: { ...active('ready'), reason: 'Data import' }, queue: [], operations: [] };
  const view = createQuiet({ state: () => state, sessions: () => rows, seen: () => true, markSeen() {},
    act: async () => {}, close() {}, open: id => opened.push(id), confirm() {}, error() {}, changed() {} });
  view.open('b');
  owner.title = 'Renamed owner'; view.update();
  assert.ok(find(view.el, 'button', 'Open Renamed owner'));
  assert.match(view.banner(() => {}).textContent, /Renamed owner/);
  owner.state = 'exited'; state.active.phase = 'recovering'; view.update();
  find(view.el, 'button', 'Open to inspect').dispatchEvent(new Event('click'));
  assert.deepEqual(opened, ['a']);
  rows = []; view.update();
  const context = descendants(view.el).find(node => node.attrs['data-quiet-owner'] === 'a');
  assert.match(context.textContent, /Atlas/);
  assert.match(context.textContent, /Data import/);
  const missing = find(view.el, 'button', 'Open Atlas');
  assert.ok(!missing || missing.disabled === true || Object.hasOwn(missing.attrs, 'disabled'));
});

test('released context identifies its last owner without implying an active reservation', () => {
  const last = { ...active('released'), reason: 'Full build' };
  assert.equal(quietOwner(null, null, []), null);
  const owner = quietOwner(null, last, [{ id: 'a', title: 'Current name', agent: 'codex', state: 'waiting' }]);
  assert.deepEqual(owner, { id: 'a', name: 'Current name', meta: 'Codex', reason: 'Full build', role: 'Last owner', tag: '', canOpen: true });
  const state = { active: null, last, starts: [{ id: 'queued', phase: 'checking' }] };
  const view = createQuiet({ state: () => state, sessions: () => [], seen: () => true, markSeen() {},
    act: async () => {}, close() {}, open() {}, confirm() {}, error() {}, changed() {} });
  view.open('b');
  assert.match(view.el.textContent, /Last ownerAtlasRemovedFull build/);
  assert.match(view.el.textContent, /Window released/);
  assert.match(view.banner(() => {}).textContent, /Last owner: Atlas/);
});


test('participant progress separates delivery, no response, fresh progress and stale updates', () => {
  const a = { ...active('draining'), selected: 1000, now: 1300 };
  const p = { id: 'b', title: 'Compiler', status: 'finishing', at: null, detail: '' };
  assert.equal(Quiet.quietParticipant(p, a, 1).label, 'Delivering');
  assert.equal(Quiet.quietParticipant({ ...p, delivered_at: 1290 }, a, 1).label, 'Request sent');
  assert.equal(Quiet.quietParticipant({ ...p, delivered_at: 1180 }, a, 1).label, 'No response');
  assert.match(Quiet.quietParticipant({ ...p, delivered_at: 1185, stale: true }, a, 1).detail, /2 min ago/);
  assert.equal(quietStatus({ ...a, participants: [{ ...p, stale: true }] }).title, 'Still waiting for other sessions.');
  const progress = { ...p, at: 1275, detail: 'Stopping existing compiler' };
  const fresh = Quiet.quietParticipant(progress, a, 1);
  assert.equal(fresh.label, 'Finishing');
  assert.match(fresh.detail, /Stopping existing compiler.*25 s ago/);
  assert.equal(Quiet.quietParticipant({ ...progress, at: 1180 }, a, 1).label, 'No update');
  assert.equal(Quiet.quietParticipant({ ...p, status: 'waiting' }, a, 1).label, 'Waiting');
  assert.equal(Quiet.quietParticipant({ ...p, status: 'waiting' }, a, 3).label, 'Waiting');
  assert.equal(Quiet.quietParticipant({ ...p, status: 'unknown' }, a, 1).label, 'Needs checking');
  assert.equal(Quiet.quietParticipant(p, null, 4).label, 'Notice pending');
  assert.equal(Quiet.quietParticipant(progress, a, 1, 900000).label, 'Finishing', 'server clock wins over wrong client clock');
});

test('recovery before grant stays at Wait but an ever granted window always requires checked release', () => {
  assert.equal(quietStatus(active('recovering')).step, 1);
  assert.equal(quietStatus({ ...active('recovering'), ready: 123, work: false }).step, 3);
  const calls = [], opened = [], state = { active: active('recovering'), queue: [], operations: [] };
  const view = createQuiet({ state: () => state, sessions: () => [{ id: 'b', title: 'Compiler', state: 'working' }],
    seen: () => true, markSeen() {}, act: async b => calls.push(b), close() {}, open: id => opened.push(id), confirm() {}, error() {}, changed() {} });
  view.open('a');
  assert.ok(find(view.el, 'button', 'Cancel request'));
  state.active.ready = 123;
  view.update();
  assert.equal(find(view.el, 'button', 'Cancel request'), undefined);
  assert.ok(find(view.el, 'button', 'Release after checking…'));
});

test('participant inspection is read-only and exposes full long titles without claiming running after release', () => {
  const title = 'Very long compiler session '.repeat(15), opened = [], calls = [];
  const state = { active: { ...active('draining'), participants: [{ id: 'b', title, status: 'finishing', at: 123, detail: 'Waiting for existing child test' }] }, queue: [], operations: [] };
  const view = createQuiet({ state: () => state, sessions: () => [{ id: 'b', title, state: 'working' }],
    seen: () => true, markSeen() {}, act: async b => calls.push(b), close() {}, open: id => opened.push(id), confirm() {}, error() {}, changed() {} });
  view.open('a');
  const name = descendants(view.el).find(n => n.className === 'quiet-participant-name');
  assert.equal(name.attrs.title, title);
  const link = descendants(view.el).find(n => n.tagName === 'button' && n.attrs['aria-label'] === 'Open session ' + title);
  link.dispatchEvent(new Event('click'));
  assert.deepEqual(opened, ['b']);
  assert.deepEqual(calls, []);
  state.last = { ...state.active, phase: 'released' }; state.active = null;
  view.update();
  assert.match(view.el.textContent, /Notice pending/);
  assert.doesNotMatch(view.el.textContent, /Running again/);
});


test('running Piper operations explain why confirmed sessions do not yet grant the window', () => {
  const state = { active: active('draining', [{ id: 'b', title: 'API', status: 'waiting' }]), queue: [], operations: [{ session: '', kind: 'stack job' }] };
  const view = createQuiet({ state: () => state, sessions: () => [], seen: () => true, markSeen() {}, act: async () => {}, close() {}, open() {}, confirm() {}, error() {}, changed() {} });
  view.open('a');
  assert.match(view.el.textContent, /Waiting for Piper jobs to end/);
  assert.equal(quietStatus(state.active).step, 1);
});

test('who can work remains truthful in preparation, work, recovery and released pending delivery', () => {
  const state = { active: active('draining'), queue: [], operations: [] };
  const view = createQuiet({ state: () => state, sessions: () => [{ id: 'a', title: 'Atlas', state: 'working' }],
    seen: () => true, markSeen() {}, act: async () => {}, close() {}, open() {}, confirm() {}, error() {}, changed() {} });
  view.open('a');
  const who = () => descendants(view.el).find(n => n.className === 'quiet-who').textContent;
  assert.match(who(), /Nobody yet.*0 of 0 waiting/);
  for (const phase of ['ready', 'working']) {
    state.active.phase = phase; view.update();
    assert.match(who(), /Can workAtlasOthers0 of 0 waiting/);
    assert.doesNotMatch(view.el.textContent, /up to.*min|Time left|fixed end/);
  }
  state.active.phase = 'recovering'; state.active.ready = 100; view.update();
  assert.match(who(), /Check needed/);
  state.last = { ...state.active, participants: [{ id: 'b', title: 'API', resume: { status: 'pending', detail: 'Draft input' } }] };
  state.active = null; view.update();
  assert.match(who(), /All sessions.*1 pending/);
  assert.match(view.el.textContent, /Draft input/);
  assert.equal(find(view.el, 'button', 'Confirm waiting…'), undefined);
  state.last.participants[0].resume = { status: 'sent' }; view.update();
  assert.match(who(), /All sessions.*1 notices delivered/);
  assert.doesNotMatch(view.el.textContent, /Running again|May continue/);
});


test('confirmed sessions never offer manual acknowledgment and retain their actual report', () => {
  const state = { active: active('draining', [{ id: 'b', title: 'API', status: 'waiting', detail: 'Compiler ended', at: 100 }]), queue: [] };
  const view = createQuiet({ state: () => state, sessions: () => [], seen: () => true, markSeen() {}, act: async () => {}, close() {}, open() {}, confirm() {}, error() {}, changed() {} });
  view.open('a');
  assert.equal(find(view.el, 'button', 'Confirm waiting…'), undefined);
  assert.match(view.el.textContent, /Compiler ended/);
  assert.equal(Quiet.quietParticipant(state.active.participants[0], state.active, 1).label, 'Waiting');
});


const depsFor = (state, extra = {}) => ({ state: () => state, sessions: () => [{ id: 'a', title: 'Atlas', agent: 'claude', state: 'working' }, { id: 'b', title: 'API', state: 'working' }], seen: () => true, markSeen() {}, act: async () => {}, close() {}, open() {}, confirm() {}, error() {}, changed() {}, ...extra });
test('first-use request while another owner holds quiet keeps the requester and reaches the form', () => {
  const view = createQuiet(depsFor({ active: active('working'), queue: [] }, { seen: () => false }));
  view.open('b', true);
  assert.match(view.el.textContent, /Requesting fromAPI/);
  find(view.el, 'button', 'Request a quiet window').dispatchEvent(new Event('click'));
  assert.ok(find(view.el, 'input'));
  assert.match(view.el.textContent, /Requesting fromAPI/);
});
test('null-like fields and invalid dates never leak into any window state', () => {
  for (const phase of ['queued', 'draining', 'settling', 'ready', 'working', 'recovering', 'released']) {
    for (const bad of [null, undefined, NaN, Infinity, Number.MAX_VALUE]) {
      const r = { ...active(phase, [{ id: 'b', title: null, status: 'waiting', detail: null, at: bad }]), title: null, reason: null, minutes: bad, now: bad, lease_until: bad };
      const state = { active: phase === 'released' ? null : r, last: r, queue: [], events: [{ seq: 1, window: 'q1', at: bad, title: null, source: null, detail: null, kind: null }] };
      const view = createQuiet(depsFor(state)); view.open('a');
      assert.doesNotMatch(view.el.textContent, /\b(null|undefined|NaN|Infinity|Invalid Date)\b/);
    }
  }
  assert.equal(Quiet.quietDuration(null), 'Until released');
  assert.equal(Quiet.quietDuration(NaN), 'Duration unavailable');
  assert.equal(Quiet.quietTime(Number.MAX_VALUE), 'Not recorded');
});
test('fresh finishing and waiting have no manual action; only stale unconfirmed participants do', () => {
  const state = { active: active('draining', [{ id: 'b', title: 'API', status: 'finishing', stale: false }]), queue: [] };
  const view = createQuiet(depsFor(state)); view.open('a');
  assert.equal(find(view.el, 'button', 'Confirm waiting…'), undefined);
  state.active.participants[0].stale = true; view.update();
  assert.ok(find(view.el, 'button', 'Confirm waiting…'));
  state.active.participants[0].status = 'waiting'; view.update();
  assert.equal(find(view.el, 'button', 'Confirm waiting…'), undefined);
});
test('queued requester sees its own position and cancels exactly its request', async () => {
  const calls = [], state = { active: active('working', [{ id: 'b', status: 'waiting' }]), queue: [{ ...active('queued'), id: 'mine', owner: 'b', title: 'API' }] };
  assert.equal(quietBadge(state, 'b'), 'Queued');
  const view = createQuiet(depsFor(state, { act: async b => calls.push(b) })); view.open('b');
  assert.match(view.el.textContent, /you’re next/);
  assert.match(view.el.textContent, /Current holder.*Your request/);
  assert.match(view.el.textContent, /SessionPositionDetailSource.*Holding.*Next · #1/);
  assert.match(view.el.textContent, /Next · #1.*Not recorded/);
  let selected;
  const banner = view.banner(id => { selected = id; });
  assert.match(banner.textContent, /Queued · you’re next/);
  find(banner, 'button').dispatchEvent(new Event('click'));
  assert.equal(selected, 'b');
  assert.equal(find(view.el, 'button', 'Release window'), undefined);
  find(view.el, 'button', 'Cancel my request').dispatchEvent(new Event('click'));
  await new Promise(r => setTimeout(r, 0));
  assert.deepEqual(calls, [{ action: 'cancel', id: 'mine' }]);
});

test('released summary does not claim a new request is being prepared', () => {
  const state = { active: null, last: { ...active('released'), owner: 'a' }, queue: [] };
  const view = createQuiet(depsFor(state)); view.open('a');
  assert.match(view.banner(() => {}, true).textContent, /Window released/);
  assert.doesNotMatch(view.banner(() => {}, true).textContent, /Setting up a request/);
});

test('a recovered legacy window explains its partial history without inventing earlier events', () => {
  const state = { active: active('recovering'), queue: [], events: [{ seq: 1, window: 'q1', kind: 'recovering', at: 1000 }] };
  const view = createQuiet(depsFor(state)); view.open('a');
  assert.match(view.el.textContent, /Earlier activity for this window was not recorded/);
  assert.doesNotMatch(view.el.textContent, /Requested a quiet window/);
});
test('heartbeat-only polls keep the interactive panel DOM intact', () => {
  const state = { active: { ...active('working'), now: 100, lease_until: 180 }, queue: [] };
  const view = createQuiet(depsFor(state)); view.open('a'); const first = view.el.firstChild;
  state.active.now = 115; state.active.lease_until = 205; view.update();
  assert.equal(view.el.firstChild, first);
});
test('request owner is openable before submit; update warning preserves the draft', () => {
  const state = { active: null, queue: [] }, opened = []; let outdated = false;
  const view = createQuiet(depsFor(state, { open: id => opened.push(id), outdated: () => outdated })); view.open('b', true);
  find(view.el, 'button', 'Open API').dispatchEvent(new Event('click'));
  assert.deepEqual(opened, ['b']);
  const input = find(view.el, 'input'); input.value = 'Long import'; input.dispatchEvent(new Event('input'));
  outdated = true; view.update();
  assert.ok(find(view.el, 'button', 'Reload'));
  assert.equal(find(view.el, 'input').value, 'Long import');
  assert.ok(find(view.el, 'button', 'Request quiet window').disabled);
});

test('an exception makes the window shared, never quiet, and says who keeps working', () => {
  const ready = active('ready', [{ id: 'b', title: 'API', status: 'exempt', exempt: 'allowed', at: 1000 }, { id: 'c', title: 'Docs', status: 'waiting' }]);
  assert.equal(quietStatus(ready).label, 'Shared · Atlas can work, API keeps working');
  assert.equal(quietStatus(ready).tone, 'warn');
  assert.equal(quietStatus({ ...ready, phase: 'draining' }).label, 'Asking 1 sessions to pause · 1 of 1 waiting');
  assert.equal(quietBadge({ active: ready }, 'b'), 'Exception');
  const returning = active('working', [{ id: 'b', title: 'API', status: 'finishing', exempt: 'returning', rev: 2, confirmed_by: 'operator', at: 1000 }]);
  assert.equal(quietStatus(returning).label, 'Shared · API is stopping its work');
  assert.equal(Quiet.quietParticipant(returning.participants[0], returning).detail, 'Exception removed · must stop and confirm again');
  const reported = { ...returning.participants[0], confirmed_by: 'agent report', detail: 'stopping benchmark' };
  assert.equal(Quiet.quietParticipant(reported, returning).detail, 'Exception removed · stopping benchmark');
  assert.equal(Quiet.quietParticipant({ ...reported, status: 'unknown' }, returning).label, 'Needs checking');
});

test('the operator grants and removes an exception only after confirming its consequence', async () => {
  const calls = [], asked = [];
  const state = { active: { ...active('ready', [{ id: 'b', title: 'API', status: 'waiting', at: 1000 }]), now: 1001 }, queue: [], events: [] };
  const view = createQuiet(depsFor(state, { act: async b => calls.push(b), confirm: (title, text, run, label) => { asked.push({ title, text, label }); run(); } }));
  view.open('a');
  find(view.el, 'button', 'Let continue…').dispatchEvent(new Event('click'));
  await new Promise(r => setTimeout(r));
  assert.deepEqual(calls.pop(), { action: 'exempt', id: 'q1', session: 'b', allow: true });
  assert.match(asked[0].text, /shared, not quiet, so Atlas’s results may be affected/);
  assert.equal(asked[0].label, 'Let API continue');
  state.active.participants[0] = { id: 'b', title: 'API', status: 'exempt', exempt: 'allowed', at: 1000 };
  view.update();
  assert.match(view.el.textContent, /Not quiet: API has an exception/);
  assert.match(view.el.textContent, /Also workingAPI · exception/);
  find(view.el, 'button', 'Remove exception…').dispatchEvent(new Event('click'));
  assert.deepEqual(calls.pop(), { action: 'exempt', id: 'q1', session: 'b', allow: false });
  assert.match(asked[1].text, /must stop its own work and confirm again/);
});

test('a silent session whose exception was removed can be confirmed by the operator in a ready window', async () => {
  const calls = [];
  const state = { active: active('working', [{ id: 'b', title: 'API', status: 'finishing', exempt: 'returning', rev: 4, stale: false }, { id: 'c', title: 'Docs', status: 'finishing', stale: true }]), queue: [] };
  const view = createQuiet(depsFor(state, { act: async b => calls.push(b), confirm: (title, text, run) => run() })); view.open('a');
  assert.equal(find(view.el, 'button', 'Confirm waiting…'), undefined);
  state.active.participants[0].stale = true; view.update();
  assert.match(view.el.textContent, /API · no recent update/);
  assert.doesNotMatch(view.el.textContent, /Docs · no recent update/);
  find(view.el, 'button', 'Confirm waiting…').dispatchEvent(new Event('click'));
  await new Promise(r => setTimeout(r));
  assert.deepEqual(calls.pop(), { action: 'ack', id: 'q1', session: 'b', finished: true });
});

test('exception states never leak null-like values and released windows offer no exception', () => {
  for (const bad of [null, undefined, NaN, Infinity]) {
    for (const exempt of ['allowed', 'returning']) {
      const r = { ...active('working', [{ id: 'b', title: null, status: exempt === 'allowed' ? 'exempt' : 'finishing', exempt, rev: bad, detail: null, at: bad, confirmed_by: null }]), title: null, now: bad };
      const view = createQuiet(depsFor({ active: r, last: null, queue: [], events: [{ seq: 1, window: 'q1', at: bad, kind: 'exempt_granted', title: null, detail: null, source: null }] }));
      view.open('a');
      assert.doesNotMatch(view.el.textContent, /\b(null|undefined|NaN|Infinity|Invalid Date)\b/);
    }
  }
  const view = createQuiet(depsFor({ active: null, last: { ...active('released', [{ id: 'b', title: 'API', status: 'waiting' }]) }, queue: [], events: [] }));
  view.open('a');
  assert.equal(find(view.el, 'button', 'Let continue…'), undefined);
});
