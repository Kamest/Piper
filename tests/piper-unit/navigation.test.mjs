import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NAV_TOOLS, parseToolPins, serializeToolPins, toggleToolPin, hiddenToolState } from '../../piper/web/dist/navigation.js';
import { readFileSync } from 'node:fs';

test('navigation pins: defaults, empty choice, corrupt storage, duplicates and unknown keys', () => {
  for (const raw of [null, '', '{', 'null', '{}', 'true', '{"pins":7}']) assert.deepEqual(parseToolPins(raw), ['inbox', 'db', 'host', 'stacks', 'notes']);
  assert.deepEqual(parseToolPins('{"pins":[]}'), []);
  assert.deepEqual(parseToolPins('{"pins":["keys","db","db","unknown",42,"prod"]}'), ['prod', 'db', 'keys']);
});

test('the Inbox unpins like any tool; pins stored before it could be unpinned keep it on the rail', () => {
  assert.deepEqual(parseToolPins('[]'), ['inbox'], 'an older bare array: the Inbox was always shown');
  assert.deepEqual(parseToolPins('["keys","db","prod"]'), ['inbox', 'prod', 'db', 'keys']);
  const off = toggleToolPin(parseToolPins(null), 'inbox', false);
  assert.deepEqual(parseToolPins(serializeToolPins(off)), ['db', 'host', 'stacks', 'notes'], 'unpinned stays unpinned');
  assert.deepEqual(parseToolPins(serializeToolPins(toggleToolPin(off, 'inbox', true))), ['inbox', 'db', 'host', 'stacks', 'notes']);
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /viewOn\('gitlab'\) && \{ id: 'inbox'/, 'All tools lists the Inbox while its feature is on');
  assert.match(main, /const inboxTool = tools\.find\(t => t\.id === 'inbox' && toolPins\.includes\(t\.id\)\)/);
  assert.match(main, /writeStore\(TOOL_PINS_KEY, serializeToolPins\(toolPins\)\)/);
  assert.doesNotMatch(main, /!viewOn\('gitlab'\) \? null : item\('inbox'/, 'no rail Inbox outside the pins');
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
  assert.deepEqual(parseToolPins('{"pins":["http"]}'), ['http']);
});

test('Dev stacks uses the same persistent pin without forcing existing choices', () => {
  assert.deepEqual(parseToolPins(null), ['inbox', 'db', 'host', 'stacks', 'notes']);
  assert.deepEqual(parseToolPins('{"pins":["db","host"]}'), ['db', 'host']);
  assert.deepEqual(parseToolPins('{"pins":[]}'), []);
  assert.deepEqual(toggleToolPin(['db', 'host', 'stacks'], 'stacks', false), ['db', 'host']);
  assert.deepEqual(toggleToolPin(['db', 'host'], 'stacks', true), ['db', 'host', 'stacks']);
  assert.deepEqual(hiddenToolState([{id:'stacks',active:true,count:2}], []), {active:'stacks',danger:false,count:2});
});
