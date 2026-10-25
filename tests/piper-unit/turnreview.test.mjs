import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildQueue } from '../../piper/web/dist/queue.js';
import { createTurnReview, revertDisabled, revertFileLabel, reviewFoot } from '../../piper/web/dist/turnreview.js';
import { mergeAudit } from '../../piper/web/dist/board.js';

const summary = (files = 2, add = 3, del = 1) => ({ files, add, del, turns: 2 });
const session = (id, extra = {}) => ({ id, kind: 'project', project: 'acme/shop', path: '/example/shop', title: 'Example changes',
  state: 'waiting', created: 1, since: 2, pid: 1, hidden: false, at_prompt: true, review_pending: true, review_summary: summary(), ...extra });
const input = (sessions, actions = []) => ({ sessions, actions, events: [], seen: 0, board: new Map(), unseen: new Set(), pins: new Set(), order: [], label: x => x, now: 100 });
const action = (level, id) => ({ key: `${level}:${id}`, level, source: 'session', subject: { session: id, key: id }, actions: [], title: 'Example', body: '', since: 2 });

test('To review replaces generic waiting, sorts by change size, hides for pending decisions/running turns', () => {
  const big = session('big', { review_summary: summary(3, 100, 12) }), small = session('small');
  const queue = buildQueue(input([big, small], [action('waiting', 'small')]));
  assert.deepEqual(queue.review.map(r => r.session), ['small', 'big']);
  assert.equal(queue.needs.length, 0);
  assert.equal(queue.running.length, 0);
  assert.match(queue.review[0].status, /2 files · \+3 −1 · 2 turns/);
  assert.doesNotMatch(queue.review[0].status, /tests/);
  assert.equal(buildQueue(input([small], [action('confirm', 'small')])).review.length, 0);
  assert.equal(buildQueue(input([{ ...small, state: 'working' }])).review.length, 0);
  assert.equal(buildQueue(input([{ ...small, review_pending: false }])).review.length, 0);
  assert.match(buildQueue(input([session('tested', { review_summary: { ...summary(), tests: { status: 'passed', turn: 2 } } })])).review[0].status, /tests passed/);
});

test('running/starting turns disable Revert; reverted rows remain restorable, unknown tests stay absent', () => {
  assert.equal(revertDisabled(session('s', { state: 'working' }), []), true);
  assert.equal(revertDisabled(session('s'), [{ n: 3, to: null }]), true);
  assert.equal(revertDisabled(session('s'), [{ n: 2, to: 'tree', reverted: true }]), false);
  assert.equal(revertDisabled(session('s'), [{ n: 1, to: null }, { n: 2, to: 'tree' }]), false, 'an old incomplete snapshot is not a running turn');
  assert.equal(reviewFoot(summary(), 1), '1 note');
  assert.equal(reviewFoot({ ...summary(), tests: { status: 'passed', turn: 5 } }, 1), 'Tests passed after turn 5 · 1 note');
  assert.equal(revertFileLabel({ path: 'new.txt', removed: true, added_in: 4 }, 3), 'removed (added in turn 4)');
  assert.equal(revertFileLabel({ path: 'app.txt', removed: false }, 3), 'back to turn 3');
});

class Text {
  constructor(text) { this.textContent = text; }
  remove() { this.parent?.children.splice(this.parent.children.indexOf(this), 1); }
}
class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.className = ''; this.listeners = {}; this.dataset = {}; this.value = ''; }
  get firstChild() { return this.children[0]; }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(text) { this.children = [new Text(text)]; }
  append(...children) { for (let c of children) { if (typeof c === 'string') c = new Text(c); c.parent = this; this.children.push(c); } }
  prepend(c) { c.parent = this; this.children.unshift(c); }
  remove() { this.parent?.children.splice(this.parent.children.indexOf(this), 1); }
  after(c) { c.parent = this.parent; this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, c); }
  setAttribute(k, v) { this.attrs[k] = v; if (k.startsWith('data-')) this.dataset[k.slice(5)] = String(v); }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  querySelectorAll(selector) { return all(this).filter(e => selector.startsWith('.') ? e.className.split(' ').includes(selector.slice(1)) : selector === '[data-file]' && e.dataset.file); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  classList = { add: c => { this.className += ' ' + c; } };
  focus() {}
}
const all = el => el.children.flatMap(c => c instanceof El ? [c, ...all(c)] : []);
const tick = () => new Promise(resolve => setImmediate(resolve));

test('real Review render: newest turns, diff line note, batch send, confirmation, Restore and Undo', async () => {
  const previous = { document: globalThis.document, window: globalThis.window, localStorage: globalThis.localStorage, fetch: globalThis.fetch };
  const storage = new Map();
  globalThis.localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) };
  globalThis.document = { createElement: tag => new El(tag), createTextNode: text => new Text(text) };
  globalThis.window = { localStorage, setTimeout, clearTimeout };
  const turns = [{ n: 1, from: 'a', to: 'b', prompt: 'First change', started: 1, ended: 2, summary: summary(0, 0, 0) },
    { n: 2, from: 'b', to: 'c', prompt: 'Second change', started: 3, ended: 4, summary: summary(1) }];
  const requests = [];
  const row = { path: 'src/app.txt', status: 'M', sensitive: false, withheld: false, work_sha: 'version', add: 1, del: 1 };
  let dialog, toast, done = false, failRevert = false;
  globalThis.fetch = async (url, init = {}) => {
    const path = new URL(url, 'http://example.com').pathname, body = init.body ? JSON.parse(init.body) : null;
    requests.push({ path, body });
    let data;
    if (path.endsWith('/turns')) data = { turns };
    else if (path.endsWith('/review/diff')) return new Response('diff --git a/src/app.txt b/src/app.txt\n--- a/src/app.txt\n+++ b/src/app.txt\n@@ -1 +1 @@\n-before\n+after\n');
    else if (path.endsWith('/review/revert') && !body) data = { n: 1, undo: '', current: 'now', files: [{ path: 'app.txt', removed: false }, { path: 'new.txt', removed: true, added_in: 2 }] };
    else if (path.endsWith('/review/revert')) {
      if (failRevert) return new Response(JSON.stringify({ error: 'Cannot restore \u202efile' }), { status: 409 });
      turns[1].reverted = true; data = { ok: true, n: 1, undo: 'undo1' };
    }
    else if (path.endsWith('/review/dispatch')) data = { sent: true };
    else if (path.endsWith('/review/done')) { done = true; data = { ok: true }; }
    else data = { id: 'review', root: '/example/shop', files: [row], turns, note: '' };
    return new Response(JSON.stringify(data));
  };
  try {
    const view = createTurnReview({ session: () => session('s', { path: '/example/shop/src' }), modal: (title, box) => { dialog = { title, box }; }, closeModal() {}, toast: (text, action) => { toast = { text, action }; }, changed() {} });
    view.show('s'); await tick(); await tick();
    assert.match(view.el.textContent, /All turns since the session started/);
    assert.deepEqual(all(view.el).filter(e => e.dataset.turn).map(e => e.dataset.turn), ['2', '1']);
    const emptyTurn = all(view.el).find(e => e.dataset.turn === '1');
    assert.match(emptyTurn.textContent, /no files/);
    assert.doesNotMatch(emptyTurn.textContent, /0 files|\+0|−0/);
    assert.equal(all(view.el).find(e => e.textContent === 'Send notes to the agent').attrs.disabled, '');
    assert.doesNotMatch(view.el.textContent, /Send 0 notes/);
    assert.match(view.el.textContent, /before.*after/);
    const noteButton = all(view.el).find(e => e.attrs.title === 'Note on src/app.txt line 1' && !e.attrs.disabled);
    noteButton.listeners.click();
    all(view.el).find(e => e.tagName === 'textarea').value = 'Handle the empty case.';
    all(view.el).find(e => e.tagName === 'button' && e.textContent === 'Add note').listeners.click();
    assert.match(view.el.textContent, /1 note/);
    assert.ok(all(view.el).find(e => e.textContent === 'Send 1 note to the agent'));
    noteButton.listeners.click();
    all(view.el).find(e => e.tagName === 'textarea').value = 'Keep the fallback.';
    all(view.el).find(e => e.tagName === 'button' && e.textContent === 'Add note').listeners.click();
    await all(view.el).find(e => e.tagName === 'button' && e.textContent === 'Send 2 notes to the agent').listeners.click();
    await tick();
    assert.equal(all(view.el).find(e => e.textContent === 'Send notes to the agent').attrs.disabled, '');
    assert.match(requests.find(r => r.path.endsWith('/dispatch')).body.text, /app.txt:1/);
    assert.equal(requests.find(r => r.path.endsWith('/dispatch')).body.items[0].path, 'app.txt');
    all(view.el).find(e => e.className === 'mini tr-revert' && e.parent.dataset.turn === '1').listeners.click(); await tick();
    assert.match(dialog.title, /end of turn 1/);
    assert.match(dialog.box.textContent, /Your current files are saved first/);
    assert.match(dialog.box.textContent, /index are not touched/);
    assert.match(dialog.box.textContent, /removed \(added in turn 2\)/);
    await all(dialog.box).find(e => e.textContent === '↶ Revert files').onclick(); await tick();
    assert.equal(toast.action.label, 'Undo');
    assert.match(view.el.textContent, /Restore/);
    all(view.el).find(e => e.className === 'mini tr-revert' && e.textContent === 'Restore').listeners.click(); await tick();
    assert.equal(dialog.title, 'Restore turn 2?');
    assert.match(dialog.box.textContent, /These files come back from turn 2:/);
    assert.doesNotMatch(dialog.box.textContent, /Later changes are undone/);
    assert.match(dialog.box.textContent, /Your current files are saved first/);
    assert.match(dialog.box.textContent, /index are not touched/);
    assert.match(dialog.box.textContent, /agent gets a note/);
    failRevert = true;
    await all(dialog.box).find(e => e.textContent === 'Restore files').onclick();
    assert.match(dialog.box.textContent, /Cannot restore \[U\+202E\]file/);
    assert.doesNotMatch(dialog.box.textContent, /\u202e/);
    failRevert = false;
    await all(dialog.box).find(e => e.textContent === 'Restore files').onclick(); await tick();
    assert.equal(toast.text, 'Restored turn 2');
    toast.action.run(); await tick();
    assert.equal(dialog.title, 'Undo revert?');
    assert.ok(all(dialog.box).find(e => e.textContent === 'Undo revert'));
    all(view.el).find(e => e.textContent === 'Done reviewing').listeners.click(); await tick();
    assert.equal(done, true);
  } finally { Object.assign(globalThis, previous); }
});

test('History keeps each Undo entry and tab/queue placement follows the approved boards', () => {
  const events = [1, 2].map(n => ({ event: 'turn-revert', session: 's', ts: `2026-01-01T00:00:0${n}`, turn: 3, undo: 'undo' + n }));
  assert.equal(mergeAudit(events).length, 2);
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.ok(main.indexOf("tab('convo', 'Conversation'") < main.indexOf("tab('review', 'Review'"));
  assert.ok(main.indexOf("tab('review', 'Review'") < main.indexOf("tab('files', 'Files'"));
  assert.match(main, /v === 'review' && s.review_pending \? h\('span', \{ class: 'chip link tr-tab-new' \}, 'NEW'\) : null/);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /body:has\(\.turn-review\) #toasts \{ bottom: 88px; \}/);
  assert.match(css, /@media \(max-width: 680px\) \{\s*body:has\(\.turn-review\) #toasts \{ bottom: 128px; \}/);
  const queue = readFileSync(new URL('../../piper/web/src/queue.ts', import.meta.url), 'utf8');
  assert.ok(queue.indexOf("secHead('Blocked'") < queue.indexOf("secHead('To review'"));
  assert.ok(queue.indexOf("secHead('To review'") < queue.indexOf("secHead('Running'"));
  assert.match(queue, /if \(sec.review.length\)/);
});
