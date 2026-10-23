// node --test: pure helpers of the Production section, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { freshness, prodGroups, rowFacts, safeUrl, scheduleLabel, STALE_AFTER } from '../../piper/web/dist/prod.js';

const SRC = readFileSync(new URL('../../piper/web/src/prod.ts', import.meta.url), 'utf8');
const CSS = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');

const it = (over) => ({ key: over.title, project: 'shop', obs: 'webstore', kind: 'error', title: 't', fingerprint: '', count: 0,
  first_seen: '', trend: '', service: '', detail: '', url: '', new: false, muted: false, ...over });

test('new first, alerts before errors before rates, then by count; muted apart', () => {
  const g = prodGroups([
    it({ title: 'rate', kind: 'rate', new: true }),
    it({ title: 'e1', count: 2, new: true }),
    it({ title: 'e2', count: 9, new: true }),
    it({ title: 'al', kind: 'alert', new: true }),
    it({ title: 'old', count: 50 }),
    it({ title: 'hush', new: true, muted: true }),
  ]);
  assert.deepEqual(g.fresh.map((x) => x.title), ['al', 'e2', 'e1', 'rate']);
  assert.deepEqual(g.known.map((x) => x.title), ['old']);
  assert.deepEqual(g.muted.map((x) => x.title), ['hush']);
});

test('schedule label and https-only links', () => {
  assert.equal(scheduleLabel([1, 2, 3, 4, 5], '07:30'), 'Mon–Fri 07:30');
  assert.equal(scheduleLabel([3, 1], '06:00'), 'Mon, Wed 06:00');
  assert.equal(safeUrl('https://grafana.example.com/x?a=1'), 'https://grafana.example.com/x?a=1');
  assert.equal(safeUrl('javascript:alert(1)'), '');
  assert.equal(safeUrl('http://grafana.example.com/'), '');
});

test('F16 freshness: over 24 h is stale with the date, else just "checked … ago"', () => {
  const now = 1_800_000_000;
  const stamp = () => 'Oct 16, 15:27';
  assert.deepEqual(freshness(now - 2 * 3600, now, stamp), { stale: false, text: 'checked 2 h ago' });
  assert.deepEqual(freshness(now - 10, now, stamp), { stale: false, text: 'checked just now' });
  assert.equal(freshness(now - STALE_AFTER, now, stamp).stale, false);
  assert.deepEqual(freshness(now - 30 * 3600, now, stamp), { stale: true, text: 'Last check 30 h ago (Oct 16, 15:27) — data is stale' });
  assert.deepEqual(freshness(now - 3 * 86400 - 60, now, stamp), { stale: true, text: 'Last check 3 days ago (Oct 16, 15:27) — data is stale' });
});

test('F16 row facts: project · count / 24 h · since · trend; no kind word for an error; the place apart', () => {
  const e = rowFacts(it({ title: 'NPE', title_project: 'Shop', count: 701, first_seen: 'Oct 15', trend: '↑', fingerprint: 'NPE @ Foo' }));
  assert.deepEqual(e, { kind: '', facts: ['Shop', '701× / 24 h', 'since Oct 15', '↑'], place: 'NPE @ Foo' });
  assert.equal(rowFacts(it({ kind: 'alert' })).kind, 'alert');
  assert.equal(rowFacts(it({ kind: 'rate' })).kind, 'error rate');
  assert.equal(rowFacts(it({ title: 'x', fingerprint: 'x' })).place, '');   // the fingerprint = the title: not twice
  assert.deepEqual(rowFacts(it({ count: 0 })).facts, ['shop']);
});

test('F16 structure: stale bar with Check now as primary, actions on hover, Mute ▾ menu, suggestions as a link', () => {
  assert.match(SRC, /class: 'prod-stale hidden', role: 'status'/);
  assert.match(SRC, /runBtn\.classList\.toggle\('primary', isStale\)/);
  assert.match(SRC, /\(isStale \? stale : head\)\.append\(runBtn\)/);
  assert.match(SRC, /isStale = !!fr\?\.stale && d\.status !== 'running' && d\.status !== 'waiting'/);
  assert.match(SRC, /class: `irow ra-row/);
  assert.match(SRC, /class: 'irow-act row-actions'/);
  assert.match(SRC, /'Mute', fi\('chev-d'\)/);   // pages polish: the caret is the mask icon
  assert.match(SRC, /label: 'For a week'[^\n]*days: 7/);
  assert.match(SRC, /label: 'Forever'[^\n]*days: 0/);
  assert.match(SRC, /'Unmute'/);   // Z: the Muted group keeps Unmute
  assert.match(SRC, /group\('Known', g\.known, false/);
  assert.match(SRC, /group\('Muted', g\.muted, false/);
  assert.match(SRC, /mapping \$\{n === 1 \? 'suggestion' : 'suggestions'\}`, fi\('chev-r'\)/);
  assert.doesNotMatch(SRC, /'Mute for a week'|'forever'\)/);   // no four buttons per row
  assert.doesNotMatch(SRC, /ibx-badge/);   // the count is in the NEW · n heading
  // the status lines stay (running / waiting slots / failed + Try again / nothing mapped) and the plan footer
  for (const t of ['Production check running… ', 'waiting for free slots', 'The check failed: ', 'Try again', 'no project has an observability mapping', 'Automatic ${scheduleLabel']) assert.ok(SRC.includes(t), t);
  const block = CSS.slice(CSS.indexOf('/* ==== 9.74 F16'), CSS.indexOf('/* ==== end F16 ==== */'));
  assert.ok(block.length > 100);
  assert.match(block, /\.prod \.prod-stale \{[^}]*var\(--warn\)/);
  assert.match(block, /\.prod \.irow:hover \.prod-n/);   // the count gives way to the actions
  assert.doesNotMatch(block, /font-size: \d+px|border-radius: \d+px/);
});
