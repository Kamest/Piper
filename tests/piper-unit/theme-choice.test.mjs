// node --test: N1 - the theme from Settings › Interface, applied at once, terminals recoloured.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const props = {};
let light = false;
globalThis.window = { matchMedia: () => ({ matches: light }) };
globalThis.document = { documentElement: { dataset: {}, style: { setProperty: (k, v) => { props[k] = v; } } } };
const { applyChoice } = await import('../../piper/web/dist/theme.js');

const themes = JSON.parse(readFileSync(new URL('../../piper/web/themes.json', import.meta.url), 'utf8'));
const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const tok = (name) => themes.themes[name].tokens;
const first = (name) => Object.keys(tok(name))[0];

test('N1: dark / light / indigo paint that theme; an unknown value falls back to dark', () => {
  const file = {};   // every choice is its own key in Piper's themes file
  const k = first('light');
  for (const name of ['light', 'indigo', 'dark']) {
    applyChoice(themes, name);
    assert.equal(props['--' + k], tok(file[name] ?? name)[k], name);
    assert.equal(document.documentElement.dataset.scheme, themes.themes[file[name] ?? name].scheme, name);
  }
  applyChoice(themes, 'light');
  applyChoice(themes, 'solarized');
  assert.equal(document.documentElement.dataset.scheme, 'dark');
});

test('N1: auto follows the OS scheme', () => {
  light = true;
  assert.equal(applyChoice(themes, 'auto'), 'light');
  light = false;
  assert.equal(applyChoice(themes, 'auto'), 'dark');
});

test('N1: the poll applies a changed theme and recolours every open terminal; auto listens to the OS', () => {
  const main = src('main.ts');
  assert.match(main, /const want = state\?\.gui_flags\?\.theme \|\| window\.__PIPER__\?\.theme \|\| 'dark';.*\n\s+if \(want === themeNow\) return;/, 'the boot blob before the first poll');
  assert.match(main, /if \(!first\) TermView\.rethemeAll\(\);/);
  assert.match(main, /syncTheme\(\);\n\s+Object\.assign\(TERM_FLAGS, \{ hostLinks/, 'on every poll');
  assert.match(main, /matchMedia\('\(prefers-color-scheme: light\)'\)\.addEventListener\('change'/);
  assert.match(src('term.ts'), /static rethemeAll\(\): void \{\n\s+for \(const t of LIVE\) t\.retheme\(\);/);
});
