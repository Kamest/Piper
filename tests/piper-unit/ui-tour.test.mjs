// node --test: what the tour of presets / themes / widths found after the redesign.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { NoticeLog } from '../../piper/web/dist/notices.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');

test('N2: a state hint is one alert however many reloads repeat it; events still repeat after a minute', () => {
  let now = 1_000_000;
  const log = new NoticeLog(undefined, () => now);
  log.add('Desktop notifications are blocked', 'toast', undefined, undefined, true);
  now += 10 * 60_000;
  log.add('Desktop notifications are blocked', 'toast', undefined, undefined, true);
  assert.equal(log.list().length, 1);
  assert.equal(log.list()[0].ts, now, 'moved to the newest time');
  log.add('Push done'); now += 2 * 60_000; log.add('Push done');
  assert.equal(log.list().filter((n) => n.text === 'Push done').length, 2, 'an event is not a state');
  assert.match(src('main.ts'), /notices\.add\(text, 'toast', .*, undefined, cls === 'notification-help', id \|\| undefined\);/);
  assert.match(src('main.ts'), /toLocaleTimeString\(\[\], \{ hour: '2-digit', minute: '2-digit', hour12: false \}\)/, '24 h like the queue');
});

test('N8: a blocked start whose body has "Approve and start" keeps Retry quiet in the header (one primary)', () => {
  const main = src('main.ts');
  assert.match(main, /class: s\.state === 'blocked' && !grid && approvesInBody\(s\) \? '' : 'primary', onclick: \(\) => void act\(s, 'retry'\)/);
  assert.match(main, /function approvesInBody\(s: Session\): boolean \{ return s\.blocks\.every\(\(b\) => b\.kind === 'settings-changed'\); \}/,
    'a declaration: the header may render before the line runs (TDZ)');
  assert.match(main, /const onlySettings = approvesInBody\(s\);/);
});
