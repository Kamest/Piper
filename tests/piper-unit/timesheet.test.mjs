// node --test: pure helpers of the Timesheet view, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cellMinutes, copyText, fmtClock, fmtHours, roundMinutes, rowLabel, startFocusBeat } from '../../piper/web/dist/timesheet.js';

test('rounding applies only as chosen, in quarter hours', () => {
  assert.equal(roundMinutes(7, 'none'), 7);
  assert.equal(roundMinutes(7, 'up'), 15);
  assert.equal(roundMinutes(15, 'up'), 15);
  assert.equal(roundMinutes(22, 'down'), 15);
  assert.equal(roundMinutes(22.4, 'nearest'), 15);
  assert.equal(roundMinutes(23, 'nearest'), 30);
  assert.equal(roundMinutes(0, 'up'), 0);
});

test('hours use the Czech decimal comma, raw time as h:mm', () => {
  assert.equal(fmtHours(75), '1,25 h');
  assert.equal(fmtHours(0), '0 h');
  assert.equal(fmtClock(65.4), '1:05');
});

test('the user\'s own minutes win over the computed ones', () => {
  assert.equal(cellMinutes({ raw_min: 12, auto_text: '', blocks: [] }), 12);
  assert.equal(cellMinutes({ raw_min: 12, minutes: 30, auto_text: '', blocks: [] }), 30);
});

test('row labels: issue with title, MR, project without an issue', () => {
  assert.equal(rowLabel({ key: 'grp/p#issue#12', title: 'Shipping', project: 'p' }), 'Shipping (grp/p#12)');
  assert.equal(rowLabel({ key: 'grp/p#mr#3', title: '', project: 'p' }), 'grp/p!3');
  assert.equal(rowLabel({ key: 'p:x/p', title: '', project: 'x/p' }), 'x/p · no issue');
});

test('copied text: rounded per line, edited text wins, manual rows, empty days left out', () => {
  const rep = {
    rows: [
      { key: 'grp/p#issue#12', title: 'Shipping', project: 'p', sessions: [],
        days: { '2026-09-28': { raw_min: 7, auto_text: 'fix', blocks: [] },
                '2026-09-29': { raw_min: 50, minutes: 60, text: 'Analysis and fix', auto_text: 'x', blocks: [] } } },
    ],
    manual: [{ id: 'a', day: '2026-09-29', label: 'Meeting', minutes: 30, text: '' }],
  };
  const txt = copyText(rep, ['2026-09-28', '2026-09-29', '2026-09-30'], 'up');
  assert.equal(txt, [
    '2026-09-28 (0,25 h)', '  Shipping (grp/p#12)\t0,25 h\tfix',
    '2026-09-29 (1,5 h)', '  Shipping (grp/p#12)\t1 h\tAnalysis and fix', '  Meeting\t0,5 h\t',
  ].join('\n'));
  assert.equal(copyText({ rows: [], manual: [] }, ['2026-09-28'], 'up'), '');
});

test('N9: cellError - a cell\'s own minutes are 0–1440 whole; empty / letters / out of range say why instead of saving 0:00 or 24:00', async () => {
  const { cellError } = await import('../../piper/web/dist/timesheet.js');
  for (const m of ['0', '23', '47', '1440']) assert.equal(cellError(m), '', m);
  for (const m of ['', '  ', '-5', '1441', '99999', '30.5', 'abc']) assert.match(cellError(m), /0 to 1440/, m);
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/timesheet.ts', import.meta.url), 'utf8');
  assert.match(src, /const why = cellError\(min\.value\); if \(why\) return void deps\.toast\(why\); void post\('edit'/);
  // N9: Enter in the cell, manual-row and Assign fields runs their button (it did nothing)
  assert.match(src, /enterRuns\(\[min, text\], save\)/);
  assert.match(src, /enterRuns\(\[label, min, text\], add\)/);
  assert.match(src, /enterRuns\(\[key\], assign\)/);
});

test('manualError: a label and 1–1440 whole minutes, else the reason (no silent no-op)', async () => {
  const { manualError } = await import('../../piper/web/dist/timesheet.js');
  assert.equal(manualError('call', '30'), '');
  assert.match(manualError('  ', '30'), /Say what/);
  for (const m of ['0', '-30', '1441', '7.5', '', 'x']) assert.match(manualError('call', m), /1 to 1440/, m);
  assert.equal(manualError('call', '1440'), '');
});

test('N10-D-6: a long unbroken manual-row label / issue title wraps inside the card (it ran past it at 1024)', async () => {
  const css = (await import('node:fs')).readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.tsheet ul\.small li,\s*\.tstable td:first-child \{\s*overflow-wrap: anywhere;/);
});


test('focus beat follows the live feature switch without settings polling', async () => {
  const old = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch };
  const handlers = new Map(), timers = [], calls = [];
  let enabled = false;
  globalThis.window = { addEventListener: (ev, fn) => handlers.set(ev, fn), setInterval: (fn) => timers.push(fn), localStorage: { getItem: () => '' } };
  globalThis.document = { visibilityState: 'visible', hasFocus: () => true };
  globalThis.fetch = async (path) => { calls.push(path); return new Response('{}', { status: 200 }); };
  try {
    startFocusBeat(() => 's1', () => enabled);
    assert.equal(timers.length, 1);
    handlers.get('pointerdown')();
    timers[0]();
    assert.deepEqual(calls, []);
    enabled = true;
    handlers.get('pointerdown')();
    timers[0]();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(calls, ['/api/timesheet/focus']);
    enabled = false;
    timers[0]();
    assert.equal(calls.length, 1);
  } finally {
    Object.assign(globalThis, old);
  }
});
