// node --test: F04 terminal - a plain click on path:line asks where, the terminal has no card.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const renderer = `
      export class Terminal {
        unicode = {}; parser = { registerOscHandler() {} }; options = {};
        buffer = { active: { getLine: () => ({ translateToString: () => globalThis.__line }) } };
        loadAddon() {} registerLinkProvider() {} attachCustomKeyEventHandler() {} onData() {} onBinary() {}
        dispose() {} focus() {} reset() {}
      }
      export class FitAddon {} export class WebglAddon {} export class Unicode11Addon {}
      export class WebLinksAddon {} export class SearchAddon {}
    `;
class Element extends EventTarget { append() {} remove() {} contains(node) { return node === this; } }
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { platform: 'MacIntel' } });
globalThis.document = { createElement: () => new Element(), documentElement: {}, addEventListener() {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
const loc = { href: '' };
globalThis.window = { addEventListener() {}, setTimeout() { return 1; }, clearTimeout() {}, location: loc };
const moduleUrl = new URL('../../piper/web/dist/term.js', import.meta.url);
const source = readFileSync(moduleUrl, 'utf8').replace(/from '([^']+)'/g, (_m, spec) =>
  `from '${spec.startsWith('@xterm/') ? 'data:text/javascript;base64,' + Buffer.from(renderer).toString('base64') : new URL(spec, moduleUrl).href}'`);
const { TermView, TERM_FLAGS } = await import('data:text/javascript;base64,' + Buffer.from(source + '\n//# sourceURL=' + moduleUrl.href).toString('base64'));

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const menu = readFileSync(new URL('../../piper/web/src/menu.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const click = (mods = {}) => ({ metaKey: false, ctrlKey: false, shiftKey: false, ...mods });

function links(line, extra = {}) {
  const calls = [];
  const view = new TermView('s1', '/work/demo', {
    onRefused() {}, onExit() {}, onConnection() {}, onImage: async () => '',
    onOpenFile: (...a) => calls.push(['open', ...a]), onOutside: (...a) => calls.push(['outside', ...a]),
    onPathMenu: (_e, ...a) => calls.push(['menu', ...a]), ...extra,
  });
  globalThis.__line = line;
  return { found: view.fileLinks(1), calls, view };
}

test('F04: a plain click on path:line opens the path menu with the checkout path, the IntelliJ path and the line', () => {
  const { found, calls } = links('at src/Main.java:42 and /tmp/claude/x.txt:3');
  assert.deepEqual(found.map((l) => l.text), ['src/Main.java:42', '/tmp/claude/x.txt:3']);
  found[0].activate(click(), found[0].text);
  found[1].activate(click(), found[1].text);
  assert.deepEqual(calls, [['menu', 'src/Main.java', '/work/demo/src/Main.java', 42], ['menu', '', '/tmp/claude/x.txt', 3]]);
});

test('F04: ⌘-click still opens the Files tab (policy term_click_opens_files), ⇧-click IntelliJ', () => {
  TERM_FLAGS.clickFiles = true;
  const { found, calls } = links('src/Main.java:42');
  found[0].activate(click({ metaKey: true }), found[0].text);
  assert.deepEqual(calls, [['open', 'src/Main.java', 42, true]]);
  found[0].activate(click({ shiftKey: true }), found[0].text);
  assert.equal(calls.length, 1);
  assert.equal(loc.href, `idea://open?file=${encodeURIComponent('/work/demo/src/Main.java')}&line=42`);
  TERM_FLAGS.clickFiles = false;
});

test('F04: the menu - Open in Files ↵, Open beside ⇧↵, (sandbox file), Open in IDEA, Copy path; ⇧↵ runs beside', () => {
  const fn = main.slice(main.indexOf('function pathMenu('), main.indexOf('\n}\n', main.indexOf('function pathMenu(')));
  const order = ["label: 'Open in Files', keys: '↵'", "label: 'Open beside', keys: '⇧↵'", "label: 'Open (sandbox file)'", "label: 'Open in IDEA'", "label: 'Copy path'"];
  const at = order.map((k) => fn.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at);
  assert.match(fn, /shiftEnter: \(\) => openTermFile\(v, rel, line, false\)/);
  assert.match(fn, /const proj = !sshOnly\(v\.id\) && v\.kind === 'project' && !!rel;/, 'SSH-only paths cannot open a structured Files view');
  assert.match(fn, /!refHost\(v\.id\) \? \{ label: 'Open in IDEA'/, 'a remote server path must not open an unrelated local IDEA file');
  assert.match(main, /onPathMenu: \(e, rel, abs, line\) => pathMenu\(e, v, rel, abs, line\),/);
  assert.match(menu, /if \(e\.key === 'Enter' && e\.shiftKey && opts\.shiftEnter\) \{ stop\(\); close\(true\); opts\.shiftEnter\(\); return; \}/);
});

test('F04: no card around a single terminal - the whole area, 12 px inside; the grid keeps its panes', () => {
  const block = css.slice(css.indexOf('/* ==== 9.70 F04'), css.indexOf('/* ==== end F04 ==== */'));
  assert.match(block, /#main > \.body:not\(\.grid\):has\(> \.term:not\(\.hidden\):not\(\.slot\)\) \{ padding: 0; background: var\(--well\); \}/);
  assert.match(block, /#main > \.body:not\(\.grid\) > \.term:not\(\.slot\) \{ border-radius: 0; padding: 12px 16px; \}/);
});


test('central reference routing handles paths, HTTP, GitLab, DB, commits and sessions on a plain click', () => {
  const routed = [];
  const {found, calls} = links('src/Main.java api.http #12 !3 pipeline 4 public.users a12bc34 session ab123456', {
    onReference: ref => routed.push(ref),
  });
  for (const link of found) link.activate(click(), link.text);
  assert.deepEqual(routed.map(r => r.kind), ['file','file','gitlab','gitlab','gitlab','db','commit','session']);
  assert.equal(calls.length,0,'the old Files/IDE menu must not intercept routed clicks');
  assert.equal(routed[1].path,'api.http');
});


test('reference hitboxes follow wrapped terminal cells and wide Unicode prefixes', () => {
  const {view} = links('');
  const row = (text,isWrapped=false) => {
    const cells=[];
    for(const c of text) { const width=c==='界'?2:1; cells.push({getChars:()=>c,getWidth:()=>width}); if(width===2)cells.push({getChars:()=>'',getWidth:()=>0}); }
    return {isWrapped,translateToString:()=>text,length:cells.length,getCell:i=>cells[i]};
  };
  let rows=[row('界 src/A.ts:7')];
  view.term.buffer.active.getLine=i=>rows[i];
  assert.deepEqual(view.fileLinks(1)[0].range,{start:{x:4,y:1},end:{x:13,y:1}});
  rows=[row('at src/main/'),row('App.java:12',true)];
  const first=view.fileLinks(1)[0],second=view.fileLinks(2)[0];
  assert.equal(first.text,'src/main/App.java:12');
  assert.deepEqual(first.range,second.range);
  assert.deepEqual(first.range,{start:{x:4,y:1},end:{x:11,y:2}});
});
