// node --test: W2 host terminal - the view's pure helpers (web/src/hostterm.ts) and its keys (clipkeys.ts hostKey)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { exitText, hostItems, pickActive, shellProjects, tildePath } from '../../piper/web/dist/hostterm.js';
import { hostKey, scratchKey } from '../../piper/web/dist/clipkeys.js';

const row = (id, alive = true, rc = null, project = 'webstore') => ({ id, project, path: `/Users/me/p/${project}`, title: project, created: 1, rc, alive });
const key = (code, mods = {}) => ({ code, altKey: false, shiftKey: false, metaKey: false, ctrlKey: false, ...mods });

test('hostItems: nothing while the policy is off, an older piperd or a scoped token ({})', () => {
  assert.deepEqual(hostItems({}), []);
  assert.deepEqual(hostItems({ host_terms: {} }), []);
  assert.deepEqual(hostItems({ host_terms: { on: false, items: [row('host-1')] } }), []);
  assert.deepEqual(hostItems({ host_terms: { on: true } }), []);
  assert.deepEqual(hostItems({ host_terms: { on: true, items: [row('host-1')] } }).map((x) => x.id), ['host-1']);
});

test('pickActive keeps the wanted tab, else the last running shell, else the last one', () => {
  const items = [row('host-a'), row('host-b'), row('host-c', false, 0)];
  assert.equal(pickActive(items, 'host-a'), 'host-a');
  assert.equal(pickActive(items, 'host-gone'), 'host-b');
  assert.equal(pickActive([row('host-x', false, 1), row('host-y', false, 0)], ''), 'host-y');
  assert.equal(pickActive([], 'host-a'), '');
});

test('shellProjects: only checkouts on this Mac, the current project first', () => {
  const projects = [
    { key: 'a', title: 'A', path: '/p/a', gitlab: [], exists: true },
    { key: 'b', title: 'B', path: '/p/b', gitlab: [], exists: false },
    { key: 'c', title: 'C', path: '/p/c', gitlab: [], exists: true },
  ];
  assert.deepEqual(shellProjects(projects, 'c').map((p) => p.key), ['c', 'a']);
  assert.deepEqual(shellProjects(projects, '').map((p) => p.key), ['a', 'c']);
  assert.deepEqual(shellProjects(projects, 'b').map((p) => p.key), ['a', 'c']);   // no checkout: not offered
});

test('exitText: a grey tab says how the shell ended', () => {
  assert.equal(exitText({ alive: true, rc: null }), '');
  assert.equal(exitText({ alive: false, rc: 0 }), 'exit 0');
  assert.equal(exitText({ alive: false, rc: 130 }), 'exit 130');
  assert.equal(exitText({ alive: false, rc: null }), 'ended');   // ptyhost restarted: the code is unknown
});

test('tildePath: the home folder as ~, never a prefix of another user', () => {
  assert.equal(tildePath('/Users/me/p/webstore', 'me'), '~/p/webstore');
  assert.equal(tildePath('/Users/me', 'me'), '~');
  assert.equal(tildePath('/home/me/x', 'me'), '~/x');
  assert.equal(tildePath('/Users/meow/x', 'me'), '/Users/meow/x');
  assert.equal(tildePath('/Users/me/x', ''), '/Users/me/x');
});

test('hostKey: ⌥F12 toggles from anywhere; ⌥⇧T / ⌥⇧W only inside the host view', () => {
  assert.equal(hostKey(key('F12', { altKey: true }), false), 'toggle');
  assert.equal(hostKey(key('F12', { altKey: true }), true), 'toggle');
  assert.equal(hostKey(key('KeyT', { altKey: true, shiftKey: true }), true), 'new');
  assert.equal(hostKey(key('KeyW', { altKey: true, shiftKey: true }), true), 'close');
  // a session's terminal keeps ⌥⇧T / ⌥⇧W
  assert.equal(hostKey(key('KeyT', { altKey: true, shiftKey: true }), false), '');
  assert.equal(hostKey(key('KeyW', { altKey: true, shiftKey: true }), false), '');
  // not ours: ⇧⌘F12 (hide all panels), plain F12, ⌥T without ⇧ (a Czech Mac types † there), Ctrl+Alt (AltGr)
  assert.equal(hostKey(key('F12', { altKey: true, metaKey: true }), true), '');
  assert.equal(hostKey(key('F12', { altKey: true, shiftKey: true }), true), '');
  assert.equal(hostKey(key('F12'), true), '');
  assert.equal(hostKey(key('KeyT', { altKey: true }), true), '');
  assert.equal(hostKey(key('KeyW', { altKey: true, shiftKey: true, ctrlKey: true }), true), '');
});

test('scratchKey: ⌥⇧S by position (a Mac types Í), never with ⌘ or Ctrl (AltGr)', () => {
  assert.equal(scratchKey(key('KeyS', { altKey: true, shiftKey: true })), true);
  assert.equal(scratchKey(key('KeyS', { altKey: true })), false);
  assert.equal(scratchKey(key('KeyS', { shiftKey: true })), false);
  assert.equal(scratchKey(key('KeyS', { altKey: true, shiftKey: true, metaKey: true })), false);
  assert.equal(scratchKey(key('KeyS', { altKey: true, shiftKey: true, ctrlKey: true })), false);
  assert.equal(scratchKey(key('KeyT', { altKey: true, shiftKey: true })), false);
});
