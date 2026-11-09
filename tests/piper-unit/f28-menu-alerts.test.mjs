// node --test: F28 - one session menu with icons, Stop / Remove last in red; alerts in groups.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const dist = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const menu = readFileSync(new URL('../../piper/web/src/menu.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const fn = (src, name) => src.slice(src.indexOf(`function ${name}(`), src.indexOf('\n}\n', src.indexOf(`function ${name}(`)) + 2);

test('toast resize notifications coalesce into a future frame without writing during observer delivery', () => {
  const frames = [];
  let measured = 0;
  const schedule = new Function('window', 'syncToastPosition', `
    let toastFrame = 0;
    ${fn(dist, 'scheduleToastPosition')}; return scheduleToastPosition;
  `)({ requestAnimationFrame(callback) { frames.push(callback); return frames.length; } }, () => { measured++; });
  for (let i = 0; i < 5; i++) schedule();
  assert.equal(measured, 0, 'observer delivery never performs the layout writes synchronously');
  assert.equal(frames.length, 1, 'multiple resize sources share one frame');
  frames[0]();
  assert.equal(measured, 1);
  schedule(); schedule();
  assert.equal(frames.length, 2, 'later changes can request a fresh frame');
  assert.equal(measured, 1);
  frames[1]();
  assert.equal(measured, 2);
});

test('toasts stay above the inline approval through resize and restore their normal placement after it leaves', () => {
  const classes = () => ({ values: new Set(), toggle(name, on) { if (on) this.values.add(name); else this.values.delete(name); }, remove(name) { this.values.delete(name); }, contains(name) { return this.values.has(name); } });
  const props = new Map(), observed = [], removed = [];
  const mainRect = { left: 64, top: 32, bottom: 768, width: 668 }, cardRect = { top: 488, width: 664, height: 200 };
  const placement = new Map();
  const main = { getBoundingClientRect: () => mainRect, style: { setProperty: (k, v) => placement.set(k, v) } }, body = { classList: classes() };
  const card = { parentElement: main, getBoundingClientRect: () => cardRect };
  const box = { parentElement: body, scrollHeight: 500, classList: classes(), style: { setProperty: (k, v) => props.set(k, v), removeProperty: k => props.delete(k) } };
  const elements = { pending: card, toasts: box }, window = { innerWidth: 1024, innerHeight: 768 };
  const composer = { el: { getBoundingClientRect: () => ({ top: 650, height: 118 }) } };
  const headEl = { getBoundingClientRect: () => ({ bottom: 80 }) };
  const sync = new Function('main', 'document', 'window', 'observer', 'composer', 'headEl', `
    let toastPending = null, toastStack = null;
    const toastLayoutObserver = observer;
    ${fn(dist, 'syncToastPosition')}; return syncToastPosition;
  `)(main, { body, getElementById: id => elements[id] ?? null }, window,
    { observe: element => observed.push(element), unobserve: element => removed.push(element) }, composer, headEl);
  sync();
  assert.equal(body.classList.contains('pending-inline'), true);
  assert.equal(placement.get('--pending-bottom'), '118px', 'card stays above the visible composer');
  assert.equal(placement.get('--pending-top'), '48px', 'card stays below the session header');
  composer.el.getBoundingClientRect = () => ({ top: 0, height: 0 });
  sync();
  assert.equal(placement.get('--pending-bottom'), '0px', 'hidden composer leaves no empty strip');
  assert.deepEqual(Object.fromEntries(props), { '--toast-inline-left': '398px', '--toast-inline-bottom': '288px', '--toast-inline-width': '644px', '--toast-inline-height': '440px' });
  assert.equal(box.classList.contains('toast-scroll'), true, 'long stacks scroll instead of covering approval controls');
  assert.equal(box.parentElement, body, 'alerts remain outside the modal-inert app');
  assert.deepEqual(observed, [card, box]);
  Object.assign(window, { innerWidth: 390, innerHeight: 844 });
  Object.assign(mainRect, { left: 52, top: 119, width: 338 });
  cardRect.top = 600; box.scrollHeight = 100;
  sync();
  assert.deepEqual(Object.fromEntries(props), { '--toast-inline-left': '221px', '--toast-inline-bottom': '252px', '--toast-inline-width': '314px', '--toast-inline-height': '465px' });
  assert.equal(box.classList.contains('toast-scroll'), false);
  cardRect.top = 140;
  sync();
  assert.equal(props.get('--toast-inline-height'), '44px');
  assert.equal(props.get('--toast-inline-bottom'), '712px', 'even a tiny viewport keeps the lower edge above the card');
  elements.pending = null;
  sync();
  assert.equal(body.classList.contains('pending-inline'), false);
  assert.equal(box.classList.contains('toast-scroll'), false);
  assert.equal(props.size, 0, 'removing the card restores the normal stylesheet positioning');
  assert.deepEqual(removed, [card]);
  elements.toasts = null;
  sync();
  assert.deepEqual(removed, [card, box], 'detached containers leave no stale observer');
});

test('the phone Agent drawer removes its covered detail from keyboard navigation and restores it', () => {
  const hiddenButton = {}, activityButton = {}, composer = {};
  const main = { inert: false, contains: element => element === hiddenButton };
  const media = { matches: false }, document = { activeElement: hiddenButton };
  let focused = 0;
  const access = new Function('main', 'drawerPhone', 'document', 'feed', `
    let drawer = false;
    ${fn(dist, 'syncDrawerAccess')};
    return { sync: syncDrawerAccess, open(value) { drawer = value; syncDrawerAccess(); } };
  `)(main, media, document, { focusInput() { focused++; document.activeElement = composer; } });
  access.open(true);
  assert.equal(main.inert, false, 'a wide drawer leaves the adjacent detail usable');
  assert.equal(focused, 0);
  media.matches = true;
  access.sync();
  assert.equal(main.inert, true, 'shrinking the open drawer covers the detail');
  assert.equal(document.activeElement, composer, 'focus leaves the newly covered detail');
  access.open(false);
  assert.equal(main.inert, false, 'closing restores the detail immediately');
  document.activeElement = activityButton;
  access.open(true);
  assert.equal(main.inert, true);
  assert.equal(document.activeElement, activityButton, 'the activity bar remains independently usable');
  assert.equal(focused, 1);
  media.matches = false;
  access.sync();
  assert.equal(main.inert, false, 'widening restores the adjacent detail without closing the drawer');
});

test('closing an overlay restores reachable focus while the phone Agent still covers the detail', () => {
  const hiddenButton = { isConnected: true, focus() { calls.push('hidden'); } };
  const activityButton = { isConnected: true, focus() { calls.push('activity'); } };
  const main = { inert: true, contains: element => element === hiddenButton }, calls = [];
  const close = new Function('main', 'document', 'overlay', 'clear', 'syncDrawerAccess', 'feed', 'terms', `
    let view = 'files', active = 'session', tokenAsked = false;
    ${fn(dist, 'closedAll')};
    return (back, nextView = 'files') => { view = nextView; closedAll(back); };
  `)(main, { body: { children: [] } }, { classList: { remove() {} } }, () => {}, () => {},
    { focusInput() { calls.push('composer'); } }, { get: () => ({ focus() { calls.push('terminal'); } }) });
  close(hiddenButton);
  close(null, 'term');
  close(activityButton);
  assert.deepEqual(calls, ['composer', 'composer', 'activity'], 'covered targets fall back to the composer; reachable openers stay reachable');
  calls.length = 0;
  main.inert = false;
  close(hiddenButton);
  close(null, 'term');
  assert.deepEqual(calls, ['hidden', 'terminal'], 'a wide or closed drawer preserves normal focus restoration');
});

test('a busy dialog blocks global Escape even when its disabled submit lost keyboard focus', () => {
  let closes = 0;
  const layer = { dataset: { escBlocked: '1' }, querySelector: () => null };
  const keys = new Function('overlay', 'topLayer', 'closeModal', `${fn(dist, 'globalKeys')}; return globalKeys;`)(
    { classList: { contains: () => true } }, () => layer, () => { closes++; });
  keys({ key: 'Escape', preventDefault() {} });
  assert.equal(closes, 0, 'a running creation job stays visible with its Cancel action');
  layer.dataset.escBlocked = '';
  keys({ key: 'Escape', preventDefault() {} });
  assert.equal(closes, 1, 'Escape works normally after the job has ended');
});

test('discard hints disappear with their dialog while unrelated and nested dialog notices remain', () => {
  class El extends EventTarget {
    children = [];
    isConnected = false;
    matches() { return false; }
    append(...nodes) { for (const node of nodes) if (node instanceof El) { this.children.push(node); node.parent = this; node.isConnected = true; } }
    remove() { this.isConnected = false; if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  }
  const body = new El(), timers = new Map(), notices = [];
  const document = { body, getElementById: () => body.children[0] };
  const h = (_tag, _props, ...children) => { const el = new El(); el.append(...children); return el; };
  const window = { setInterval(callback) { const id = timers.size + 1; timers.set(id, callback); return id; }, clearInterval(id) { timers.delete(id); } };
  const modalHints = new WeakMap();
  const toast = new Function('document', 'h', 'window', 'notices', 'bellSoon', 'modalHints', 'scheduleToastPosition', `${fn(dist, 'toast')}; return toast;`)(
    document, h, window, { add: text => notices.push(text) }, () => {}, modalHints, () => {});
  const disarmDiscard = new Function('modalHints', `${fn(dist, 'disarmDiscard')}; return disarmDiscard;`)(modalHints);
  const first = new EventTarget(), nested = new EventTarget();
  toast('An unrelated service warning');
  toast('Discard first draft?', undefined, '', first);
  toast('Discard nested draft?', undefined, '', nested);
  const [warning, hint, nestedHint] = body.children[0].children;
  assert.equal(hint.isConnected, true, 'the first close attempt must still show its warning');
  nested.dispatchEvent(new Event('close'));
  assert.equal(nestedHint.isConnected, false, 'a confirmed close must not leave its hint over the next dialog');
  assert.equal(hint.isConnected, true, 'closing a nested dialog must retain the parent warning');
  assert.equal(warning.isConnected, true, 'other alerts must not be cleared');
  first.dataset = { escArmed: '1' };
  disarmDiscard(first);
  assert.equal(first.dataset.escArmed, undefined, 'editing requires a fresh confirmation');
  assert.equal(hint.isConnected, false);
  assert.equal(warning.isConnected, true);
  toast('Discard edited first draft?', undefined, '', first);
  const nextHint = body.children[0].children.at(-1);
  first.dispatchEvent(new Event('close'));
  assert.equal(nextHint.isConnected, false, 'the fresh warning follows its dialog lifetime too');
  assert.equal(notices.length, 4, 'all alerts remain in Recent alerts');
  for (const callback of [...timers.values()]) callback();
  assert.equal(timers.size, 1, 'closed hint timers clean themselves up');
});

test('F28: the session menu - icons, groups View / Order, Continue / Stop… / Remove last (Stop and Remove red); Z items kept', () => {
  const m = fn(main, 'openRailMenu');
  const order = ["label: 'Open', icon: 'fi-open'", "label: 'Side by side', icon: 'fi-columns'", "'Remove from grid' : 'Add to grid'", "{ group: 'View' }", "label: 'Files', icon: 'fi-dir'",
    "label: 'HTTP requests', icon: 'fi-http'", "label: 'Conversation', icon: 'fi-chat'", "label: 'History', icon: 'fi-history'", "label: 'Terminal beside this session', icon: 'fi-term'", "{ group: 'Order' }",
    "'Unpin' : 'Pin to top', icon: 'fi-pin'", "label: 'Move up', icon: 'fi-up'", "label: 'Move down', icon: 'fi-down'", "label: 'Rename…', icon: 'fi-pen'", 'null,',
    "label: continueLabel(s), icon: 'fi-refresh'", "label: 'Stop…', icon: 'fi-square', run: () => confirmStop(s), danger: true", "label: 'Remove', icon: 'fi-x'"];
  const at = order.map((k) => m.indexOf(k));
  assert.ok(at.every((i) => i > 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at);
  assert.match(m, /title: sshOnly\(s\.id\) \? 'remove the ended session from SSH session records' : 'from the list - transcript and audit stay', danger: true \}/);
  assert.match(m, /on\('http'\) && !sshOnly\(s\.id\) \? \{ label: 'HTTP requests'/, 'Z: HTTP with the module');
  assert.match(m, /hasTerminal\(s\) && on\('grid'\) \? \{ label: inGrid/, 'Z: grid rows with the grid module');
});

test('F28: menu.ts keeps an icon column for every item once one has an icon', () => {
  assert.match(menu, /const icons = rows\.some\(\(r\) => r\.kind === 'item' && r\.item\.icon !== undefined\);/);
  assert.match(menu, /icons \? h\('span', \{ class: 'micon', 'aria-hidden': 'true' \}, it\.icon\?\.startsWith\('fi-'\) \? h\('i', \{ class: `fi \$\{it\.icon\}` \}\) : it\.icon \?\? ''\) : null,/);
});

test('F28: alerts - Needs you (with an action) above Earlier, Clear all in the head, the desktop notification state + Kept 24 h', () => {
  const desktopPermission = new Function(`${fn(dist, 'desktopPermission')}; return desktopPermission;`)();
  assert.equal(desktopPermission('granted').text, 'Desktop notifications on');
  assert.equal(desktopPermission('denied').cls, 'warn');
  assert.match(desktopPermission('denied').title, /site settings/);
  assert.equal(desktopPermission('default').ask, true);
  assert.equal(desktopPermission('granted').empty, 'Desktop notifications are on.');
  const o = fn(main, 'openNotices');
  assert.match(o, /const needs = list\.filter\(\(n\) => n\.action\);/);
  assert.match(o, /needs\.length \? h\('div', \{ class: 'nt-sec' \}, 'Needs you'\) : null, \.\.\.needs\.map\(row\),/);
  assert.match(o, /h\('b', \{\}, 'Alerts'\)/);
  assert.match(o, /'Kept 24 h'/);
  assert.match(o, /const emptyText = mobilePushClient\(\) \? 'Nothing new\.' : `Nothing new\. \$\{perm\.empty\}`;/);
  assert.doesNotMatch(o, /cleared on close/);
  const block = css.slice(css.indexOf('/* ==== 9.80 F28'), css.indexOf('/* ==== end F28 ==== */'));
  assert.match(block, /\.nt-sec \{[^}]*text-transform: uppercase/);
});
