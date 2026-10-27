/** Native Settings → Servers, implementing Claude's Pen boards SFh35 (R1–R8). */
import { ApiError, call } from './api.js';
import { fi, h } from './dom.js';
import { hosts, type Host } from './environment.js';
import { markBidi } from './text.js';

type Provider = { environment: string; remote_project: string; local_project: string; enabled: boolean };
type Probe = { service_installed: boolean; service_running?: boolean; systemd: boolean; linger: boolean; tmux: boolean; claude: boolean; codex: boolean; notice?: string; project_root_matches?: boolean; service_project_root?: string | null; requested_project_root?: string | null };
interface Deps { changed(): void; toast(text: string): void; modal(title: string, ...content: HTMLElement[]): void; closeModal(): void }
export function createServerSettings(deps: Deps) {
  const el = h('div', { class: 'server-settings' });
  let servers: Host[] = [], providers: Provider[] = [];
  let draft: Host | null = null, original: Host | null = null;
  let busy = false, loaded = false, result = '', failed = false, probe: Probe | null = null;
  let installDeferred = false;
  let installUnknown = false, installing = false, installStarted = 0;
  let probeFailed = false;
  let installRejected = '';
  const rootMismatch = () => draft?.mode === 'service' && !!probe?.service_running && probe.project_root_matches === false;
  const connectionResult = (server: Host) => rootMismatch() ? 'Connected over SSH · Piper service runs in another projects folder' : probe?.service_running && server.mode === 'service' ? 'Connected · Piper service' : server.mode === 'ssh' ? 'Connected over SSH · SSH only' : probe?.service_installed ? 'Connected over SSH · Piper service is not running' : 'Connected over SSH · Piper service is not installed yet';
  const checkedProbes = new Map<string, Probe>();
  let providerBusy = false, providerOpen = false, providerError = '', providerRemote = '', providerLocal = '';
  const dirty = () => !!draft && (!original || JSON.stringify(draft) !== JSON.stringify(original));
  const read = async () => {
    const r = await call<{ environments: Host[]; providers: Provider[] }>('GET', '/api/environments');
    servers = r.data?.environments ?? []; providers = r.data?.providers ?? []; loaded = true;
    el.dispatchEvent(new CustomEvent('servers-loaded', { bubbles: true }));
  };
  function choose(server: Host | null, copy?: Host): void {
    if (dirty()) return deps.modal('Discard server changes?', h('p', {}, 'The server has unsaved changes.'), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Keep editing'), h('button', { onclick: () => { draft = null; deps.closeModal(); choose(server, copy); } }, 'Discard changes')));
    original = server ? { ...server } : null;
    draft = server ? { ...server } : { id: 'server-' + crypto.randomUUID().slice(0, 12), label: copy?.label ?? '', host: copy?.host ?? '', project_path: copy?.project_path ?? '', mode: copy?.mode ?? 'service' };
    const status = server ? hosts.get(server.id) : undefined;
    probe = server ? checkedProbes.get(server.id) ?? null : null;
    result = server ? status?.online ? server.mode === 'ssh' ? 'Connected over SSH · SSH only' : 'Connected · Piper service' : status?.snapshot ? 'Server unavailable · last known state' : probe ? probe.service_running && server.mode === 'service' ? 'Connected · Piper service' : server.mode === 'ssh' ? 'Connected over SSH · SSH only' : probe?.service_installed ? 'Connected over SSH · Piper service is not running' : 'Connected over SSH · Piper service is not installed yet' : 'Connection not checked' : '';
    if (server && rootMismatch()) result = connectionResult(server);
    failed = probeFailed = false; installDeferred = installUnknown = false; installRejected = ''; providerOpen = false; providerError = providerRemote = providerLocal = ''; draw();
    el.querySelector<HTMLInputElement>('input')?.focus();
  }
  async function run(action: () => Promise<void>, message: string, installAction = false): Promise<void> {
    if (busy) return;
    busy = true; failed = probeFailed = false; installing = installAction; installUnknown = false; installRejected = '';
    if (installAction) installStarted = Date.now();
    result = message; draw();
    try { await action(); } catch (e) {
      failed = true; installUnknown = installAction && !(e instanceof ApiError && e.status === 400);
      if (installAction && !installUnknown) { installRejected = e instanceof Error ? e.message : 'Update refused'; result = draft ? connectionResult(draft) : installRejected; }
      else result = e instanceof Error ? e.message : 'Request failed';
    }
    finally { busy = installing = false; draw(); }
  }
  async function test(server: Host): Promise<void> {
    installDeferred = false;
    probe = (await call<Probe>('POST', `/api/environments/${server.id}/probe`, {}, undefined, 40000).catch(e => { probeFailed = true; throw e; })).data;
    if (probe) checkedProbes.set(server.id, probe);
    result = connectionResult(server);
    deps.changed();
  }
  function save(): void {
    if (!draft || busy || providerBusy) return;
    const snapshot = { ...draft };
    void run(async () => {
      const saved = (await call<Host>('POST', '/api/environments', snapshot)).data!;
      original = { ...saved }; draft = { ...saved };
      await read(); deps.changed(); await test(saved);
    }, 'Connecting…');
  }
  function mark(): void {
    const changed = dirty();
    el.querySelector<HTMLElement>('.st-savebar')!.hidden = !changed;
    const save = el.querySelector<HTMLButtonElement>('.st-save')!;
    save.disabled = busy || providerBusy || !draft?.label.trim() || !draft.host.trim() || !draft.project_path.startsWith('/');
    el.dispatchEvent(new CustomEvent('server-dirty', { bubbles: true, detail: changed }));
  }
  const button = (text: string, go: () => void, className = '') => h('button', { type: 'button', class: className, disabled: busy || providerBusy, onclick: go }, text);
  function load(): void {
    if (busy) return;
    busy = true; failed = false; result = ''; draw();
    void read().catch(e => { result = e instanceof Error ? e.message : 'Loading failed'; failed = true; })
      .finally(() => { busy = false; draw(); });
  }
  function draw(): void {
    queueMicrotask(refresh);
    el.replaceChildren(); el.setAttribute('aria-busy', String(busy || providerBusy));
    if (!draft) {
      if (!loaded) {
        if (failed) el.append(h('p', { class: 'bad', role: 'alert' }, markBidi(result)), button('Try again', load));
        else el.append(h('p', { class: 'mut' }, 'Loading servers…'));
        return;
      }
      if (result && failed) el.append(h('p', { class: 'bad', role: 'alert' }, markBidi(result)));
      const list = h('div', { class: servers.length ? 'server-list' : 'server-empty' });
      if (!servers.length) list.append(h('b', {}, 'No servers yet'), h('p', { class: 'mut small' }, 'Add a server you can reach with ssh from this computer. Piper can install a small service there so sessions keep their state when this computer disconnects, or work over plain SSH.'), h('button', { class: 'primary', onclick: () => choose(null) }, fi('plus'), 'Add server'));
      for (const server of servers) {
        const status = hosts.get(server.id);
        const running = status?.snapshot?.sessions.filter(s => !s.hidden && !['exited', 'error', 'blocked'].includes(s.state)).length ?? 0;
        const count = !status?.checkedAt && !status?.snapshot ? 'Not checked' : !status.online ? 'Server unavailable' : running ? `${running} ${running === 1 ? 'session' : 'sessions'}` : 'Connected';
        list.append(h('button', { type: 'button', class: 'server-row', disabled: busy || providerBusy, onclick: () => choose(server) },
          h('span', { class: 'server-dot' + (status?.online ? ' online' : '') }),
          h('span', { class: 'server-row-main' }, h('b', {}, server.label), h('span', { class: 'mut small mono', title: server.host + ' · ' + server.project_path }, server.host + ' · ' + server.project_path), h('span', { class: 'server-row-summary' }, (server.mode === 'ssh' ? 'SSH only' : 'Piper service') + ' · ' + count)),
          h('span', { class: 'mut small server-mode' }, server.mode === 'ssh' ? 'SSH only' : 'Piper service'),
          h('span', { class: 'mut small server-count' }, count), fi('chev-r')));
      }
      el.append(list);
      if (servers.length) el.append(h('button', { class: 'server-add', onclick: () => choose(null) }, fi('plus'), 'Add server'), h('p', { class: 'server-list-note mut small' }, 'Settings always belong to this computer. Each server keeps its own sessions; if one becomes unavailable, only its sessions are affected.'));
      return;
    }
    const target = draft;
    const running = hosts.get(target.id)?.snapshot?.sessions.filter(s => !s.hidden && !['exited', 'error', 'blocked'].includes(s.state)).length ?? 0;
    const back = () => {
      if (dirty()) return deps.modal('Discard server changes?', h('p', {}, 'The server has unsaved changes.'), h('div', { class: 'row' }, button('Keep editing', deps.closeModal), button('Discard changes', () => { draft = original = null; deps.closeModal(); draw(); })));
      draft = original = null; result = ''; draw();
    };
    el.append(h('div', { class: 'server-crumb' }, 'Sessions › Servers › ' + (original?.label || 'New server')), h('button', { class: 'link small server-back', onclick: back }, fi('chev-l'), 'Servers'), h('h3', { class: 'server-title' }, original?.label || 'Add server'));
    if (installUnknown) el.append(
      h('div', { class: 'server-status', role: 'status' }, h('span', { class: 'server-unknown-dot' }), h('span', { class: 'mut small' }, 'Install result unknown')),
      h('div', { class: 'server-install server-install-unknown' }, h('p', { class: 'mut small' }, "Piper couldn't confirm the install. It may have finished, failed or still be running. Check the status before installing again."), h('div', { class: 'server-install-actions' }, button('Check status', () => void run(() => test({ ...original! }), 'Connecting…'), 'primary'), h('details', {}, h('summary', {}, 'Details'), h('p', { class: 'mut small' }, markBidi(result))))));
    else if (probeFailed && failed) el.append(
      h('div', { class: 'server-status', role: 'alert' }, h('span', { class: 'server-error-dot' }), h('span', { class: 'mut small' }, "Can't connect over SSH")),
      h('div', { class: 'server-install server-ssh-error' }, h('p', { class: 'mono' }, markBidi(result)),
        /host key verification failed/i.test(result) ? h('p', { class: 'mut small' }, 'Connect once from a terminal on this computer to confirm the key, then try again:') : null,
        /host key verification failed/i.test(result) ? h('div', { class: 'server-ssh-command' }, h('code', {}, 'ssh ' + target.host), h('button', { class: 'ghost', 'aria-label': 'Copy SSH command', onclick: () => void navigator.clipboard.writeText('ssh ' + target.host).then(() => deps.toast('Copied')).catch(() => deps.toast('Could not copy')) }, fi('copy'))) : null,
        button('Try again', () => void run(() => test({ ...original! }), 'Connecting…'), 'primary')));
    else if (result) el.append(h('div', { class: 'server-status', role: failed || rootMismatch() ? 'alert' : 'status', 'aria-live': 'polite' }, busy ? h('span', { class: 'spinner' }) : h('span', { class: rootMismatch() ? 'server-error-dot' : 'server-dot' + (!failed && (probe || hosts.get(target.id)?.online) ? ' online' : '') }),
      h('span', { class: rootMismatch() ? 'small' : failed ? 'bad' : 'mut small' }, markBidi(result)), original && !busy ? button('Test again', () => void run(() => test({ ...original! }), 'Connecting…'), 'small') : null));
    if (installing) el.append(h('div', { class: 'server-install-progress', role: 'progressbar', 'aria-label': 'Installing Piper service' }), h('p', { class: 'mut small server-install-time' }, `Started ${new Date(installStarted).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}. This can take up to 2 minutes. You can leave this page; Piper finishes the install and checks the service.`));
    if (probe && target.mode === 'service' && (!probe.service_running || rootMismatch()) && !installDeferred && !installUnknown && !installing) el.append(h('div', { class: 'server-install' }, h('b', {}, markBidi(rootMismatch() ? `Piper service on ${target.label} uses another projects folder` : probe.service_installed ? 'Start or update Piper service' : `Install Piper service on ${target.label}?`)),
      rootMismatch() ? h('div', { class: 'server-root-paths small mono' }, h('div', {}, h('span', { class: 'mut' }, 'Expected'), h('span', {}, markBidi(probe.requested_project_root ?? target.project_path))), h('div', {}, h('span', { class: 'mut' }, 'Running'), h('span', {}, markBidi(probe.service_project_root ?? 'unknown')))) : null,
      h('p', { class: 'mut small' }, markBidi(rootMismatch() ? `Sessions on ${target.label} stay blocked until the folders match. Piper never changes the service on its own.` : 'It goes into your account on the server (no sudo, no open ports) and starts with your user. Installing can take up to 2 minutes; Piper checks the service when it finishes.')),
      rootMismatch() ? h('p', { class: 'mut small' }, markBidi(`Update service restarts this account's shared Piper service on ${target.host} in ${probe.requested_project_root ?? target.project_path}. Other servers added for ${probe.service_project_root ?? 'the previous folder'} will then show this warning and need updating.`)) : null,
      probe.systemd ? h('div', { class: 'server-install-actions' }, button(probe.service_installed ? 'Update service' : 'Install Piper service', () => void run(async () => {
        try { await call('POST', `/api/environments/${target.id}/install`, {}, undefined, 125000); }
        catch (e) { if (e instanceof ApiError && e.status === 400) throw e; throw new Error('Installation outcome is not confirmed. Test the connection before repeating the installation.'); }
        installing = false; result = `Installed · checking the service on ${target.label}…`; draw();
        await test({ ...target });
      }, 'Installing Piper service…', true), 'primary'), button('Not now', () => { installDeferred = true; draw(); }, 'ghost')) : h('p', { class: 'mut small' }, 'Automatic installation needs Linux with a working systemd user service. Add an SSH-only connection instead.'),
      installRejected ? h('div', { class: 'server-install-error', role: 'alert' }, h('span', { class: 'server-error-dot' }), h('p', { class: 'small' }, markBidi(installRejected))) : null,
      h('div', { class: 'server-install-alternative' }, h('span', { class: 'mut small' }, rootMismatch() ? 'Prefer to leave the service as it is?' : 'Prefer nothing installed?'), button('Add this host as an SSH-only server…', () => choose(null, { ...target, mode: 'ssh' }), 'link small'))));
    el.append(h('h3', { class: 'server-group' }, 'Connection'));
    const field = (key: 'label' | 'host' | 'project_path', title: string, help: string, placeholder: string) => {
      const fixed = !!original && key !== 'label';
      const input = h('input', { id: 'server-' + key, value: target[key], placeholder, readonly: fixed, disabled: busy, autocomplete: 'off', spellcheck: 'false', class: 'st-wide' + (key !== 'label' ? ' mono' : '') }) as HTMLInputElement;
      input.addEventListener('input', () => { target[key] = input.value; mark(); });
      return h('div', { class: 'st-field server-field' }, h('div', {}, h('label', { for: input.id }, title), h('div', { class: 'mut small' }, fixed ? running ? `Fixed — ${running} ${running === 1 ? 'session runs' : 'sessions run'} here.` : 'Fixed for this server.' : original ? 'Shown in New session and in the queue.' : help)), h('div', { class: fixed ? 'server-locked' : '' }, fixed ? fi('lock') : null, input));
    };
    el.append(field('label', 'Name', 'Shown in New session and next to its sessions in the queue. You can rename it any time.', 'Build box'), field('host', 'SSH host', 'user@host or an alias from your ~/.ssh/config. Piper connects the same way ssh does from this computer.', 'dev@build-01'), field('project_path', 'Projects folder', 'Piper lists the Git checkouts inside it.', '/home/dev/projects'));
    if (!original) el.append(h('h3', { class: 'server-group' }, 'How Piper runs there'));
    const choices = h('div', { class: 'sc-levels server-levels', role: 'radiogroup', 'aria-label': 'Server connection mode' });
    for (const [mode, title, text] of [['service', 'Piper service', 'Piper installs a small service in your account on the server (no sudo, no open ports). Sessions keep running there when this computer sleeps or disconnects — until one needs an approval or a sign-in, or the server stops. Hole sandbox or direct.'], ['ssh', 'SSH only', 'Nothing is installed. Sessions run directly in your account, without the Hole sandbox; Piper follows them over SSH while this computer is connected.']] as const) {
      choices.append(h('button', { type: 'button', class: 'sc-level' + (target.mode === mode ? ' on' : ''), role: 'radio', 'aria-checked': String(target.mode === mode), disabled: busy || !!original, onclick: () => { target.mode = mode; draw(); } }, h('span', { class: 'sc-radio' }), h('span', { class: 'sc-level-body' }, h('span', { class: 'server-mode-title' }, h('b', {}, title), mode === 'service' ? h('span', { class: 'server-recommended' }, 'Recommended') : null), h('span', { class: 'mut small' }, text))));
    }
    if (original) el.append(h('div', { class: 'st-field server-field' }, h('div', {}, h('label', {}, 'Runs there'), h('div', { class: 'mut small' }, target.mode === 'ssh' ? 'Direct sessions over SSH.' : 'Hole sandbox or direct, chosen per session.')), h('div', { class: 'server-locked' }, fi('lock'), h('span', {}, markBidi(target.mode === 'ssh' ? 'SSH only' : rootMismatch() ? `Piper service · runs in ${probe?.service_project_root ?? 'unknown'}` : 'Piper service')))));
    else el.append(choices);
    el.append(h('p', { class: 'server-fixed mut small' }, original ? 'To use another host, folder or SSH only, add another server. This one keeps its sessions.' : 'Host, folder and this choice are fixed once the server is added. To change them later, add another server.'));
    if (probe) el.append(h('h3', { class: 'server-group' }, 'On the server'), h('div', { class: 'server-capabilities small' }, ...(['claude', 'codex', 'tmux'] as const).map(key => h('span', { 'data-agent-capability': key, class: probe![key] ? '' : 'mut' }, fi(probe![key] ? 'check' : 'minus'), key === 'claude' ? 'Claude' : key === 'codex' ? 'Codex' : key)), target.mode === 'service' ? h('span', { class: probe.linger ? '' : 'mut' }, fi(probe.linger ? 'check' : 'minus'), probe.linger ? 'Keeps running after logout' : 'Logout persistence not enabled') : null));
    const details = h('details', { class: 'server-more' }, h('summary', {}, 'Technical details', h('span', { class: 'mut small' }, original ? 'service and version' : 'SSH port, key and jump host come from ~/.ssh/config')), h('p', { class: 'mut small' }, 'SSH port, key and jump hosts come from ~/.ssh/config. Piper requires a verified host key and Python 3.10+. The service uses ~/.local/share/piper-server, ~/.piper-server, systemd --user piper-server.service and loopback port 17896. SSH-only requires tmux.'));
    el.append(details);
    if (!original) el.append(h('details', { class: 'server-more' }, h('summary', {}, "This computer's HTTP & database tools", h('span', { class: 'mut small' }, 'optional · off')), h('p', { class: 'mut small' }, 'Add and connect the server first to map its projects to this computer.')));
    if (original?.mode === 'service') {
      const mappings = providers.filter(p => p.environment === target.id && p.enabled);
      const remote = h('input', { value: providerRemote, placeholder: 'group/repo', 'aria-label': 'Project on ' + target.label, class: 'mono', disabled: busy || providerBusy }) as HTMLInputElement;
      const local = h('input', { value: providerLocal, placeholder: 'group/repo', 'aria-label': 'Project on this computer', class: 'mono', disabled: busy || providerBusy }) as HTMLInputElement;
      const update = async (mapping: { remote_project: string; local_project: string; enabled: boolean }) => {
        if (busy || providerBusy) return;
        providerBusy = true; providerError = ''; draw();
        try {
          await call('POST', '/api/environments/provider', { environment: target.id, ...mapping });
          await read(); providerRemote = providerLocal = ''; deps.changed();
        } catch (e) {
          providerError = e instanceof Error ? e.message : 'Could not update this mapping';
          // The controller rotates/revokes the local grant before contacting the server.
          // Read its authoritative state even if remote delivery failed; never replay the mutation.
          try { await read(); deps.changed(); } catch { /* keep the error and last known mappings */ }
        }
        finally { providerBusy = false; draw(); }
      };
      const add = button('Add mapping', () => void update({ remote_project: remote.value.trim(), local_project: local.value.trim(), enabled: true }), 'small server-provider-add');
      const inputs = () => {
        providerRemote = remote.value; providerLocal = local.value;
        add.disabled = busy || providerBusy || !providerRemote.trim() || !providerLocal.trim();
      };
      remote.addEventListener('input', inputs); local.addEventListener('input', inputs); inputs();
      const list = h('div', { class: 'server-provider-list' });
      if (mappings.length) list.append(h('div', { class: 'server-provider-head' }, h('span', {}, 'On ' + target.label), h('span', {}, 'On this computer')));
      else list.append(h('p', { class: 'mut small' }, 'No projects mapped yet.'));
      for (const p of mappings) list.append(h('div', { class: 'server-provider' }, h('span', { class: 'mono' }, p.remote_project), h('span', { class: 'mut', 'aria-hidden': 'true' }, '→'), h('span', { class: 'mono' }, p.local_project), button('Revoke', () => void update({ ...p, enabled: false }), 'small')));
      list.append(h('div', { class: 'server-provider server-provider-new' }, h('label', {}, h('span', { class: 'small mut server-provider-label' }, 'Project on ' + target.label), remote), h('span', { class: 'mut server-provider-arrow', 'aria-hidden': 'true' }, '→'), h('label', {}, h('span', { class: 'small mut server-provider-label' }, 'Project on this computer'), local), add));
      const fold = h('details', { class: 'server-more server-providers', open: providerOpen }, h('summary', {}, "This computer's HTTP & database tools", h('span', { class: 'mut small' }, mappings.length ? `optional · ${mappings.length} projects mapped` : 'optional · off')),
        h('p', { class: 'mut small' }, `Map one project on ${target.label} to one project on this computer. Sessions in that server project get all approved HTTP requests and databases of the local project. Credentials stay on this computer and your approval rules still apply. Applies to new sessions; unavailable while this computer is disconnected.`), list,
        providerError ? h('p', { class: 'bad small', role: 'alert' }, markBidi(providerError)) : null);
      fold.addEventListener('toggle', () => { providerOpen = fold.open; });
      el.append(fold);
    }
    if (original) {
      const remove = button('Remove server…', () => deps.modal('Remove server connection?', h('p', {}, 'Removes this connection from Piper. Agents, processes and files on the server are kept. Notebook tool mappings are revoked.'), h('div', { class: 'row' }, button('Cancel', deps.closeModal), button('Remove connection', () => { deps.closeModal(); void run(async () => { await call('POST', `/api/environments/${target.id}/remove`, {}); draft = original = null; await read(); deps.changed(); result = ''; }, 'Removing connection…'); }, 'danger'))), 'link small server-remove') as HTMLButtonElement;
      remove.disabled = busy || providerBusy || running > 0;
      el.append(h('div', { class: 'server-remove-row' }, remove, running ? h('span', { class: 'mut small' }, `Stop or end its ${running} ${running === 1 ? 'session' : 'sessions'} first.`) : null));
    }
    const footer = h('div', { class: 'st-savebar' }, h('span', { class: 'st-sdot' }), h('span', { class: 'st-scount grow' }, original ? 'Unsaved changes' : 'New server'), button(original ? 'Discard' : 'Cancel', () => { draft = original ? { ...original } : null; if (!draft) original = null; draw(); }, 'ghost st-undo'), h('button', { class: 'primary st-save', disabled: busy || providerBusy, onclick: save }, original ? 'Save' : 'Add and connect', h('kbd', {}, '⌘S')));
    el.append(footer); mark();
  }
  function refresh(): void {
    const status = original ? hosts.get(original.id) : undefined;
    const available = status?.online ? status.snapshot?.agent_availability : undefined;
    if (!available) return;
    for (const key of ['claude', 'codex'] as const) {
      const cap = el.querySelector<HTMLElement>(`[data-agent-capability="${key}"]`);
      if (cap) { cap.classList.toggle('mut', !available[key]); cap.replaceChildren(fi(available[key] ? 'check' : 'minus'), key === 'claude' ? 'Claude' : 'Codex'); }
    }
  }
  return { el, dirty, refresh, count: () => servers.length, show() { if (!loaded) load(); else draw(); }, discard() { draft = original ? { ...original } : null; }, save };
}
