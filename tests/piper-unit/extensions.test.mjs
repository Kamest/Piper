// node --test: Settings › Extensions (9.168) - state → words / button, requests, the first-start card,
// the Load / Turn off flows against a fake piperd and the restart handshake with the poll
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';

class Element extends EventTarget {
  constructor(tag) { super(); this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.parent = null; this.className = ''; this.hidden = false; this.disabled = false; this.value = ''; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'hidden') this.hidden = true; if (k === 'disabled') this.disabled = true; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...nodes) { for (const n of nodes) { if (n.parent) n.remove(); n.parent = this; this.children.push(n); } }
  prepend(...nodes) { for (const n of nodes.reverse()) { if (n.parent) n.remove(); n.parent = this; this.children.unshift(n); } }
  replaceChildren(...nodes) { this.children.forEach(n => { n.parent = null; }); this.children = []; this.append(...nodes); }
  showModal() { this.open = true; } close() { this.open = false; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  get firstChild() { return this.children[0] ?? null; }
  get firstElementChild() { return this.children.find(n => n instanceof Element) ?? null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === document.body; }
  get textContent() { return this.children.map((n) => n.textContent).join(''); }
  set textContent(t) { this.children = t ? [{ textContent: t, remove() {} }] : []; }
  focus() {} click() { if (!this.disabled) { this.onclick?.(); this.dispatchEvent(new Event('click')); } } scrollIntoView() { this.scrolled = true; }
  querySelector(sel) { return all(this).find((n) => sel.startsWith('.') ? n.className.split(' ').includes(sel.slice(1)) : n.tagName === sel) ?? null; }
}
const all = (node) => node.children.flatMap((c) => c instanceof Element ? [c, ...all(c)] : []);
const doc = new EventTarget();
Object.assign(doc, { createElement: (t) => new Element(t), createTextNode: (t) => ({ textContent: t, remove() {} }), body: new Element('body'),
  getElementById: (id) => all(doc.body).find((n) => n.attrs.id === id) ?? null });
globalThis.document = doc;
globalThis.window = { localStorage: { getItem: () => 'tok', setItem() {}, removeItem() {} } };
globalThis.CustomEvent ??= class extends Event {};
// This DOM fixture does not render a clock; real ticking/focus is exercised in the browser/Electron flow.
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};

const X = await import('../../piper/web/dist/extensions.js');
const { rowAction } = await import('../../piper/web/dist/setup.js');
const tick = () => new Promise((r) => setTimeout(r, 0));
const button = (root, text) => all(root).find((n) => n.tagName === 'button' && n.textContent === text);
const restartText = () => all(doc.body).find(n => n.className === 'restart-card')?.textContent || '';

const ext = (over = {}) => ({ name: 'demo', label: 'Demo tools', description: '', path: '/Users/me/work/demo-tools/extension', remote: 'https://github.com/acme/demo-tools.git',
  commit: '4f2a91c0d1', source: 'registered', api: 2, compatible: true, trusted: false, state: 'found', error: '', adds: [], trusted_remote: '', restart_needed: false, ...over });
const L = (over = {}) => ({ extensions: [], pending: [], offer: [], core_api: 2, apis: [1, 2], source: 'trust', restart_needed: false, caller: 'local', can_approve: true, ...over });
const req = (over = {}) => ({ id: 'r1', kind: 'trust', path: '/Users/me/work/demo-tools/extension', label: 'Demo tools', remote: 'github.com/acme/demo-tools', commit: '4f2a91c',
  from: 'phone', at: Date.now() / 1000, mine: false, ...over });

/** a fake piperd: GET /api/extensions answers `listing`, POSTs are recorded and answered by `answers[action]` */
function piperd(listing, answers = {}) {
  const posts = [];
  globalThis.fetch = async (path, init) => {
    if (path === '/api/ping') return new Response(JSON.stringify({ok:true,version:1,service_instance:posts.some(p=>p[0]==='/api/restart')?'new':'old'}));
    if (init.method === 'POST') {
      posts.push([path, JSON.parse(init.body)]);
      const [status, body] = answers[path.split('/').pop()] ?? [200, { ok: true }];
      return new Response(JSON.stringify(body), { status });
    }
    return new Response(JSON.stringify(typeof listing === 'function' ? listing() : listing));
  };
  return posts;
}
function ui() {
  const shown = { toasts: [], modal: null };
  const deps = { toast: (t) => shown.toasts.push(t), modal: (title, ...c) => { shown.modal = new Element('div'); shown.modal.title = title; shown.modal.append(...c.filter(Boolean)); doc.body.append(shown.modal); },
    closeModal: () => { shown.modal?.remove(); shown.modal = null; } };
  return { shown, deps };
}

test('every state has its word, colour and one button (Declined looks like Found; Incompatible has none)', () => {
  const l = L();
  const v = (o) => X.rowView(ext(o), l);
  assert.deepEqual([v({ state: 'loaded', trusted: true }).word, v({ state: 'loaded', trusted: true }).tone, v({ state: 'loaded', trusted: true }).button.label], ['Loaded', 'ok', 'Turn off…']);
  assert.deepEqual([v({}).word, v({}).tone, v({}).button.label], ['Found', 'info', 'Load…']);
  assert.match(v({}).line, /^Registered by its checkout · not loaded$/);
  assert.deepEqual([v({ state: 'declined' }).word, v({ state: 'declined' }).tone, v({ state: 'declined' }).button.label], ['Found', 'info', 'Load…']);
  assert.match(v({ state: 'declined' }).line, /not loaded/);
  const oc = v({ state: 'origin-changed', trusted: true, trusted_remote: 'https://github.com/acme/reports', remote: 'https://github.com/someone/reports' });
  assert.deepEqual([oc.word, oc.tone, oc.button.label, oc.noteTone], ['Origin changed', 'warn', 'Review…', 'warn']);
  assert.equal(oc.note, 'Load it again only if you trust the new origin.');
  const inc = v({ state: 'incompatible', api: 9, compatible: false });
  assert.deepEqual([inc.word, inc.tone, inc.button], ['Incompatible', 'bad', null]);
  assert.equal(inc.line, 'Extension API 9 · this Piper needs API 1 or 2');
  const failed = v({ state: 'failed', trusted: true, error: 'No module named yaml' });
  assert.deepEqual([failed.word, failed.tone, failed.button.label, failed.button.act, failed.line], ['Failed to load', 'bad', 'Restart Piper', 'restart', 'No module named yaml']);
  assert.equal(v({ state: 'loaded', trusted: true, adds: ['Dev stack', 'dev databases'] }).line, 'Adds: Dev stack, dev databases');
  // a change that waits for the restart: turned off but still running / trusted but not loaded yet
  assert.equal(v({ state: 'loaded', trusted: false, restart_needed: true }).button.label, 'Restart Piper');
  assert.equal(v({ trusted: true, restart_needed: true }).button.label, 'Restart Piper');
  // PIPER_EXTENSIONS decides: no Turn off that would do nothing
  assert.equal(X.rowView(ext({ state: 'loaded', trusted: true, source: 'env' }), L({ source: 'env' })).button, null);
  for (const s of ['loaded', 'found', 'declined', 'origin-changed', 'incompatible', 'failed']) assert.ok(v({ state: s }).hint.length > 20, `${s} explains itself on hover`);
});

test('the meta line: folder with ~, short origin, short commit; an origin change says was / now', () => {
  assert.equal(X.metaLine(ext()), '~/work/demo-tools/extension · github.com/acme/demo-tools · 4f2a91c');
  assert.equal(X.metaLine(ext({ state: 'origin-changed', trusted_remote: 'git@github.com:acme/reports.git', remote: 'https://github.com/someone/reports' })),
    '~/work/demo-tools/extension · was github.com/acme/reports, now github.com/someone/reports · 4f2a91c');
  assert.equal(X.metaLine(ext({ remote: '', commit: '', path: '/home/u/x' })), '~/x');
  assert.equal(X.shortRemote('ssh://git@gitlab.example.test:2222/dev/skeleton.git'), 'gitlab.example.test:2222/dev/skeleton');
});

test('who asks, in words; the nav count and the first-start offer are this computer’s only', () => {
  assert.equal(X.pendingTitle(req()), 'Your phone asks to load Demo tools');
  assert.equal(X.pendingTitle(req({ from: 'remote (me@example.com)' })), 'A remote browser asks to load Demo tools');
  assert.equal(X.pendingTitle(req({ from: 'remote', kind: 'add' })), 'A remote browser asks to add Demo tools');
  assert.match(X.pendingMeta(req({ from: 'remote (me@example.com)' })), /^me@example\.com · \d\d:\d\d · ~\/work\/demo-tools\/extension · github\.com\/acme\/demo-tools · 4f2a91c$/);
  assert.match(X.pendingMeta(req()), /^Phone · /);
  const e = ext();
  assert.equal(X.reviewCount(L({ pending: [req()], offer: [e.path], extensions: [e] })), 2);
  assert.equal(X.reviewCount(L({ pending: [req()], can_approve: false, caller: 'phone' })), 0);
  assert.equal(X.offered(L({ offer: [e.path], extensions: [e] })).label, 'Demo tools');
  assert.equal(X.offered(L({ offer: [e.path], extensions: [e], can_approve: false, caller: 'remote' })), null, 'a phone is not prompted into a request');
  assert.equal(rowAction({ key: 'extension', level: 'info', ok: false, title: 'x', text: '', fix: 'piper extensions trust /x', settings: '', action: 'extension-enable' }).label, 'Open Extensions');
  assert.equal(rowAction({ key: 'service-legacy', level: 'warn', ok: false, title: 'x', text: '', fix: 'piper install-service', settings: '', action: 'service-reinstall' }).label, 'Reinstall service…');
});

test('this computer: a phone’s request is approved through the Load dialog, then Piper restarts once', async () => {
  const p = req();
  let listing = L({ pending: [p], extensions: [ext()] });
  const posts = piperd(() => listing, { approve: [200, { ok: true, path: p.path, restart: true }], restart: [202, { ok: true, restarting: true }] });
  const { shown, deps } = ui();
  const page = X.createExtensionSettings(deps);
  doc.body.append(page.el);
  page.show(); await tick(); await tick();
  assert.match(page.el.textContent, /Your phone asks to load Demo tools/);
  assert.match(page.el.textContent, /On this computer/);
  assert.equal(page.count(), 1);
  button(page.el, 'Review and load…').click();
  assert.equal(shown.modal.title, 'Load Demo tools?');
  assert.match(shown.modal.textContent, /outside the sandbox/);
  assert.match(shown.modal.textContent, /Folder~\/work\/demo-tools\/extensionFromgithub\.com\/acme\/demo-toolsCommit4f2a91c/);
  listing = L({ extensions: [ext({ trusted: true, restart_needed: true })], restart_needed: true });
  button(shown.modal, 'Load and restart Piper').click();
  await tick(); await tick(); await tick();
  assert.deepEqual(posts.map(([path]) => path), ['/api/extensions/approve', '/api/restart'], 'approve, then exactly one restart');
  assert.deepEqual(posts[0][1], { id: 'r1' });
  assert.equal(shown.modal, null);
  assert.match(restartText(), /Piper is back/);
  assert.equal(X.restartAnswered(), true, 'the changed service instance confirms the restart');
  assert.equal(X.restarting(), false);
  assert.doesNotMatch(restartText(), /Restarting/);
  page.el.remove();
});

test('Deny and a 409 (origin changed since the request) keep the computer unchanged', async () => {
  const p = req();
  const posts = piperd(L({ pending: [p], extensions: [ext()] }), { approve: [409, { error: 'the extension\'s origin changed since the request' }] });
  const { shown, deps } = ui();
  const page = X.createExtensionSettings(deps);
  doc.body.append(page.el);
  page.show('x'); await tick(); await tick();
  button(page.el, 'Deny').click(); await tick();
  assert.deepEqual(posts[0], ['/api/extensions/deny', { id: 'r1' }]);
  button(page.el, 'Review and load…').click();
  button(shown.modal, 'Load and restart Piper').click();
  await tick(); await tick();
  assert.match(shown.modal.textContent, /origin changed since the request\. The request was dropped\./);
  assert.ok(!posts.some(([path]) => path === '/api/restart'), 'no restart after a refused approval');
  deps.closeModal(); page.el.remove();
});

test('a phone: Load… asks the computer (202), the row waits and the request can be withdrawn; Turn off works directly', async () => {
  const mine = req({ mine: true });
  let listing = L({ caller: 'phone', can_approve: false, extensions: [ext(), ext({ name: 'stack', label: 'Acme dev stack', path: '/Users/me/www/acme-stack', state: 'loaded', trusted: true })] });
  const posts = piperd(() => listing, { trust: [202, { ok: true, pending: mine }], untrust: [200, { ok: true, restart: true }], restart: [202, { ok: true }] });
  const { shown, deps } = ui();
  const page = X.createExtensionSettings(deps);
  doc.body.append(page.el);
  page.show('x'); await tick(); await tick();
  assert.match(page.el.textContent, /Loading code runs it on your computer, so it is confirmed there/);
  button(page.el, 'Load…').click();
  assert.match(shown.modal.textContent, /This sends a request to Piper on the computer/);
  listing = { ...listing, pending: [mine] };
  button(shown.modal, 'Ask on the computer').click();
  await tick(); await tick(); await tick();
  assert.deepEqual(posts.map(([p]) => p), ['/api/extensions/trust']);
  assert.match(shown.toasts.at(-1), /Asked your computer to load Demo tools/);
  assert.ok(!X.restarting(), 'a request restarts nothing');
  assert.match(page.el.textContent, /Waiting for approval on your computer/);
  button(page.el, 'Cancel request').click(); await tick();
  assert.deepEqual(posts.at(-1), ['/api/extensions/cancel', { id: 'r1' }]);
  button(page.el, 'Turn off…').click();
  assert.equal(shown.modal.title, 'Turn off Acme dev stack?');
  button(shown.modal, 'Turn off and restart Piper').click();
  await tick(); await tick(); await tick();
  assert.deepEqual(posts.slice(-2).map(([p]) => p), ['/api/extensions/untrust', '/api/restart']);
  assert.match(restartText(), /Piper is back/);
  X.restartAnswered();
  page.el.remove();
});

test('the first-start card: Review and load… opens the dialog, Not now declines it for good', async () => {
  const e = ext({ description: 'Demo stack panel' });
  let listing = L({ offer: [e.path], extensions: [e] });
  const posts = piperd(() => listing);
  await X.loadExtensions();
  const { shown, deps } = ui();
  let opened = 0;
  const card = X.offerCard({ ...deps, openSettings: () => { opened++; } });
  assert.match(card.textContent, /An extension was found: Demo tools/);
  assert.match(card.textContent, /~\/work\/demo-tools\/extension · github\.com\/acme\/demo-tools · 4f2a91c/);
  button(card, 'Review and load…').click();
  assert.equal(shown.modal.title, 'Load Demo tools?');
  deps.closeModal();
  button(card, 'Settings › Extensions').click();
  assert.equal(opened, 1);
  listing = L({ extensions: [{ ...e, state: 'declined' }] });
  button(card, 'Not now').click();
  await tick(); await tick(); await tick();
  assert.deepEqual(posts.at(-1), ['/api/extensions/decline', { path: e.path }]);
  assert.equal(X.offerCard({ ...deps, openSettings() {} }), null, 'gone once declined');
});

test('Add folder… registers a path; a doctor hint marks its row', async () => {
  const e = ext();
  const posts = piperd(L({ extensions: [e] }), { add: [200, { ok: true, path: e.path }] });
  const { shown, deps } = ui();
  const page = X.createExtensionSettings(deps);
  doc.body.append(page.el);
  page.show('Extension Demo tools found at /Users/me/work/demo-tools/extension - load it'); await tick(); await tick();
  assert.ok(page.el.querySelector('.ext-focus'), 'the row the doctor named is marked');
  button(page.el, 'Add folder…').click();
  const input = all(shown.modal).find((n) => n.tagName === 'input');
  input.value = '~/work/other'; input.dispatchEvent(new Event('input'));
  button(shown.modal, 'Add').click(); await tick(); await tick();
  assert.deepEqual(posts.at(-1), ['/api/extensions/add', { path: '~/work/other' }]);
  assert.match(shown.toasts.at(-1), /Added/);
  page.el.remove();
});

test('9.171: piperd running older code than its checkout - a persistent notice with Restart, the command on a 404', async () => {
  assert.equal(X.needsRestart(null), false, 'no answered poll yet: nothing');
  assert.equal(X.needsRestart({ code_outdated: false }), false);
  assert.equal(X.needsRestart({ code_outdated: true }), true);
  assert.equal(X.needsRestart({ gui_build: 'g' }), true, 'a piperd from before 9.171 says nothing about its code');
  const { shown, deps } = ui();
  assert.equal(X.updateNotice({ code_outdated: false }, deps), null);
  assert.equal(doc.getElementById('update-banner'), null);
  const el = X.updateNotice({ code_outdated: true }, deps);
  assert.equal(el.attrs.id, 'update-banner');
  assert.equal(doc.body.children[0], el, 'its own line on top (not #banner: every answered poll removes that one)');
  assert.match(el.textContent, /^Piper was updated\. Restart Piper to finish - sessions keep running\.Restart$/);
  assert.equal(X.updateNotice({ code_outdated: true }, deps), el, 'the next poll keeps the same notice');
  // Restart: POST /api/restart, the notice goes, the "Restarting Piper…" toast stands until piperd answers
  const posts = piperd(L());
  button(el, 'Restart').click(); await tick(); await tick();
  assert.deepEqual(posts.map((p) => p[0]), ['/api/restart']);
  assert.equal(doc.getElementById('update-banner'), null);
  assert.equal(X.restarting(), false);
  X.restartAnswered();
  assert.equal(X.updateNotice({ code_outdated: false }, deps), null, 'the restarted piperd runs the new code');
  // a piperd from before /api/restart answers 404: the command instead of the button, no toast
  piperd(L(), { restart: [404, { error: 'not found' }] });
  const old = X.updateNotice({ gui_build: 'g' }, deps);
  button(old, 'Restart').click(); await tick(); await tick();
  assert.equal(button(old, 'Restart'), undefined);
  assert.match(old.textContent, /cannot restart itself - run piper restart in a terminal; sessions keep running/);
  assert.equal(X.restarting(), false);
  assert.deepEqual(shown.toasts, []);
  assert.equal(X.updateNotice({ gui_build: 'g' }, deps), old, 'the same old piperd: the command stays');
  // 9.172: then a piperd that reports its code (restarted in a terminal, outdated again by the next pull): Restart again
  const newer = X.updateNotice({ code_outdated: true }, deps);
  assert.notEqual(newer, old);
  assert.equal(old.isConnected, false);
  assert.ok(button(newer, 'Restart'));
  assert.match(newer.textContent, /^Piper was updated\. Restart Piper to finish/);
  // another error: a toast, the button stays
  newer.remove();
  X.updateNotice({ code_outdated: false }, deps);
  piperd(L(), { restart: [403, { error: 'boom' }] });
  const again = X.updateNotice({ code_outdated: true }, deps);
  button(again, 'Restart').click(); await tick(); await tick();
  assert.match(restartText(), /Restart was refused \(HTTP 403\)/);
  assert.ok(button(again, 'Restart'));
  X.updateNotice({ code_outdated: false }, deps);
  assert.equal(doc.getElementById('update-banner'), null);
});
