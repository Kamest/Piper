// node --test: N11/N13/N14 - Settings: "applies…" once, switches, the open section in the nav.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { sharedApply, revealNavItem } from '../../piper/web/dist/settings.js';

const src = readFileSync(new URL('../../piper/web/src/settings.ts', import.meta.url), 'utf8');
const NEW = 'applies to newly started sessions (running ones keep what they started with)';

test('N11: the "applies…" most rows share is said once; only the rows that differ keep their line', () => {
  const f = (apply, apply_text = apply === 'session' ? NEW : apply === 'now' ? 'takes effect immediately' : 'after a restart') => ({ apply, apply_text });
  assert.deepEqual(sharedApply([f('session'), f('session')]), { text: NEW, all: true });
  assert.deepEqual(sharedApply([f('session'), f('session'), f('now'), f('restart')]), { text: NEW, all: false });
  assert.deepEqual(sharedApply([f('session'), f('restart')]), { text: '', all: false }, 'no text two rows share');
  assert.deepEqual(sharedApply([f('now'), f('now')]), { text: '', all: false }, '"immediately" is never said');
  assert.match(src, /apply\.all \? shared\[0\]!\.toUpperCase\(\) \+ shared\.slice\(1\) : `Unless a setting says otherwise, it \$\{shared\}`/);
  assert.match(src, /fieldRow\(s, f, !!shared && f\.apply_text === shared, q\)/);
});

test('N13: a bool setting is the switch of Features / the Inbox rules, the input keeps its id', () => {
  assert.match(src, /const i = h\('input', \{ type: 'checkbox', id, role: 'switch' \}\)/);
  assert.match(src, /return h\('label', \{ class: 'switch' \}, i, h\('span', \{ class: 'track', 'aria-hidden': 'true' \}\)\);/);
});

test('N14: the open section is scrolled into the nav (the nav, not the page), the nav keeps its scroll across redraws', () => {
  const rect = (top, bottom) => ({ top, bottom });
  const mk = (itemTop, itemBottom) => {
    const on = { getBoundingClientRect: () => rect(itemTop, itemBottom) };
    return { scrollTop: 0, getBoundingClientRect: () => rect(100, 500), querySelector: () => on };
  };
  const below = mk(520, 550); revealNavItem(below); assert.equal(below.scrollTop, 58);
  const inside = mk(200, 230); revealNavItem(inside); assert.equal(inside.scrollTop, 0);
  const above = mk(60, 90); above.scrollTop = 100; revealNavItem(above); assert.equal(above.scrollTop, 52);
  const none = { scrollTop: 7, querySelector: () => null }; revealNavItem(none); assert.equal(none.scrollTop, 7);
  assert.match(src, /nav\.scrollTop = navTop;\n\s+(?:nav\.scrollLeft = navLeft;\n\s+)?revealNavItem\(nav\);/);
});

test('N15 (9.94): the Usage days span the card like the project table - no 760 px cap', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  for (const sel of ['.usage .usec-h {', '.usage .ulegrow {', '.usage .udays {']) {
    const rule = css.slice(css.indexOf(sel), css.indexOf('}', css.indexOf(sel)));
    assert.ok(rule.length > sel.length, sel);
    assert.doesNotMatch(rule, /max-width/, sel);
  }
});
