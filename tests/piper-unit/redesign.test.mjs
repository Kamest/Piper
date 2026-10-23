// node --test: 9.62 redesign base - neutral greys, the primary
// button light on dark (--acc), green only for state, buttons without a frame, row actions on hover, section headings.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const themes = JSON.parse(readFileSync(new URL('../../piper/web/themes.json', import.meta.url), 'utf8')).themes;

const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const lum = (h) => {
  const [r, g, b] = rgb(h).map((c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

test('T1: every theme has the primary pair acc / acc-fg, readable (4.5:1)', () => {
  for (const [name, t] of Object.entries(themes)) {
    assert.match(t.tokens.acc ?? '', /^#[0-9a-f]{6}$/i, `${name}: acc`);
    assert.ok(contrast(t.tokens.acc, t.tokens['acc-fg']) >= 4.5, `${name}: acc-fg on acc`);
  }
  assert.ok(lum(themes.dark.tokens.acc) > lum(themes.dark.tokens.page), 'dark: a light button');
  assert.ok(lum(themes.light.tokens.acc) < lum(themes.light.tokens.page), 'light: a dark button');
});

test('T1: dark and light surfaces are neutral grey - no blue cast (indigo keeps its tint)', () => {
  for (const name of ['dark', 'light']) {
    for (const k of ['page', 'card', 'well', 'tile', 'tilebtn', 'line', 'fg', 'mut', 'faint', 'neutral-bg']) {
      const [r, , b] = rgb(themes[name].tokens[k]);
      assert.ok(Math.abs(b - r) <= 6, `${name}.${k} ${themes[name].tokens[k]} leans blue`);
    }
  }
});

test("T1: app.css's fallback block is the dark theme (the page before theme.ts runs looks the same)", () => {
  const root = css.slice(css.indexOf(':root {'), css.indexOf('}', css.indexOf(':root {')));
  for (const [k, v] of Object.entries(themes.dark.tokens)) {
    assert.match(root, new RegExp(`--${k}: ${v};`, 'i'), `--${k}`);
  }
});

test('Z3: the primary button is --acc, never a green fill; plain buttons have no frame', () => {
  assert.match(css, /\nbutton\.primary \{ background: var\(--acc\); color: var\(--acc-fg\);/);
  assert.match(css, /\nbutton \{[^}]*border: 1px solid transparent;/);
  const greenPrimary = [...css.matchAll(/([^{}\n]*\.primary[^{}]*)\{([^}]*)\}/g)]
    .filter((m) => /background:\s*var\(--ok\)/.test(m[2])).map((m) => m[1].trim());
  assert.deepEqual(greenPrimary, [], 'a primary button painted green');
  assert.match(css, /button\.primary kbd \{/, 'a key inside the primary button reads on it');
});

test('Z4: row actions hide by opacity (Tab still reaches them) and show on hover, focus and selection', () => {
  assert.match(css, /\.ra-row \.row-actions \{ opacity: 0;/);
  assert.match(css, /\.ra-row:focus-within \.row-actions/);
  assert.match(css, /@media \(hover: none\) \{ \.ra-row \.row-actions \{ opacity: 1; \} \}/);
  assert.doesNotMatch(css, /\.row-actions \{[^}]*(visibility: hidden|display: none)/);
});

test('Z5: a section heading is 11 px capitals in --faint-t, not --faint', () => {
  assert.match(css, /\.sec-h \{ font-size: var\(--t-xs\);[^}]*text-transform: uppercase;[^}]*color: var\(--faint-t\); \}/);
});
