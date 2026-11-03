import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, mkdir, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const resources = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../resources');

// The app's discovery is the core's extfind.py next to a build-time extensions.py shim. When the core
// imported ENV and the shim kept only API/APIS, discovery died at import: every first start ended on
// "Setup stopped" (CI e2e: no Continue button on the check screen).
test('prepared discovery shim defines every name extfind imports, and discovery runs', async t => {
  if (!existsSync(path.join(resources, 'extfind.py'))) return t.skip('resources not prepared');
  const extfind = await readFile(path.join(resources, 'extfind.py'), 'utf8');
  const shim = await readFile(path.join(resources, 'extensions.py'), 'utf8');
  const imported = [...extfind.matchAll(/^from extensions import ([^\n(]+)$/gm)]
    .flatMap(m => m[1].split(',').map(s => s.trim().split(/\s+as\s+/)[0])).filter(Boolean);
  assert.ok(imported.includes('API'), 'extfind imports API');
  for (const name of imported) assert.match(shim, new RegExp(`^${name} = `, 'm'), name + ' missing from the shim');
  const root = await mkdtemp(path.join(os.tmpdir(), 'piper-extshim-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const d of ['home', 'state', 'projects']) await mkdir(path.join(root, d));
  const env = { PATH: process.env.PATH, HOME: path.join(root, 'home'), PIPER_HOME: path.join(root, 'state'), PROJECT_PATH: path.join(root, 'projects') };
  const out = execFileSync('python3', [path.join(resources, 'extfind.py'), '--json'], { env, encoding: 'utf8', timeout: 30000 });
  assert.ok(Array.isArray(JSON.parse(out)));
});
