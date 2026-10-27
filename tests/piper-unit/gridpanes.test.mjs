// node --test: W26 grid panes - sessions, host shells and Database panes in one grid: the pure helpers (web/src/gridlayout.ts), the layout gate (layout.ts) and the wiring in main.ts / hostterm.ts
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { addPane, besideOnly, GRID_DB_MAX, GRID_MAX, gridPanes, HTTP_PANE, keepPick, layoutFor, migratePick, nextDbPane, paneKind, presetOf, PRESETS, removePane, replaceFocused, shapeOf, swapPanes } from '../../piper/web/dist/gridlayout.js';
import { defaultLayout, layoutReduce } from '../../piper/web/dist/layout.js';
import { hostKey } from '../../piper/web/dist/clipkeys.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const main = src('main.ts');

test('ended sessions offer Continue / Retry in the grid header and in each session pane', () => {
  const header = main.slice(main.indexOf('function header('), main.indexOf('function layoutControl('));
  assert.match(header, /if \(ended && !sshOnly\(s\.id\) && \(grid \|\| s\.state !== 'exited'\)\)/);
  const pane = main.slice(main.indexOf('function paneLabel('));
  assert.match(pane, /if \(s && \['exited', 'error', 'blocked'\]\.includes\(s\.state\)\) el\.append\(\s*h\('button', \{ class: 'mini primary', onclick: \(\) => void act\(s, 'retry'\) \}, continueLabel\(s\)\)\)/);
});

test('limits: 9 panes, 3 Database panes, a 3 × 3 layout; Auto fills 7-9 panes in three columns', () => {
  assert.equal(GRID_MAX, 9);
  assert.equal(GRID_DB_MAX, 3);
  assert.equal(presetOf('3x3').cap, 9);
  assert.deepEqual(shapeOf('3x3', 9), { dir: 'row', groups: [3, 3, 3] });
  assert.deepEqual(shapeOf('3x3', 7), { dir: 'row', groups: [3, 3, 1] });
  assert.deepEqual(shapeOf('auto', 7).groups, [2, 2, 3]);
  assert.deepEqual(shapeOf('auto', 8).groups, [2, 3, 3]);
  assert.deepEqual(shapeOf('auto', 9).groups, [3, 3, 3]);
  assert.deepEqual(shapeOf('auto', 12).groups, [3, 3, 3]);
});

test('paneKind by id: host-<hex> = host shell, db:1..9 = Database pane, the rest a session', () => {
  assert.equal(paneKind('ab12cd34'), 'session');
  assert.equal(paneKind('host-1f2e'), 'host');
  assert.equal(paneKind('db:2'), 'db');
  assert.equal(paneKind('db:x'), 'session');
  assert.equal(paneKind('db:10'), 'session');
});

test('nextDbPane: the lowest free db:n, none at the cap', () => {
  assert.equal(nextDbPane([]), 'db:1');
  assert.equal(nextDbPane(['db:1', 'a']), 'db:2');
  assert.equal(nextDbPane(['db:2']), 'db:1');
  assert.equal(nextDbPane(['db:1', 'db:2', 'db:3']), '');
});

test('gridPanes: a pick keeps its live sessions, listed shells, Database panes; 9.45 migratePick: Automatic once', () => {
  const live = ['s1', 's2', 's3'];
  assert.deepEqual(migratePick(null, true, live), live, 'the grid was on: its live sessions');
  assert.deepEqual(migratePick(null, false, live), [], 'Single: an empty grid (nothing to overwrite later)');
  assert.deepEqual(migratePick(['s2'], true, live), ['s2'], 'a stored pick stays');
  assert.equal(migratePick(null, true, Array.from({ length: 12 }, (_, i) => `s${i}`)).length, GRID_MAX);
  assert.deepEqual(gridPanes(['host-a', 's2', 'db:1', 'gone', 's2'], live, ['host-a'], true), ['host-a', 's2', 'db:1']);
  assert.deepEqual(gridPanes(['host-a', 's2', 'db:1'], live, null, true), ['s2', 'db:1'], 'the host module off');
  assert.deepEqual(gridPanes(['host-a', 's2', 'db:1'], live, ['host-a'], false), ['host-a', 's2'], 'module db off');
  assert.deepEqual(gridPanes(['host-gone', 's1'], live, ['host-a'], true), ['s1'], 'a shell closed elsewhere');
  assert.deepEqual(gridPanes(['db:1', 'db:2', 'db:3', 'db:4'], live, [], true), ['db:1', 'db:2', 'db:3']);
  assert.deepEqual(gridPanes([], live, [], true), [], 'an emptied pick stays empty');
});

test('9.45 addPane / removePane / swapPanes / replaceFocused / layoutFor: adds never replace, a preset never hides', () => {
  assert.deepEqual(addPane(['a', 'b'], 'files', 'a', 'a'), { pick: ['a', 'files', 'b'] }, 'after the focused pane');
  assert.deepEqual(addPane(['a', 'b'], 'files', '', 'b'), { pick: ['a', 'b', 'files'] }, 'else after the active one');
  assert.deepEqual(addPane(['b'], 'files', '', 'a'), { pick: ['b', 'a', 'files'] }, 'the active session joins when missing');
  assert.deepEqual(addPane([], 'x', '', ''), { pick: ['x'] });
  const same = ['a', 'files'];
  assert.equal(addPane(same, 'files', '', 'a').pick, same, 'in already: nothing');
  const full = Array.from({ length: GRID_MAX }, (_, i) => `s${i}`);
  assert.equal(addPane(full, 'files', '', 's0').refuse, `The grid has ${GRID_MAX} panes at most`);
  assert.deepEqual(removePane(['a', 'b', 'c'], 'b'), { pick: ['a', 'c'], next: 'c' });
  assert.deepEqual(removePane(['a', 'b', 'c'], 'c'), { pick: ['a', 'b'], next: 'b' });
  assert.deepEqual(removePane(['a'], 'a'), { pick: [], next: '' });
  assert.deepEqual(removePane(['a'], 'x'), { pick: ['a'], next: '' });
  assert.deepEqual(swapPanes(['a', 'b', 'c'], 'a', 'c'), ['c', 'b', 'a']);
  assert.deepEqual(swapPanes(['a', 'b'], 'a', 'x'), ['a', 'b']);
  assert.deepEqual(replaceFocused(['a', 'files', 'b'], 'c', 'b', 'a'), ['a', 'files', 'c'], 'the focused session pane');
  assert.deepEqual(replaceFocused(['a', 'files', 'b'], 'c', 'files', 'a'), ['c', 'files', 'b'], 'a focused Files pane: the active one');
  assert.deepEqual(replaceFocused(['files'], 'c', '', ''), ['c', 'files'], 'no session pane: it joins first');
  assert.equal(layoutFor('2x2', 4), '2x2');
  assert.equal(layoutFor('2x2', 5), 'auto', 'more panes than the preset holds: Auto, none hidden');
  assert.equal(layoutFor('columns', 7, 900, 650), 'auto', 'seven narrow columns would cover their controls');
  assert.equal(layoutFor('columns', 2, 900, 650), 'columns', 'a simple split fits');
  assert.equal(layoutFor('rows', 7, 900, 650), 'auto', 'short rows would clip the toolbars');
  assert.equal(layoutFor('big-top', 7, 900, 650), 'auto');
  assert.equal(layoutFor('columns', 7, 1800, 900), 'columns', 'the requested layout fits on a larger screen');
  assert.ok(!PRESETS.some((p) => p.id === 'two'), '9.38\'s 2-pane preset is gone');
});

test('keepPick: panes hidden by a module that is off survive an edit, the rest is capped at 9', () => {
  assert.deepEqual(keepPick(['s1'], ['host-a', 's1', 'db:1'], false, true), ['s1', 'host-a']);
  assert.deepEqual(keepPick(['s1'], ['host-a', 's1', 'db:1'], true, false), ['s1', 'db:1']);
  assert.deepEqual(keepPick(['s1'], ['host-a', 's1', 'db:1'], true, true), ['s1'], 'removed while on = gone');
  assert.deepEqual(keepPick(['s1'], null, false, false), ['s1']);
  assert.equal(keepPick(Array.from({ length: 12 }, (_, i) => `s${i}`), null, true, true).length, GRID_MAX);
});

test('the grid turns on with only host-shell / Database panes (no live session)', () => {
  const c = { view: 'term', active: '', lastActive: '', ids: [], gridIds: [], hasPty: () => false, splitBlock: () => 'x',
    paneBlock: () => '', gridOn: true, feedOn: false, sessionView: false };
  assert.equal(layoutReduce(defaultLayout(true), { t: 'grid' }, c).fx.refuse, 'No live session for the grid');
  const r = layoutReduce(defaultLayout(true), { t: 'grid' }, { ...c, gridOther: true });
  assert.equal(r.fx.refuse, undefined);
  assert.equal(r.state.center.kind, 'grid');
});

test('⌥⇧T / ⌥⇧W reach the grid (hostKey with inHost for the grid); ⌥⇧W only for a focused host-shell pane', () => {
  assert.equal(hostKey({ code: 'KeyT', altKey: true, shiftKey: true, metaKey: false, ctrlKey: false }, true), 'new');
  assert.match(main, /const hk = hostKey\(e, view === 'host' \|\| inGrid\);/);
  assert.match(main, /hk === 'close' && view !== 'host' && !\(paneKind\(gridFocus\) === 'host' && gridIdsShown\(\)\.includes\(gridFocus\)\)\) return;/);
  assert.match(main, /if \(view === 'host'\) void hostView\.open\(\); else newGridShell\(\);/);
});

test('main.ts wiring: panes are ids, host shells borrow the Host view\'s terminal, Database panes are own DbViews', () => {
  assert.match(main, /return gridPanes\(gridPick \?\? migratePick\(null, lay\.center\.kind === 'grid', liveIds\(\)\), state\.sessions\.filter\(\(x\) => !x\.hidden\)\.map\(\(x\) => x\.id\), hostsOn\(\) \? hostItems\(state\)\.map\(\(x\) => x\.id\) : null, on\('db'\), on\('http'\)\);/);
  assert.match(main, /if \(gridPick === null\) storePick\(migratePick\(null, lay\.center\.kind === 'grid', liveIds\(\)\)\);/, '9.45: migrated once on the first state');
  assert.match(main, /window\.localStorage\.removeItem\('piper\.gridpins'\)/, '9.45: the pins are gone');
  // 9.45 browser: syncLayout runs at load (gridMembers, gridAdd) - a `let` / arrow const after it = TDZ, Piper did not load
  const boot = main.indexOf('\nsyncLayout();\n');
  assert.ok(boot > 0 && main.indexOf('\nlet gridAdd = false;') < boot && main.indexOf('\nlet gridPick') < boot);
  for (const fn of ['hasTerminal', 'hostsOn', 'liveIds']) assert.match(main, new RegExp(`\\nfunction ${fn}\\(`), `${fn} is hoisted`);
  assert.match(main, /const next = keepPick\(ids, gridPick, hostsOn\(\), on\('db'\), on\('http'\)\);/);
  assert.match(main, /storePick\(fn\(gridMembers\(\)\)\);/);
  assert.match(main, /const t = k === 'host' \? hostView\.term\(id\) : null;/, 'one terminal (one ws) per shell');
  assert.match(main, /storeKey: id === DB_HERE \? 'side-' \+ owner : `grid-\$\{id\.slice\(3\)\}`, compact: true,/, 'its own remembered connection');
  assert.match(main, /agentLabel: \(\) => \{ const s = session\(active\);/, '"→ <session>" names who gets it');
  assert.match(main, /\[\.\.\.dbPanes\.values\(\), \.\.\.dbHerePanes\.values\(\)\]\.some\(\(p\) => p\.view\.hasUnsaved\(\)\)/, 'an open transaction in a pane asks before unload');
  assert.match(main, /gridOther: gridMembers\(\)\.some\(\(x\) => paneKind\(x\) !== 'session'\)/);
  assert.match(main, /'Grid: choose panes…'/);
  assert.match(main, /'Grid: add host shell'/);
  assert.match(main, /'Grid: add Database pane'/);
  assert.match(main, /ex \? 'HOST' : 'HOST · no sandbox'/, 'a host pane always says it has no sandbox');
  assert.match(main, /'remove from grid \(the shell keeps running\)'/);
});

test('hostterm.ts: a terminal moved back from a grid pane, term(id, make), pick(id), a tab\'s "Add to grid"', () => {
  const ht = src('hostterm.ts');
  assert.match(ht, /if \(t\.el\.parentElement !== body\) body\.append\(t\.el\);/);
  assert.match(ht, /term\(id, make = true\) \{/);
  assert.match(ht, /if \(!make\) return null;/);
  assert.match(ht, /label: g\.has\(id\) \? 'Remove from grid' : 'Add to grid'/);
  assert.match(ht, /if \(then\) then\(r\.data\.id\);/);
});

test('app.css: a host pane keeps the red frame, a Database pane fills its cell', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.body\.grid \.term\.hostpane \{ border-color: var\(--bad\); \}/);
  assert.match(css, /\.term\.dbpane > \.dbview \{ flex: 1;/);
});

test('browser e2e fixes: a narrow pane title, a short Database pane, an ended shell, the chooser\'s cwd, ⤢ → Host view takes the keys', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  // the title: the cwd / connection hint gives way, the name keeps "Database" readable
  assert.match(css, /\.tlabel > \.mut \{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 20; \}/);
  assert.match(css, /\.tlabel \.name \{ min-width: 5em; \}/);
  // an ended shell: buttons on one line, the tools may wrap inside the title, never into the next pane
  assert.match(css, /\.tlabel > button\.mini \{ flex: none; white-space: nowrap; \}/);
  assert.match(css, /\.tlabel:has\(\.ht-pill\.dead\) \{ flex-wrap: wrap;/);
  assert.match(css, /\.body\.grid \.term\.hostpane:has\(\.ht-pill\.dead\) \{ border-color: var\(--line\); \}/);
  // a short Database pane: one scrolling bar with Run first, a smaller editor, the results stay visible
  assert.match(css, /\.term\.dbpane \.db-bar \{[^}]*flex-wrap: nowrap; overflow-x: auto;/);
  assert.match(css, /\.term\.dbpane \.db-bar > \.primary \{ order: -1; \}/);
  assert.match(css, /\.term\.dbpane \.db-editor \{ height: 28%; min-height: 64px; \}/);
  // a narrow one: the tree yields to an open tab (the console's own picker switches); .wide (the list) keeps one column
  assert.match(css, /@container gt \(max-width: 720px\) \{/);
  assert.match(css, /\.term\.dbpane > \.dbview\.compact:not\(\.wide\) \{ grid-template-columns:/);
  assert.match(css, /\.term\.dbpane > \.dbview\.compact:has\(\.db-tab\):not\(\.db-treeon\) > \.db-left \{ visibility: hidden; border: 0; \}/);
  // the chooser: a host shell's cwd truncates, a session's state keeps its width
  assert.match(main, /const item = \(id: string, name: string, note: string, max: boolean, path = false\) =>/);
  assert.match(main, /false, !exitText\(x\)\);/);
  // ⤢ on a host pane: show() runs before setView renders the view into the page - focus once it is there
  assert.match(src('hostterm.ts'), /queueMicrotask\(\(\) => \{ if \(shown\) views\.get\(active\)\?\.focus\(\); \}\);/);
});

test('9.39 a narrow Database pane: "▤ Tables" in the tab strip swaps the open tab for the table tree, a table / console / Esc goes back', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const db = src('dbview.ts');
  // the toggle with an open tab (CSS: a narrow grid pane, N10 the Database view ≤ 640 px); without a tab the tree shows anyway
  assert.match(db, /if \(connTabs\(\)\.length\) tabsEl\.append\(h\('button', \{ class: 'db-tab-tree'/);
  assert.match(db, /if \(!connTabs\(\)\.length\) treeOn = false;/);
  assert.match(db, /el\.classList\.toggle\('db-treeon', treeOn && !!conn\);/);
  // opening a table or a console, or switching the connection, leaves the tree
  assert.equal((db.match(/^\s*treeOn = false;/gm) || []).length, 3);
  assert.match(db, /if \(treeOn && e\.key === 'Escape' && !mod\) \{ e\.preventDefault\(\); treeOn = false; draw\(\); return true; \}/);
  // CSS: the button is hidden outside a narrow pane; in the tree mode the tabs stay on top, the tree replaces the body
  assert.match(css, /^\.db-tab-tree \{ display: none;/m);
  assert.match(css, /\.term\.dbpane \.db-tab-tree \{ display: inline-flex; \}/);
  assert.match(css, /\.dbview\.compact\.db-treeon:has\(\.db-tab\) > \.db-main > \.db-body \{ display: none; \}/);
  // not the global .tree class (line-level font / overflow rule of the Files tree)
  assert.doesNotMatch(db, /classList\.toggle\('tree'/);
});

test('9.42 HTTP pane: a pane kind like Files, kept while the http module is off, the beside pair without the grid module', () => {
  assert.equal(HTTP_PANE, 'http');
  assert.equal(paneKind('http'), 'http');
  assert.equal(paneKind('files'), 'files');
  // gridPanes: shown with the module on (default), dropped with it off; keepPick remembers it meanwhile
  assert.deepEqual(gridPanes(['a', 'http', 'files'], ['a'], null, true), ['a', 'http', 'files']);
  assert.deepEqual(gridPanes(['a', 'http', 'files'], ['a'], null, true, false), ['a', 'files']);
  assert.deepEqual(keepPick(['a'], ['a', 'http'], true, true, false), ['a', 'http']);
  assert.deepEqual(keepPick(['a'], ['a', 'http'], true, true, true), ['a']);
  // the beside pair (no grid module): one session with Files / HTTP / its Database
  assert.equal(besideOnly(['a', 'http']), true);
  assert.equal(besideOnly(['a', 'files', 'http', 'db:here']), true);
  assert.equal(besideOnly(['a', 'b', 'http']), false);
  assert.equal(besideOnly(['a', 'http', 'db:1']), false);
  const c = { view: 'term', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: [], hasPty: () => true, splitBlock: () => '',
    paneBlock: () => '', gridOn: false, feedOn: true, sessionView: true };
  assert.deepEqual(layoutReduce({ ...defaultLayout(true) }, { t: 'pane', pane: 'http' }, c).fx.pick, ['a', 'http']);
});

test('9.42 main.ts: the HTTP pane lends the HTTP tab\'s panel, follows the active session, is in Add to grid / Choose panes', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(main, /const httpPaneEl = h\('div', \{ class: 'term httppane hidden' \}, httpLabel\);/);
  assert.match(main, /if \(httpPanel\.el\.parentElement !== httpPaneEl\) httpPaneEl\.append\(httpPanel\.el\);/);
  assert.match(main, /if \(httpFor !== active\) \{\s*httpFor = active;\s*if \(s\?\.kind === 'project'\) httpPanel\.show\(s\.project\);/);
  assert.match(main, /const el = t\?\.el \?\? d\?\.el \?\? \(k === 'http' \? httpPane\(\) : filesPane\(\)\);/);
  assert.match(main, /if \(!shown\.includes\(HTTP_PANE\)\) httpPaneEl\.classList\.add\('hidden'\);/);
  assert.match(main, /k === 'http' \? httpPaneEl : hostView/);
  assert.match(main, /if \(gridFocus && k === 'http'\) return httpPanel\.project\(\);/);
  assert.match(main, /on\('db'\), on\('http'\)\);/);
  assert.match(main, /keepPick\(ids, gridPick, hostsOn\(\), on\('db'\), on\('http'\)\)/);
  // Add to grid and Choose panes: under "Beside the active session", only with the http module
  assert.match(main, /proj && on\('http'\) && !members\.includes\(HTTP_PANE\) \? \{ label: h\('span', \{ class: 'ap-row' \}, ic\('http'\), `HTTP · \$\{pname\}`\), search: `http \$\{pname\}`, run: \(\) => dispatch\(\{ t: 'add', id: HTTP_PANE \}\) \}/);
  assert.match(main, /on\('http'\) && \(proj \|\| chosen\.includes\(HTTP_PANE\)\) \? item\(HTTP_PANE, 'HTTP requests'/);
  assert.match(main, /pane === HTTP_PANE && !on\('http'\) \? 'The HTTP module is off \(Settings > Features\)'/);
  assert.match(main, /\} else if \(kind === 'http'\) \{/);
  assert.match(main, /alone = \['the whole HTTP tab', \(\) => setView\('http'\)\];/);
  // a narrow pane stacks the list over the request
  assert.match(css, /\.term\.httppane > \.httppanel \{ flex: 1;/);
  assert.match(css, /@container gt \(max-width: 640px\) \{ \.term\.httppane > \.httppanel \{ grid-template-columns: minmax\(0, 1fr\);/);
});


test('Single clears dimensions of hidden panes dropped from the remembered grid', () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const body = compiled.match(/function clearGridLayout\(\) \{([\s\S]*?)\n\}/)[1];
  const pane = () => ({
    classList: { remove(name) { assert.equal(name, 'gcell'); this.removed = true; } },
    style: { left: '0px', top: '0px', width: '50%', height: '100%' },
  });
  const hidden = pane(), current = pane();
  const bodyEl = { querySelectorAll(selector) {
    assert.equal(selector, ':scope > .gcell');
    return [hidden, current];
  } };
  const run = new Function('bodyEl', 'gridCells', 'gridShape', 'splitEls', body);
  // Replacing/removing a session drops it from gridCells, but keeps its terminal DOM for later.
  run(bodyEl, [current], {}, new Map());
  for (const item of [hidden, current]) {
    assert.equal(item.classList.removed, true);
    assert.deepEqual(item.style, { left: '', top: '', width: '', height: '' });
  }
});


test('database chooser names existing panes and adds only to its cancellable draft', () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const section = compiled.slice(compiled.indexOf('function gridChooser()'));
  const body = section.slice(section.indexOf("const dbs ="), section.indexOf("\n        }\n    };"));
  const h = (tag, props, ...children) => ({ tag, props, children });
  const render = new Function('chosen', 'dbPanes', 'paneKind', 'GRID_DB_MAX', 'GRID_MAX', 'h', 'list', 'item', 'nextDbPane', 'draw', "const DB_HERE = 'db:here';\n" + body);
  const chosen = ['db:1'];
  const panes = new Map([['db:1', { view: { current: () => ({ project: 'shop', label: 'TEST' }) } }]]);
  let nodes = [], draws = 0;
  const list = { append: (...items) => nodes.push(...items), querySelector: () => null };
  const run = () => { nodes = []; render(chosen, panes, paneKind, GRID_DB_MAX, GRID_MAX, h, list,
    (id, name, note) => ({ id, name, note }), nextDbPane, () => draws++); };
  run();
  assert.equal(nodes.find(x => x.id === 'db:1').name, 'shop · TEST');
  assert.equal(nodes.some(x => x.id === 'db:2'), false);
  const add = nodes.find(x => x.tag === 'button');
  assert.equal(add.props.class, 'grid-db-add');
  assert.equal(add.children[0].props['aria-hidden'], 'true', 'the decorative plus is not part of the accessible name');
  assert.equal(add.children[1].trim(), 'Add database panel');
  nodes.find(x => x.tag === 'button').props.onclick();
  assert.deepEqual(chosen, ['db:1', 'db:2']);
  assert.equal(panes.size, 1, 'adding must not instantiate a view before Show');
  assert.equal(draws, 1);
  run();
  assert.equal(nodes.find(x => x.id === 'db:2').name, 'Choose a database…');
  nodes.find(x => x.tag === 'button').props.onclick();
  run();
  assert.equal(nodes.some(x => x.tag === 'button'), false, 'no fourth independent panel');
  chosen.push('a', 'b', 'c', 'd', 'e', 'f');
  chosen.splice(chosen.indexOf('db:3'), 1);
  chosen.push('g');
  run();
  assert.equal(nodes.find(x => x.tag === 'button').props.disabled, true, 'respect total grid limit');
});

test('grid chooser excludes ended sessions and their saved picks, keeps starting/blocked sessions and tools', () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const setup = compiled.split('function gridChooser() {')[1].split("const show = h(")[0];
  const select = new Function('state', 'gridMembers', 'hostsOn', 'hostItems', 'paneKind',
    setup + 'return {sessions, chosen};');
  const sessions = [
    { id: 'ready', state: 'ready', pid: 10 },
    { id: 'ended', state: 'exited', pid: 11 }, // scrollback keeps its old PID
    { id: 'ended-no-pid', state: 'exited', pid: null },
    { id: 'starting', state: 'starting', pid: null },
    { id: 'blocked', state: 'blocked', pid: null },
    { id: 'hidden', state: 'ready', hidden: true },
  ];
  const saved = ['ended', 'ready', 'files', 'db:1', 'host-test', 'hidden'];
  const result = select({ sessions }, () => saved, () => false, () => [], paneKind);
  assert.deepEqual(result.sessions.map(x => x.id), ['ready', 'starting', 'blocked']);
  assert.deepEqual(result.chosen, ['ready', 'files', 'db:1', 'host-test']);
  assert.equal(saved[0], 'ended', 'opening/cancelling the dialog must not mutate the workspace');
});


test('grid chooser bounds both columns and lets rows scroll without shrinking', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(main, /class: 'plist gridchooser'/);
  assert.match(css, /\.gridchooser \{[^}]*scrollbar-gutter: stable;/);
  assert.match(css, /\.gridchooser \.pitem \{[^}]*grid-template-columns: minmax\(0, 1fr\) minmax\(0, 42%\);[^}]*flex-shrink: 0;/);
});


test('session tools detach from ownership when joining a shared grid, retaining full restore membership', () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const helpers = compiled.slice(compiled.indexOf('function captureWorkspace()'), compiled.indexOf('const GLOBAL_VIEWS'));
  const store = compiled.slice(compiled.indexOf('function storePick('), compiled.indexOf('/** change the grid', compiled.indexOf('function storePick(')));
  const context = new Function('paneKind', 'keepPick', `
    let gridPick = ['a'], active = 'a', gridLayout = 'auto', gridFr = {main:[], cross:[]};
    let lay = {center:{kind:'grid'}}, workspaceOwner = '', globalWorkspace = null;
    let workspaceFr = null, gridShape, gridAdd = false, gridAddBack = null;
    const sessionWorkspaces = new Map(), writeStore = () => {}, machineKey = k => k;
    const window = {localStorage:{setItem(){}}}, hostsOn = () => true, on = () => true;
    const gridMembers = () => gridPick;
    ${helpers}
${store}
    return {ownWorkspace, storePick,
      activate(id) { switchWorkspace(id); active = id; },
      single() { lay.center = {kind:'single'}; },
      state() { return {owner:workspaceOwner, pick:gridPick, saved:sessionWorkspaces}; }};
  `)(paneKind, keepPick);
  context.ownWorkspace('a');
  context.storePick(['a', 'db:here']);
  context.storePick(['a', 'b', 'db:here', 'host-1']);
  assert.equal(context.state().owner, '');
  assert.deepEqual(context.state().saved.get('a').pick, ['a', 'db:here'], 'retain the tools before joining the explicit grid');
  context.ownWorkspace('a');
  assert.equal(context.state().owner, '', 'adding another tool must not claim a multi-agent grid');
  context.single(); context.activate('c');
  assert.deepEqual(context.state().pick, ['a', 'b', 'db:here', 'host-1'], 'Single → another session keeps the full restore set for the capacity guard');
});
