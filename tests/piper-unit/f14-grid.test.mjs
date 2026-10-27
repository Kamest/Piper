// node --test: F14 grid - a bar with state + name, its buttons on hover, "+ Add pane" as a popover.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const menu = readFileSync(new URL('../../piper/web/src/menu.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const fn = (name) => main.slice(main.indexOf(`function ${name}(`), main.indexOf('\n}\n', main.indexOf(`function ${name}(`)) + 2);
const block = css.slice(css.indexOf('/* ==== 9.78 F14'), css.indexOf('/* ==== end F14 ==== */'));

test('F14: the pane bar keeps ⤢ ⋯ ✕; ◀ ▶ moved into ⋯ (Move left / right, with the reason when not possible)', () => {
  const p = fn('paneLabel');
  assert.doesNotMatch(p, /'◀'\)|'▶'\)/);
  // 9.G: the three are line icons now (expand / shrink, more, x); data-act names them for gui.py
  assert.match(p, /h\('span', \{ class: 'pane-tools' \},\n\s+h\('button', \{ class: 'mini', 'data-act': z \? 'unzoom' : 'zoom', title: kbd\(z \?[\s\S]*?fi\(z \? 'shrink' : 'expand', 'gi'\)\),\n\s+more,\n\s+h\('button', \{ class: 'mini', 'data-act': 'remove'[\s\S]*?fi\('x', 'gi'\)\)\)\);/, 'the board\'s order ⤢ ⋯ ✕');
  assert.match(p, /\{ label: 'Move left', title: 'swap with the pane before \(or drag the title\)', disabled: why\(-1\), run: \(\) => move\(-1\) \}/);
  assert.match(p, /\{ label: 'Move right', title: 'swap with the pane after \(or drag the title\)', disabled: why\(1\), run: \(\) => move\(1\) \}/);
  assert.match(p, /'the agent stays on the left, project tools on the right'/);
  assert.match(p, /el\.draggable = !z/, 'drag-swap stays');
  assert.match(p, /continueLabel\(s\)/, 'Continue in an ended pane stays');
  assert.match(p, /class: 'name', ondblclick: \(event: Event\) => \{ event\.stopPropagation\(\); if \(s\.kind !== 'advisor'\) renameSession\(s\); \}/);

});

test('F14: the empty cell is "+ Add pane" (+ × and the count only near the limit); the popover opens at once when asked for', () => {
  const g = fn('gridSlot');
  assert.match(g, /'\+ Add pane'/);
  assert.match(g, /members\.length >= GRID_MAX - 2/);
  assert.match(g, /onclick: cancelGridAdd/);
  assert.doesNotMatch(g, /Add to grid|ht-pill/);
  assert.match(g, /if \(slotFresh\) \{/);
  assert.match(main, /if \(gridAdd && !wasAdd && r\.fx\.add\) slotFresh = true;/);
});

test('F14: the popover - sessions, + New session, host shells with >_ (no HOST chip), Files / HTTP / Database beside, + Database pane; a search hint', () => {
  const e = fn('addPaneEntries');
  const order = ["{ group: 'Sessions' }", "'+ New session…'", "{ group: 'Host shells' }", '`Host shell · ${x.title}`', '+ New host shell', "{ group: 'Beside the active session' }",
    '`Files · ${pname}`', '`HTTP · ${pname}`', "'Database · this project'", "{ group: 'Database' }", "'+ Database pane'"];
  const at = order.map((k) => e.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at);
  assert.doesNotMatch(e, /HOST'|ht-pill/);
  assert.match(e, /title: `a login shell on this Mac - no sandbox · \$\{x\.path\}`/);
  assert.match(e, /disabled: dbs >= GRID_DB_MAX \? `\$\{GRID_DB_MAX\} Database panes at most` : false/);
  assert.match(fn('addPaneMenu'), /search: true, placeholder: 'Session, shell, Files, DB…'/);
  assert.match(menu, /else if \(hint\) searchRow\.append\(h\('span', \{ class: 'mph' \}, opts\.placeholder\)\);/);
});

test('F14: CSS - tools on hover / focus (always on touch), the active pane framed in its state\'s colour', () => {
  assert.match(block, /\.body\.grid \.tlabel \.pane-tools \{ opacity: 0;/);
  assert.match(block, /\.body\.grid \.term:hover \.tlabel \.pane-tools, \.body\.grid \.term:focus-within \.tlabel \.pane-tools \{ opacity: 1; \}/);
  assert.match(block, /@media \(hover: none\) \{ \.body\.grid \.tlabel \.pane-tools \{ opacity: 1; \} \}/);
  assert.match(block, /\.dot\.st-waiting[^{]*\{ border-color: var\(--warn\); \}/);
  assert.match(block, /\.dot\.st-error[^{]*\{ border-color: var\(--bad\); \}/);
  assert.match(block, /@container gt \(max-width: 360px\) \{\n  \.body\.grid \.term:not\(\.hostpane\) \.tlabel \{ flex-wrap: nowrap; \}/, 'no empty second row in a narrow pane');
});
