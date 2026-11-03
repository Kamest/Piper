import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installMobileViewport } from '../../piper/web/dist/mobileui.js';

test('mobile shell tracks visible keyboard area, pans with focus, and keeps pinch zoom native', () => {
  const props = new Map(), timers = [];
  const viewport = Object.assign(new EventTarget(), { height: 700, offsetTop: 0, scale: 1 });
  const browser = Object.assign(new EventTarget(), { innerHeight: 844, visualViewport: viewport, setTimeout: fn => timers.push(fn) });
  const meta = { content: 'width=device-width, initial-scale=1' };
  const field = { tagName: 'TEXTAREA' };
  const doc = Object.assign(new EventTarget(), { activeElement: field, querySelector: () => meta,
    documentElement: { clientHeight: 844, style: { setProperty: (key, value) => props.set(key, value) } } });
  globalThis.window = browser; globalThis.document = doc;
  Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'iPhone', platform: 'iPhone', maxTouchPoints: 5 }, configurable: true });
  installMobileViewport();
  assert.equal(meta.content, 'width=device-width, initial-scale=1, maximum-scale=1', 'iOS must not auto-zoom into small fields');
  assert.equal(props.get('--mobile-viewport-height'), '700px');
  viewport.height = 440; viewport.offsetTop = 80;
  viewport.dispatchEvent(new Event('resize'));
  assert.equal(props.get('--mobile-viewport-height'), '440px');
  assert.equal(props.get('--mobile-viewport-top'), '80px');
  viewport.offsetTop = 100; viewport.dispatchEvent(new Event('scroll'));
  assert.equal(props.get('--mobile-viewport-top'), '100px');
  viewport.scale = 2; viewport.height = 220; viewport.offsetTop = 150;
  viewport.dispatchEvent(new Event('resize'));
  assert.equal(props.get('--mobile-viewport-height'), '440px', 'zoom must not resize the layout to the magnified viewport');
  viewport.scale = 1; viewport.height = 390; viewport.offsetTop = 0;
  browser.dispatchEvent(new Event('resize'));
  assert.equal(props.get('--mobile-viewport-height'), '390px');
});

test('keyboard gone: the shell returns to full height even when zoomed or Safari sent no resize', () => {
  const props = new Map(), timers = [];
  const viewport = Object.assign(new EventTarget(), { height: 420, offsetTop: 60, scale: 1.4 });
  const browser = Object.assign(new EventTarget(), { innerHeight: 600, visualViewport: viewport, setTimeout: fn => timers.push(fn) });
  const doc = Object.assign(new EventTarget(), { activeElement: { tagName: 'INPUT', type: 'text' }, querySelector: () => null,
    documentElement: { clientHeight: 844, style: { setProperty: (key, value) => props.set(key, value) } } });
  globalThis.window = browser; globalThis.document = doc;
  props.set('--mobile-viewport-height', '420px'); props.set('--mobile-viewport-top', '60px');
  // a fresh module instance: installMobileViewport binds once per page
  return import('../../piper/web/dist/mobileui.js?keyboard-gone').then(({ installMobileViewport: install }) => {
    install();
    assert.equal(props.get('--mobile-viewport-height'), '420px', 'zoomed while typing keeps the layout');
    doc.activeElement = { tagName: 'BUTTON' };   // card approved: focus moved to a button, keyboard hides
    doc.dispatchEvent(new Event('focusout'));
    assert.equal(props.get('--mobile-viewport-height'), '844px');
    assert.equal(props.get('--mobile-viewport-top'), '0px');
    props.set('--mobile-viewport-height', '420px');
    timers.forEach(fn => fn());
    assert.equal(props.get('--mobile-viewport-height'), '844px', 'the delayed check re-measures after the keyboard animation');
  });
});

test('connection notice has a grace period, escalates once, and clears all timers on verified recovery', async () => {
  const { mobileOffline } = await import('../../piper/web/dist/mobileui.js');
  const timers = new Map(); let serial = 0;
  class Node {
    constructor(text = '') { this.children = []; this.attrs = {}; this.dataset = {}; this.textContent = text; this.listeners = {}; }
    setAttribute(k, v) { this.attrs[k] = v; if (k === 'data-state') this.dataset.state = v; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(k, fn) { this.listeners[k] = fn; }
    append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
    prepend(n) { n.parent = this; this.children.unshift(n); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); }
    text() { return this.textContent + this.children.map(n => n.text()).join(''); }
  }
  const body = new Node(), app = new Node(); app.setAttribute('id', 'app'); body.append(app);
  const lookup = (node, id) => node.attrs.id === id ? node : node.children.map(n => lookup(n, id)).find(Boolean);
  globalThis.document = { hidden: false, body, getElementById: id => lookup(body, id), createElement: () => new Node(), createTextNode: text => new Node(text) };
  globalThis.window = { setTimeout(fn, ms) { const id = ++serial; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const fire = ms => { const [id, timer] = [...timers].find(([, timer]) => timer.ms === ms); timers.delete(id); timer.fn(); };
  const old = new Node(); old.setAttribute('id', 'banner'); body.append(old);
  mobileOffline(true, 'Studio Mac');
  assert.equal(app.inert, true);
  assert.equal(lookup(body, 'banner'), undefined);
  assert.equal(lookup(body, 'mobile-offline'), undefined, 'short recoveries do not flash a warning');
  mobileOffline(true, 'Studio Mac'); assert.equal(timers.size, 1, 'poll failures must not restart the grace clock');
  fire(2000);
  const notice = lookup(body, 'mobile-offline');
  assert.equal(notice.dataset.state, 'reconnecting');
  assert.equal(notice.attrs.role, 'status');
  assert.match(notice.text(), /Reconnecting to Studio Mac/);
  fire(13000);
  assert.equal(notice.dataset.state, 'offline'); assert.equal(notice.attrs.role, 'alert');
  assert.match(notice.text(), /Can't reach Studio Mac/);
  assert.match(notice.text(), /check them before repeating/);
  assert.match(notice.text(), /Retry now/);
  mobileOffline(false);
  assert.equal(app.inert, false); assert.equal(lookup(body, 'mobile-offline'), undefined); assert.equal(timers.size, 0);
  document.hidden = true; mobileOffline(true);
  assert.equal(app.inert, true); assert.equal(timers.size, 0, 'background keeps actions suspended without starting a visible outage');
  document.hidden = false; mobileOffline(true);
  assert.equal(timers.size, 1, 'foreground starts a new grace period');
  mobileOffline(false);
  assert.equal(timers.size, 0, 'a quick resume cancels delayed insertion, including a late red notice');
});
