import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { setupController } = require('../../desktop/src/lib/setup.cjs');
const { checkComputer } = require('../../desktop/src/lib/wizard.cjs');
const execute = promisify(execFile);

async function runtimeFixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'piper-runtime-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const source = path.join(root, 'bundle/python'), runtime = path.join(root, 'user/runtime');
  await fs.mkdir(path.join(source, 'bin'), { recursive: true });
  await fs.mkdir(path.join(source, 'lib/pkgconfig'), { recursive: true });
  await fs.writeFile(path.join(source, 'bin/python3.13'), 'interpreter');
  await fs.writeFile(path.join(source, 'lib/pkgconfig/python.pc'), 'metadata');
  await fs.symlink('python3.13', path.join(source, 'bin/python3'));
  await fs.symlink('python3', path.join(source, 'bin/python'));
  await fs.symlink('pkgconfig/python.pc', path.join(source, 'lib/python.pc'));
  const controller = (fileSystem = fs) => setupController({ env: {}, platform: 'darwin',
    userData: path.join(root, 'user'), resources: path.join(root, 'bundle/desktop'),
    pythonSource: source, runtimeDir: runtime, home: { native: root }, fs: fileSystem });
  return { root, source, runtime, controller };
}

test('copied runtime keeps relative symlinks and survives removal of the application bundle', async t => {
  const f = await runtimeFixture(t);
  const prepared = await f.controller().prepare();
  assert.equal(await fs.readlink(prepared.python), 'python3.13');
  assert.equal(await fs.realpath(prepared.python), path.join(f.runtime, 'bin/python3.13'));
  await fs.rm(f.source, { recursive: true });
  assert.equal(await fs.readFile(prepared.python, 'utf8'), 'interpreter');
  assert.equal(await fs.readFile(path.join(f.runtime, 'lib/python.pc'), 'utf8'), 'metadata');
});

test('preparing an old runtime repairs its links without replacing files used by a running service', async t => {
  const f = await runtimeFixture(t);
  await fs.cp(f.source, f.runtime, { recursive: true });
  assert.equal(await fs.realpath(path.join(f.runtime, 'bin/python3')), path.join(f.source, 'bin/python3.13'));
  const binary = path.join(f.runtime, 'bin/python3.13');
  await fs.writeFile(binary, 'running interpreter');
  const before = await fs.stat(binary);
  await f.controller().prepare();
  assert.equal(await fs.readlink(path.join(f.runtime, 'bin/python3')), 'python3.13');
  assert.equal(await fs.readlink(path.join(f.runtime, 'lib/python.pc')), 'pkgconfig/python.pc');
  assert.equal(await fs.readFile(binary, 'utf8'), 'running interpreter');
  assert.equal((await fs.stat(binary)).ino, before.ino);
  assert.equal(await fs.readFile(path.join(f.source, 'bin/python3.13'), 'utf8'), 'interpreter');
});

test('an old runtime with a broken application link is repaired without replacing its interpreter', async t => {
  const f = await runtimeFixture(t);
  await fs.cp(f.source, f.runtime, { recursive: true });
  await fs.unlink(path.join(f.runtime, 'bin/python3'));
  await fs.symlink('/removed/Piper.app/python3.13', path.join(f.runtime, 'bin/python3'));
  const binary = path.join(f.runtime, 'bin/python3.13');
  const before = await fs.stat(binary);
  await f.controller().prepare();
  assert.equal(await fs.readlink(path.join(f.runtime, 'bin/python3')), 'python3.13');
  assert.equal((await fs.stat(binary)).ino, before.ino);
});

test('a healthy existing runtime is reused without traversing the application or copying files', async t => {
  const f = await runtimeFixture(t);
  await fs.cp(f.source, f.runtime, { recursive: true, verbatimSymlinks: true });
  const fileSystem = { ...fs, cp: () => assert.fail('healthy runtime copied'),
    lstat: () => assert.fail('application traversed') };
  await f.controller(fileSystem).prepare();
});

test('extension discovery never writes bytecode beside a module in the application bundle', async t => {
  const f = await runtimeFixture(t);
  const resources = path.join(f.root, 'bundle/desktop');
  await fs.mkdir(resources, { recursive: true });
  const script = path.join(resources, 'extfind.py');
  await fs.writeFile(path.join(resources, 'extension_probe.py'), 'VALUE = 1\n');
  await fs.writeFile(script, 'import extension_probe\nprint("[]")\n');
  const env = { ...process.env };
  delete env.PYTHONDONTWRITEBYTECODE;
  delete env.PYTHONPYCACHEPREFIX;
  await checkComputer({ platform: 'linux', python: 'python3', extfind: script,
    installDir: '/test/piper', cli: null,
    run: async (command, args) => command === 'python3'
      ? { ...(await execute(command, args, { env })), code: 0 }
      : { stdout: '', code: 0 } });
  await assert.rejects(fs.access(path.join(resources, '__pycache__')), { code: 'ENOENT' });
});


test('desktop startup timeout counts slow probes inside the thirty second budget', async () => {
  const { service } = require('../../desktop/src/lib/service.cjs');
  let elapsed = 0, probes = 0;
  const s = service({ port: 18995, now: () => elapsed,
    fetch: async () => { elapsed += elapsed < 30000 ? Math.min(3000, 30000 - elapsed) : 3000; probes++; throw Error('stalled'); },
    delay: async ms => { elapsed += ms; } });
  await assert.rejects(s.wait(), /30 s/);
  assert.ok(elapsed <= 30000, `startup spent ${elapsed} ms`);
  assert.ok(probes < 10, 'slow probes use the shared deadline');
});

test('desktop restart has a bounded old-service wait before checking recovery', async () => {
  const { service } = require('../../desktop/src/lib/service.cjs');
  let elapsed = 0, posted = false, probes = 0;
  const s = service({ port: 18995, platform: 'linux', now: () => elapsed,
    locate: async () => '/example/piper',
    run: async () => ({ stdout: 'http://127.0.0.1:18995/#token=fake-only-token-1234567890' }),
    fetch: async (_url, init) => {
      if (init.method === 'POST') { posted = true; return { ok: true }; }
      elapsed += elapsed < 10000 ? Math.min(3000, 10000 - elapsed) : 3000; probes++;
      return { ok: true, json: async () => ({ sessions: [] }) };
    }, delay: async ms => { elapsed += ms; } });
  await s.restart();
  assert.equal(posted, true);
  assert.ok(elapsed <= 13000, `restart observation spent ${elapsed} ms`);
  assert.ok(probes <= 5);
});
