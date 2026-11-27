import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { terminalReferences } from '../../piper/web/dist/references.js';
import { cases, opens, wrapAnsi } from './termwrap-cases.mjs';

test('the generator wraps like wrap-ansi', () => {
  assert.deepEqual(wrapAnsi('see dev elopment', 7), ['see dev', 'elopment'.slice(0, 7), 't']);
  assert.deepEqual(wrapAnsi('in development/all', 8), ['in', 'developm', 'ent/all'], 'fewer breaks from the next row');
  assert.deepEqual(wrapAnsi('in development/allxy', 8), ['in devel', 'opment/a', 'llxy'], 'no extra break from this row');
  // outputs above are wrap-ansi 9.0.0's own; the port matched it on 200000 random sentences
  assert.deepEqual(wrapAnsi('a b', 3), ['a b']);
});

/** What TermView.fileLinks hands terminalReferences: displayed rows joined by '\n', full-width rows marked. */
function links(rows, cols) {
  let text = '';
  const breaks = new Set(), starts = [];
  rows.forEach((row, i) => {
    starts.push(text.length);
    text += row;
    if (i < rows.length - 1) {
      if (row.trimEnd().length >= cols - 2) breaks.add(text.length);
      text += '\n';
    }
  });
  const rowOf = offset => starts.findLastIndex(s => s <= offset);
  return terminalReferences(text, breaks).map(hit => ({ ...hit, first: rowOf(hit.start), last: rowOf(hit.end - 1) }));
}

test('every wrapped path opens exactly itself from each of its rows, never with the next word', () => {
  const failures = [];
  // PIPER_WRAP_SWEEP=1 sweeps every width 20-140 (~6M layouts, minutes); by default the widths where the reported
  // layouts fell and a spread around common pane sizes
  const widths = process.env.PIPER_WRAP_SWEEP === '1' ? Array.from({ length: 121 }, (_, i) => 20 + i)
    : [20, 24, 30, 37, 41, 58, 63, 80, 81, 117, 120];
  let ambiguous = 0, undecided = 0, total = 0;
  for (const width of widths) for (const c of cases({ widths: [width], extended: true })) {   // one width at a time
    total++;
    const hits = links(c.rows, c.cols);
    if (c.ambiguous) {
      // wrap-ansi run backwards settles most (a word alone after a short row was not cut); the rest look the same
      ambiguous++;
      if (!c.pathRows.every(row => hits.some(h => h.first <= row && h.last >= row && opens(h.ref, c)))) undecided++;
      continue;
    }
    for (const row of c.pathRows) {
      // references that touch the path's own characters on this row (a separate link beside it is fine)
      const here = hits.filter(h => h.first <= row && h.last >= row && (h.ref.kind === 'file' || h.ref.kind === 'url') && h.start < c.span[1] && h.end > c.span[0]);
      const own = here.find(h => opens(h.ref, c));
      const wrong = here.filter(h => h !== own);
      if (!own || wrong.length) {
        failures.push(`${c.name} row ${row}: ${JSON.stringify(here.map(h => h.ref))}\n    ${c.rows.join('\n    ')}`);
        break;
      }
    }
  }
  assert.ok(total > 100000, `matrix too small: ${total}`);
  assert.ok(ambiguous < total / 200, `too many edge layouts: ${ambiguous}/${total}`);
  assert.ok(undecided < ambiguous * 0.15, `edge layouts left undecided: ${undecided}/${ambiguous}`);
  console.log(`# wrap matrix: ${total} layouts, ${failures.length} wrong, ${ambiguous} at the edge of which ${undecided} undecidable`);
  assert.equal(failures.length, 0, `${failures.length}/${total} layouts open the wrong target:\n${failures.slice(0, 25).join('\n')}`);
});
