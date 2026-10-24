import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { shotLine, shotCollector } from './shots.mjs';

const feedAll = (feed, lines) => lines.map(feed);

// node --test split each screenshot into several "# ..." lines; only the first was saved (truncated PNGs).
test('a screenshot split across TAP diagnostic lines is reassembled byte for byte', () => {
  const bytes = randomBytes(70000), line = '# ' + shotLine('D2', bytes);
  const parts = [line.slice(0, 8256), ...[8256, 22404, 29568].map((at, i, all) => '# ' + line.slice(at, all[i + 1]))];
  const results = feedAll(shotCollector(), ['ok 1 - before', ...parts, 'ok 2 - after']);
  assert.equal(results[0], null);
  assert.equal(results.at(-1), null);
  const shot = results.find(r => r?.shot)?.shot;
  assert.equal(shot.name, 'D2');
  assert.ok(shot.bytes.equals(bytes));
});

test('an unsplit screenshot and ordinary diagnostics pass through', () => {
  const feed = shotCollector(), bytes = randomBytes(100);
  assert.equal(feed('# Subtest: x'), null);
  assert.ok(feed('# ' + shotLine('D1', bytes)).shot.bytes.equals(bytes));
  assert.equal(feed('# tests 1'), null);
});

test('a screenshot whose data went missing is reported, never written', () => {
  const line = '# ' + shotLine('D5', randomBytes(3000));
  const feed = shotCollector();
  feed(line.slice(0, 500));
  const result = feed('# ' + line.slice(1000));
  assert.equal(result.shot, undefined);
  assert.match(result.error, /D5 is incomplete|malformed/);
});
