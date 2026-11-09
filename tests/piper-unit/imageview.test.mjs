// Image navigation must retain a readable detail and keep zoom/pan within the image.
import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fitImage, clampImage, zoomImageAt } from '../../piper/web/dist/imageview.js';

test('fit uses the available image surface and preserves small originals', () => {
  assert.equal(fitImage(1600, 1200, 390, 720), 390 / 1600);
  assert.equal(fitImage(1200, 2400, 390, 720), 720 / 2400);
  assert.equal(fitImage(100, 80, 390, 720), 1);
  assert.equal(fitImage(0, 1200, 390, 720), 1);
  assert.equal(fitImage(1600, 1200, 0, 0), 1);
});

test('a fitted image stays centered and a zoomed image cannot be dragged off the surface', () => {
  assert.deepEqual(clampImage({scale: .1, x: 900, y: -900}, 1600, 1200, 400, 700), {scale: .25, x: 0, y: 0});
  assert.deepEqual(clampImage({scale: 1, x: 900, y: -900}, 1600, 1200, 400, 700), {scale: 1, x: 600, y: -250});
  const max = clampImage({scale: 100, x: 0, y: 0}, 1600, 1200, 400, 700);
  assert.equal(max.scale, 8);
  assert.ok(Number.isFinite(max.x) && Number.isFinite(max.y));
});

test('zoom preserves the image point under the finger rather than jumping to its center', () => {
  const next = zoomImageAt({scale: .5, x: 20, y: -10}, 1, 100, 50);
  assert.deepEqual(next, {scale: 1, x: -60, y: -70});
  assert.equal((100 - next.x) / next.scale, (100 - 20) / .5);
  assert.equal((50 - next.y) / next.scale, (50 + 10) / .5);
});

test('moving a pinch center pans the same image detail while magnifying', () => {
  const next = zoomImageAt({scale: .5, x: 20, y: -10}, 1, 100, 50, 15, -25);
  assert.deepEqual(next, {scale: 1, x: -45, y: -95});
  assert.equal((115 - next.x) / next.scale, (100 - 20) / .5);
  assert.equal((25 - next.y) / next.scale, (50 + 10) / .5);
});

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.childNodes = []; this.attrs = {}; this.listeners = {}; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  append(...items) { this.childNodes.push(...items); }
  get textContent() { return this.childNodes.map(x => x.textContent ?? x).join(''); }
}
globalThis.document ??= {createElement: tag => new Element(tag), createTextNode: text => ({textContent: text})};
const elements = root => [root, ...(root.childNodes ?? []).flatMap(elements)];
const {parseMarkdown, renderMarkdown} = await import('../../piper/web/dist/markdown.js');

test('a local Markdown image opens its actual path without fetching an inline thumbnail', () => {
  const opened = [];
  const root = renderMarkdown(parseMarkdown('![Draft](docs/sample.svg)'), {openRel: path => opened.push(path)});
  const nodes = elements(root), link = nodes.find(x => x.tagName === 'BUTTON');
  assert.ok(link);
  assert.match(link.textContent, /Draft/);
  assert.match(link.textContent, /sample.svg/);
  assert.equal(nodes.some(x => x.tagName === 'IMG'), false);
  assert.deepEqual(opened, []);
  link.listeners.click();
  assert.deepEqual(opened, ['docs/sample.svg']);
});

test('an absolute Markdown image path remains absolute for source-session resolution', () => {
  const opened = [];
  const root = renderMarkdown(parseMarkdown('![Draft](/documents/sample.svg)'), {openRel: path => opened.push(path)});
  elements(root).find(x => x.tagName === 'BUTTON').listeners.click();
  assert.deepEqual(opened, ['/documents/sample.svg']);
});

test('remote images require a web-link click and forbidden schemes never become images or local actions', () => {
  for (const href of ['https://example.com/image.png', 'javascript:alert(1)', 'data:image/png;base64,AAAA']) {
    const opened = [];
    const root = renderMarkdown(parseMarkdown(`![Remote](${href})`), {openRel: path => opened.push(path)});
    const nodes = elements(root);
    assert.equal(nodes.some(x => x.tagName === 'IMG' || x.tagName === 'BUTTON'), false);
    assert.deepEqual(opened, []);
    const anchor = nodes.find(x => x.tagName === 'A');
    assert.equal(!!anchor, href.startsWith('https:'));
    if (anchor) assert.equal(anchor.attrs.rel, 'noopener noreferrer');
  }
});
