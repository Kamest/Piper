// node --test: the final browser walk - card beside the context panel, sticky Settings search,
// a shortcut that removes a menu's anchor closes the menu, Timesheet says the project once.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const menu = readFileSync(new URL('../../piper/web/src/menu.ts', import.meta.url), 'utf8');
const ts = readFileSync(new URL('../../piper/web/src/timesheet.ts', import.meta.url), 'utf8');
const block = css.slice(css.indexOf('/* ==== 9.134'), css.indexOf('/* ==== end 9.134'));

test('the approval card sits left of the open context panel, not over its commit box', () => {
  assert.match(block, /body:not\(\.no-inspector\):not\(\.drawer-open\) #pending \{ right: calc\(var\(--ctx-w\) \+ 16px\); \}/);
  // where the card cannot fit beside the panel it goes back to the edge; the drawer keeps its own rules
  assert.match(block, /@media \(max-width: 1299px\) \{ body:not\(\.no-inspector\):not\(\.drawer-open\):not\(\.queue-mini\):not\(\.no-queue\) #pending \{ right: 16px; \} \}/);
  assert.match(block, /@media \(max-width: 1000px\) \{ body:not\(\.no-inspector\):not\(\.drawer-open\) #pending \{ right: 16px; \} \}/);
  assert.match(css, /body\.drawer-open #pending \{ right: calc\(var\(--drawer-w\) \+ 16px\); \}/);
});

test('the Settings search stays at the top of the scrolling side nav', () => {
  assert.match(block, /\.settings \.st-side > \.st-searchbox \{ position: sticky; top: 0;/);
});

test('a shortcut that takes the menu anchor away (⌘G under "+ Add pane") closes the menu', () => {
  const i = menu.indexOf('if (anchor && (e.metaKey || e.ctrlKey || e.altKey)) window.setTimeout(() => { if (!anchor.isConnected) close(false); }, 0);');
  assert.ok(i > 0, 'anchor check present');
  assert.ok(i < menu.indexOf('if (!searchable() || e.metaKey || e.ctrlKey || e.altKey) return;'), 'runs before the modifier early return');
});

test('Timesheet assign row names the project only when it differs from the session', () => {
  assert.match(ts, /u\.project && u\.project !== u\.name \? h\('span', \{ class: 'mut' \}, ` \$\{u\.project\}`\) : null/);
});
