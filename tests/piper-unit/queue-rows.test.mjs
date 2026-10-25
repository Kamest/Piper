// node --test: N17/N19 - no doubled name in a queue row, Recently without "resolved:", 24h times, carets.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { rowTitle, dropLead, recentTitle, buildQueue } from '../../piper/web/dist/queue.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');

test('N19: a summary that starts with the row\'s own name loses it', () => {
  const ga = { kind: 'advisor', project: '', title: 'Global agent' };
  assert.equal(rowTitle(ga, 'Global agent wants to start a session'), 'Global agent — wants to start a session');
  assert.equal(rowTitle(ga, 'Global agent: wants to start a session'), 'Global agent — wants to start a session');
  const shop = { kind: 'project', project: 'acme/shop', title: 'shop' };
  assert.equal(rowTitle(shop, 'shop wants to push'), 'shop — wants to push');
  assert.equal(rowTitle(shop, 'shopping list'), 'shop — shopping list', 'a word that only starts with the name stays');
  assert.equal(dropLead('shop', 'shop'), 'shop', 'nothing left = unchanged');
  assert.equal(dropLead('x', ''), 'x');
});

test('N19: Recently says "… · resolved", not the technical "resolved: …" prefix', () => {
  assert.equal(recentTitle('resolved: shop: wants to push'), 'shop: wants to push · resolved');
  assert.equal(recentTitle('CI shop passes again'), 'CI shop passes again');
  const q = buildQueue({ sessions: [], actions: [], board: new Map(), now: 100, seen: 0,
    events: [{ id: 'e1', ts: 90, n: 1, source: 'confirm', title: 'resolved: shop: wants to push', body: '', subject: { type: 'session', key: 'x' } }] });
  assert.equal(q.recent[0].title, 'shop: wants to push · resolved');
});

test('N17: visible clock times are 24h like the rest of the app', () => {
  assert.match(src('prod.ts'), /month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false/);
  for (const f of ['prod.ts', 'dbview.ts', 'timesheet.ts', 'mm.ts', 'queue.ts', 'main.ts', 'restorenotice.ts', 'evidence.ts'])
    assert.doesNotMatch(src(f), /minute: '2-digit' \}/, `${f}: a 12h time on screen`);
});

test('N17: Production\'s folded Known / Muted get a caret', () => {
  const block = css.slice(css.indexOf('/* ==== 9.92 N17'), css.indexOf('/* ==== end N17 ==== */'));
  assert.match(block, /details\.ibx-group > summary\.ibx-ghead::after \{ content: '▸';/);
  assert.match(block, /details\.ibx-group\[open\] > summary\.ibx-ghead::after \{ content: '▾'; \}/);
  assert.match(src('prod.ts'), /return h\('details', \{ class: 'ibx-group' \}, h\('summary', \{ class: 'ibx-ghead sec-h' \}/);
});

test('N19b (9.96): "Session grp/demo wants to …" loses its lead too', () => {
  const demo = { kind: 'project', project: 'grp/demo', title: 'demo' };
  assert.equal(rowTitle(demo, 'Session grp/demo wants to allow the domain api.example.org'), 'demo — wants to allow the domain api.example.org');
  assert.equal(rowTitle(demo, 'Session demo: wants to push'), 'demo — wants to push');
  assert.equal(rowTitle(demo, 'Sessions are fine'), 'demo — Sessions are fine');
});
