// node --test: F13 context panel - empty sections in one "Not in use" line, Session in words.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { isoStatus, sessionSummary } from '../../piper/web/dist/inspector.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const main = src('main.ts');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');

test('F13: Session says agent · sandbox · how long, never the container name', () => {
  const now = 1_800_000_000;
  assert.equal(sessionSummary('claude', 'hole-sandbox-acme-app-960c4300', now - 8040, now), 'Claude · sandbox · 2 h 14 min');
  assert.equal(sessionSummary('codex', 'x', now - 300, now), 'Codex · sandbox · 5 min');
  assert.equal(sessionSummary(undefined, null, now - 10, now), 'Claude · just started', 'no container yet: no "sandbox"');
  assert.equal(sessionSummary('claude', 'x', now - 3 * 86400, now), 'Claude · sandbox · 3 d');
  assert.match(main, /info\.status\.append\(h\('span', \{ title: s\.instance \?\? '' \}, ' ' \+ sessionSummary\(s\.agent, s\.instance, s\.created\)\)\)/);
  assert.match(main, /prop\('Instance', s\.instance \?\? '—', 'mut'\)/, 'the container stays in the unfolded rows');
});

test('F13: Isolation reads "✓ N passed"', () => {
  assert.deepEqual(isoStatus({ ok: true, checks: 12 }, {}).map((b) => b.text), ['✓ 12 passed']);
});

test('F13: Diagnostics / Stack / Database mark themselves empty; the panel lists them in one line, a click brings one back', () => {
  assert.match(src('diag.ts'), /sec\.el\.toggleAttribute\('data-empty', none && !c\?\.error\);/);
  assert.match(src('stack.ts'), /sec\.el\.toggleAttribute\('data-empty', !busy && sum\.text === 'none'\);/);
  assert.match(src('dbsection.ts'), /sec\.el\.toggleAttribute\('data-empty', !err && !!list && !list\.connections\.length && !claude\.length && !waiting\.length\);/);
  const fn = main.slice(main.indexOf('function syncNotInUse('), main.indexOf('\n}\n', main.indexOf('function syncNotInUse(')));
  assert.match(fn, /el\.classList\.toggle\('unused', el\.hasAttribute\('data-empty'\) && !revealed\.has\(el\.dataset\.k \?\? ''\)\)/);
  assert.match(fn, /notInUse\.append\('Not in use: '/);
  assert.match(fn, /revealed\.add\(k\); unfold\(k\); syncNotInUse\(\);/);
  assert.match(main, /new MutationObserver\(syncNotInUse\)\.observe\(insp, \{ subtree: true, attributes: true, attributeFilter: \['data-empty'\] \}\);/,
    'a section that empties (or fills) after its own fetch moves at once');
  assert.match(main, /if \(!sameSession\) revealed\.clear\(\);/);
});

test('F13: Last turn sits right under Commit; quiet uppercase heads with the value on the right, no chevrons', () => {
  assert.match(main, /if \(turn && insp\.contains\(git\.el\)\) git\.el\.after\(turn\);/);
  const block = css.slice(css.indexOf('/* ==== 9.71 F13'), css.indexOf('/* ==== end F13 ==== */'));
  assert.match(block, /#inspector \.sec-tg::before \{ display: none; \}/);
  assert.match(block, /#inspector \.sec-t \{[^}]*text-transform: uppercase/);
  assert.match(block, /#inspector \.sec\.unused \{ display: none; \}/);
});
