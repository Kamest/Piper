import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { api } from '../../piper/web/dist/api.js';
import { createPullControl, openPushDialog } from '../../piper/web/dist/gitwindow.js';
import { commitHeaderTooltips } from '../../piper/web/dist/gitpanel.js';

class Element extends EventTarget {
  constructor(tag) { super(); this.tagName = tag; this.children = []; this.attrs = {}; this.className = ''; this.style = {}; this.isConnected = true; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'disabled') this.disabled = true; if (k === 'id') this.id = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...nodes) { for (let n of nodes) { if (typeof n === 'string') n = document.createTextNode(n); n.parent?.removeChild(n); n.parent = this; this.children.push(n); } }
  removeChild(n) { this.children.splice(this.children.indexOf(n), 1); n.parent = null; }
  remove() { this.parent?.removeChild(this); }
  get firstChild() { return this.children[0]; }
  get textContent() { return this.children.map(n => n.textContent).join(''); }
  set textContent(s) { this.children = [{ textContent: s }]; }
  closest(selector) { return selector === 'button[data-tip]' ? this.tagName === 'button' && this.attrs['data-tip'] ? this : this.parent?.closest?.(selector) ?? null : null; }
  contains(node) { return node === this || this.children.some(c => c === node || c.contains?.(node)); }
  matches(selector) { return selector === ':focus-visible' && !!this.focusVisible; }
  getBoundingClientRect() { return this.rect ?? { left: 240, right: 312, top: 490, bottom: 510, width: 220, height: 68 }; }
  get classList() { return { contains: c => this.className.split(' ').includes(c), toggle: () => {} }; }
}
const all = n => n.children.flatMap(c => c instanceof Element ? [c, ...all(c)] : []);
const button = (el, name) => all(el).find(n => n.tagName === 'button' && n.textContent === name);
globalThis.document = { createElement: tag => new Element(tag), createTextNode: textContent => ({ textContent, remove() { this.parent?.removeChild(this); } }) };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const original = api.gitRemote;

test('Pull remains available with no known remote commits; duplicate clicks make one request', async t => {
  t.after(() => { api.gitRemote = original; });
  let resolve, calls = 0, changes = 0;
  api.gitRemote = (...args) => { calls++; assert.deepEqual(args, ['demo', 'pull', 'ff-only', false]); return new Promise(r => { resolve = r; }); };
  const view = createPullControl({ project: 'demo', upstream: 'origin/main', changed: () => { changes++; } });
  assert.match(view.el.textContent, /last fetch/);
  const go = button(view.el, 'Pull');
  assert.equal(go.className, 'mini', 'idle Pull uses the shared button style, not a navigation link');
  go.dispatchEvent(new Event('click')); go.dispatchEvent(new Event('click'));
  assert.equal(calls, 1); assert.equal(view.busy, true);
  assert.equal(button(view.el, 'Pulling…').disabled, true);
  assert.equal(button(view.el, 'Pulling…').className, 'mini');
  resolve({ data: { output: 'Already up to date.' } }); await tick();
  assert.equal(changes, 1); assert.equal(view.busy, false);
  assert.match(view.el.textContent, /Pull complete/);
});

test('incoming commits retain the normal Pull button and its primary next-action state', () => {
  const options = { project: 'demo', upstream: 'origin/main', behind: 3, changed() {} };
  assert.equal(button(createPullControl(options).el, 'Pull').className, '');
  assert.equal(button(createPullControl({ ...options, primary: true }).el, 'Pull').className, 'primary');
});

test('header Pull is an accessible icon with no ordinary footer row, busy guard and ff-only clicks', async t => {
  t.after(() => { api.gitRemote = original; });
  const modes = []; let resolve;
  api.gitRemote = (_project, _action, mode) => { modes.push(mode); return new Promise(r => { resolve = r; }); };
  const view = createPullControl({ project: 'demo', upstream: 'origin/main', behind: 2, header: true, changed() {} });
  const go = view.trigger;
  assert.ok(go, 'header mode exposes its persistent action button');
  assert.match(go.attrs['aria-label'], /^Pull 2 commits from origin\/main/);
  assert.match(go.attrs['data-tip-d'], /origin\/main/);
  assert.equal(go.attrs.title, undefined, 'custom help avoids a duplicate native tooltip');
  assert.ok(all(go).some(n => n.className === 'fi fi-down'));
  assert.match(go.className, /^mini icon git-pull behind$/);
  assert.ok(!go.className.split(' ').includes('opt'), 'Pull stays visible when the section is folded');
  assert.equal(view.el.hidden, true);
  go.dispatchEvent(new Event('click')); go.dispatchEvent(new Event('click'));
  assert.deepEqual(modes, ['ff-only']); assert.equal(go.disabled, true);
  assert.equal(go.attrs['data-tip'], 'Pulling origin/main…');
  assert.equal(go.attrs['data-tip-d'], '');
  resolve({ data: { needs_merge: true, ahead: 1, behind: 2 } }); await tick();
  assert.equal(go.disabled, false); assert.equal(view.el.hidden, false); assert.equal(view.recovery, true);
  assert.ok(button(view.el, 'Pull with merge'));
  go.dispatchEvent(new Event('click'));
  assert.deepEqual(modes, ['ff-only', 'ff-only'], 'header does not silently merge after divergence');
  resolve({ data: { output: 'OK' } }); await tick();
  assert.equal(view.el.hidden, true); assert.equal(view.recovery, false);
});

const dispatch = (head, type, target, extra = {}) => {
  const event = new Event(type);
  for (const [key, value] of Object.entries({ target, ...extra })) Object.defineProperty(event, key, { value });
  head.dispatchEvent(event);
};

test('header explanations support keyboard focus, safe text, viewport edges, Escape and redraw cleanup', t => {
  const previous = globalThis.window;
  globalThis.window = Object.assign(new EventTarget(), { innerWidth: 320, innerHeight: 568 });
  t.after(() => { globalThis.window = previous; });
  const head = new Element('div'), go = new Element('button'), icon = new Element('i');
  go.setAttribute('data-tip', 'Refresh'); go.setAttribute('data-tip-d', 'Re-read local status. <script> is plain text.');
  go.focusVisible = true; go.append(icon); head.append(go);
  const view = commitHeaderTooltips(head);
  dispatch(head, 'focusin', icon);
  assert.equal(view.el.hidden, false);
  assert.equal(go.attrs['aria-describedby'], view.el.id);
  assert.match(view.el.textContent, /<script> is plain text/);
  assert.ok(Number.parseFloat(view.el.style.left) >= 8);
  assert.ok(Number.parseFloat(view.el.style.left) + 220 <= 312);
  assert.ok(Number.parseFloat(view.el.style.top) + 68 <= 560, 'bottom edge flips the tooltip above');
  const escape = new Event('keydown'); Object.defineProperty(escape, 'key', { value: 'Escape' }); window.dispatchEvent(escape);
  assert.equal(view.el.hidden, true); assert.equal(go.attrs['aria-describedby'], undefined);
  for (const event of ['scroll', 'resize', 'blur', 'pointerdown']) {
    dispatch(head, 'focusin', icon); window.dispatchEvent(new Event(event)); assert.equal(view.el.hidden, true, event);
  }
  dispatch(head, 'focusin', icon); dispatch(head, 'focusout', go); assert.equal(view.el.hidden, true);
  dispatch(head, 'focusin', icon); dispatch(head, 'click', go); assert.equal(view.el.hidden, true);
  dispatch(head, 'focusin', icon); view.hide(); assert.equal(view.el.hidden, true);
});

test('header hover waits briefly, cancels on leave and ignores touch without running an action', async t => {
  const previous = globalThis.window;
  globalThis.window = Object.assign(new EventTarget(), { innerWidth: 1280, innerHeight: 800 });
  t.after(() => { globalThis.window = previous; });
  const head = new Element('div'), go = new Element('button');
  go.setAttribute('data-tip', 'Git window'); go.setAttribute('data-tip-d', 'Branches and history.'); head.append(go);
  let clicks = 0; go.addEventListener('click', () => clicks++);
  const view = commitHeaderTooltips(head);
  dispatch(head, 'pointerover', go, { pointerType: 'touch' });
  await new Promise(r => setTimeout(r, 410)); assert.equal(view.el.hidden, true);
  dispatch(head, 'pointerover', go, { pointerType: 'mouse' });
  assert.equal(view.el.hidden, true);
  dispatch(head, 'pointerout', go, { relatedTarget: null });
  await new Promise(r => setTimeout(r, 410)); assert.equal(view.el.hidden, true);
  dispatch(head, 'pointerover', go, { pointerType: 'mouse' });
  await new Promise(r => setTimeout(r, 410)); assert.equal(view.el.hidden, false);
  assert.equal(clicks, 0);
  view.hide();
});

test('header Pull reveals failures for recovery and hides the footer again after a successful retry', async t => {
  t.after(() => { api.gitRemote = original; });
  let fail = true; const settled = [];
  api.gitRemote = async () => { if (fail) throw new Error('Local edits would be overwritten.'); return { data: { output: 'OK' } }; };
  const view = createPullControl({ project: 'demo', upstream: 'origin/main', header: true, changed() {},
    pending: busy => { if (!busy) settled.push(view.recovery); } });
  view.trigger.dispatchEvent(new Event('click')); await tick();
  assert.equal(view.el.hidden, false); assert.match(view.el.textContent, /would be overwritten/);
  assert.deepEqual(settled, [true]);
  fail = false; view.trigger.dispatchEvent(new Event('click')); await tick();
  assert.equal(view.el.hidden, true); assert.deepEqual(settled, [true, false]);
});

test('divergence requires another explicit click; merge conflicts remain visible and can open Files', async t => {
  t.after(() => { api.gitRemote = original; });
  const calls = [], opened = [];
  api.gitRemote = async (...args) => { calls.push(args); return { data: calls.length === 1 ? { needs_merge: true, ahead: 1, behind: 2 } : { conflicts: ['src/a<script>.ts'], state: 'merge' } }; };
  const view = createPullControl({ project: 'demo', upstream: 'origin/main', changed() {}, openFile: p => opened.push(p) });
  button(view.el, 'Pull').dispatchEvent(new Event('click')); await tick();
  assert.equal(calls.length, 1); assert.ok(button(view.el, 'Pull with merge'));
  assert.match(view.el.textContent, /fast-forward pull is not possible/);
  assert.match(view.el.textContent, /Your 1 local commit and 2 on origin\/main differ/);
  button(view.el, 'Pull with merge').dispatchEvent(new Event('click')); await tick();
  assert.equal(calls[1][2], 'merge'); assert.equal(calls.length, 2);
  assert.match(view.el.textContent, /Merge stopped: 1 file conflicts/);
  assert.match(view.el.textContent, /Ask the agent/);
  assert.equal(button(view.el, 'Pull with merge'), undefined);
  button(view.el, 'Resolve').dispatchEvent(new Event('click'));
  assert.deepEqual(opened, ['src/a<script>.ts']);
});

test('toolbar Pull never silently upgrades to merge after detecting divergence', async t => {
  t.after(() => { api.gitRemote = original; });
  const modes = [];
  api.gitRemote = async (_project, _action, mode) => { modes.push(mode); return { data: { needs_merge: true } }; };
  const view = createPullControl({ project: 'demo', upstream: 'origin/main', changed() {} });
  view.pull(); await tick(); view.pull(); await tick();
  assert.deepEqual(modes, ['ff-only', 'ff-only']);
  assert.ok(button(view.el, 'Pull with merge'));
});

test('failed Pull retains an actionable error and can be retried without losing the view', async t => {
  t.after(() => { api.gitRemote = original; });
  let fail = true;
  api.gitRemote = async () => { if (fail) throw new Error('Local edits would be overwritten. Commit them first.'); return { data: { output: 'OK' } }; };
  const view = createPullControl({ project: 'demo', upstream: 'origin/main', changed() {} });
  button(view.el, 'Pull').dispatchEvent(new Event('click')); await tick();
  assert.match(view.el.textContent, /Commit them first/);
  assert.equal(view.busy, false);
  fail = false; button(view.el, 'Pull').dispatchEvent(new Event('click')); await tick();
  assert.doesNotMatch(view.el.textContent, /would be overwritten/);
});

test('Push behind check carries its recovery; explicit merge refreshes checks in the same dialog', async t => {
  t.after(() => { api.gitRemote = original; });
  const preview = { branch: 'main', remote: 'origin', target: 'main', upstream: 'origin/main', behind: 2, command: 'git push', new: false,
    commits: [], files: [], checks: [{ level: 'block', code: 'behind', text: 'Remote has 2 commits not here.' }] };
  let modal, closes = 0, pushes = 0, previews = 0;
  api.gitRemote = async (...args) => { assert.deepEqual(args, ['demo', 'pull', 'ff-only', true]); return { data: { output: 'OK' } }; };
  openPushDialog({ project: 'demo', modal: (_title, el) => { modal = el; }, closeModal: () => { closes++; }, push: () => { pushes++; },
    preview: async () => { previews++; return previews === 1 ? preview : { ...preview, behind: 0, checks: [], new: true }; } });
  await tick();
  const pull = button(modal, 'Pull 2 commits'); assert.ok(pull);
  pull.dispatchEvent(new Event('click')); await tick(); await tick();
  assert.equal(previews, 2); assert.equal(closes, 0); assert.equal(pushes, 0);
  assert.doesNotMatch(modal.textContent, /Remote has 2/);
});

test('unfinished merge shows conflict recovery without contradicting it with Pull first', async () => {
  let modal;
  openPushDialog({ project: 'demo', modal: (_title, el) => { modal = el; }, closeModal() {}, push() {},
    preview: async () => ({ branch: 'main', remote: 'origin', target: 'main', behind: 2, state: 'merge', conflicts: ['a.ts'],
      commits: [], files: [], command: 'git push', checks: [{ level: 'block', code: 'operation', text: 'Finish merge' },
        { level: 'block', code: 'behind', text: 'Pull first' }] }) });
  await tick();
  assert.match(modal.textContent, /Merge stopped/);
  assert.doesNotMatch(modal.textContent, /Pull first/);
});

test('Push file list retains both names of a moved file', async () => {
  let modal;
  openPushDialog({ project: 'demo', modal: (_title, el) => { modal = el; }, closeModal() {}, push() {},
    preview: async () => ({ branch: 'main', remote: 'origin', target: 'main', behind: 0, commits: [], command: 'git push', checks: [],
      files: [{ path: 'assets/new.bin', old_path: 'archive/old.bin', status: 'R', add: 0, del: 0, binary: true }] }) });
  await tick();
  const row = all(modal).find(el => el.tagName === 'button' && el.attrs.title === 'archive/old.bin → assets/new.bin');
  assert.ok(row);
  assert.match(row.textContent, /new\.bin ← old\.bin/);
});

test('review 2026-10-31 (B2): every worktree creation shows the backend warning about an unshielded live session', async () => {
  const { readFileSync } = await import('node:fs');
  const gw = readFileSync(new URL('../../piper/web/dist/gitwindow.js', import.meta.url), 'utf8');
  const main = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  assert.match(gw, /Worktree \$\{r\?\.key \?\? n\} on \$\{r\?\.branch \?\? ''\}\$\{r\?\.warning \? ` · \$\{r\.warning\}` : ''\}/, 'the Git window toast carries it');
  assert.match(main, /if \(made\.warning\)\s*toast\(made\.warning\)/, 'new session with a new worktree');
  assert.match(main, /if \(r\.warning\)\s*toast\(r\.warning\)/, 'the start dialog worktree picker');
});
