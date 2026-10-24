import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { forArch, missingRuntimes } = require('../build/arch.cjs');
const build = require('../package.json').build;

// desktop-v0.1.1: the arm64 Linux job also built an x86_64 AppImage without Python, and the Windows jobs a
// combined installer; each job must build only its own architecture.
test('targets are restricted to the job architecture', () => {
  for (const arch of ['x64', 'arm64']) {
    const out = forArch(build, arch);
    for (const key of ['mac', 'win', 'linux']) {
      assert.ok(out[key].target.length, key);
      for (const t of out[key].target) assert.deepEqual(t.arch, [arch], `${key} ${t.target}`);
    }
  }
  assert.equal(forArch(build, undefined), build);
});

test('a build without the architecture runtime is refused', () => {
  const present = new Set(['/r/vendor/linux-x64/python']);
  assert.deepEqual(missingRuntimes(forArch(build, 'x64'), 'linux', '/r', d => present.has(d)), []);
  assert.deepEqual(missingRuntimes(build, 'linux', '/r', d => present.has(d)), ['/r/vendor/linux-arm64/python']);
  assert.deepEqual(missingRuntimes(forArch(build, 'arm64'), 'win32', '/r', () => false), ['/r/vendor/win-arm64/python']);
});
