// node --test: R2 (2026-10-10, the user's decision) - importing a CLI transcript that read files the project hides
// asks first: the 409 hidden_paths of POST /api/conversations/<id>/import becomes a notice in the picker
// (names only, Import anyway / Cancel) and only Import anyway re-posts with confirm_hidden: true (web/src/convo.ts)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';

class FakeText { constructor(s) { this.nodeType = 3; this.data = s; } get textContent() { return this.data; } }
class FakeEl {
  constructor(tag) { this.nodeType = 1; this.tagName = tag.toUpperCase(); this.attrs = new Map(); this.childNodes = []; this.className = ''; this.listeners = {}; }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  append(...xs) { for (const x of xs) this.childNodes.push(typeof x === 'string' ? new FakeText(x) : x); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(s) { this.childNodes = [new FakeText(s)]; }
}
const all = (el, tag) => [el, ...(el.childNodes ?? []).flatMap((c) => c.nodeType === 1 ? all(c, tag) : [])].filter((e) => e.tagName === tag);
globalThis.document ??= { createElement: (t) => new FakeEl(t), createTextNode: (s) => new FakeText(s) };
globalThis.window ??= { localStorage: { getItem: () => 'tok', setItem() {}, removeItem() {} } };
const posted = [];
let answer = () => new Response('{"session":"new"}', { status: 200 });
globalThis.fetch = async (path, init) => { posted.push([path, JSON.parse(init.body)]); return answer(); };
const { api, ApiError } = await import('../../piper/web/dist/api.js');
const { hiddenImportNotice, hiddenPaths } = await import('../../piper/web/dist/convo.js');

test('a 409 with hidden_paths is a hidden-files question; any other error is not', async () => {
  answer = () => new Response(JSON.stringify({ error: 'The transcript holds content of files hidden from Claude: a.pem. Import anyway?',
    hidden_paths: ['config/secrets/db.properties', 'a.pem'] }), { status: 409 });
  const e = await api.importConversation('local-1', 'p/app').catch((x) => x);
  assert.ok(e instanceof ApiError);
  assert.deepEqual(hiddenPaths(e), ['config/secrets/db.properties', 'a.pem']);
  assert.deepEqual(posted.at(-1), ['/api/conversations/local-1/import', { project: 'p/app', confirm_checkout: false }]);
  assert.equal(hiddenPaths(new ApiError(409, 'another checkout', '', { error: 'x' })), null);
  assert.equal(hiddenPaths(new ApiError(404, 'gone', '', { hidden_paths: ['a'] })), null);
  assert.equal(hiddenPaths(new Error('x')), null);
  assert.deepEqual(hiddenPaths(new ApiError(409, 'unreadable', '', { hidden_paths: [] })), []);
});

test('the notice names the paths, Cancel and Import anyway call back; only a confirmed import sends confirm_hidden', async () => {
  const calls = [];
  const el = hiddenImportNotice(['config/secrets/db.properties', 'deploy/site.pem'], () => calls.push('import'), () => calls.push('cancel'));
  assert.match(el.textContent, /The transcript holds content of files hidden from Claude/);
  assert.match(el.textContent, /Import anyway\?/);
  assert.deepEqual(all(el, 'LI').map((li) => li.textContent), ['config/secrets/db.properties', 'deploy/site.pem']);
  const buttons = all(el, 'BUTTON');
  assert.deepEqual(buttons.map((b) => b.textContent), ['Cancel', 'Import anyway']);
  buttons[0].listeners.click();
  buttons[1].listeners.click();
  assert.deepEqual(calls, ['cancel', 'import']);
  assert.match(hiddenImportNotice([], () => {}, () => {}).textContent, /cannot be read/);
  answer = () => new Response('{"session":"new"}', { status: 200 });
  await api.importConversation('local-1', 'p/app', true, true);
  assert.deepEqual(posted.at(-1)[1], { project: 'p/app', confirm_checkout: true, confirm_hidden: true });
});


test('conversation labels Codex replies and preserves Claude labels', async () => {
  const { createConvo } = await import('../../piper/web/dist/convo.js');
  const originalFetch = globalThis.fetch;
  window.clearInterval = () => {};
  window.setInterval = () => 1;
  document.addEventListener ??= () => {};   // F05: the conversation's ⌘F listener
  Object.defineProperty(FakeEl.prototype, 'firstChild', { configurable: true, get() { return this.childNodes[0]; } });
  FakeEl.prototype.remove = function () { this.parent.childNodes.splice(this.parent.childNodes.indexOf(this), 1); };
  const append = FakeEl.prototype.append;
  FakeEl.prototype.append = function (...xs) {
    append.call(this, ...xs);
    for (const x of this.childNodes) x.parent = this;
  };
  try {
    for (const agent of ['codex', 'claude']) {
      globalThis.fetch = async () => new Response(JSON.stringify({ agent, usage: null, turns: [
        { role: 'user', text: 'Question' }, { role: 'assistant', text: 'Answer' }
      ] }), { status: 200 });
      const errors = [];
      const convo = createConvo({ toast: (s) => errors.push(s), modal() {}, closeModal() {}, jump() {} });
      convo.show(agent);
      await new Promise((resolve) => setImmediate(resolve));
      assert.deepEqual(errors, []);
      assert.match(convo.el.textContent, agent === 'codex' ? /CodexAnswer/ : /ClaudeAnswer/);
      assert.match(convo.el.textContent, /You \/ taskQuestion/);
      convo.hide();
    }
  } finally {
    globalThis.fetch = originalFetch;
    FakeEl.prototype.append = append;
    delete FakeEl.prototype.firstChild;
    delete FakeEl.prototype.remove;
  }
});

// F05 (9.77): markdown rendered, tool calls as grey rows, one human usage line
test('F05 usage line, tool meta and the text split at its tool markers', async () => {
  const { usageLine, toolMeta, segments } = await import('../../piper/web/dist/convo.js');
  const u = usageLine({ input: 478, output: 141000, cache_read: 36300000, cache_write: 916000, messages: 239 });
  assert.equal(u.text, '239 answers · 37.4 M tokens (99 % cache)');
  assert.match(u.title, /input 478 · output 141,000 · cache read 36,300,000 · cache write 916,000/);
  assert.deepEqual(usageLine(null), { text: '', title: '' });
  assert.equal(usageLine({ input: 0, output: 0, cache_read: 0, cache_write: 0, messages: 1 }).text, '1 answer');
  assert.equal(toolMeta({ name: 'Bash', arg: '', ms: 1200, lines: 3 }), '1.2 s · 3 lines');
  assert.equal(toolMeta({ name: 'Bash', arg: '', ms: 75000, error: true }), 'failed · 1 min');
  assert.equal(toolMeta({ name: 'Read', arg: '', lines: 1 }), '1 line');
  assert.equal(toolMeta({ name: 'Read', arg: '' }), '');
  const bash = { name: 'Bash', arg: 'mvn test' };
  assert.deepEqual(segments('Checking.\n[tool Bash]\n[tool Read]\nDone **ok**', [bash]),
    [{ md: 'Checking.' }, { tool: bash }, { tool: { name: 'Read', arg: '' } }, { md: 'Done **ok**' }]);
  assert.deepEqual(segments('see [tool result] inline'), [{ md: 'see [tool result] inline' }]);
});

test('F05 structure: markdown.ts renders the turns, "Search all" is the search icon, ⌘F filters this conversation', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/convo.ts', import.meta.url), 'utf8');
  assert.match(src, /renderMarkdown\(parseMarkdown\(showBidi\(text\)\)/);
  assert.match(src, /linkRefs\(root, open\)/, 'path:line stays a link');
  assert.ok(!src.includes("'⚙ '"), 'the folded "⚙ tool ×n" line is gone');
  assert.match(src, /title: 'Search all conversations', onclick: \(\) => searchAll\(\) \}, h\('i', \{ class: 'fi fi-search', 'aria-hidden': 'true' \}\)\)/, '9.104 an icon, not the ⌕ glyph');
  assert.match(src, /placeholder: 'Filter transcript'/);
  assert.match(src, /e\.key\.toLowerCase\(\) !== 'f'/);
  assert.match(src, /'You \/ task'/, 'the You / Claude label stays');
});
