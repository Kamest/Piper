// node --test: N16 - Host shells with the session bar's pattern: full bleed, 44 px, underline tabs.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const ts = readFileSync(new URL('../../piper/web/src/hostterm.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const block = css.slice(css.indexOf('/* ==== 9.103 N16'), css.indexOf('/* ==== end N16 ==== */'));

test('N16: the bar - title, tabs, + ▾ as one control, no line of key hints (tooltips + Keys sheet)', () => {
  assert.match(ts, /h\('span', \{ class: 'ht-new' \}, addBtn, pickBtn\), h\('span', \{ class: 'grow' \}\), endedBtn\);/);
  assert.doesNotMatch(ts, /ht-hint/);
  assert.match(ts, /'Close \(HUP the shell\) · ⌥⇧W'/);
  assert.match(ts, /`\$\{kbd\('⌥F12'\)\} shows \/ hides this view`/);
});

test('N16: the working folder keeps its leading slash - its text is an ltr span inside the rtl box', () => {
  assert.match(ts, /cwd\.replaceChildren\(h\('span', \{ dir: 'ltr' \}, cur \?/);
  assert.match(block, /\.ht-cwd > span \{ unicode-bidi: isolate; \}/);
});

test('N16: CSS - full bleed, 44 px bar, 44 px underline tabs in the host colour, × on hover / the open tab', () => {
  assert.match(block, /#main > \.body:has\(> \.view > \.hostterm:not\(\.hidden\)\) \{ padding: 0; \}/);
  assert.match(block, /\.ht-bar \{ height: 44px;/);
  assert.match(block, /\.ht-tab \{ height: 44px;[^}]*border-bottom: 2px solid transparent;/);
  assert.match(block, /\.ht-tab\.on \{[^}]*border-bottom-color: var\(--bad\);/);
  assert.match(block, /\.ht-tab:hover \.ht-x, \.ht-tab\.on \.ht-x, \.ht-x:focus-visible \{ opacity: 1; \}/);
});
