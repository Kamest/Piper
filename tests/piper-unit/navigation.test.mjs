import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NAV_TOOLS, parseToolPins, toggleToolPin, hiddenToolState } from '../../piper/web/dist/navigation.js';

test('navigation pins: defaults, empty choice, corrupt storage, duplicates and unknown keys', () => {
  for (const raw of [null, '', '{', 'null', '{}', 'true']) assert.deepEqual(parseToolPins(raw), ['db', 'host', 'stacks']);
  assert.deepEqual(parseToolPins('[]'), []);
  assert.deepEqual(parseToolPins('["keys","db","db","unknown",42,"prod"]'), ['prod', 'db', 'keys']);
});

test('navigation pins: stable canonical order, no mutation or loss of disabled modules', () => {
  const initial = ['db', 'host'];
  const pinned = toggleToolPin(initial, 'prod', true);
  assert.deepEqual(pinned, ['prod', 'db', 'host']);
  assert.deepEqual(initial, ['db', 'host']);
  assert.deepEqual(toggleToolPin(pinned, 'db', false), ['prod', 'host']);
  assert.deepEqual(toggleToolPin(pinned, 'prod', true), pinned);
  assert.deepEqual(parseToolPins(JSON.stringify(NAV_TOOLS)), [...NAV_TOOLS]);
});

test('hidden tool state: only unpinned available destinations contribute activity and alerts', () => {
  const tools = [{ id: 'prod', active: false, count: 3 }, { id: 'host', active: true, danger: true }, { id: 'db', active: false }];
  assert.deepEqual(hiddenToolState(tools, ['db']), { active: 'host', danger: true, count: 3 });
  assert.deepEqual(hiddenToolState(tools, ['prod', 'host']), { active: null, danger: false, count: 0 });
  assert.deepEqual(hiddenToolState([], ['host']), { active: null, danger: false, count: 0 });
});


test('HTTP can be pinned before the first request without disturbing other pins', () => {
  assert.ok(NAV_TOOLS.includes('http'));
  assert.deepEqual(toggleToolPin(['db', 'host'], 'http', true), ['db', 'http', 'host']);
  assert.deepEqual(parseToolPins('["http"]'), ['http']);
});

test('Dev stacks uses the same persistent pin without forcing existing choices', () => {
  assert.deepEqual(parseToolPins(null), ['db', 'host', 'stacks']);
  assert.deepEqual(parseToolPins('["db","host"]'), ['db', 'host']);
  assert.deepEqual(parseToolPins('[]'), []);
  assert.deepEqual(toggleToolPin(['db', 'host', 'stacks'], 'stacks', false), ['db', 'host']);
  assert.deepEqual(toggleToolPin(['db', 'host'], 'stacks', true), ['db', 'host', 'stacks']);
  assert.deepEqual(hiddenToolState([{id:'stacks',active:true,count:2}], []), {active:'stacks',danger:false,count:2});
});
