import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const vendor = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../vendor');

// Linux Python's terminfo (hp2621A, hp2621a) broke packaging on case-insensitive file systems:
// electron-builder failed with EEXIST copying the runtime into the app.
test('vendored Python has no names differing only by case', async t => {
  const trees = existsSync(vendor) ? (await readdir(vendor)).map(d => path.join(vendor, d, 'python')).filter(existsSync) : [];
  if (!trees.length) return t.skip('no vendored Python prepared');
  for (const tree of trees) {
    assert.ok(!existsSync(path.join(tree, 'share/terminfo')), tree);
    const walk = async dir => {
      const names = new Map();
      for (const entry of await readdir(dir, { withFileTypes: true })) {
        const key = entry.name.toLowerCase();
        assert.ok(!names.has(key), `${path.join(dir, entry.name)} collides with ${names.get(key)}`);
        names.set(key, entry.name);
        if (entry.isDirectory()) await walk(path.join(dir, entry.name));
      }
    };
    await walk(tree);
  }
});
