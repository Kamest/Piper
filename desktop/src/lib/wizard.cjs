'use strict';
const { installArgs, wsl } = require('./platform.cjs');
const { redact } = require('./policy.cjs');

/** Pure state transitions; only the controller performs effects after an explicit action. */
class Wizard {
  constructor() { this.state = { screen: 'check', checking: true, rows: [], extensions: [], index: 0, choices: {}, steps: [], log: [], error: '' }; }
  checked(rows, extensions) {
    Object.assign(this.state, { rows, extensions, checking: false, screen: 'check', error: '' });
    for (const e of extensions) this.state.choices[e.path] ??= e.compatible && (e.trusted || e.source === 'registered') ? 'load' : 'skip';
  }
  continue() {
    if (this.state.checking) return;
    if (this.state.rows.some(r => r.id === 'wsl' && r.status === 'problem')) { this.state.screen = 'wsl'; return; }
    if (this.state.rows.some(r => r.id === 'docker' && r.status === 'problem')) { this.state.screen = 'runtime'; return; }
    if (this.state.rows.some(r => r.status === 'problem')) return;
    this.pending = this.state.extensions.filter(e => e.compatible && !e.trusted);
    this.state.consent = this.pending;
    this.state.index = 0;
    this.state.screen = this.pending.length ? 'extension' : 'install';
  }
  choose(choice) {
    const e = this.pending?.[this.state.index];
    if (!e || !['load', 'skip'].includes(choice)) throw new Error('Invalid extension choice');
    // Exactly one extension may be selected.
    if (choice === 'load') for (const p of Object.keys(this.state.choices)) this.state.choices[p] = 'skip';
    this.state.choices[e.path] = choice;
  }
  next() { if (++this.state.index >= this.pending.length) this.state.screen = 'install'; }
  selected() { return this.state.extensions.find(e => e.compatible && this.state.choices[e.path] === 'load')?.path; }
  line(value) {
    const line = redact(value); this.state.log.push(line); this.state.log = this.state.log.slice(-250);
    const phases = [/hole/i, /clon|updating|checkout/i, /service|piperd|launchd|systemd/i, /doctor|\bok\b|\bwarn\b/i];
    const i = phases.findIndex(re => re.test(line));
    if (i >= 0) this.state.phase = Math.max(this.state.phase || 0, i);
  }
  fail(error) { this.state.error = redact(error.message); this.state.screen = 'error'; }
  retry() { this.state.error = ''; this.state.screen = 'install'; }
}

/** Checks are read-only; missing installable components differ from blocking prerequisites. */
async function checkComputer({ run, platform, distro, python, extfind, cli, installDir, serviceEnv = {}, onRow = () => {} }) {
  const rows = [];
  const add = row => { rows.push(row); onRow([...rows]); };
  const invoke = (cmd, args) => platform === 'win32' ? run(...wsl(cmd, args, distro, serviceEnv), { acceptFailure: true }) : run(cmd, args, { acceptFailure: true });
  const check = async (cmd, args) => { try { return !(await invoke(cmd, args)).code; } catch { return false; } };
  if (platform === 'win32') {
    add({ id: 'wsl', name: 'WSL 2', status: distro ? 'ok' : 'problem', detail: distro || 'WSL 2 and a Linux distribution are required.' });
    if (!distro) return { rows, extensions: [] };
  }
  add({ id: 'docker', name: 'Docker', status: 'running', detail: 'Checking Docker and Podman…' });
  const runtime = await check('docker', ['info']) || await check('podman', ['info']);
  Object.assign(rows.at(-1), { status: runtime ? 'ok' : 'problem', detail: runtime ? 'A container runtime is running.' : 'No running Docker or Podman was found.' }); onRow([...rows]);
  let git = true;
  if (platform === 'darwin') {
    const location = await invoke('sh', ['-c', 'command -v git']);
    // Apple's git stub can open a system installer even for --version. Check CLT without prompting first.
    if (location.stdout.trim() === '/usr/bin/git') git = await check('xcode-select', ['-p']);
  }
  git = git && await check('git', ['--version']);
  add({ id: 'git', name: 'git', status: git ? 'ok' : 'problem', detail: git ? 'git is available.' : 'Required to install Piper.', action: platform === 'darwin' ? 'git-install' : null });
  const hasPython = await check(python, ['-c', 'import sys; sys.exit(sys.version_info < (3, 10))']);
  add({ id: 'python', name: 'Python', status: hasPython ? 'ok' : 'problem', detail: hasPython ? 'Bundled with the app (3.13). Your own Python is not needed.' : 'The bundled Python runtime is unavailable. Reinstall the desktop app.' });
  add({ id: 'hole', name: 'hole · the sandbox', status: await check('hole', ['--version']) ? 'ok' : 'install', detail: 'Missing components are installed into ~/.local/bin.' });
  add({ id: 'piper', name: 'Piper', status: cli ? 'ok' : 'install', detail: cli ? 'An existing Piper command was found.' : `Will be cloned into ${installDir}.` });
  add({ id: 'extensions', name: 'Extensions', status: 'running', detail: 'Looking for Piper extensions…' });
  const found = JSON.parse((await invoke(python, [extfind, '--json'])).stdout);
  if (!Array.isArray(found)) throw new Error('Invalid extension discovery result');
  Object.assign(rows.at(-1), { status: 'ok', detail: `Found ${found.length} extension${found.length === 1 ? '' : 's'}.` }); onRow([...rows]);
  return { rows, extensions: found };
}

async function install({ run, platform, distro, installer, env, extension, serviceEnv, onLine }) {
  const args = [installer, ...installArgs(env, extension)];
  return platform === 'win32' ? run(...wsl('sh', args, distro, serviceEnv), { onLine, timeout: 30 * 60 * 1000 })
    : run('sh', args, { onLine, timeout: 30 * 60 * 1000 });
}
module.exports = { Wizard, checkComputer, install };
