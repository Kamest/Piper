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

/**
 * Extensions to offer before the service starts, mirroring the core's first-start offer: only while nothing is
 * trusted, only registered, added or checkout-shipped ones that are compatible and not declined. The list's own
 * `declined` field decides; `declined` (the file's map) only serves a core that does not report the field. A list
 * from an older core (no `origin_changed` field) never offers: that core cannot record a "Not now".
 */
function offerable(found, declined = {}) {
  if (!Array.isArray(found) || !found.length || !found.every(e => e && typeof e.path === 'string')) return [];
  if (found.some(e => e.trusted) || !found.some(e => 'origin_changed' in e)) return [];
  const no = e => typeof e.declined === 'boolean' ? e.declined : Object.hasOwn(declined, e.path);
  return found.filter(e => e.compatible && !e.trusted && ['registered', 'added', 'checkout'].includes(e.source) && !no(e));
}

/** One state per row of the Extensions window: piperd's API names it, a CLI list is derived. */
function extensionState(e) {
  if (typeof e.state === 'string') return e.state;
  if (!e.compatible) return 'incompatible';
  if (e.trusted && e.origin_changed) return 'origin-changed';
  return e.trusted ? 'loaded' : 'found';
}

function service({ port, run, fetch, platform, distro, serviceEnv = {}, locate, tokenFile, declinedFile, readFile, delay = ms => new Promise(r => setTimeout(r, ms)), now = () => performance.now() }) {
  const origin = `http://127.0.0.1:${port}`;
  let cli, credentials;
  const command = async (args, opts) => {
    cli ||= await locate();
    if (!cli) throw new Error('Piper command was not found');
    return platform === 'win32' ? run(...wsl('sh', [cli, ...args], distro(), serviceEnv), opts) : run('sh', [cli, ...args], opts);
  };
  const probe = async (timeout = 3000) => {
    try {
      const r = await fetch(origin + '/api/state', { headers: credentials ? { Authorization: 'Bearer ' + credentials.token } : {}, signal: AbortSignal.timeout(Math.max(1, Math.ceil(timeout))), redirect: 'error' });
      // An authenticated piperd rejects an unauthenticated probe. Do not treat unrelated HTML as Piper.
      if (r.status === 401) { const body = await r.json(); return typeof body.error === 'string' && /token|auth|unauthor/i.test(body.error); }
      if (!r.ok) return false;
      const body = await r.json(); return Array.isArray(body.sessions);
    } catch { return false; }
  };
  const wait = async () => {
    const deadline = now() + 30000;
    for (let i = 0; i < 30 && now() < deadline; i++) {
      if (await probe(Math.min(3000, deadline - now()))) return;
      const remaining = deadline - now();
      if (remaining <= 0) break;
      await delay(Math.min(1000, remaining));
    }
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
  const request = async (method, route, body) => {
    if (!credentials) await url();
    return fetch(origin + route, { method, headers: { Authorization: 'Bearer ' + credentials.token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(15000), redirect: 'error' });
  };
  const failure = async r => { let text = ''; try { text = (await r.json()).error || ''; } catch { /* not JSON */ } return new Error(text || `Piper answered ${r.status}`); };
  const ensure = async ({ decline = [] } = {}) => {
    // A "Not now" is recorded by the CLI before the service starts. A core without that command rejects it as a
    // usage error (exit 2): only then is it recorded through the started service. Another failure: asked again.
    const later = [];
    for (const p of decline) if ((await command(['extensions', 'decline', p], { acceptFailure: true })).code === 2) later.push(p);
    await command(['ensure']); await wait();
    const address = await url();
    for (const p of later) { try { await request('POST', '/api/extensions/decline', { path: p }); } catch { /* an older service: asked again next time */ } }
    return address;
  };
  const listCLI = async () => {
    const found = JSON.parse((await command(['extensions', '--json'])).stdout);
    if (!Array.isArray(found)) throw new Error('Invalid extension list');
    return found;
  };
  /** Fallback for a core whose list has no `declined` field: its file, read directly. */
  const declined = async () => {
    if (!declinedFile) return {};
    try { const data = JSON.parse(await readFile(declinedFile, 'utf8')); return data && typeof data === 'object' && !Array.isArray(data) ? data : {}; }
    catch { return {}; }
  };
  return {
    probe, command, wait, url, ensure, listCLI,
    async start() {
      if (await probe()) return { state: 'attach', url: await url() };
      cli = await locate();
      if (!cli) return { state: 'wizard' };
      // Without a terminal the CLI never asks; ask here before the service starts without the extension.
      let offer = [];
      try { const found = await listCLI(); offer = offerable(found, found.some(e => e && 'declined' in e) ? {} : await declined()); } catch { offer = []; }
      if (offer.length) return { state: 'offer', extensions: offer };
      return { state: 'attach', url: await ensure() };
    },
    /** piperd's own list when it is up and has the extensions API; null means: use the CLI. */
    async extensionsAPI() {
      if (!await probe()) return null;
      try {
        const r = await request('GET', '/api/extensions');
        if (!r.ok) return null;
        const body = await r.json();
        return Array.isArray(body?.extensions) ? body : null;
      } catch { return null; }
    },
    async changeExtension(action, extensionPath) {
      if (!['trust', 'untrust', 'decline'].includes(action)) throw new Error('Unknown extension action');
      const r = await request('POST', '/api/extensions/' + action, { path: extensionPath });
      if (!r.ok) throw await failure(r);
      return r.json();
    },
    /** piperd re-executes itself (sessions survive): wait until the old socket is gone, then for the new one. */
    async restart() {
      const r = await request('POST', '/api/restart', {});
      if (!r.ok) throw await failure(r);
      const deadline = now() + 10000;
      for (let i = 0; i < 40 && now() < deadline; i++) {
        if (!await probe(Math.min(3000, deadline - now()))) break;
        const remaining = deadline - now();
        if (remaining <= 0) break;
        await delay(Math.min(250, remaining));
      }
      await wait();
    },
    /** Answer a confirmation card (tray, notification button): only Allow or Deny, never "always". */
    async decide(id, approve) {
      if (!/^[\w.:-]{1,128}$/.test(String(id)) || typeof approve !== 'boolean') throw new Error('Invalid card');
      const r = await request('POST', '/api/pending/' + encodeURIComponent(id), { approve });
      if (!r.ok) throw await failure(r);
    },
    /** Type a reply into a session waiting at its prompt (the notification's Reply), as the GUI's composer does. */
    async send(session, text) {
      if (!/^[\w.:-]{1,128}$/.test(String(session)) || typeof text !== 'string' || !text.trim() || text.length > 20000) throw new Error('Invalid reply');
      const r = await request('POST', '/api/sessions/' + encodeURIComponent(session) + '/send', { text });
      if (!r.ok) throw await failure(r);
    },
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
module.exports = { locateCLI, service, offerable, extensionState };
