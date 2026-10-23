// node --test: the context panel's Database section - one connection in the header
// and one action: Open beside / Open ↗ / Add…
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { sectionHead } from '../../piper/web/dist/dbsection.js';

const C = (name, cls, label) => ({ name, class: cls, ...(label ? { label } : {}) });

test('sectionHead: loading, none for this project · Add…, the dev connection first with Open beside', () => {
  assert.deepEqual(sectionHead(null, 'grp/demo', true), { cls: null, text: '…', action: null });
  assert.deepEqual(sectionHead([], 'grp/demo', true), { cls: null, text: 'none for this project', action: { label: 'Add…', kind: 'add', connection: '' } });
  const conns = [C('grp/demo/test/shop', 'test'), C('grp/demo/dev/acme', 'dev')];
  assert.deepEqual(sectionHead(conns, 'grp/demo', true),
    { cls: 'dev', text: 'dev/acme', action: { label: 'Open beside', kind: 'pane', connection: 'grp/demo/dev/acme' } });
  assert.deepEqual(sectionHead(conns, 'grp/demo', false).action, { label: 'Open ↗', kind: 'view', connection: 'grp/demo/dev/acme' },
    'without the pane: the view');
  assert.equal(sectionHead([C('grp/demo/prod/x', 'prod')], 'grp/demo', true).cls, 'prod', 'no dev: the first one');
  assert.equal(sectionHead([C('grp/demo/dev/x', 'dev', 'Shop DB')], 'grp/demo', true).text, 'Shop DB', 'a label wins');
});

test('wiring: one action button in the header (no Open pane + ↗ view pair), dot classes, "no password"', () => {
  const src = readFileSync(new URL('../../piper/web/src/dbsection.ts', import.meta.url), 'utf8');
  assert.match(src, /sec\.actions\.append\(actBtn\);/);
  assert.doesNotMatch(src, /'Open pane'|'↗ view'|'Open view ↗'|'Add a connection'/);
  assert.doesNotMatch(src, /classBadge/);
  assert.match(src, /needsPassword\(c\) \? 'no password'/);
  assert.match(src, /if \(a\.kind === 'pane'\) deps\.openPane\?\.\(a\.connection\);\s*else if \(a\.kind === 'view'\) deps\.open\(a\.connection\);\s*else deps\.open\(\);/);
});
