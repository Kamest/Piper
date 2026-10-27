import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { agentsEnabled } from '../../piper/web/dist/dbview.js';
import { indexGroups, indexItems } from '../../piper/web/dist/gitpanel.js';
import { SCRATCH_DIR, SCRATCH_TYPES, isScratch, markdownModeControl, scratchPaths } from '../../piper/web/dist/files.js';
import { sideSplitter } from '../../piper/web/dist/dom.js';

class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.clientWidth = 1000; this.scrollLeft = this.scrollTop = 0; this.style = { setProperty: (k, v) => { this.vars ??= {}; this.vars[k] = v; } }; this.classList = { add() {}, remove() {} }; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(k, cb) { this.listeners[k] = cb; }
  append(...children) { this.children.push(...children); }
  getBoundingClientRect() { return { left: 0, right: 300, top: 0, width: 300, height: 500 }; }
}
globalThis.document = { createElement: tag => new El(tag), createTextNode: text => ({ textContent: text }) };
globalThis.localStorage = { values: new Map(), getItem(key) { return this.values.get(key) ?? null; }, setItem(key, value) { this.values.set(key, value); } };
globalThis.getComputedStyle = () => ({ visibility: 'visible' });
globalThis.ResizeObserver = class { observe() {} };

test('Markdown Preview reads successive manual edits instead of retaining its first render', () => {
  const source = readFileSync(new URL('../../piper/web/dist/files.js', import.meta.url), 'utf8');
  const start = source.indexOf('function setMode(t, m)');
  const code = source.slice(start, source.indexOf('function toggleDiff()', start));
  let text = 'Original Markdown';
  const tab = { mode: 'preview', md: { text }, ed: { text: () => text, scrollTop: () => 17 } };
  const setMode = new Function('drawRight', `${code}; return setMode;`)(() => {
    if (tab.mode === 'preview' && !tab.md) tab.md = { text: tab.ed.text() };
  });
  for (const next of ['Saved manual edit', 'Second edit before autosave']) {
    setMode(tab, 'code');
    text = next;
    setMode(tab, 'preview');
    assert.equal(tab.md.text, next, 'returning to Preview must show the editor text');
    assert.equal(tab.scroll, 17, 'mode switches preserve editor position');
  }
});

test('production table Submit asks for confirmation even when the connection is Only you', async () => {
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('async function submit(t)'), source.indexOf('function revert(t)'));
  for (const enabled of [true, false]) {
    const calls = [], modals = [];
    const submit = new Function('connection', 'calls', 'modals', 'h', `
      const curConn=()=>connection, pendingCount=()=>1, pendingStatements=()=>[{sql:'UPDATE customer SET name=? WHERE id=?',params:['Changed',1]}];
      const bodyEl={contains:()=>false}, saving=new Set(), session='', pj=()=> 'grp/demo';
      const deps={toast(){},closeModal(){},modal:(title,...nodes)=>modals.push({title,nodes})};
      const dbApi={execute:async body=>{calls.push(body);return {data:{rowcount:[1]}}}}, loadTable=async()=>{};
      const rowsText=String, errText=String, classBadge=()=>document.createElement('span'), showStatement=s=>s.sql, focusGrid=()=>{}, draw=()=>{};
      ${code}
      return submit;
    `)({class:'prod',name:'private-prod',def:{agent_enabled:enabled}}, calls, modals,
      (tag, attrs, ...children) => { const el = new El(tag); Object.entries(attrs ?? {}).forEach(([key,value]) => typeof value === 'function' ? el.listeners[key.slice(2)]=value : el.attrs[key]=value); el.append(...children); return el; });
    await submit({id:'table',conn:'private-prod',schema:'main',edits:new Map(),deleted:new Set(),inserted:[],sel:null,rows:[]});
    assert.equal(calls.length, 0, 'Submit must wait for the human confirmation');
    assert.equal(modals[0]?.title, 'Change PRODUCTION data?');
    const buttons = modals[0].nodes.at(-1).children;
    buttons.find(b => b.children.includes('Change production')).listeners.click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls[0].confirm, 'prod');
  }
});

test('Stage requests index-to-working review so MM reverted to HEAD and AD deletions remain stageable', async () => {
  const source = readFileSync(new URL('../../piper/web/dist/gitpanel.js', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('async function changeIndex('), source.indexOf('function openSource(', source.indexOf('async function changeIndex(')));
  const scopes = [], staged = [];
  const change = new Function('scopes', 'staged', `
    let busy=false;
    const api={reviewFiles:async(id,scope)=>{scopes.push(scope);return {data:{id:'pinned'}}},reviewStage:async(...args)=>staged.push(args),reviewUnstage:async()=>{}};
    const deps={toast:e=>{throw new Error(e)}},errText=String,refresh=async()=>{};
    ${code}
    return changeIndex;
  `)(scopes, staged);
  await change('session', ['reverted.txt', 'deleted-new.txt'], false);
  assert.deepEqual(scopes, ['unstaged']);
  assert.deepEqual(staged, [['session','pinned','reverted.txt'], ['session','pinned','deleted-new.txt']]);
});

test('Shared consoles inherit the schema of the transaction that actually opened', () => {
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('const syncTx ='), source.indexOf('let activeTab', source.indexOf('const syncTx =')));
  const first = {schema:'a',tx:{open:true}}, second = {schema:'b',tx:null};
  const sync = new Function('joined', 'shared', `${code};return syncTx;`)(() => [first,second], () => true);
  sync(first);
  assert.equal(second.tx, first.tx);
  assert.equal(second.schema, 'a', 'unqualified writes and autocomplete must agree on the actual schema');
  first.tx = null; sync(first);
  assert.equal(second.tx, null);
});

test('switching an RW console to a Shared connection joins the open peers schema immediately', () => {
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const start = source.indexOf("pickBtn.addEventListener('click'");
  const code = source.slice(start, source.indexOf('const mode =', start));
  const row = {kind:'console',conn:'old',schema:'old-schema',mode:'rw',tx:null}, peer = {kind:'console',conn:'target',schema:'a',mode:'rw',tx:{open:true}};
  const setupStart = source.indexOf('const joined =');
  const setup = source.slice(setupStart, source.indexOf('let activeTab',setupStart));
  new Function('t','peer', `
    const tabs=[t,peer],shared=()=>true,allConns=()=>[{name:'target',class:'dev',project:'p'}],CLS_LABEL={dev:'dev'},rawProjectKey=x=>x,short=x=>x.name;
    const schemaOf=new Map([['target','b']]); let conn='old';const draw=()=>{},loadObjects=async()=>{};
    const pickBtn={addEventListener:(name,fn)=>fn()},popMenu=(anchor,items)=>items[0][1]();
    ${setup}
    ${code}
  `)(row,peer);
  assert.equal(row.conn, 'target');
  assert.equal(row.schema, 'a');
  assert.equal(row.tx, peer.tx);
});

test('reload cleanup uses each original session, never the currently active DB pane owner', async () => {
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function sweepTx()');
  const code = source.slice(start, source.indexOf('// ---------------------------------------------------------------- loading', start));
  const calls = [], forgotten = [];
  const records = [{project:'project-a',connection:'a',session:'session-a',tx:'old-a'},
    {project:'project-b',connection:'b',session:'session-b',tx:'old-b'}, {project:'legacy',connection:'l',tx:'old-l'}];
  const sweep = new Function('records','calls','forgotten', `
    let txSwept=false; const PAGE_TX='this-page',session='session-b',readTx=()=>records,
    forgetTx=tx=>forgotten.push(tx), dbApi={tx:async body=>calls.push(body)},deps={toast(){}};
    ${code};return sweepTx;
  `)(records,calls,forgotten);
  await sweep(); await sweep();
  assert.deepEqual(calls.map(c => [c.project,c.session,c.tx]), [['project-a','session-a','old-a'],['project-b','session-b','old-b'],['legacy','','old-l']]);
  assert.deepEqual(forgotten, ['old-a','old-b','old-l']);
});

test('a running console cannot close before its transaction handle arrives', () => {
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const start = source.indexOf('function closeTab(');
  const code = source.slice(start, source.indexOf('// ---------------------------------------------------------------- table data', start));
  const row = {id:'q1',kind:'console',running:true,tx:null}, notices = [];
  const run = new Function('row','notices', `
    let tabs=[row],activeTab=row.id;const connTabs=()=>tabs,draw=()=>{},deps={toast:m=>notices.push(m)};
    ${code};closeTab(row.id);return tabs;
  `);
  assert.deepEqual(run(row,notices), [row]);
  assert.match(notices[0], /running|finish/i);
  assert.deepEqual(run({...row,running:false},[]), []);
});

test('a failed first RW statement still exposes and remembers the worker transaction for rollback', async () => {
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function runConsole(');
  const code = source.slice(start, source.indexOf('async function txEnd(', start));
  const row = {id:'q1',conn:'db',mode:'rw',schema:'b',text:'INVALID SQL',caret:0,running:false,tx:null,results:[]};
  const remembered = [], ops = [];
  const run = new Function('row','remembered','ops', `
    const shared=()=>false,joined=()=>[row],bodyEl={querySelector:()=>null},statementAt=text=>({sql:text}),curConn=()=>({class:'dev'}),draw=()=>{};
    const session='owner-a',viewTx='view-a-',pj=()=> 'project-a',txId=()=> 'tx-a',pageSize=()=>500,errText=String;
    const syncTx=()=>{},forgetTx=()=>{},rememberTx=x=>remembered.push(x),loadHistory=async()=>{};
    const dbApi={tx:async body=>{ops.push(body.op);if(body.op==='exec')throw new Error('SQL failed - transaction still open');return {data:{tx_open:true,since:123,schema:'a'}}}};
    ${code};return runConsole;
  `)(row,remembered,ops);
  await run(row);
  assert.deepEqual(ops, ['exec','state']);
  assert.equal(row.tx?.open, true);
  assert.equal(row.schema, 'a', 'error recovery must use the worker schema before synchronizing peers');
  assert.match(row.results[0].error, /SQL failed/);
  assert.equal(row.running, false);
  assert.deepEqual(remembered, [{project:'project-a',connection:'db',session:'owner-a',tx:'tx-a'}]);
});

test('new test/prod databases are private; legacy definitions keep access and explicit values win', () => {
  for (const cls of ['test', 'prod']) { assert.equal(agentsEnabled({ cls }, false), false); assert.equal(agentsEnabled({ cls }, true), true); }
  assert.equal(agentsEnabled({ cls: 'dev' }, false), true);
  assert.equal(agentsEnabled({ cls: 'prod', agent_enabled: true }, false), true);
  assert.equal(agentsEnabled({ cls: 'dev', agent_enabled: false }, true), false);
});

test('Git index groups distinguish staged, unstaged, new, deleted and partly staged files', () => {
  const files = [['M',' ', 'staged'], [' ','M','unstaged'], ['M','M','partly'], ['?','?','new'], ['D',' ','deleted']].map(([x,y,path]) => ({ x,y,path }));
  const result = indexGroups(files);
  assert.deepEqual(result.staged.map(f => f.path), ['staged','partly','deleted']);
  assert.deepEqual(result.unstaged.map(f => f.path), ['unstaged','partly','new']);
});

test('Git index lists follow the Tree toggle: directories, nested files and folding', () => {
  const files = ['src/b/Two.java', 'README.md', 'src/b/One.java', 'src/a/Three.java'].map(path => ({ path }));
  const show = (items) => items.map(i => 'dir' in i ? `[${i.dir}${i.open ? '' : '+'}]` : `${i.inTree ? '  ' : ''}${i.file.path}`);
  assert.deepEqual(show(indexItems(files, false, new Set(), 's')), files.map(f => f.path));
  assert.deepEqual(show(indexItems(files, true, new Set(), 's')),
    ['README.md', '[src/a]', '  src/a/Three.java', '[src/b]', '  src/b/One.java', '  src/b/Two.java']);
  // a folded directory hides its files only in its own group (Staged vs Unstaged)
  assert.deepEqual(show(indexItems(files, true, new Set(['s:src/b']), 's')), ['README.md', '[src/a]', '  src/a/Three.java', '[src/b+]']);
  assert.equal(indexItems(files, true, new Set(['s:src/b']), 'n').length, 6);
});

test('Markdown exposes labelled modes and Edit invokes the actual code mode', () => {
  const chosen = []; const control = markdownModeControl('preview', mode => chosen.push(mode));
  assert.equal(control.children.length, 2);
  assert.equal(control.children[0].attrs['aria-pressed'], 'true');
  assert.equal(control.children[1].attrs['aria-pressed'], 'false');
  control.children[1].listeners.click(); control.children[0].listeners.click();
  assert.deepEqual(chosen, ['code','preview']);
});

test('splitter restores/clamps width, supports keyboard on either edge and reset', () => {
  localStorage.setItem('resize', '9999');
  const root = new El('div'), panel = new El('div');
  const control = sideSplitter(root, panel, '--width', 'resize', 200, 480, 260, 'left');
  assert.equal(root.vars['--width'], '480px');
  let prevented = false;
  control.listeners.keydown({ key: 'ArrowLeft', preventDefault() { prevented = true; } });
  assert.ok(prevented); assert.equal(root.vars['--width'], '310px');
  control.listeners.keydown({ key: 'Home', preventDefault() {} });
  assert.equal(root.vars['--width'], '260px'); assert.equal(localStorage.getItem('resize'), '260');
  assert.equal(control.attrs.role, 'separator'); assert.equal(control.attrs['aria-orientation'], 'vertical');
});

test('splitter reserves the actual neighboring panel widths', () => {
  const root = new El('div'), panel = new El('div');
  const control = sideSplitter(root, panel, '--width', 'dynamic-resize', 200, 560, 300, 'right', () => 760);
  control.listeners.keydown({ key: 'ArrowRight', shiftKey: true, preventDefault() {} });
  assert.equal(root.vars['--width'], '240px');
});

test('inspector width can live on an ancestor so body hide/drawer overrides keep precedence', () => {
  const root = new El('div'), panel = new El('div'), ancestor = new El('html');
  const control = sideSplitter(root, panel, '--ctx-w', 'ancestor-resize', 240, 560, 300, 'left', 280, [], ancestor.style);
  control.listeners.keydown({ key: 'ArrowLeft', preventDefault() {} });
  assert.equal(ancestor.vars['--ctx-w'], '310px');
  assert.equal(root.vars, undefined);
});


test('splitter retains restored width until a detached panel has layout geometry', () => {
  const callbacks = [];
  const Previous = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class { constructor(callback) { callbacks.push(callback); } observe() {} };
  try {
    const root = new El('div'), panel = new El('div'); root.clientWidth = 0;
    localStorage.setItem('detached-resize', '420');
    sideSplitter(root, panel, '--width', 'detached-resize', 200, 480, 300);
    root.clientWidth = 1000; callbacks[0]();
    assert.equal(root.vars['--width'], '420px');
    assert.equal(localStorage.getItem('detached-resize'), '420');
  } finally { globalThis.ResizeObserver = Previous; }
});

test('Scratch files: the Scratches scope lists development/scratch files only, every menu type has a backend starter', () => {
  assert.equal(SCRATCH_DIR, 'development/scratch');
  assert.deepEqual(scratchPaths({ entries: [{ name: 'scratch_2.sql', type: 'file' }, { name: 'old', type: 'dir' }, { name: 'a.http', type: 'file' }] }),
    ['development/scratch/a.http', 'development/scratch/scratch_2.sql']);
  assert.deepEqual(scratchPaths(undefined), [], 'a missing folder is no scratch file');
  assert.ok(isScratch('development/scratch/scratch.sql'));
  assert.ok(!isScratch('development/scratch/old/x.sql') && !isScratch('src/development/scratch/x.sql') && !isScratch('development/notes.md'));
  const py = readFileSync(new URL('../../piper/workbench.py', import.meta.url), 'utf8');
  const at = py.indexOf('SCRATCH_TYPES = {'); const types = py.slice(at, py.indexOf('\n\n', at));
  for (const [, ext] of SCRATCH_TYPES) assert.match(types, new RegExp(`"${ext}":`), `piperd has no starter for .${ext}`);
  const files = readFileSync(new URL('../../piper/web/dist/files.js', import.meta.url), 'utf8');
  assert.ok(files.includes("'SCRATCH · NOT IN GIT'") && files.includes('this checkout · never committed · the agent can read it'));
});

test('A worktree session on the main checkout\'s stack says so instead of "this session\'s checkout"', () => {
  const db = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  assert.ok(db.includes("session && c.stack !== 'main' ? 'this session\\'s checkout' : 'main checkout'"));
  const st = readFileSync(new URL('../../piper/web/dist/settings.js', import.meta.url), 'utf8');
  assert.ok(st.includes("'stack_scope_project', 'worktree_stack'"), 'the switch sits beside the dev stack access');
});
