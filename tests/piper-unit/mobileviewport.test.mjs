import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installMobileViewport } from '../../piper/web/dist/mobileui.js';

test('mobile shell tracks visible keyboard area, pans with focus, and keeps pinch zoom native', () => {
  const props = new Map();
  const viewport = Object.assign(new EventTarget(), { height: 700, offsetTop: 0, scale: 1 });
  const browser = Object.assign(new EventTarget(), { innerHeight: 844, visualViewport: viewport });
  globalThis.window = browser;
  globalThis.document = { documentElement: { style: { setProperty: (key, value) => props.set(key, value) } } };
  installMobileViewport();
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
