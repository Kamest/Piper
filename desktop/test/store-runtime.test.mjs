import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { updatesEnabled } = require('../src/lib/policy.cjs');
const { wslBootstrap } = require('../src/lib/platform.cjs');
const { setupController } = require('../src/lib/setup.cjs');

test('Store packages never start release checks or electron-updater', () => {
  assert.equal(updatesEnabled(true, true), false);
  assert.equal(updatesEnabled(false, true), false);
  assert.equal(updatesEnabled(true, undefined), true);
  assert.equal(updatesEnabled(true, false), true);
  assert.equal(updatesEnabled(false, false), false);
});

test('Store WSL bootstrap keeps UAC but never registers a WindowsApps exe for RunOnce', () => {
  const executable = 'C:\\Program Files\\WindowsApps\\Piper_Test\\app\\Piper.exe';
  const [command, args] = wslBootstrap(executable, true);
  assert.equal(command, 'powershell.exe');
  assert.match(args.at(-1), /Start-Process wsl.exe.*--install --no-distribution.*-Verb RunAs/);
  assert.doesNotMatch(args.at(-1), /HKCU|RunOnce|Piper\.exe|Restart-Computer|shutdown/);
  assert.match(wslBootstrap(executable)[1].at(-1), /RunOnce/);
});

function fixture(windowsStore = true) {
  const calls = [], copies = [], files = new Set();
  let distro = false, installFails = false;
  const userData = '/app';
  const marker = path.join(userData, 'resume-wsl');
  const fs = {
    access: async file => { if (!files.has(file)) throw Error('missing'); },
    writeFile: async file => { files.add(file); },
    appendFile: async () => {}, mkdir: async () => {},
    rm: async file => { files.delete(file); },
    cp: async (source, target) => { copies.push([source, target]); files.add(path.join(target, 'bin/python3')); },
    realpath: async file => path.join('/physical', file)
  };
  const run = async (command, args) => {
    calls.push([command, args]);
    if (args.includes('--list')) return { stdout: distro ? '* Ubuntu          Running         2\n' : '' };
    if (args.includes('--install')) {
      if (installFails) throw Error('WSL not ready');
      distro = true;
    }
    if (args.includes('printf "%s" "$HOME"')) return { stdout: '/home/test' };
    if (args.includes('wslpath')) return { stdout: '/mnt/c' + args.at(-1).replace(/\\/g, '/') };
    return { stdout: '' };
  };
  const options = { env: {}, platform: 'win32', windowsStore, arch: 'x64', userData,
    home: { native: '/home/native', linux: '' }, resources: 'C:\\Program Files\\WindowsApps\\Piper_Test\\app\\resources\\desktop',
    pythonSource: 'C:\\Program Files\\WindowsApps\\Piper_Test\\app\\resources\\python', pythonVersion: 'test',
    executable: 'C:\\Program Files\\WindowsApps\\Piper_Test\\app\\Piper.exe', fs, run, changed: () => {} };
  return { calls, copies, files, marker, options, controller: () => setupController(options),
    enableDistro: () => { distro = true; }, failInstall: value => { installFails = value; } };
}

test('a new Store process resumes the saved WSL checkpoint on manual launch', async () => {
  const f = fixture();
  const before = f.controller();
  await before.action('wsl');
  assert.ok(f.files.has(f.marker));
  assert.match(before.wizard.state.message, /then open Piper/);
  assert.doesNotMatch(f.calls[0][1].at(-1), /RunOnce/);
  const after = f.controller();
  await after.resume();
  assert.equal(after.distro(), 'Ubuntu');
  assert.ok(f.calls.some(([cmd, args]) => cmd === 'wsl.exe' && args.join(' ') === '--install -d Ubuntu --no-launch'));
  assert.ok(!f.files.has(f.marker));
  f.calls.length = 0;
  await f.controller().resume();
  assert.equal(f.calls.length, 0);
});

test('failed WSL resume retains the checkpoint; non-Store bootstrap still registers RunOnce', async () => {
  for (const store of [true, false]) {
    const f = fixture(store);
    await f.controller().action('wsl');
    assert.equal(f.calls[0][1].at(-1).includes('RunOnce'), !store);
    f.failInstall(true);
    await assert.rejects(f.controller().resume(), /WSL not ready/);
    assert.ok(f.files.has(f.marker));
    f.failInstall(false);
    await f.controller().resume();
    assert.ok(!f.files.has(f.marker));
  }
});

test('Store setup stages packaged resources natively before WSL reads their physical paths', async () => {
  const f = fixture();
  f.enableDistro();
  const c = f.controller();
  await c.windows();
  const paths = await c.prepare();
  assert.deepEqual(f.copies, [
    [f.options.resources, path.join('/app', 'wsl-resources')],
    [f.options.pythonSource, path.join('/app', 'runtime', 'python-test')]
  ]);
  const translated = f.calls.filter(([, args]) => args.includes('wslpath')).map(([, args]) => args.at(-1));
  assert.deepEqual(translated, [path.join('/physical', '/app', 'wsl-resources'), path.join('/physical', '/app', 'runtime', 'python-test')]);
  assert.ok(!translated.some(p => p.includes('WindowsApps')));
  assert.equal(paths.python, '/home/test/.local/share/piper-desktop/runtime/python-test/bin/python3');
  assert.ok(f.calls.some(([, args]) => args.at(-1).includes('cp -R')));
  await c.prepare();
  assert.equal(f.copies.filter(([source]) => source === f.options.pythonSource).length, 1);
});

test('non-Store setup continues to copy the original bundled sources directly into WSL', async () => {
  const f = fixture(false);
  f.enableDistro();
  const c = f.controller();
  await c.windows();
  await c.prepare();
  assert.deepEqual(f.copies, []);
  const translated = f.calls.filter(([, args]) => args.includes('wslpath')).map(([, args]) => args.at(-1));
  assert.deepEqual(translated, [f.options.resources, f.options.pythonSource]);
});
