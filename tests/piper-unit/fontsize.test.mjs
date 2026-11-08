import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { applyFontSizes, fontSize, onEditorFontSize } from '../../piper/web/dist/fontsize.js';

test('font preferences accept only the published values and remain independent', () => {
  assert.equal(fontSize('ui_font_size', '16'), 16);
  assert.equal(fontSize('editor_font_size', '18'), 18);
  for (const invalid of ['18', '13px', 'NaN', 'Infinity', '', null, 16])
    assert.equal(fontSize('ui_font_size', invalid), 13);
  for (const invalid of ['13', '20', '-1', {}, undefined])
    assert.equal(fontSize('editor_font_size', invalid), 12.5);
});

test('Save applies only the two chosen CSS variables; default removes them and live editors remeasure once', () => {
  const values = new Map();
  const old = globalThis.document;
  globalThis.document = { documentElement: { style: {
    setProperty: (key, value) => values.set(key, value),
    removeProperty: key => values.delete(key),
  } } };
  let measurements = 0;
  const unwatch = onEditorFontSize(() => measurements++);
  try {
    applyFontSizes({});
    assert.equal(values.size, 0, 'the default must leave existing typography unchanged');
    applyFontSizes({ui_font_size:'16', editor_font_size:'18'});
    assert.equal(values.get('--ui-scale'), String(16 / 13));
    assert.equal(values.get('--editor-fs'), '18px');
    assert.equal(measurements, 1);
    applyFontSizes({ui_font_size:'16', editor_font_size:'18'});
    assert.equal(measurements, 1, 'ordinary state polling must not repeatedly remeasure editors');
    applyFontSizes({ui_font_size:'12', editor_font_size:'18'});
    assert.equal(values.get('--ui-scale'), String(12 / 13));
    assert.equal(values.get('--editor-fs'), '18px');
    assert.equal(measurements, 1, 'UI size must not change editor size');
    applyFontSizes({ui_font_size:'13', editor_font_size:'12.5'});
    assert.equal(values.size, 0);
    assert.equal(measurements, 2);
    unwatch();
    applyFontSizes({ui_font_size:'13', editor_font_size:'14'});
    assert.equal(measurements, 2, 'destroyed editors must be unsubscribed');
    applyFontSizes({});
  } finally {
    unwatch();
    if (old === undefined) delete globalThis.document; else globalThis.document = old;
  }
});

test('every existing UI pixel font participates and default typography is byte-equivalent', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  // This snapshot covers the default typography declarations, not layout/colours.
  // Removing the preference multiplier must reproduce the default declarations exactly.
  // The migration news refresh adds two scaled declarations and raises its label/tag text to 11px.
  // The new project-path annotation shares the existing scaled token; it has no legacy declaration.
  const legacy = css.replace(/(\.wk-pop-path, \.wk-project-path\s*\{)\s*font-size: var\(--t-xs\);/, '$1');
  assert.notEqual(legacy, css, 'project path annotations use the existing UI typography token');
  // New viewer declarations have no legacy default; the scale check below still covers the entire CSS.
  const defaults = legacy.replace(/\.imageview[^{}]*\{[^{}]*\}/g, '').replace(/calc\(([^;{}]+?) \* var\(--ui-scale, 1\)\)/g, '$1');
  const declarations = defaults.match(/(?<![-\w])(?:font-size|font):\s*[^;{}]+|--t-(?:xs|sm|md|lg|xl):\s*[^;{}]+/g);
  assert.equal(declarations.length, 789);
  assert.equal(createHash('sha256').update(declarations.join('\n')).digest('hex'),
    'abc67c998029c40da8cbf442f85fcd8904a28a194fa97d4e101eb7bbf0cbff6a');
  for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (/\.cm-|\.xterm/.test(rule[1])) continue; // editor/terminal have their own preference
    for (const declaration of rule[2].matchAll(/(?<![-\w])(font-size|font):\s*([^;{}]+)/g)) {
      let value = declaration[2].replace(/calc\([^;{}]+? \* var\(--ui-scale, 1\)\)/g, 'scaled');
      if (declaration[1] === 'font') value = value.replace(/\/\s*\d+(?:\.\d+)?px/g, '/line-height');
      assert.doesNotMatch(value, /(?<![\w.-])\d+(?:\.\d+)?px/, `unscaled UI typography in ${rule[1].trim()}`);
    }
  }
});
