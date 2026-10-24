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

// The NSIS build failed on Windows: 7-Zip could not read Linux Python's links (bin/python3 -> python3.13),
// which Windows tar extracts as directory symlinks.
test('Windows runtime links are replaced by copies of their targets', async () => {
  const { mkdtemp, mkdir, writeFile, symlink, lstat, readFile, chmod, stat, rm } = await import('node:fs/promises');
  const os = await import('node:os');
  const { inlineSymlinks } = await import('../build/symlinks.mjs');
  const root = await mkdtemp(path.join(os.tmpdir(), 'piper-links-'));
  try {
    await mkdir(path.join(root, 'bin'));
    await mkdir(path.join(root, 'lib/pkgconfig'), { recursive: true });
    await writeFile(path.join(root, 'bin/python3.13'), 'interpreter');
    await chmod(path.join(root, 'bin/python3.13'), 0o755);
    await symlink('python3', path.join(root, 'bin/python'));      // a chain: python -> python3 -> python3.13
    await symlink('python3.13', path.join(root, 'bin/python3'));
    await writeFile(path.join(root, 'lib/pkgconfig/python-3.13.pc'), 'pc');
    await symlink('pkgconfig', path.join(root, 'lib/pc'));         // a link to a directory
    assert.equal(await inlineSymlinks(root), 3);
    for (const name of ['bin/python', 'bin/python3']) {
      const file = path.join(root, name);
      assert.ok((await lstat(file)).isFile(), name);
      assert.equal(await readFile(file, 'utf8'), 'interpreter');
      assert.equal((await stat(file)).mode & 0o111, 0o111, `${name} stays executable`);
    }
    assert.ok((await lstat(path.join(root, 'lib/pc'))).isDirectory());
    assert.equal(await readFile(path.join(root, 'lib/pc/python-3.13.pc'), 'utf8'), 'pc');
    await symlink('../../outside', path.join(root, 'bin/escape'));
    await assert.rejects(inlineSymlinks(root), /outside/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
