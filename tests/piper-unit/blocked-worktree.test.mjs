import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { blockGroupKey, protectedText } from '../../piper/web/dist/text.js';

const source = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const panelCode = source.slice(source.indexOf('const KIND_TEXT ='), source.indexOf('function renderInspector()'));
function render(blocks) {
  const calls = [];
  const h = (tag, attrs, ...children) => ({ tag, attrs, children, append(...items) { this.children.push(...items); } });
  const context = vm.createContext({ h, blockGroupKey, protectedText, act: (...args) => calls.push(args) });
  vm.runInContext(panelCode, context);
  const session = { id: 'blocked-test', blocks };
  const panel = context.blockedPanel(session);
  const walk = n => n && typeof n === 'object' ? [n, ...n.children.flatMap(walk)] : [n];
  const nodes = walk(panel);
  return { text: nodes.filter(n => typeof n === 'string').join(' '), buttons: nodes.filter(n => n?.tag === 'button'), calls, session };
}
const link = { kind: 'worktree-link-unverified', path: '/tmp/stale', detail: 'link does not point back' };

test('worktree block explains manual recovery and Retry dispatches the existing action', () => {
  const p = render([link]);
  assert.match(p.text, /may be stale/);
  assert.match(p.text, /also applies in Minimal/);
  assert.match(p.text, /git worktree prune --dry-run --verbose/);
  assert.match(p.text, /every listed record is obsolete/);
  assert.match(p.text, /repair its links instead/);
  assert.doesNotMatch(p.text, /sensitive content|Fix the settings file|rm -rf/);
  assert.equal(p.buttons.length, 1);
  assert.equal(p.buttons[0].children[0], 'Retry');
  p.buttons[0].attrs.onclick();
  assert.deepEqual(p.calls, [[p.session, 'retry']]);
});

test('mixed findings retain their remedies; settings-only still offers approval', () => {
  const mixed = render([link, { kind: 'uncovered', path: '/app/secret', detail: '/app' }]);
  assert.deepEqual(mixed.buttons.map(b => b.children[0]), ['Add excludes (1)', 'Allow in this project (1)', 'Retry']);
  const settings = render([{ kind: 'settings-changed', path: '/app/.hole/settings.json' }]);
  assert.deepEqual(settings.buttons.map(b => b.children[0]), ['Approve and start']);
  assert.doesNotMatch(settings.text, /worktree records/);
});
