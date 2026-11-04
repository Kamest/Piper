import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../piper/web/dist/restartui.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');

function fixture() {
  const timers = new Map(), views = []; let serial = 0, observations = 0;
  class Node {
    constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.isConnected = true; }
    setAttribute(k, v) { this.attrs[k] = v; }
    addEventListener() {}
    append(...nodes) { nodes.forEach(n => { n.parent = this; this.children.push(n); }); }
    prepend(n) { n.parent = this; this.children.unshift(n); }
    remove() { this.isConnected = false; if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() { document.activeElement = this; }
    setSelectionRange(start, end, direction) { Object.assign(this, { selectionStart: start, selectionEnd: end, selectionDirection: direction }); }
  }
  const body = new Node('body'), draft = new Node('textarea'); draft.setSelectionRange(2, 7, 'forward'); body.append(draft);
  const document = { body, activeElement: draft, createElement: tag => new Node(tag), getSelection: () => null, getElementById: () => null };
  const window = { setTimeout: (fn, ms) => { timers.set(++serial, { fn, ms }); return serial; }, clearTimeout: id => timers.delete(id) };
  const h = (tag, attrs, ...children) => { const n = new Node(tag); Object.assign(n.attrs, attrs); n.append(...children.filter(x => typeof x === 'object')); return n; };
  class RestartCheck {
    constructor(changed) { this.changed = changed; this.status = null; }
    observeOutage() {
      observations++; this.status = { kind: 'outage', checking: true, reachable: false, phase: 'waiting', finding: 'Checking.' };
      this.changed(this.status);
    }
    clearOutage() { this.status = null; }
    answerOutage() { this.status = { ...this.status, reachable: true, checking: false, phase: 'returned' }; this.changed(this.status); }
  }
  const context = vm.createContext({ window, document, h, RestartCheck, markBidi: s => s, restartLogo: () => 'blob:retained-logo',
    createRestartView: actions => {
      const view = { actions, el: new Node('view'), dispose() {}, update(s) { this.status = s; } };
      views.push(view); return view;
    } });
  vm.runInContext(source, context);
  const fire = ms => { const [id, timer] = [...timers].find(([, t]) => t.ms === ms); timers.delete(id); timer.fn(); };
  return { context, timers, views, fire, body, draft, document, observations: () => observations };
}

test('short outage stays silent, repeated failures keep the grace deadline and recovery cancels all UI timers', () => {
  const f = fixture(); f.context.connectionLost(); f.context.connectionLost();
  assert.equal(f.observations(), 1); assert.equal(f.timers.size, 2);
  assert.equal(f.body.children.length, 1); assert.equal(f.views.length, 0);
  f.context.connectionRestored();
  assert.equal(f.timers.size, 0); assert.equal(f.body.children.length, 1);
  assert.equal(f.document.activeElement, f.draft);
});

test('neutral connection notice gives way to one diagnostic overlay; verified recovery retains draft focus and selection', () => {
  const f = fixture(); f.context.connectionLost(); f.fire(2000);
  const notice = f.body.children[0];
  assert.equal(notice.attrs.class, 'connection-notice'); assert.equal(notice.attrs.role, 'status');
  assert.equal(f.views.length, 0);
  f.fire(5000);
  assert.equal(notice.isConnected, false); assert.equal(f.views.length, 1);
  assert.equal(f.body.children.at(-1).open, true);
  f.context.connectionRestored();
  assert.equal(f.views[0].status.phase, 'returned');
  f.views[0].actions.close();
  assert.equal(f.document.activeElement, f.draft);
  assert.equal(f.draft.selectionStart, 2); assert.equal(f.draft.selectionEnd, 7);
  assert.equal(f.body.children.length, 1);
});

test('hiding unavailable diagnostics leaves a neutral route back instead of reopening the modal', () => {
  const f = fixture(); f.context.connectionLost(); f.fire(5000);
  f.views[0].actions.close();
  assert.equal(f.body.children[0].attrs.class, 'connection-notice');
  assert.equal(f.body.children[0].children[1].textContent, 'Piper is not answering');
  assert.equal(f.views.length, 1);
  f.context.connectionRestored();
  assert.equal(f.body.children.length, 1);
});

test('a second outage after a recovered card starts new checks and a fresh grace period', () => {
  const f = fixture(); f.context.connectionLost(); f.fire(5000);
  f.context.connectionRestored();
  assert.equal(f.views[0].status.reachable, true);
  f.context.connectionLost();
  assert.equal(f.observations(), 2);
  assert.equal(f.body.children.length, 1, 'the previous online card must not remain over a new outage');
  assert.equal(f.timers.size, 2);
  f.fire(5000);
  assert.equal(f.views.length, 2); assert.equal(f.views[1].status.reachable, false);
});

test('outage identity retains the product logo before the service disappears; packaged Desktop needs no fetch', async () => {
  const code = readFileSync(new URL('../../piper/web/dist/restartview.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
  let requests = 0;
  const asset = {};
  const context = vm.createContext({ location: { protocol: 'http:' }, AbortSignal,
    fetch: async (path, options) => {
      requests++; assert.equal(path, '/piper/icon.svg'); assert.ok(options.signal);
      return { ok: true, blob: async () => asset };
    }, URL: { createObjectURL: blob => { assert.equal(blob, asset); return 'blob:retained-product'; } } });
  vm.runInContext(code, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext('restartLogo()', context), 'blob:retained-product');
  context.fetch = () => assert.fail('an outage must not refetch its logo');
  assert.equal(vm.runInContext('restartLogo()', context), 'blob:retained-product');
  assert.equal(requests, 1);
  vm.runInNewContext(code, { location: { protocol: 'file:' }, fetch: () => assert.fail('packaged view cannot depend on the service') });
});
