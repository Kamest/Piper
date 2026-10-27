// node --test: UX cleanup after the grid - the pure
// helpers and the wiring in main.ts each decision changed.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { inboxCommands } from '../../piper/web/dist/inbox.js';
import { defaultLayout, FOLD_VIEWS, layoutReduce, QUEUE_VIEWS } from '../../piper/web/dist/layout.js';
import { DB_HERE, FILES_PANE, gridPanes, paneKind } from '../../piper/web/dist/gridlayout.js';
import { endedToClose, ENDED_CLOSE_S, splitHosts } from '../../piper/web/dist/hostterm.js';
import { pick } from '../../piper/web/dist/palette.js';
import { NOTICE_MAX, NoticeLog } from '../../piper/web/dist/notices.js';
import { buildQueue } from '../../piper/web/dist/queue.js';
import { attachLabel } from '../../piper/web/dist/text.js';
import { keyGate, SHORTCUTS, shownShortcuts } from '../../piper/web/dist/shortcuts.js';
import { matchingPreset, presetChips, presetModules } from '../../piper/web/dist/setup.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const main = src('main.ts');

test('D9 A1: the inbox rows follow the forge - GitHub only has no GitLab automation', () => {
  const gh = inboxCommands('GitHub', false, 0).map((r) => r.label);
  assert.deepEqual(gh, ['GitHub inbox', 'Refresh GitHub']);
  const gl = inboxCommands('Inbox', true, 3);
  assert.deepEqual(gl.map((r) => r.act), ['focus', 'refresh', 'rules']);
  assert.deepEqual(gl.map((r) => r.label), ['Inbox', 'Refresh Inbox', 'GitLab automation…']);   // not "Inbox inbox"
  assert.equal(inboxCommands('GitLab', true, 0)[0].label, 'GitLab inbox');
  assert.match(gl[0].hint, /^3 new · .*MRs/);
  assert.match(inboxCommands('GitHub', false, 0)[0].hint, /PRs/);
});

test('D9 A1: no palette row is gated by the gitlab module alone; the Keys group is the inbox view', () => {
  assert.doesNotMatch(main, /module: 'gitlab'/);
  assert.doesNotMatch(main, /'GitLab view': 'gitlab'/);
  assert.match(main, /g === 'inbox' \? viewOn\('gitlab'\)/);
});

test('D9 A3: ⌘G, ⌘\\ and ⌘0 reach the browser while their module is off', () => {
  assert.match(main, /e\.key === 'g' && keyOn\('⌘G'\)/);
  assert.match(main, /appKey\(e\) === '\\\\' && keyOn\('⌘\\\\'\)/);
  assert.match(main, /appKey\(e\) === '0' && keyOn\('⌘0'\)/);
});

test('D8 R17: one source of the queue / fold views, the host shells keep the queue', () => {
  assert.ok(QUEUE_VIEWS.has('host'));
  assert.deepEqual([...FOLD_VIEWS], ['files', 'http']);
  assert.doesNotMatch(main, /const (QUEUE|STRIP)_VIEWS/);
});

test('D4: Attach in <app> names the terminal Settings picked, generic without one', () => {
  assert.equal(attachLabel('Warp'), 'Attach in Warp');
  assert.equal(attachLabel('Ghostty'), 'Attach in Ghostty');
  assert.equal(attachLabel(''), 'Open in terminal');
  assert.equal(attachLabel(undefined), 'Open in terminal');
  assert.doesNotMatch(main, /'Open in terminal'/);
  assert.doesNotMatch(main, /'Terminal \(host\)'/);
  assert.match(main, /id: 'host', label: 'Host shells'/);
  assert.match(src('hostterm.ts'), /'ht-title', title: `[^`]*` \}, 'Host shells'/);
  assert.doesNotMatch(main, /'Host shell here', keys: kbd\('⌥F12'\)/);   // ⌥F12 toggles the view, it opens no shell here
});

test('Attach is available in the session overflow menu, without a header button', () => {
  const header = main.slice(main.indexOf('function header('), main.indexOf('function header(') + 10000);
  const menu = main.slice(main.indexOf('function openRailMenu('), main.indexOf('function popMenu('));
  assert.doesNotMatch(header, /attachLabel\(state\.terminal_app\)/);
  assert.match(menu, /live && s\.pid !== null && !s\.tty \? \{ label: attachLabel\(state\.terminal_app\), icon: 'fi-attach', run: \(\) => void openInTerminal\(s\)/);
});

test('D8: Audit is core, History is called History', () => {
  assert.match(main, /\n {4}\{ id: 'audit', label: 'Audit'/);
  assert.doesNotMatch(main, /on\('usage'\)[^\n]*id: 'audit'/);
  assert.doesNotMatch(main, /Session history \(audit\)|'audit of this session'/);
});

test('D8 R14: Continue / Retry in the queue acts at once, a lost conversation is toasted', () => {
  assert.match(main, /continueSession: \(sid\) => \{ const s = session\(sid\); if \(s\) void act\(s, 'retry'\); \}/);
  assert.match(main, /a === 'retry' && r\?\.lost/);
});

test('D8: no 1100 px threshold for the context panel', () => {
  assert.doesNotMatch(main, /innerWidth > 1100/);
});

test('D14 A15: no dead TS left - callerless code and test-only exports are gone', () => {
  assert.doesNotMatch(src('history.ts'), /export function openGraph/);
  assert.doesNotMatch(src('menu.ts'), /export function menuOpen/);
  assert.doesNotMatch(src('sendkeys.ts'), /SEND_KEY/);
  assert.doesNotMatch(src('editor.ts'), /indentEdit|newlineEdit/);
  assert.doesNotMatch(src('codenav.ts'), /findMatches|firstMatchFrom|markClasses|FIND_MAX/);
  assert.doesNotMatch(src('gitpanel.ts'), /conversationIndex|markCounts/);
  assert.doesNotMatch(src('board.ts'), /boardOrder/);
});

test('D14 A11/A12: review always on (no diffReview fallback), Diagnostics in Files follows the diag module', () => {
  for (const f of ['main.ts', 'files.ts', 'gitpanel.ts', 'types.ts']) assert.doesNotMatch(src(f), /diff_?[rR]eview/, f);
  assert.match(src('files.ts'), /k !== 'diag' \|\| !!deps\.diagOn\?\.\(\)/);
  assert.match(main, /diagOn: \(\) => on\('diag'\)/);
});

test('D14 A15: features.py counts the grid from the same GRID_MAX, every module has its gallery sketch', async () => {
  const { GRID_MAX } = await import('../../piper/web/dist/gridlayout.js');
  const py = readFileSync(new URL('../../piper/features.py', import.meta.url), 'utf8');
  assert.equal(Number(py.match(/^GRID_MAX = (\d+)$/m)?.[1]), GRID_MAX);
  assert.doesNotMatch(py, /Up to 6|four terminals|All 13/);
  const { existsSync } = await import('node:fs');
  for (const [, id] of py.matchAll(/^ {4}\("([a-z]+)", "/gm)) if (id !== 'host')   // D10: in no preset, no gallery tile
    assert.ok(existsSync(new URL(`../../piper/web/img/modules/${id}.svg`, import.meta.url)), id);
});

test('D14 A15: the literally dead CSS classes are gone (note-resolved is composed at runtime and stays)', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  for (const c of ['gitlog', 'review-history', 'review-keys', 'httpline', 'ichip', 'lnote', 'noted', 'fcount', 'fcur', 'fedit', 'ffind',
    'fhit', 'rpath', 'drop-into', 'p-warn', 'qm-sep', 'gf-all', 'db-cbody', 'db-chevron', 'db-miss', 'db-pg', 'db-pgt', 'db-subtree', 'dbc-who', 'hl-h'])
    assert.doesNotMatch(css, new RegExp(`\\.${c}\\b`), c);
  assert.match(css, /\.review-note\.note-resolved/);
});

test('D15: the Keys dialog and the handler read one registry - a gated key is live exactly while its row shows', () => {
  assert.equal(keyGate('⌘G'), 'grid');
  assert.equal(keyGate('⌘\\'), 'grid');
  assert.equal(keyGate('⌘0'), 'agent');
  assert.equal(keyGate('⌘.'), 'feed');
  assert.equal(keyGate('⌘K'), undefined);
  const none = shownShortcuts(() => false).map((g) => g.title);
  assert.deepEqual(none, ['Navigation', 'Queue (when it has the focus)', 'Session', 'Files']);
  const nav = shownShortcuts(() => false)[0].rows.map((r) => r.k);
  for (const k of ['⌘G', '⌘\\', '⌘0', '⌘.']) assert.ok(!nav.includes(k), k);
  assert.equal(shownShortcuts(() => true).length, SHORTCUTS.length);
  // no hand-kept module map or Ctrl list beside the registry
  assert.doesNotMatch(main, /SHORTCUT_MODULE|CTRL_ROWS|const SHORTCUTS/);
  for (const k of ['⌘G', '⌘\\\\', '⌘0', '⌘.', '⌘D']) assert.ok(main.includes(`keyOn('${k}')`), k);   // ⌘\\ as written in the source
});

test('D15 A8: every ⌘-key of globalKeys has a row; the missing ones are in; descriptions stay short', () => {
  const body = main.slice(main.indexOf('function globalKeys('), main.indexOf('/** ⌘F: xterm renders'));
  const keys = new Set([...body.matchAll(/(?:e\.key(?:\.toLowerCase\(\))?|appKey\(e\)) === '([^']+)'/g)].map((m) => m[1].replace('\\\\', '\\').toUpperCase()));   // '\\\\' in the source = ⌘\\
  const rows = SHORTCUTS.flatMap((g) => g.rows.map((r) => r.k)).join(' ').toUpperCase();
  assert.ok(rows.includes('⇧⌘⏎'), '9.45: ⇧⌘⏎ (Enter) has its row in the ⏎ notation');
  for (const k of keys) if (!['ARROWUP', 'ARROWDOWN', 'ESCAPE', 'ENTER'].includes(k)) assert.ok(rows.includes(k === 'F12' ? '⌘F12' : `⌘${k}`) || rows.includes(`⌘⇧${k}`), `⌘${k} has no Keys row`);
  for (const k of ['⌘.', '⌘⌥A', '?']) assert.ok(SHORTCUTS.some((g) => g.rows.some((r) => r.k === k)), k);
  for (const g of SHORTCUTS) for (const r of g.rows) assert.ok(r.d.length <= 60, `${r.k}: ${r.d}`);
  assert.doesNotMatch(JSON.stringify(SHORTCUTS), /two terminals|beside: databases|actions: beside/);
});

test('D10: the tab-hiding and start-options policy keys are gone, the host shells are a module', () => {
  for (const f of ['main.ts', 'types.ts', 'settings.ts', 'hostterm.ts', 'gridlayout.ts'])
    assert.doesNotMatch(src(f), /customize_tabs|remember_start_opts|host_terminal/, f);   // tab hiding + remembered options are back in 9.44, without a key
  assert.equal(keyGate('⌥F12'), 'host');
  assert.match(main, /g === 'host' \? hostsOn\(\)/);   // the key and the Keys row follow host_terms.on
  assert.match(main, /if \(hk === 'toggle' && !keyOn\('⌥F12'\)\) return;/);
  assert.match(main, /settings\.show\('features'\)/);   // the off view's Open Settings (9.40: Integrations folded into Features)
});

test('D10 + 9.38: Setup after the welcome keeps the presets and the gallery, plus a link to Settings › Features', () => {
  const setup = src('setup.ts');
  assert.match(setup, /if \(f\) el\.append\(presetsEl\(f\), galleryEl\(f\)\);/);
  assert.doesNotMatch(setup, /f && welcome\) el\.append\(presetsEl/);
  assert.match(setup, /deps\.openSettings\('features'\) \}, 'Modules: Settings › Features'/);
  assert.match(setup, /'Installation check & features'/);
  const f = { modules: {}, onboarded: true, preset: 'all', presets: [],
    list: [['agent', 'Global agent', ''], ['db', 'Databases', ''], ['host', 'Host shells', '']],
    preset_modules: { basic: ['db'], all: ['agent', 'db'] } };
  assert.deepEqual(presetModules(f), ['agent', 'db']);
  assert.deepEqual(presetChips(f, 'all'), ['all 2 modules']);   // the host shells are in no preset
});

test('D10: Settings › HTTP requests › Edit environments… opens the HTTP tab\'s environment dialog', () => {
  assert.match(src('settings.ts'), /if \(id === 'open-http-env'\) return deps\.openHttpEnv\?\.\(\);/);
  assert.match(src('settings.ts'), /f\.is_default === false && s\.id !== 'features';/);
  assert.match(main, /openHttpEnv: \(\) => \{/);
  assert.match(src('httppanel.ts'), /async openEnvironments\(p: string\)/);
  assert.match(src('httpview.ts'), /cls, envDialog, running/);
  // a project without environments can create its first environment directly in the editor
  assert.match(src('httpview.ts'), /if \(!st\.env \|\| !info\.envs\.length\) return envFileEditor\(info\.files\.public\.path\);/);
});

const lctx = (o = {}) => ({ view: 'term', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: ['a', 'b'], hasPty: () => true,
  splitBlock: () => '', paneBlock: () => '', gridOn: true, feedOn: true, sessionView: true, ...o });

test('Overview opens the queue; clicking its active icon folds it to the session strip', () => {
  for (const left of ['queue', 'strip', 'hidden']) {
    const r = layoutReduce({ ...defaultLayout(true), left }, { t: 'overview' }, lctx());
    assert.equal(r.state.left, 'queue', left);
    assert.equal(r.fx.focus, 'queue');
  }
  const again = layoutReduce(layoutReduce(defaultLayout(true), { t: 'overview' }, lctx()).state, { t: 'overview' }, lctx());
  assert.equal(again.state.left, 'queue', 'the palette command always opens the whole queue');
  const folded = layoutReduce(again.state, { t: 'overview', on: false }, lctx());
  assert.equal(folded.state.left, 'strip');
  assert.equal(folded.state.leftBack, 'strip');
  assert.equal(folded.state.leftFold, false);
  assert.equal(folded.fx.focus, 'detail');
  assert.equal(folded.fx.hub, undefined);
  assert.deepEqual(folded.state.center, again.state.center);
  assert.equal(folded.state.right, again.state.right);
  const reopened = layoutReduce(folded.state, { t: 'overview', on: true }, lctx());
  assert.equal(reopened.state.left, 'queue');
  assert.equal(reopened.fx.focus, 'queue');
  for (const view of ['files', 'http', 'convo', 'history', 'gitlab', 'prod', 'host', 'db', 'settings', 'audit', 'usage', 'timesheet', 'setup']) {
    const r = layoutReduce({ ...defaultLayout(true), left: 'queue' }, { t: 'overview', on: true }, lctx({ view }));
    assert.equal(r.fx.toTerm, true, `${view}: Overview returns to the session even if the queue is already visible`);
    assert.equal(r.fx.hub, undefined);
    assert.equal(r.state.left, 'queue');
    assert.deepEqual(r.state.center, { kind: 'single' });
  }
  for (const center of [{ kind: 'grid' }, { kind: 'single', pane: 'files' }]) {
    const r = layoutReduce({ ...defaultLayout(true), center, right: 'agent', rightBack: 'context' }, { t: 'overview', on: true }, lctx());
    assert.deepEqual(r.state.center, center.kind === 'grid' ? center : { kind: 'single' },
      'the grid stays; a zoomed tool returns to the active session');
    assert.equal(r.state.right, 'context', 'the Agent drawer closes so Overview becomes active');
    assert.equal(r.state.left, 'queue');
    assert.equal(r.fx.pick, undefined, 'the saved grid panes stay intact');
  }
  // the grid: Overview only opens / folds the queue beside it, the panes stay (user correction 2026-10-19)
  for (const left of ['strip', 'hidden']) {
    const open = layoutReduce({ ...defaultLayout(true), center: { kind: 'grid' }, left }, { t: 'overview', on: true }, lctx());
    assert.deepEqual([open.state.center, open.state.left], [{ kind: 'grid' }, 'queue'], left);
    const fold = layoutReduce(open.state, { t: 'overview', on: false }, lctx());
    assert.deepEqual([fold.state.center, fold.state.left], [{ kind: 'grid' }, 'strip'], left);
    assert.equal(fold.fx.pick, undefined);
  }
  assert.match(main, /const overviewOn = [^;]*!zoomed\(\);/);
  assert.doesNotMatch(main, /const overviewOn = [^;]*!grid;/);
  assert.match(main, /item\('overview', 'Overview', '', overviewOn,/);
  assert.match(main, /const overviewOn = queueShown\(\) && !stripOn\(\) && view === 'term'/);
  assert.match(main, /dispatch\(\{ t: 'overview', on: !overviewOn \}\)/);
});

test('D12: the palette - one row per session with its actions behind →, Ended last, Git window, no "New session in"', () => {
  assert.doesNotMatch(main.slice(main.indexOf('function commands()'), main.indexOf('function palette()')), /New session in \$\{/);
  assert.match(main, /ended\.push\(\{ \.\.\.row, group: 'Ended', folded: true \}\)/);
  assert.match(main, /keepOn\(\[\.\.\.sessions, \.\.\.actions, \.\.\.goTo, \.\.\.ended\], on\)/, 'F18: Sessions · Actions · Go to · Ended');
  assert.match(main, /label: 'Git window', hint: .*git\.gitWindow\(cur\.project\)/);
  assert.doesNotMatch(main, /'Git: repository history'/);
  const gp = src('gitpanel.ts');
  assert.match(gp, /onclick: \(\) => gitWindow\(project\) \}, h\('i', \{ class: 'fi fi-history' \}\)/);
  // the sub-actions are reached through their session, not found at the top level
  const rows = [{ label: 'piper', sub: [{ label: 'Stop…', run() {} }], run() {} }, { label: 'Settings', run() {} }];
  assert.deepEqual(pick(rows, 'stop').map((r) => r.label), []);
  assert.deepEqual(pick(rows, 'pip').map((r) => r.label), ['piper']);
  const pal = src('palette.ts');
  assert.match(pal, /sub\?: Command\[\]/);
  assert.match(pal, /ArrowRight/);
  assert.match(pal, /pcrumb/);
});

test('D13: the Global agent has no Files / HTTP in its header, its GitLab row says all projects', () => {
  assert.match(main, /const proj = s\.kind === 'project';/);
  for (const r of [/proj && shown\('files'\) \? tab\('files'/, /proj && httpTab\(s\.project\) && shown\('http'\) \? tab\('http'/]) assert.match(main, r);
  assert.match(main, /: pane === DB_HERE && !on\('db'\) \?/, 'D1: Files / Database panes only for a project session (paneBlock)');
  assert.match(main, /all projects · \$\{s\.gitlab\.length\}/);
});

test('D6 → F28: one session menu everywhere (the header ⋯ too) - what the header already has is there, disabled when it does not apply', () => {
  const body = main.slice(main.indexOf('function openRailMenu('), main.indexOf('function openRailMenu(') + 4000);
  assert.match(body, /function openRailMenu\(s: Session, x: number, y: number, back\?: \(\) => void\): void \{/);
  assert.doesNotMatch(body, /head \?|!head/);
  assert.doesNotMatch(body, /head \? null/);   // null is a separator in a MenuEntry list
  assert.match(main, /'button\[aria-label="more actions"\]'\)\?\.focus\(\)\);/);
});
test('D5: the chooser shows running (and chosen) shells, the ended ones fold; Close ended / the timer skip grid panes', () => {
  const x = (id, alive, ended) => ({ id, project: 'p', path: '/p', title: id, created: 0, rc: alive ? null : 0, alive, ended });
  const items = [x('h1', true), x('h2', false, 100), x('h3', false, 100), x('h4', false, null)];
  const { live, ended } = splitHosts(items, ['h3']);
  assert.deepEqual(live.map((r) => r.id), ['h1', 'h3']);
  assert.deepEqual(ended.map((r) => r.id), ['h2', 'h4']);
  const grid = (id) => id === 'h3';
  assert.deepEqual(endedToClose(items, grid), ['h2', 'h4']);   // Close ended: every ended one outside the grid
  assert.deepEqual(endedToClose(items, grid, 100 + ENDED_CLOSE_S - 1), []);
  assert.deepEqual(endedToClose(items, grid, 100 + ENDED_CLOSE_S), ['h2']);   // no end time (before D5): only by hand
  assert.equal(ENDED_CLOSE_S, 600);
  assert.match(main, /window\.setInterval\(\(\) => hostView\.closeStale\(\), 60_000\)/);
  assert.match(main, /hostItems\(state\)\.filter\(\(x\) => x\.alive && !members\.includes\(x\.id\)\)/);   // the grid's empty cell
  assert.match(src('hostterm.ts'), /markBidi\(`Close ended \(\$\{nEnded\}\)`\)/);
});

test('D11: forge items never reach the queue - no Inbox filter or duplicate Inbox link', () => {
  const subj = { type: 'issue', key: 'g#issue#1', label: 'shop #1' };
  const q = buildQueue({ sessions: [], actions: [{ key: 'gl:1', kind: 'action', level: 'gitlab', source: 'gitlab', title: 'x', body: '', quote: null,
    subject: subj, since: 0, actions: [] }], events: [], seen: 0, board: new Map(), unseen: new Set(), pins: new Set(), order: [], label: (x) => x, now: 1 });
  assert.equal(q.needs.length + q.new.length, 0);
  const qs = src('queue.ts');
  assert.ok(!/agentOn|pillLabel/.test(qs), 'the old deps are gone');
  assert.doesNotMatch(qs, /class: 'link small q-inbox'/);
});

test('D11: the bell is "Recent alerts" everywhere', () => {
  assert.ok(!/'Notifications'/.test(main));
  assert.match(main, /'aria-label': 'Recent alerts'/);
  assert.match(main, /kept across reloads/);
  assert.match(main, /localStorage\.getItem\('piper\.alerts'\)|'piper\.alerts'/);
});

test('D11: Recent alerts survive a reload, without their buttons; a broken store is tolerated', () => {
  let saved = null;
  const store = { get: () => saved, set: (v) => { saved = v; } };
  const a = new NoticeLog(store, () => 10);
  a.add('pushed', 'toast', { label: 'Undo', run() {} }, 5);
  a.add('second', 'desktop', undefined, 6);
  a.markRead();
  a.add('third', 'files', undefined, 7);
  const b = new NoticeLog(store, () => 10);
  assert.deepEqual(b.list().map((n) => [n.text, n.src, n.read, n.ts, n.action]),
    [['third', 'files', false, 7, undefined], ['second', 'desktop', true, 6, undefined], ['pushed', 'toast', true, 5, undefined]]);
  assert.equal(b.unread, 1);
  b.clear();
  assert.equal(new NoticeLog(store, () => 10).list().length, 0);
  for (let i = 0; i < NOTICE_MAX + 5; i++) b.add(`n${i}`);
  assert.equal(new NoticeLog(store, () => 10).list().length, NOTICE_MAX);
  assert.equal(new NoticeLog({ get: () => '{not json', set() {} }).list().length, 0);
  const blocked = new NoticeLog({ get: () => { throw new Error('denied'); }, set: () => { throw new Error('denied'); } });
  blocked.add('still works');
  assert.equal(blocked.list().length, 1);
  assert.equal(new NoticeLog().add('no store').text, 'no store');
});

test('D1: Files and the active project\'s Database are grid panes - kinds, the chooser keeps them, ⌘O / ⌘D add next to the session', () => {
  assert.equal(FILES_PANE, 'files');
  assert.equal(DB_HERE, 'db:here');
  assert.equal(paneKind('files'), 'files');
  assert.equal(paneKind('db:here'), 'db');
  assert.equal(paneKind('db:3'), 'db');
  assert.equal(paneKind('host-1'), 'host');
  assert.equal(paneKind('dbfoo'), 'session');
  // a stored pick keeps Files and db:here (db:here does not count against GRID_DB_MAX - it is not a numbered pane)
  assert.deepEqual(gridPanes(['a', 'files', 'db:here', 'db:1', 'db:2'], ['a'], null, true), ['a', 'files', 'db:here', 'db:1', 'db:2']);
  assert.deepEqual(gridPanes(['a', 'db:here'], ['a'], null, false), ['a'], 'module db off: its pane goes');
  // besidePick / followActive (D1) gave way to addPane / replaceFocused (9.45, gridpanes.test.mjs)
});

// D2's splitPick (⌘\\ = the 2-pane preset) gave way to 9.45's add (layout.test.mjs)

test('D1-D3 wiring: no Beside / DB toggle, no rail Grid icon, db_side_pane gone, the context panel opens the pane', () => {
  assert.doesNotMatch(main, /\bbesideOn\b|dbSideOn|showSideFiles|sideEl|setSideWidth|t: 'aside'/);
  assert.doesNotMatch(main, /item\('grid', 'Grid'/, 'D3: the rail has no Grid icon - the segment and ⌘G');
  assert.doesNotMatch(main, /'open it beside the terminal/, 'D1: the Keys sheet names the Files pane, Beside is gone');
  assert.doesNotMatch(readFileSync(new URL('../../piper/README.md', import.meta.url), 'utf8'), /beside the terminal/i);
  assert.match(main, /dispatch\(\{ t: 'pane', pane: FILES_PANE \}\)/);
  assert.match(main, /dispatch\(\{ t: 'pane', pane: DB_HERE \}\)/);
  assert.match(main, /openPane: \(connection\) => openDbPane\(connection\) \}\);/, '9.37: Open pane also without the grid module (the beside pair)');
  assert.match(main, /storeKey: id === DB_HERE \? 'side-' \+ owner :/, 'session side panes remember their own connection');
  const ds = src('dbsection.ts');
  // F10: one action - Open beside (the pane) when there is one, else Open ↗ (the view); dbsection.test.mjs
  assert.match(ds, /action: paneOn \? \{ label: 'Open beside', kind: 'pane', connection: c\.name \} : \{ label: 'Open ↗', kind: 'view', connection: c\.name \}/);
  assert.match(ds, /const paneOn = \(\) => !!deps\.openPane && \(deps\.paneOn\?\.\(\) \?\? true\);/);
  for (const f of ['../../piper/guard.py', '../../piper/settings.py', '../../piper/piperd.py', '../../piper/web/src/types.ts'])
    assert.doesNotMatch(readFileSync(new URL(f, import.meta.url), 'utf8'), /db_side_pane/, f);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /\.sidepane|\.sidesplit|\.body\.split|\.body\.side/);
  assert.match(css, /\.term\.filespane > \.files \{/);
  const rows = SHORTCUTS.flatMap((g) => g.rows);
  assert.equal(rows.find((r) => r.k === '⌘D')?.gate, 'db');
  assert.match(rows.find((r) => r.k === '⌘O')?.d ?? '', /Files pane/);
  assert.ok(!shownShortcuts((g) => g !== 'db').flatMap((g) => g.rows).some((r) => r.k === '⌘D'), 'module db off: no ⌘D row');
});

test('D16: the limits follow the usage module, Audit is readable, Usage explains itself', async () => {
  const { AUDIT_INTERNAL, auditDetail, auditLabel } = await import('../../piper/web/dist/board.js');
  const { USAGE_EMPTY } = await import('../../piper/web/dist/usage.js');
  assert.match(main, /limits\.sync\(on\('usage'\) \? state\.limits : null, on\('usage'\) && on\('codex'\) \? state\.codex_limits : null\)/);
  assert.equal(auditLabel('session-create'), 'Session created');
  assert.equal(auditLabel('gui-inbox-start'), 'Inbox start');   // the fallback: no dashes, no gui- prefix
  assert.ok(AUDIT_INTERNAL.has('attest') && AUDIT_INTERNAL.has('stack-grant') && !AUDIT_INTERNAL.has('git-guard'));
  const spawn = { event: 'spawn', ts: 't', argv: ['hole', 'start', '--project', '/Users/x/p', '--mount', '/a:/b', '--', 'claude'], ok: true };
  assert.equal(auditDetail(spawn, true), 'argv: hole start … +6 · ok: true');
  assert.match(auditDetail(spawn), /^argv: hole start --project \/Users\/x\/p/);
  // N6: a host git run's args are the record itself, folded never
  const git = { event: 'git-run', ts: 't', args: ['push', '--force', 'origin', 'main', '--no-verify'], rc: 0 };
  assert.equal(auditDetail(git, true), auditDetail(git));
  assert.match(USAGE_EMPTY.join(' '), /transcript.*limits/s);
  assert.match(src('usage.ts'), /head\.hidden = !!data && noUsage\(data\);[\s\S]*\}, 'Load again'\)/, 'empty state = text + Load again, no chart options');
  assert.match(src('../app.css'), /\.usage \.bhead label\[hidden\] \{ display: none; \}/);
  const board = src('board.ts');
  assert.match(board, /let actionsOnly = true;/, '9.37: Actions by default');
  assert.match(board, /\[\[true, 'Actions',[^\n]*\[false, 'All',/, 'Actions first');
});

test('D7: one Composer - the Conversation reply bar and the queue reply box are its collapsed form', async () => {
  const { composerHint, COMPACT_KEYS } = await import('../../piper/web/dist/composer.js');
  assert.equal(composerHint('fix login', true, true), 'Claude is working - queued until the turn ends');
  assert.equal(composerHint('fix login', true, false), 'Reply to fix login…');
  assert.equal(COMPACT_KEYS, '⏎ sends · ⇧⏎ new line · Esc close');
  assert.match(composerHint('x', false, false), /^Message for the session · ⌘⏎/);
  const queue = src('queue.ts');
  const comp = src('composer.ts');
  assert.doesNotMatch(main, /convoBar|convoDraft|cv-reply/, 'no second input under Conversation');
  assert.doesNotMatch(queue, /replyBox|qi-reply|replyDraft/, 'no third input in the queue');
  assert.match(queue, /e\.key === 'r' && it\?\.session && deps\.canReply\(it\.session\)\) deps\.reply\(it\.session\)/);
  assert.match(main, /reply: \(sid\) => replyTo\(sid\),/);
  assert.match(main, /composer\.replyTo\(sid\);/);
  // Conversation opens it collapsed by itself, Esc keeps it closed until the next visit, leaving takes the auto one along
  assert.match(main, /composer\.toggle\(true, \{ compact: true, focus: false \}\);\s*composerAuto = true;/);
  assert.match(main, /if \(\(v !== 'term' && v !== 'convo'\) \|\| \(composerAuto && v !== 'convo'\)\) composer\.toggle\(false\);/);
  assert.match(main, /if \(view === 'convo'\) composerDismissed = true;/);
  assert.match(main, /row\('pen', 'Composer', '⌘E', composer\.open\(\), live && \(view === 'term' \|\| view === 'convo'\) \? '' :/, 'F03: in Beside ▾');
  // a pinned reply never retargets, lets go when its session ends, and puts an unrelated draft back afterwards
  assert.match(comp, /if \(pinned && deps\.live && !deps\.live\(bound\)\) pinned = false;/);
  assert.match(comp, /if \(!pinned && \(!bound \|\| !ta\.value\.trim\(\)\)\) bound = cur;/);
  assert.match(comp, /stash = \{ to: bound, text: ta\.value \};/);
  assert.match(comp, /e\.stopPropagation\(\);   \/\/ the global Esc/);
  assert.match(src('../app.css'), /\.composer\.compact \.cfull, \.composer\.compact select \{ display: none; \}/);
  assert.match(src('../app.css'), /grid-template-areas: "target size" "text text" "keys send"/, 'the composer layout');
});

test('9.38 / 9.45: the grid is just its panes - the Add to grid cell only when asked for', () => {
  assert.match(main, /function gridSlot\(shown: number\): HTMLElement \| null \{\n  if \(!gridAdd \|\| !on\('grid'\)\) return null;/);
  assert.match(main, /gridAdd = r\.state\.center\.kind === 'grid' && \(r\.fx\.add \?\? \(r\.fx\.pick \? false : gridAdd\)\);/, 'the cell: fx.add; another pick takes it away');
  assert.doesNotMatch(main, /gridLayout = id;\n  gridAdd/, 'a layout change does not bring it back');
  assert.match(main, /if \(open\) dispatch\(\{ t: 'addCell' \}\); else toggleGrid\(true\);/, 'Beside ▾ asks for it (F03)');
  assert.match(main, /onclick: cancelGridAdd/, 'Add cell has an explicit cancellation');
  assert.match(main, /if \(back\) commitLayout\(\{ state: back.state, fx: \{ pick: back.pick \} \}\);/, 'Cancel restores the remembered grid');
  assert.match(main, /else \{ syncLayout\(\); render\(\); \}/, 'Cancel updates the derived grid and header');
  assert.doesNotMatch(main, /gridSaved|fx\.fresh|'two'|Automatic \(live sessions\)|gridPins|pinToSlot/, '9.45: no pins, no Automatic, no 2-pane preset');
  assert.match(main, /proj \? \{ group: 'Beside the active session' \} : false,/, 'F14: in the "+ Add pane" popover');
});

test('9.37 follow-ups: beside without the grid module, Audit Actions first, Choose panes lists Files / db:here, menu hint gives way', async () => {
  const { besideOnly } = await import('../../piper/web/dist/gridlayout.js');
  assert.equal(besideOnly(['a', 'files']), true);
  assert.equal(besideOnly(['a', 'files', 'db:here']), true);
  assert.equal(besideOnly(['a', 'b', 'files']), false, 'two sessions = the grid module');
  assert.equal(besideOnly(['a', 'db:1']), false, 'db:1..3 = the grid module');
  assert.equal(besideOnly(['a']), false);
  assert.equal(besideOnly([]), false);
  const main = src('main.ts');
  assert.match(main, /if \(!gridAdd \|\| !on\('grid'\)\) return null;/, 'no Add to grid cell without the module');
  assert.doesNotMatch(main, /if \(!on\('grid'\) \|\| s\?\.kind !== 'project'/, 'a path click opens the Files pane without the grid module');
  assert.match(main, /function gridChooser[\s\S]*?item\(FILES_PANE, 'Files'[\s\S]*?item\(DB_HERE, 'Database · this project'[\s\S]*?'Beside the active session'/);
  const css = src('../app.css');
  assert.match(css, /\.cmenu \.mhint \{[^}]*min-width: 0;[^}]*text-overflow: ellipsis;/);
  assert.match(css, /\.cmenu \.mlist > \* \{ flex: none; \}/);
  assert.match(src('menu.ts'), /h\('span', \{ class: 'mhint', title: it\.hint \}/);
});

test('9.40 (changed by F19): Settings › Features = a preset segment and module rows, switches are drafts (Save asks for Host shells)', () => {
  const setup = src('setup.ts');
  const st = src('settings.ts');
  // one renderer for both pages
  assert.match(setup, /export function moduleTile\(id: string, label: string, text: string, on: boolean, flip: \(\) => void,/);
  assert.match(setup, /export function presetCard\(f: FeaturesSummary,/);
  assert.match(setup, /return moduleTile\(id, label, text, on, \(\) => void save\(\{ modules: \{ \[id\]: !on \} \}\), \{/);
  assert.match(setup, /presetCard\(f, p, f\.preset === p\[0\], \(\) => void save\(\{ preset: p\[0\] \}\)\)/);
  // Settings (F19): every field of the section a row (the services and host included), a flip = setVal (draft), a preset = the action
  assert.match(st, /import \{ moduleRow, presetChips, presetDiff \} from '\.\/setup\.js';/);
  assert.match(st, /\(\) => \{ setVal\(s\.id, f\.key, !on\); draw\(\); \}/);
  assert.match(st, /badge: f\.security \? h\('span', \{ class: 'st-badge'/);
  assert.match(st, /onclick: \(\) => void action\(s, 'preset-' \+ id\) \}, label\)/);
  assert.match(st, /s\.id === 'features' \? \[featuresEl\(s, fields, fields === s\.fields\)\]/);
  assert.match(st, /s\.id !== 'features' \|\| !a\.id\.startsWith\('preset-'\)/);   // presets are the cards, not bar buttons
  assert.match(main, /features: \(\) => state\.features \}\);/);
  // the selected card follows the saved values (features.matching_preset), the host shells never count
  const f = { modules: {}, onboarded: true, preset: 'basic', presets: [['basic', 'Basic', ''], ['all', 'Everything', '']],
    list: [['db', 'Databases', ''], ['grid', 'Grid', ''], ['host', 'Host shells', '']], preset_modules: { basic: ['db'], all: ['db', 'grid'] } };
  assert.equal(matchingPreset(f, { db: true, grid: false, host: true }), 'basic');
  assert.equal(matchingPreset(f, { db: true, grid: true }), 'all');
  assert.equal(matchingPreset(f, { db: false, grid: true }), 'custom');
  // F19: the segment follows the draft (presetDiff picks the nearest preset, Custom when anything differs)
  assert.match(st, /const diff = fs\?\.preset_modules \? presetDiff\(fs, draft\) : null;/);
  // the shared CSS and the host sketch
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /:is\(\.setup, \.st-feat\) \.su-tile \{/);
  assert.match(css, /\.st-feat \.su-presets, \.st-feat \.su-gallery \{ grid-template-columns: repeat\(auto-fill, minmax\(210px, 1fr\)\); \}/);
  assert.match(readFileSync(new URL('../../piper/web/img/modules/host.svg', import.meta.url), 'utf8'), /Host shells/);
});

test('Git window at 1024 px: the commit column keeps its file pane and toolbar (gui.py, D12 Git window)', () => {
  // the modal is 100vw - 32 = 992 px wide at 1024; the commit column (third) held 240 px with a 270 px file pane in it
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const cols = css.match(/\.gw-body \{[^}]*grid-template-columns: ([^;]+);/)[1];
  const defaults = cols.replace(/var\(--[^,]+,\s*([^()]+)\)/g, '$1');
  const mins = defaults.match(/minmax\([^)]*\)|\S+/g).map((t) => Number(t.match(/\d+/)[0]));   // each track's minimum
  const top = Number(css.match(/\.gw-top \{[^}]*grid-template-columns: (\d+)px/)[1]);
  assert.equal(mins.length, 3, cols);
  assert.ok(mins.reduce((a, b) => a + b, 0) <= 1024 - 32, `columns ${cols} do not fit 992 px`);
  assert.ok(/minmax\(\d+px, 1fr\)$/.test(cols.trim()) && mins[2] >= top + 150, `commit column ${cols} vs file pane ${top}px`);
});

test('9.44: right-click on the session bar hides tabs (no policy key), Files / DB toggles put the project pane in the grid', () => {
  assert.match(main, /class: 'tabs', 'aria-label': 'session view', oncontextmenu: \(e: Event\) => tabsMenu\(e as MouseEvent, s\)/);
  assert.match(main, /readStore\('piper\.tabs\.hidden'\)/);
  assert.match(main, /writeStore\('piper\.tabs\.hidden'/);
  for (const k of ['convo', 'history', 'files', 'http']) assert.match(main, new RegExp(`shown\\('${k}'\\)`), k);
  assert.match(main, /on\('composer'\) && !hiddenTabs\.has\('composer'\) \? row\('pen', 'Composer'/, 'F03: a hidden Composer leaves Beside ▾');
  assert.match(main, /label: 'Show all', run: \(\) => setHiddenTabs\(new Set\(\)\)/);
  assert.match(main, /proj \? row\('dir', 'Files', '⌘O', paneShown\(FILES_PANE\)/, 'F03: the toggles are Beside ▾ rows');
  assert.match(main, /proj && on\('db'\) \? row\('db', 'Database', '⌘D', paneShown\(DB_HERE\)/);
  // the toggle's dispatch from Single: the active session + the project's pane on the right
  const c = { view: 'term', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: [], hasPty: () => true, splitBlock: () => '',
    paneBlock: () => '', gridOn: true, feedOn: true, sessionView: true };
  assert.deepEqual(layoutReduce({ ...defaultLayout(true) }, { t: 'pane', pane: DB_HERE }, c).fx.pick, ['a', DB_HERE]);
  assert.deepEqual(layoutReduce({ ...defaultLayout(true) }, { t: 'pane', pane: FILES_PANE }, c).fx.pick, ['a', FILES_PANE]);
});

test('9.44: the Composer is a module, off by default - no toggle, ⌘E, palette row, auto-open; Reply / → Claude use the terminal', () => {
  assert.equal(SHORTCUTS.find((g) => g.title === 'Session').rows.find((r) => r.k === '⌘E').gate, 'composer');   // the Keys sheet
  assert.ok(!shownShortcuts((g) => g !== 'composer').find((g) => g.title === 'Session').rows.some((r) => r.k === '⌘E'));
  assert.match(main, /on\('composer'\) && !hiddenTabs\.has\('composer'\) \? row\('pen', 'Composer', '⌘E', composer\.open\(\)/);
  assert.match(main, /e\.key === 'e' && !e\.shiftKey && on\('composer'\)\) \{/);   // keyGate reads only Navigation
  assert.match(main, /if \(on\('composer'\)\) cmds\.push\(\{ label: 'Composer'/);
  assert.match(main, /if \(view !== 'convo' \|\| !on\('composer'\)\) return;/);
  assert.match(main, /if \(!on\('composer'\)\) return select\(sid\);/);
  assert.match(main, /toAgent: \(text\) => toSession\(text\)/);
  assert.match(main, /if \(toTerm\(\)\) t\.insert\(text\);/);
  assert.match(main, /if \(!on\('composer'\) && composer\.open\(\)\) composer\.toggle\(false\);/);
  assert.ok(readFileSync(new URL('../../piper/web/img/modules/composer.svg', import.meta.url), 'utf8').startsWith('<svg'));
});

test('Project start options: no browser-global memory; explicit server-side remembering at launch', () => {
  const box = main.slice(main.indexOf('function startOptsBox('), main.indexOf('const TIPS:'));
  assert.doesNotMatch(box, /localStorage/);
  assert.match(box, /startOptionsDrafts\(target\(\)\.project_session_defaults/);
  assert.match(box, /'Remember', h\('span', \{ class: 'so-rem-long' \}, ' for this project'\)/);
  assert.match(box, /Enabled globally/);
  assert.match(box, /cb\.disabled = !project \|\| enforced\.has\(key\)/);
  assert.match(main, /opts\.load\(chosen\)/);
  assert.match(main, /const snapshot = \{ project: chosen, task: taskValue\(\)/);
  assert.match(main, /snapshot\.remember/);
});
