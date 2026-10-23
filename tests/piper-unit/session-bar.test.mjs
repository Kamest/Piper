// node --test: the session bar - 44 px, every control on one centre line, chips as pills.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const block = css.slice(css.indexOf('/* ==== 9.102 the session bar'), css.indexOf('/* ==== end session bar ==== */'));

test('9.102: a 44 px bar and 44 px tabs with a 2 px underline; after F03 so it wins', () => {
  assert.ok(block.length > 300 && css.indexOf('/* ==== 9.102') > css.indexOf('/* ==== end F03 ==== */'));
  assert.match(block, /\.head\.detail\.one \.dh-top \{ height: 44px; min-height: 44px;/);
  assert.match(block, /\.head\.detail\.one button\.tab \{ height: 44px; display: inline-flex; align-items: center; border-bottom-width: 2px;/);
});

test('9.102: the controls are centred boxes - no leftover 5 px under the Beside group, icons as blocks', () => {
  assert.match(block, /\.head\.detail\.one \.layoutctl \{ gap: 4px; padding-bottom: 0; \}/);
  assert.match(block, /\.head\.detail\.one \.layoutctl button, \.head\.detail\.one \.actions button\.icon \{ display: inline-flex; align-items: center; justify-content: center;\s+height: 30px;/);
  assert.match(block, /\.head\.detail\.one \.layoutctl button \.fi, \.head\.detail\.one \.layoutctl button \.chev \{ display: block; \}/);
  assert.match(block, /\.head\.detail\.one \.dh-chips \.chip \{ display: inline-flex; align-items: center; height: 22px;/);
});

test('9.102: a narrow detail still wraps the tabs into their own row (F03 670 px)', () => {
  assert.match(block, /@container dh \(max-width: 670px\) \{ \.head\.detail\.one \.dh-top \{ height: auto; min-height: 44px; \}/);
});
