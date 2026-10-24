'use strict';
const path = require('node:path');
const { parseServiceURL } = require('./policy.cjs');
const { wsl } = require('./platform.cjs');

/** Locate a CLI without executing shell startup files or searching arbitrary PATH entries. */
async function locateCLI({ env, home, installDir, exists, platform, run, distro }) {
  const candidates = env.PIPER_DESKTOP_CLI ? [env.PIPER_DESKTOP_CLI] : [
    platform === 'win32' ? '$HOME/.local/bin/piper' : path.join(home, '.local/bin/piper'),
    platform === 'win32' ? `${installDir}/scripts/piper.sh` : path.join(installDir, 'scripts/piper.sh')
  ];
  for (let candidate of candidates) {
    if (platform === 'win32') {
      if (!distro) return null;
      if (candidate.startsWith('$HOME/')) {
        const r = await run('wsl.exe', ['-d', distro, '--exec', 'sh', '-c', 'printf "%s" "$HOME"']);
        candidate = r.stdout.trim() + candidate.slice(5);
      }
      const r = await run(...wsl('test', ['-f', candidate], distro), { acceptFailure: true });
      if (!r.code) return candidate;
    } else if (await exists(candidate)) return candidate;
  }
  return null;
}

function service({ port, run, fetch, platform, distro, serviceEnv = {}, locate, tokenFile, readFile, delay = ms => new Promise(r => setTimeout(r, ms)) }) {
  const origin = `http://127.0.0.1:${port}`;
  let cli, credentials;
  const command = async (args, opts) => {
    cli ||= await locate();
    if (!cli) throw new Error('Piper command was not found');
    return platform === 'win32' ? run(...wsl('sh', [cli, ...args], distro(), serviceEnv), opts) : run('sh', [cli, ...args], opts);
  };
  const probe = async () => {
    try {
      const r = await fetch(origin + '/api/state', { headers: credentials ? { Authorization: 'Bearer ' + credentials.token } : {}, signal: AbortSignal.timeout(3000), redirect: 'error' });
      // An authenticated piperd rejects an unauthenticated probe. Do not treat unrelated HTML as Piper.
      if (r.status === 401) { const body = await r.json(); return typeof body.error === 'string' && /token|auth|unauthor/i.test(body.error); }
      if (!r.ok) return false;
      const body = await r.json(); return Array.isArray(body.sessions);
    } catch { return false; }
  };
  const wait = async () => {
    for (let i = 0; i < 30; i++) { if (await probe()) return; await delay(1000); }
    throw new Error('Piper service did not answer within 30 s');
  };
  const url = async () => {
    cli ||= await locate();
    if (cli) credentials = parseServiceURL((await command(['open', '--print'])).stdout, port);
    else {
      // Attach even when only the service remains. Read-only: never create or rotate its token.
      let token;
      try { token = (await readFile(tokenFile, 'utf8')).trim(); } catch { throw new Error('The running Piper service needs its CLI to obtain the window URL'); }
      credentials = parseServiceURL(origin + '/#token=' + token, port);
    }
    return credentials.url;
  };
  return {
    probe, command, wait, url,
    async start() {
      if (await probe()) return { state: 'attach', url: await url() };
      cli = await locate();
      if (!cli) return { state: 'wizard' };
      await command(['ensure']); await wait();
      return { state: 'attach', url: await url() };
    },
    async ensure() { await command(['ensure']); await wait(); return url(); },
    async state() {
      if (!credentials) await url();
      const r = await fetch(origin + '/api/state', { headers: { Authorization: 'Bearer ' + credentials.token }, signal: AbortSignal.timeout(4000), redirect: 'error' });
      if (!r.ok) throw new Error('Piper is not responding');
      const body = await r.json();
      if (!Array.isArray(body.sessions)) throw new Error('Invalid Piper state');
      return body;
    }
  };
}
module.exports = { locateCLI, service };
