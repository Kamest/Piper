'use strict';
const { redact } = require('./policy.cjs');

/**
 * The tray's Machine menu (9.173): the app OPERATES the container runtime and the Piper service through their own
 * commands (docker / open / the piper CLI) and never owns them: it starts nothing at its own start, never stops
 * Docker, never installs, never uses sudo. Everything works while piperd is down.
 */

/** Ways to start a container runtime that is installed but stopped, tried in this order; the first that exits 0 wins. */
function runtimeStarters(platform) {
  if (platform === 'darwin') return [['docker', ['desktop', 'start']], ['open', ['-a', 'Docker']], ['open', ['-a', 'OrbStack']], ['podman', ['machine', 'start']]];
  if (platform === 'win32') return [['docker.exe', ['desktop', 'start']]];
  // Linux: Docker Desktop's CLI or its user unit, rootless Podman's socket - a system dockerd needs root: never here
  return [['docker', ['desktop', 'start']], ['systemctl', ['--user', 'start', 'docker-desktop']], ['podman', ['machine', 'start']]];
}

function machine({ platform, run, inLinux = (c, a) => [c, a], command, delay = ms => new Promise(r => setTimeout(r, ms)), now = () => Date.now() }) {
  /** 'docker' / 'podman' when its daemon answers (`info`, as the setup check does), '' when neither does. */
  async function runtime() {
    for (const name of ['docker', 'podman']) {
      try { if (!(await run(...inLinux(name, ['info']), { acceptFailure: true, timeout: 15000 })).code) return name; } catch { /* not installed */ }
    }
    return '';
  }
  return {
    runtime,
    /** 'running' when one already answers; else start one and wait until it answers (2 min). */
    async startRuntime({ wait = 120000 } = {}) {
      if (await runtime()) return 'running';
      let started = false;
      for (const [cmd, args] of runtimeStarters(platform)) {
        try { if (!(await run(cmd, args, { acceptFailure: true, timeout: 180000 })).code) { started = true; break; } } catch { /* not installed */ }
      }
      if (!started) throw new Error('No Docker Desktop, OrbStack or Podman machine could be started here. Start your container runtime yourself.');
      for (const end = now() + wait; now() < end;) { if (await runtime()) return 'started'; await delay(3000); }
      throw new Error('The container runtime was started but does not answer yet. Try again in a minute.');
    },
    /** `piper update`: the CLI's own first line ("Piper is up to date (…)" / "Piper updated a -> b"). */
    async update() {
      // onLine gets both streams, already redacted: the CLI's `piper: …` line is the reason of a refusal
      let reason = '', usage = false;
      const r = await command(['update'], { acceptFailure: true, timeout: 300000, onLine: l => {
        if (l.startsWith('piper: ')) reason = l.slice(7);
        else if (l.startsWith('piper.sh - ')) usage = true;
      } });
      if (r.code === 2 && usage && !reason)
        throw new Error('This Piper has no `piper update` yet (it came before 0.1.3). Update it the way it was installed, usually `git pull` in its folder; the next `piper` command restarts it.');
      if (r.code) throw new Error('Piper was not updated: ' + (reason || 'run `piper update` in a terminal to see why.'));
      return redact(r.stdout.trim().split('\n')[0] || '') || 'Piper is up to date';
    },
    /**
     * Text for a bug report or a colleague: versions, the service, the runtime, `piper status` and `piper doctor`.
     * Every line passes redact(): no token, password or URL credentials; never the GUI link itself.
     */
    async diagnostics({ app, os: osName, arch, online }) {
      const lines = [`Piper desktop ${app} on ${osName} ${arch}`, `Service: ${online ? 'answering' : 'not answering'}`];
      lines.push(`Container runtime: ${(await runtime()) || 'not running'}`);
      for (const [title, args] of [['piper status', ['status']], ['piper doctor', ['doctor']]]) {
        try {
          const r = await command(args, { acceptFailure: true, timeout: 60000 });
          lines.push('', `$ ${title} (exit ${r.code})`, ...r.stdout.trim().split('\n').slice(0, 200));
        } catch (e) { lines.push('', `$ ${title}`, String(e.message || e)); }
      }
      return lines.map(l => redact(l)).join('\n');
    }
  };
}

module.exports = { machine, runtimeStarters };
