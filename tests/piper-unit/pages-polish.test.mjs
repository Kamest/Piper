// node --test: the pages polish - Settings, Usage, Audit, Production, Database, Alerts.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const between = (a, b) => css.slice(css.indexOf(a), css.indexOf(b));

test('Security panel keeps its content height and lets the Settings section scroll', () => {
  assert.match(css, /\.sc-panel \{ flex: none; min-width: 0; \}/);
  assert.doesNotMatch(css, /\.sc-overview/);
  assert.match(css, /\.settings \.st-sec \{[^}]*overflow: auto;/);
});

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const controls = between('/* ==== 9.122 pages polish: Settings, Usage, Audit', '/* ==== end pages polish: controls ==== */');
const PAGES = ':is(.settings, .usage, .audit, .prod-view, .dbview)';

test('mask icons replace the text glyphs: the .fi idiom, a fi() helper, currentColor in a control', () => {
  for (const n of ['plus', 'x', 'more', 'chev-d', 'chev-r', 'chev-l', 'ext']) assert.match(controls, new RegExp(`\\.fi-${n} \\{ -webkit-mask-image: url\\("data:image/svg\\+xml,[^"]+"\\); mask-image: url\\(`));
  assert.match(src('dom.ts'), /export function fi\(name: string, cls = ''\): HTMLElement \{\n  return h\('i', \{ class: `fi fi-\$\{name\}\$\{cls \? ' ' \+ cls : ''\}`, 'aria-hidden': 'true' \}\);/);
  assert.ok(controls.includes(`${PAGES} :is(button, a) .fi { --fic: currentColor; }`));
});

test('no text glyph stands in for an icon on the pages (settings, production, usage, audit, database)', () => {
  const settings = src('settings.ts');
  assert.match(settings, /fi\('plus'\), 'Add a project by hand'\)/);
  assert.match(settings, /class: 'icon st-prm'[^\n]*'aria-label': 'remove the project'/);
  assert.match(settings, /\} \}, fi\('x'\)\) : null\)\)\);/);
  assert.match(settings, /fi\('chev-d', adv \? '' : 'closed'\), groupTitle\(g\)\)/);
  assert.match(settings, /open \? 'Hide' : 'Change', fi\('chev-d'\)/);
  assert.match(settings, /'Turn on', fi\('chev-r'\)/);
  assert.doesNotMatch(settings, /'✕'|'\+ add a project|'▾ ' : '▸ '|'Turn on ›'|'‹ Back/);
  const prod = src('prod.ts');
  assert.match(prod, /'aria-label': 'open in Grafana' \}, fi\('ext'\)\)/);
  assert.match(prod, /fi\('check'\), 'No errors or alerts in the last check\.'/);
  assert.doesNotMatch(prod, /suggestions'\} →`|'↗'\)|'Mute ▾'/);
  assert.match(src('usage.ts'), /class: 'ghost icon', title: 'count again', 'aria-label': 'count again'[^\n]*fi\('refresh'\)\);/);
  assert.match(src('board.ts'), /class: 'icon', title: 'load again', 'aria-label': 'load again'[^\n]*fi\('refresh'\)\)\), body\);/);
  const db = src('dbview.ts');
  assert.doesNotMatch(db, /'Import ▾'|\}, '⋯'\)|\}, '×'\)|\}, '↻'\)|class: 'db-caret' \}, ' ?▾'|\}, '‹'\)|\}, '›'\)\);|openConsole\(\) \}, '\+'\)/);
  assert.equal((db.match(/fi\('chev-d', 'db-caret'\)/g) ?? []).length, 7);
  assert.match(db, /'Import', fi\('chev-d'\)\);/);
  // the Known / Muted folds of Production: the chevron mask, no ▸ / ▾ content
  assert.match(controls, /\.prod-view details\.ibx-group > summary\.ibx-ghead::after \{ content: ''; width: 10px; height: 10px; background: currentColor;/);
  assert.match(controls, /\.prod-view details\.ibx-group\[open\] > summary\.ibx-ghead::after \{ content: ''; transform: rotate\(90deg\); \}/);
});

test('one control set on the pages: 28 px inputs / selects / buttons, 22 px small buttons and chips, 30 px icon buttons', () => {
  assert.ok(controls.includes(`${PAGES} :is(input:not([type=checkbox], [type=radio], [type=range], [type=color], [type=file]), select) { height: 28px; padding-block: 0; box-sizing: border-box; }`));
  assert.match(controls, /button:not\(\.link, \.icon, \.mini, \.small,[^{]+\{ min-height: 28px; padding-block: 0; \}/);
  assert.ok(controls.includes(`${PAGES} :is(button.mini, button.small, a.btn.small):not(.link, .icon) { display: inline-flex; align-items: center; min-height: 22px; height: 22px;`));
  assert.match(controls, /\.settings \.st-chip, \.dbview \.db-pill \{ display: inline-flex; align-items: center; height: 22px;/);
  assert.match(controls, /\.fm-seg > button, \.audit \.amode > button \{ height: 24px; padding-block: 0; \}/);
  assert.ok(controls.includes(`${PAGES} button.icon, .settings .st-prm { width: 30px; min-width: 30px; height: 30px; padding: 0;`));
  assert.match(controls, /:is\(\.settings \.st-searchbox, \.db-cp-head > \.db-find, \.db-searchbox\) \{ height: 28px; \}/);
  assert.match(controls, /\.prod-view \.prod \.prod-stale button\.small \{ height: 28px;/);   // Check now: a full-size primary
});

test('focus, hover and one motion token - only on hover / expand, only without reduced motion', () => {
  assert.match(controls, /:root \{ --dur-fast: 120ms; \}/);
  assert.ok(controls.includes(`${PAGES} :is(input, select, textarea):focus-visible { outline: 2px solid var(--link); outline-offset: -1px; }`));
  assert.ok(controls.includes(`${PAGES} button.icon:not(:disabled):hover, .settings .st-prm:hover { background: var(--tile); color: var(--fg); }`));
  assert.ok(controls.includes(`${PAGES} button.link:not(:disabled):hover { text-decoration: underline; }`));
  const motion = controls.slice(controls.indexOf('@media (prefers-reduced-motion: no-preference)'));
  assert.match(motion, /transition: background-color var\(--dur-fast\) ease, color var\(--dur-fast\) ease, border-color var\(--dur-fast\) ease;/);
  assert.equal((controls.match(/transition/g) ?? []).length, 2, 'every transition sits in the reduced-motion guard');
  assert.ok(controls.indexOf('transition') > controls.indexOf('@media (prefers-reduced-motion: no-preference)'));
  assert.doesNotMatch(controls, /#[0-9a-fA-F]{3,8}\b|rgb\(/, 'colours only from tokens');
});

test('alignment and frames: one Production frame, labels on the control line, the Database bars on 44 px', () => {
  assert.match(controls, /\.prod-view > \.inbox\.prod \{ background: none; border: 0; padding: 0; \}/);
  assert.match(controls, /\.settings \.st-ctl > \.st-env \{ padding-top: 4px; line-height: 20px; \}/);
  assert.match(controls, /\.settings \.st-ctl > label\.switch \{ margin-top: 6px; \}/);
  assert.match(controls, /\.settings \.st-sec select \{ max-width: 100%; text-overflow: ellipsis; \}/);
  assert.match(controls, /\.db-cp-head \{ min-height: 44px; padding: 0 var\(--s4\); gap: var\(--s3\); \}/);
  assert.match(controls, /\.view > \.dbview \.db-lhead \{ height: 44px; padding: 0 12px; \}/);
  assert.match(controls, /\.view > \.dbview \.db-tabs \{ height: 44px; \}/);
  assert.match(controls, /\.view > \.dbview \.db-rscroll > \.db-rsec:first-child \{ height: 44px; padding-block: 0; \}/);
  // the overview still folds its side columns (a .wide rule, not the opened connection's)
  assert.match(css, /\.dbview\.wide \{ grid-template-columns: 0 minmax\(0, 1fr\) 0; \}/);
  // the visually hidden column heads: no clipped text box for the clip scan
  assert.match(controls, /\.db-ctable \.db-chead \{ overflow: visible; clip: auto; clip-path: inset\(50%\); \}/);
});

test('empty and error states carry an action: Audit, an opened connection', () => {
  const audit = src('board.ts');
  assert.match(audit, /!events\.length \? h\('button', \{ class: 'link', onclick: \(\) => void load\(\) \}, 'Load again'\)/);
  assert.match(audit, /filter \? h\('button', \{ class: 'link', onclick: \(\) => \{ input\.value = filter = ''; draw\(\); \} \}, 'Clear the filter'\)/);
  assert.match(audit, /h\('button', \{ class: 'link', onclick: \(\) => \{ actionsOnly = false; drawMode\(\); draw\(\); \} \}, 'Show all records'\)/);
  const db = src('dbview.ts');
  assert.match(db, /\} else if \(!all \|\| all\.status\.ready\) \{   \/\/ pages polish: missing drivers are said once/);
  assert.match(db, /h\('div', \{ class: 'db-pad db-treeerr' \}, h\('p', \{ class: 'bad' \}, o\.error\),\n\s+h\('button', \{ onclick: [^\n]+'Try again'\)\)\);/);
});

test('nothing scrolls sideways at 1024: the Projects path column shrinks with an ellipsis, tools stay on one line', () => {
  assert.match(controls, /\.settings \.st-ptable \.st-path \{ width: 30%; max-width: 0; \}/);
  assert.match(controls, /\.settings \.st-ptable \.st-path > span \{ display: block; overflow: hidden; text-overflow: ellipsis; \}/);
  assert.match(src('settings.ts'), /h\('span', \{ class: 'mono small', title: r\.path \}, r\.path\)/, 'the whole path stays in the tooltip');
  assert.match(controls, /\.settings \.st-ptools > \.mut, \.settings \.st-ptools > button \{ flex: none; white-space: nowrap; \}/);
  assert.match(controls, /min-height: 22px; height: 22px; padding: 0 8px; font-size: var\(--t-sm\); white-space: nowrap; \}/);
});
