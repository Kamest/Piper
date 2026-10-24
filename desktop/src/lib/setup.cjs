'use strict';
const path = require('node:path');
const { Wizard, checkComputer, install } = require('./wizard.cjs');
const { wsl, wslRows, podmanCommand, wslBootstrap } = require('./platform.cjs');
const { createHash } = require('node:crypto');

/** Effects for the local setup window. All dependencies are explicit for fake-only tests. */
function setupController({ env, platform, arch, home, userData, resources, pythonSource, pythonVersion = 'bundled', runtimeDir, fs, run, fetch, executable, service, changed, openExternal, showLog }) {
  const wizard = new Wizard(); let distro = '', busy = false, paths, installed = false, pendingExtension, gitRequested = false;
  let logWrites = Promise.resolve();
  let installEnv = env;
  const installDir = () => installEnv.PROJECT_PATH ? path.posix.join(installEnv.PROJECT_PATH, 'piper') : path.posix.join(platform === 'win32' ? home.linux : home.native, '.local/share/piper');
  const emit = () => changed({ ...wizard.state, busy });
  const line = value => {
    wizard.line(value); const entry = wizard.state.log.at(-1) + '\n';
    logWrites = logWrites.then(() => fs.appendFile(path.join(userData, 'setup.log'), entry, { mode: 0o600 })).catch(() => {});
    emit();
  };

  async function windows() {
    if (platform !== 'win32') return;
    try {
      const list = await run('wsl.exe', ['--list', '--verbose'], { acceptFailure: true });
      distro = wslRows(list.stdout).find(d => d.version === 2)?.name || '';
      if (distro) home.linux = (await run('wsl.exe', ['-d', distro, '--', 'sh', '-c', 'printf "%s" "$HOME"'])).stdout.trim();
    } catch { distro = ''; }
  }
  async function prepare() {
    const bundle = env.PIPER_DESKTOP_INSTALL_SH || path.join(resources, 'install.sh');
    if (platform !== 'win32') {
      const runtime = runtimeDir || path.join(userData, 'runtime', 'python-' + pythonVersion);
      // Stable path: a service must not reference an AppImage's temporary mount.
      try { await fs.access(path.join(runtime, 'bin/python3')); }
      catch { await fs.mkdir(path.dirname(runtime), { recursive: true }); await fs.cp(pythonSource, runtime, { recursive: true }); }
      paths = { installer: bundle, python: path.join(runtime, 'bin/python3'), extfind: path.join(resources, 'extfind.py'),
        serviceEnv: {} };
    } else if (distro) {
      const target = home.linux + '/.local/share/piper-desktop';
      const toLinux = async p => (await run('wsl.exe', ['-d', distro, '--', 'wslpath', '-u', p])).stdout.trim();
      installEnv = { ...env };
      for (const key of ['PROJECT_PATH', 'PIPER_HOME']) if (/^[A-Za-z]:[\\/]/.test(installEnv[key] || '')) installEnv[key] = await toLinux(installEnv[key]);
      await run(...wsl('mkdir', ['-p', target], distro));
      const source = await toLinux(resources);
      await run(...wsl('cp', ['-R', source + '/.', target + '/'], distro));
      const py = await toLinux(pythonSource);
      await run(...wsl('mkdir', ['-p', target + '/runtime'], distro));
      await run(...wsl('sh', ['-c', 'test -x "$1/bin/python3" || cp -R "$2" "$1"', 'sh', target + '/runtime/python-' + pythonVersion, py], distro));
      const runtime = target + '/runtime/python-' + pythonVersion;
      paths = { installer: env.PIPER_DESKTOP_INSTALL_SH ? await toLinux(bundle) : target + '/install.sh', extfind: target + '/extfind.py', python: runtime + '/bin/python3',
        serviceEnv: { PATH: `${runtime}/bin:${home.linux}/.local/bin:/usr/local/bin:/usr/bin:/bin`, ...(installEnv.PIPER_HOME ? { PIPER_HOME: installEnv.PIPER_HOME } : {}), ...(env.PIPER_PORT ? { PIPER_PORT: env.PIPER_PORT } : {}), ...(installEnv.PROJECT_PATH ? { PROJECT_PATH: installEnv.PROJECT_PATH } : {}) } };
    }
    return paths;
  }
  async function check() {
    if (busy) return;
    busy = true; wizard.state.checking = true; wizard.state.screen = 'check'; wizard.state.retryAction = 'check'; emit();
    try {
      await windows(); await prepare();
      const result = await checkComputer({ run, platform, distro, python: paths?.python, extfind: paths?.extfind,
        serviceEnv: paths?.serviceEnv, cli: await service.locate(), installDir: installDir(),
        onRow: rows => { wizard.state.rows = rows; emit(); } });
      wizard.checked(result.rows, result.extensions);
    } catch (e) { wizard.fail(e); }
    finally { busy = false; wizard.state.checking = false; emit(); }
  }
  async function performInstall() {
    if (busy) return;
    busy = true; wizard.state.screen = 'install'; wizard.state.retryAction = 'install'; emit();
    try {
      if (!installed) {
        await install({ run, platform, distro, installer: paths.installer, env: installEnv, extension: wizard.selected(), serviceEnv: paths.serviceEnv, onLine: line });
        installed = true;
      }
      await service.ready();
    } catch (e) { wizard.fail(e); }
    finally { await logWrites; busy = false; emit(); }
  }
  async function extensions() {
    const found = JSON.parse((await service.command(['extensions', '--json'])).stdout);
    if (!Array.isArray(found)) throw new Error('Invalid extension list');
    wizard.checked([], found); wizard.state.screen = 'extensions'; emit();
  }
  async function setExtension(selected) {
    const found = JSON.parse((await service.command(['extensions', '--json'])).stdout);
    if (selected && !found.some(e => e.path === selected && e.compatible)) throw new Error('Extension is no longer available');
    // Do not trust one extension while another is trusted: the CLI deliberately refuses ambiguity.
    for (const e of found.filter(e => e.trusted && e.path !== selected)) await service.command(['extensions', 'untrust', e.path]);
    if (selected) await service.command(['extensions', 'trust', selected]);
    await service.command(['restart']); await extensions();
  }
  async function podman() {
    if (platform === 'darwin') {
      const pins = JSON.parse(await fs.readFile(path.join(resources, 'podman.json'), 'utf8'));
      const pin = pins[arch]; if (!pin) throw new Error('Use the guide to install Podman on this architecture.');
      const response = await fetch(pin.url); if (!response.ok) throw new Error('Podman download failed');
      const bytes = Buffer.from(await response.arrayBuffer());
      if (createHash('sha256').update(bytes).digest('hex') !== pin.sha256) throw new Error('Podman SHA-256 mismatch');
      const pkg = path.join(userData, 'podman.pkg'); await fs.writeFile(pkg, bytes, { mode: 0o600 });
      await run(...podmanCommand(platform, { packagePath: pkg }), { timeout: 20 * 60 * 1000, onLine: line });
      // A new machine only; never change an existing user's Podman machine.
      const machines = JSON.parse((await run('podman', ['machine', 'list', '--format', 'json'])).stdout);
      if (!machines.length) { await run('podman', ['machine', 'init', 'piper'], { timeout: 20 * 60 * 1000, onLine: line }); await run('podman', ['machine', 'start', 'piper'], { timeout: 300000, onLine: line }); }
    } else {
      let manager;
      if (platform === 'linux') for (const name of ['apt', 'dnf', 'pacman', 'zypper']) {
        if (!(await run('sh', ['-c', 'command -v "$1"', 'sh', name], { acceptFailure: true })).code) { manager = name; break; }
      }
      await run(...podmanCommand(platform, { distro, manager }), { timeout: 20 * 60 * 1000, onLine: line });
    }
  }
  return {
    wizard, check, extensions, windows, prepare, distro: () => distro, installDir,
    get busy() { return busy; },
    async action(name, value) {
      if (busy && name !== 'log') return;
      try {
        switch (name) {
          case 'check': return check();
          case 'continue': wizard.continue(); if (wizard.state.screen === 'install') return performInstall(); break;
          case 'choice': wizard.choose(value); break;
          case 'next':
            if (wizard.state.screen !== 'extension') break;
            if (wizard.state.manage) {
              if (wizard.state.choices[wizard.pending[0].path] === 'load') {
                pendingExtension = wizard.pending[0].path; wizard.state.retryAction = 'extension';
                busy = true; emit(); try { await setExtension(pendingExtension); } finally { busy = false; }
              } else await extensions();
              wizard.state.manage = false;
            }
            else { wizard.next(); if (wizard.state.screen === 'install') return performInstall(); }
            break;
          case 'back': if (wizard.state.manage) await extensions(); else wizard.state.screen = 'check'; break;
          case 'retry': {
            const action = wizard.state.retryAction;
            if (action === 'extensions') { await extensions(); break; }
            if (action === 'extension') { busy = true; emit(); try { await setExtension(pendingExtension); } finally { busy = false; } break; }
            wizard.retry(); if (action === 'install' && paths) return performInstall(); return check();
          }
          case 'git-install':
            if (platform === 'darwin' && !gitRequested) { gitRequested = true; await run('xcode-select', ['--install']); }
            return check();
          case 'guide': return openExternal('https://github.com/Kamest/Piper/blob/main/docs/GUIDE.md#desktop-app');
          case 'log': return showLog();
          case 'podman': wizard.state.retryAction = 'check'; busy = true; emit(); try { await podman(); } finally { busy = false; } return check();
          case 'wsl':
            if (platform !== 'win32') break;
            await fs.writeFile(path.join(userData, 'resume-wsl'), '1');
            await run(...wslBootstrap(executable), { timeout: 20 * 60 * 1000, onLine: line });
            wizard.state.message = 'WSL is enabled. Restart Windows to continue.'; break;
          case 'load': {
            const e = wizard.state.extensions.find(e => e.path === value && e.compatible);
            if (!e) throw new Error('Extension is no longer available');
            wizard.pending = [e]; wizard.state.consent = [e]; wizard.state.index = 0; wizard.state.manage = true; wizard.state.choices[e.path] = e.source === 'registered' ? 'load' : 'skip'; wizard.state.screen = 'extension'; break;
          }
          case 'untrust':
            if (!wizard.state.extensions.some(e => e.path === value && e.trusted)) throw new Error('Extension is no longer loaded');
            pendingExtension = null; wizard.state.retryAction = 'extension';
            busy = true; emit(); try { await setExtension(null); } finally { busy = false; } break;
          default: throw new Error('Unknown setup action');
        }
      } catch (e) { wizard.fail(e); }
      emit();
    },
    async resume() {
      if (platform !== 'win32') return;
      try { await fs.access(path.join(userData, 'resume-wsl')); } catch { return; }
      await windows();
      if (!distro) await run('wsl.exe', ['--install', '-d', 'Ubuntu', '--no-launch'], { timeout: 20 * 60 * 1000, onLine: line });
      await windows(); if (distro) await fs.rm(path.join(userData, 'resume-wsl'));
    }
  };
}
module.exports = { setupController };
