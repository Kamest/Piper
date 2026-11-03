/** Settings › Extensions (9.168): the list GET /api/extensions gives (9.167), the Load / Turn off dialogs,
 *  requests from a phone or a remote browser, the first-start card and the "Restarting Piper…" toast.
 *  Loading or turning one off writes the trust file only; the GUI then POSTs /api/restart once and waits
 *  for piperd to answer again (the poll in main.ts calls restartAnswered / restartFailed). */
import { ApiError, call } from './api.js';
import { clear, fi, h } from './dom.js';
import { markBidi } from './text.js';

export type ExtState = 'loaded' | 'found' | 'origin-changed' | 'incompatible' | 'failed' | 'declined';
export interface Extension {
  name: string; label: string; description: string; path: string; remote: string; commit: string;
  source: string; api: number; compatible: boolean; trusted: boolean; state: ExtState; error: string;
  adds: string[]; trusted_remote: string; restart_needed: boolean;
}
export interface ExtPending { id: string; kind: 'trust' | 'add'; path: string; label: string; remote: string; commit: string; from: string; at: number; mine: boolean }
export interface ExtListing {
  extensions: Extension[]; pending: ExtPending[]; offer: string[]; core_api: number; apis: number[];
  source: string; restart_needed: boolean; caller: 'local' | 'remote' | 'phone'; can_approve: boolean;
}
export interface ExtDeps { toast(text: string): void; modal(title: string, ...content: (Node | null)[]): void; closeModal(): void }
type Tone = 'ok' | 'info' | 'warn' | 'bad';
export interface RowView { word: string; tone: Tone; hint: string; line: string; note: string; noteTone: '' | 'warn';
  button: { label: string; act: 'trust' | 'untrust' | 'restart'; title: string } | null }

const SOURCE: Record<string, string> = {
  registered: 'Registered by its checkout', added: 'Added in Settings', scan: 'Found next to your projects',
  checkout: 'Ships inside Piper’s checkout', env: 'Named by PIPER_EXTENSIONS',
};
const WORD: Record<ExtState, [string, Tone, string]> = {
  loaded: ['Loaded', 'ok', 'Its code runs in Piper on this computer.'],
  found: ['Found', 'info', 'Piper sees it but runs none of its code until you load it.'],
  declined: ['Found', 'info', 'You chose Not now on the first-start card. It stays off until you load it.'],
  'origin-changed': ['Origin changed', 'warn', 'Its git remote is no longer the one you trusted, so Piper does not load it.'],
  incompatible: ['Incompatible', 'bad', 'Written for an extension API this Piper does not have, so Piper cannot load it.'],
  failed: ['Failed to load', 'bad', 'Piper tried to load it and its code raised an error. Piper runs without it.'],
};

/** ~ for the home directory (the GUI does not know $HOME: the usual macOS / Linux prefixes) */
export function shortPath(p: string): string {
  return p.replace(/^\/(?:Users|home)\/[^/]+(?=\/|$)/, '~');
}
/** github.com/acme/tools from https://github.com/acme/tools.git or git@github.com:acme/tools.git */
export function shortRemote(r: string): string {
  return r.replace(/^[a-z+]+:\/\/(?:[^@/]+@)?/i, '').replace(/^[^@/]+@([^:/]+):/, '$1/').replace(/\.git$/, '');
}
const shortCommit = (c: string) => c.slice(0, 7);

/** the dim line under a row: folder · origin · commit (origin changed: "was X, now Y") */
export function metaLine(e: Pick<Extension, 'path' | 'remote' | 'commit' | 'state' | 'trusted_remote'>): string {
  const origin = e.state === 'origin-changed'
    ? `was ${shortRemote(e.trusted_remote)}, now ${shortRemote(e.remote) || 'no git remote'}` : shortRemote(e.remote);
  return [shortPath(e.path), origin, shortCommit(e.commit)].filter(Boolean).join(' · ');
}

/** what a row says and its one button, from the extension's state */
export function rowView(e: Extension, l: Pick<ExtListing, 'core_api' | 'apis' | 'source'>): RowView {
  const [word, tone, hint] = WORD[e.state] ?? WORD.found;
  const base = { word, tone, hint, line: '', note: '', noteTone: '' as const, button: null };
  const restart = { label: 'Restart Piper', act: 'restart' as const, title: 'Piper restarts to apply it. Sessions keep running.' };
  const origin = e.description || SOURCE[e.source] || 'Found on this computer';
  switch (e.state) {
    case 'loaded':
      if (e.source === 'env' || l.source === 'env') return { ...base, line: e.adds.length ? 'Adds: ' + e.adds.join(', ') : e.description || 'Loaded',
        note: 'Piper was started with PIPER_EXTENSIONS, which decides what loads. Change it there.' };
      if (!e.trusted) return { ...base, line: e.adds.length ? 'Adds: ' + e.adds.join(', ') : e.description || 'Loaded',
        note: 'Turned off. It stops running when Piper restarts.', button: restart };
      return { ...base, line: e.adds.length ? 'Adds: ' + e.adds.join(', ') : e.description || 'Loaded',
        button: { label: 'Turn off…', act: 'untrust', title: 'Stop loading its code. Piper restarts; sessions keep running.' } };
    case 'origin-changed':
      return { ...base, line: 'Not loaded: its git remote is no longer the one you trusted', note: 'Load it again only if you trust the new origin.', noteTone: 'warn',
        button: { label: 'Review…', act: 'trust', title: 'See where its code comes from now, then load it again or leave it off.' } };
    case 'incompatible': {
      const need = l.apis.length > 1 ? `API ${l.apis.join(' or ')}` : `API ${l.core_api}`;
      return { ...base, line: `Extension API ${e.api} · this Piper needs ${need}`, note: 'Update the extension checkout.' };
    }
    case 'failed':
      return { ...base, line: e.error || 'Its code raised an error while loading', note: 'Piper runs without it. Fix it, then restart Piper.',
        button: { ...restart, title: 'Load it again after you fixed it. Sessions keep running.' } };
    default:
      if (e.trusted && e.restart_needed) return { ...base, line: origin + ' · not loaded yet', note: 'Turned on. It loads when Piper restarts.', button: restart };
      return { ...base, line: origin + ' · not loaded',
        button: { label: 'Load…', act: 'trust', title: 'Run its code in Piper on this computer. You confirm where it comes from first.' } };
  }
}

/** who asks, in words: "Your phone" (the mobile bridge) or "A remote browser" (tailscale serve) */
export function pendingTitle(p: Pick<ExtPending, 'from' | 'kind' | 'label'>): string {
  const who = p.from.startsWith('phone') ? 'Your phone' : 'A remote browser';
  return `${who} asks to ${p.kind === 'add' ? 'add' : 'load'} ${p.label}`;
}
/** device · time · folder · origin · commit */
export function pendingMeta(p: ExtPending, now = new Date()): string {
  const device = /\(([^)]+)\)/.exec(p.from)?.[1] ?? (p.from.startsWith('phone') ? 'Phone' : 'Remote browser');
  const at = new Date(p.at * 1000);
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  const day = at.toDateString() === now.toDateString() ? time : at.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + time;
  return [device, day, shortPath(p.path), shortRemote(p.remote), shortCommit(p.commit)].filter(Boolean).join(' · ');
}
/** the Settings nav's "N to review": requests to approve here and first-start offers */
export function reviewCount(l: ExtListing | null): number {
  if (!l || !l.can_approve) return 0;
  return l.pending.length + l.offer.length;
}
/** the first-start card's extension: offered (never trusted, declined or incompatible), this computer only */
export function offered(l: ExtListing | null): Extension | null {
  if (!l || !l.can_approve || !l.offer.length) return null;
  return l.extensions.find((e) => e.path === l.offer[0]) ?? null;
}

// --- the listing, shared by Settings, the first-start card and the nav badge ---
let listing: ExtListing | null = null;
let inflight: Promise<ExtListing | null> | null = null;
let queued: Promise<ExtListing | null> | null = null;
export const extensionsNow = (): ExtListing | null => listing;
function publish(l: ExtListing | null): void {
  listing = l;
  if (typeof document !== 'undefined' && typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('piper-extensions'));
}
/** GET /api/extensions (discover() reads manifests and git: never polled, asked on show / after a change) */
export function loadExtensions(): Promise<ExtListing | null> {
  if (inflight) {   // one asked before a change may answer with the old list: one more after it
    queued ??= inflight.catch(() => null).then(() => { queued = null; return loadExtensions(); });
    return queued;
  }
  inflight = call<ExtListing>('GET', '/api/extensions', undefined, undefined, 15000)
    .then((r) => { publish(r.data); return r.data; }, (e) => { if (e instanceof ApiError && e.status === 403) publish(null); throw e; })
    .finally(() => { inflight = null; });
  return inflight;
}
const post = async <T>(action: string, body: unknown = {}) => (await call<T>('POST', action.startsWith('/') ? action : '/api/extensions/' + action, body, undefined, 15000)).data!;
const errText = (e: unknown) => markBidi(e instanceof Error ? e.message : String(e));

// --- the restart (C): a persistent toast until piperd answers again; the poll reports up / down ---
let restartAt = 0;
let sawDown = false;
let restartEl: HTMLElement | null = null;
/** a restart was asked for and piperd has not answered since (the poll shows no "not responding" banner) */
export function restarting(now = Date.now()): boolean {
  if (restartAt && now - restartAt > 90_000) { restartAt = 0; restartEl?.remove(); restartEl = null; }
  return !!restartAt;
}
export function restartFailed(): void { if (restartAt) sawDown = true; }
/** a poll got an answer: true once, when it is the restarted piperd (it was down, or long enough passed) */
export function restartAnswered(now = Date.now()): boolean {
  if (!restarting(now) || (!sawDown && now - restartAt < 6000)) return false;
  restartAt = 0;
  sawDown = false;
  restartEl?.remove();
  restartEl = null;
  void loadExtensions().catch(() => {});
  return true;
}
export function expectRestart(detail: string): void {
  restartAt = Date.now();
  sawDown = false;
  if (typeof document === 'undefined' || !document.body) return;
  let box = document.getElementById('toasts');
  if (!box) { box = h('div', { id: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(box); }
  restartEl?.remove();
  restartEl = h('div', { class: 'toast ext-restart', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
    h('div', {}, h('strong', {}, 'Restarting Piper…'), h('p', { class: 'mut small' }, detail)));
  box.append(restartEl);
}
/** POST /api/restart, then wait (the poll ends the toast) */
export async function restartPiper(deps: Pick<ExtDeps, 'toast'>, detail = 'Sessions keep running; this window reconnects by itself.'): Promise<void> {
  try {
    await post('/api/restart');
    expectRestart(detail);
  } catch (e) {
    deps.toast('Piper did not restart: ' + errText(e));
  }
}

// --- 9.171: piperd runs older Python than its checkout (a `git pull`): one persistent notice with Restart ---
/** piperd says its code is outdated, or is from before 9.171 and says nothing about it */
export function needsRestart(s: { code_outdated?: boolean } | null | undefined): boolean {
  return !!s && s.code_outdated !== false;
}
export const UPDATE_TEXT = 'Piper was updated. Restart Piper to finish - sessions keep running.';
export const UPDATE_COMMAND = 'Piper was updated. This Piper cannot restart itself - run piper restart in a terminal; sessions keep running.';
let updateEl: HTMLElement | null = null;
let updateManual = false;   // the running piperd has no POST /api/restart (404): the command instead
/** after each answered poll: show, keep or remove the notice (never during a restart the GUI asked for) */
export function updateNotice(s: { code_outdated?: boolean } | null | undefined, deps: Pick<ExtDeps, 'toast'>): HTMLElement | null {
  if (!needsRestart(s) || restarting()) {
    updateEl?.remove();
    updateEl = null;
    if (s?.code_outdated === false) updateManual = false;
    return null;
  }
  if (updateEl?.isConnected) return updateEl;
  if (typeof document === 'undefined' || !document.body) return null;
  const restart = h('button', { onclick: () => void go() }, 'Restart');
  const text = h('span', {}, updateManual ? UPDATE_COMMAND : UPDATE_TEXT);
  const el = h('div', { id: 'update-banner', class: 'ibx-secnote', role: 'status' }, text, updateManual ? null : restart);
  async function go(): Promise<void> {
    restart.disabled = true;
    try {
      await post('/api/restart');
      el.remove();
      if (updateEl === el) updateEl = null;
      expectRestart('Finishing the update. Sessions keep running; this window reconnects by itself.');
    } catch (e) {
      restart.disabled = false;
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
        updateManual = true;
        text.textContent = markBidi(UPDATE_COMMAND);
        restart.remove();
      } else deps.toast('Piper did not restart: ' + errText(e));
    }
  }
  document.body.prepend(el);
  updateEl = el;
  return el;
}

function kv(rows: [string, string | Node][]): HTMLElement {
  return h('dl', { class: 'ext-kv' }, ...rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
}

/** B: "Load X?" - folder, origin, commit, what it is; a pending request is approved, a remote caller asks */
export function loadDialog(deps: ExtDeps, e: Pick<Extension, 'label' | 'path' | 'remote' | 'commit' | 'description' | 'adds' | 'state' | 'trusted_remote'>,
  opts: { request?: ExtPending; caller?: ExtListing['caller']; done?: () => void } = {}): void {
  const local = !opts.caller || opts.caller === 'local';
  const add = opts.request?.kind === 'add';
  const msg = h('p', { class: 'bad small', role: 'alert', hidden: true });
  const from: string | Node = e.state === 'origin-changed'
    ? h('span', {}, shortRemote(e.remote) || 'no git remote', h('span', { class: 'warn' }, ` · was ${shortRemote(e.trusted_remote)}`))
    : shortRemote(e.remote) || 'no git remote (a local folder)';
  const primary = h('button', { class: 'primary', onclick: () => void go() },
    add ? 'Add folder' : local ? 'Load and restart Piper' : 'Ask on the computer');
  const cancel = h('button', { onclick: deps.closeModal }, 'Cancel');
  async function go(): Promise<void> {
    primary.disabled = cancel.disabled = true;
    msg.hidden = true;
    try {
      const r = opts.request ? await post<{ restart?: boolean }>('approve', { id: opts.request.id })
        : await post<{ restart?: boolean; pending?: ExtPending }>('trust', { path: e.path });
      deps.closeModal();
      if (r && 'pending' in r && r.pending) deps.toast(`Asked your computer to load ${e.label}. Approve it there in Settings › Extensions.`);
      else if (r?.restart) await restartPiper(deps, `Loading ${e.label}. Sessions keep running; this window reconnects by itself.`);
      else deps.toast(`${e.label} added - load it from the list`);
    } catch (err) {
      primary.disabled = cancel.disabled = false;
      msg.hidden = false;
      msg.textContent = markBidi(err instanceof ApiError && err.status === 409 ? errText(err) + '. The request was dropped.' : errText(err));
      if (err instanceof ApiError && (err.status === 409 || err.status === 404)) primary.disabled = true;
    } finally {
      opts.done?.();
      void loadExtensions().catch(() => {});
    }
  }
  deps.modal(add ? `Add ${e.label}?` : `Load ${e.label}?`,
    h('p', { class: 'mut' }, add ? 'Adding only lists the folder in Settings › Extensions. Its code does not run until you load it.'
      : 'Its code will run on this computer with your rights, outside the sandbox, every time Piper starts. Load it only if you know where it comes from.'),
    kv([['Folder', shortPath(e.path)], ['From', from], ['Commit', shortCommit(e.commit) || '-'],
      ['Adds', e.adds.length ? e.adds.join(', ') : e.description || 'not said by its manifest']]),
    add ? null : h('p', { class: 'mut small ext-foot' }, local
      ? 'Piper restarts to load it. Sessions keep running. If its origin changes later, Piper stops loading it and asks again.'
      : 'Loading code runs it on your computer, so it is confirmed there. This sends a request to Piper on the computer.'),
    msg, h('div', { class: 'row' }, cancel, primary));
  primary.focus();
}

/** Turn off… : a smaller confirm, then the restart */
export function offDialog(deps: ExtDeps, e: Pick<Extension, 'label' | 'path'>, local = true): void {
  const msg = h('p', { class: 'bad small', role: 'alert', hidden: true });
  const ok = h('button', { class: 'primary', onclick: async () => {
    ok.disabled = true;
    try {
      await post('untrust', { path: e.path });
      deps.closeModal();
      await restartPiper(deps, `Turning off ${e.label}. Sessions keep running; this window reconnects by itself.`);
    } catch (err) {
      ok.disabled = false; msg.hidden = false; msg.textContent = markBidi(errText(err));
    } finally { void loadExtensions().catch(() => {}); }
  } }, 'Turn off and restart Piper');
  deps.modal(`Turn off ${e.label}?`,
    h('p', { class: 'mut' }, `Piper stops running its code and restarts. Sessions keep running.${local ? ' You can load it again here.' : ' Loading it again needs approval on the computer.'}`),
    msg, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), ok));
  ok.focus();
}

/** doctor's "Reinstall the login service": piper install-service, detached; piperd may restart with it */
export function reinstallDialog(deps: ExtDeps, done?: () => void): void {
  const msg = h('p', { class: 'bad small', role: 'alert', hidden: true });
  const ok = h('button', { class: 'primary', onclick: async () => {
    ok.disabled = true;
    try {
      await post('/api/service/reinstall');
      deps.closeModal();
      expectRestart('Reinstalling the login service. Sessions keep running; this window reconnects by itself.');
      done?.();
    } catch (err) { ok.disabled = false; msg.hidden = false; msg.textContent = markBidi(errText(err)); }
  } }, 'Reinstall and restart Piper');
  deps.modal('Reinstall the login service?',
    h('p', { class: 'mut' }, 'Piper installs its login service again (the same as piper install-service) and restarts with it. Sessions keep running. An older service an extension names is removed.'),
    msg, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), ok));
  ok.focus();
}

/** D: the first-start card (the empty main area and the setup card) while an extension is offered */
export function offerCard(deps: ExtDeps & { openSettings(): void }): HTMLElement | null {
  const e = offered(listing);
  if (!e) return null;
  let busy = false;
  return h('section', { class: 'ext-offer', role: 'region', 'aria-label': 'An extension was found' },
    h('b', {}, `An extension was found: ${e.label}`),
    h('div', { class: 'ext-meta small' }, metaLine(e)),
    h('p', { class: 'small' }, e.description ? `${e.description}. It runs on this computer outside the sandbox.` : 'It adds features to Piper and runs on this computer outside the sandbox.'),
    h('div', { class: 'ext-offer-acts' },
      h('button', { class: 'primary', onclick: () => loadDialog(deps, e) }, 'Review and load…'),
      h('button', { title: 'Do not offer it again. You can still load it in Settings › Extensions.', onclick: async () => {
        if (busy) return;
        busy = true;
        try { await post('decline', { path: e.path }); } catch (err) { deps.toast(errText(err)); }
        await loadExtensions().catch(() => {});
      } }, 'Not now'),
      h('button', { class: 'link', onclick: deps.openSettings }, 'Settings › Extensions')));
}

/** A: the Settings section's page */
export function createExtensionSettings(deps: ExtDeps) {
  const el = h('div', { class: 'ext-settings' });
  let loaded = false, error = '', focus = '', shownAt = 0, scroll = false;
  async function refresh(rescan = false): Promise<void> {
    try {
      if (rescan) publish(await post<ExtListing>('rescan'));
      else await loadExtensions();
      error = '';
    } catch (e) { error = errText(e); }
    loaded = true;
    draw();
  }
  document.addEventListener('piper-extensions', () => { loaded = true; if (el.isConnected) draw(); });

  function addDialog(remote: boolean): void {
    const input = h('input', { type: 'text', class: 'mono', placeholder: '~/work/my-extension', spellcheck: 'false', autocomplete: 'off',
      'aria-label': 'extension folder' }) as HTMLInputElement;
    const msg = h('p', { class: 'bad small', role: 'alert', hidden: true });
    const ok = h('button', { class: 'primary', disabled: true, onclick: async () => {
      ok.disabled = true; msg.hidden = true;
      try {
        const r = await post<{ pending?: ExtPending }>('add', { path: input.value.trim() });
        deps.closeModal();
        deps.toast(r?.pending ? 'Asked your computer to add the folder. Approve it there.' : 'Added. Load it from the list when you are ready.');
        await loadExtensions().catch(() => {});
      } catch (e) { ok.disabled = false; msg.hidden = false; msg.textContent = markBidi(errText(e)); }
    } }, 'Add');
    input.addEventListener('input', () => { ok.disabled = !input.value.trim(); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !ok.disabled) { e.preventDefault(); ok.click(); } });
    deps.modal('Add an extension folder', h('label', {}, 'Folder on this computer'), input,
      h('p', { class: 'mut small' }, 'A folder with piper-extension.json, outside the places Piper looks by itself. Adding only lists it here; its code runs only after you load it.'
        + (remote ? ' From here this asks Piper on the computer first.' : '')),
      msg, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), ok));
    input.focus();
  }

  async function act(fn: () => Promise<unknown>): Promise<void> {
    try { await fn(); } catch (e) { deps.toast(errText(e)); }
    await loadExtensions().catch(() => {});
  }

  function pendingBox(l: ExtListing, p: ExtPending): HTMLElement {
    const e = l.extensions.find((x) => x.path === p.path);
    const subject = { label: p.label, path: p.path, remote: p.remote, commit: p.commit, description: e?.description ?? '', adds: e?.adds ?? [],
      state: 'found' as const, trusted_remote: '' };
    return h('div', { class: 'ext-pending', role: 'group', 'aria-label': 'pending approval' },
      h('div', { class: 'ext-pending-text' }, h('b', {}, pendingTitle(p)), h('div', { class: 'small' }, pendingMeta(p))),
      h('button', { title: 'Refuse the request. Nothing changes on this computer.', onclick: () => void act(() => post('deny', { id: p.id })) }, 'Deny'),
      h('button', { class: 'primary', onclick: () => loadDialog(deps, subject, { request: p }) }, p.kind === 'add' ? 'Review and add…' : 'Review and load…'));
  }

  function rowEl(l: ExtListing, e: Extension): HTMLElement {
    const v = rowView(e, l);
    const mine = !l.can_approve ? l.pending.find((p) => p.mine && p.path === e.path && p.kind === 'trust') : undefined;
    let btn: HTMLElement | null = null;
    if (mine) btn = h('button', { title: 'Withdraw the request you sent to the computer.', onclick: () => void act(() => post('cancel', { id: mine.id })) }, 'Cancel request');
    else if (v.button) {
      const b = v.button;
      btn = h('button', { title: b.title, onclick: () => {
        if (b.act === 'restart') void restartPiper(deps);
        else if (b.act === 'untrust') offDialog(deps, e, l.can_approve);
        else loadDialog(deps, e, { caller: l.caller });
      } }, b.label);
    }
    const row = h('li', { class: 'ext-row' + (focus && (focus.includes(e.path) || focus.includes(e.label)) ? ' ext-focus' : ''), 'data-path': e.path },
      h('span', { class: 'ext-tile', 'aria-hidden': 'true' }, (e.label.trim()[0] ?? '?').toUpperCase()),
      h('div', { class: 'ext-main' },
        h('div', { class: 'ext-head' }, h('b', {}, e.label),
          h('span', { class: `ext-state ext-${v.tone}`, title: v.hint }, h('span', { class: 'ext-dot', 'aria-hidden': 'true' }), v.word)),
        h('div', { class: 'ext-line small' }, v.line),
        h('div', { class: 'ext-meta small', title: e.path }, metaLine(e)),
        mine ? h('div', { class: 'ext-wait small' }, h('span', { class: 'ext-dot', 'aria-hidden': 'true' }), 'Waiting for approval on your computer')
          : v.note ? h('div', { class: 'ext-note small' + (v.noteTone ? ' warn' : '') }, v.note) : null),
      btn ? h('div', { class: 'ext-act' }, btn) : null);
    return row;
  }

  function draw(): void {
    clear(el);
    const l = listing;
    if (!loaded && !l) { el.append(h('p', { class: 'mut', role: 'status' }, h('span', { class: 'spinner' }), ' Looking for extensions…')); return; }
    if (error && !l) {
      el.append(h('div', { class: 'st-load-error', role: 'alert' }, h('p', { class: 'warn' }, error), h('button', { onclick: () => void refresh() }, 'Try again')));
      return;
    }
    if (!l) { el.append(h('p', { class: 'mut' }, 'Extensions are managed in Piper’s own window.')); return; }
    if (l.can_approve) for (const p of l.pending) el.append(pendingBox(l, p));
    if (l.restart_needed) el.append(h('div', { class: 'ext-restart-bar' }, h('span', { class: 'grow small' }, 'A change waits for a restart. Sessions keep running through it.'),
      h('button', { title: 'Piper restarts and loads what is turned on now.', onclick: () => void restartPiper(deps) }, 'Restart Piper')));
    if (l.source !== 'trust') el.append(h('p', { class: 'warn small' }, l.source === 'off'
      ? 'Piper was started with PIPER_NO_EXTENSIONS=1: nothing loads until it starts without it.'
      : 'Piper was started with PIPER_EXTENSIONS, which decides what loads. Changes here apply when Piper starts without it.'));
    el.append(h('h3', { class: 'server-group' }, 'On this computer'));
    if (error) el.append(h('p', { class: 'warn small' }, error));
    el.append(l.extensions.length ? h('ul', { class: 'server-list ext-list', 'aria-label': 'Extensions on this computer' }, ...l.extensions.map((e) => rowEl(l, e)))
      : h('div', { class: 'server-empty' }, h('b', {}, 'No extensions found'),
        h('p', { class: 'mut small' }, 'An extension is a folder with piper-extension.json. Piper looks in registered folders, next to Piper and two levels under your projects.')));
    el.append(h('div', { class: 'ext-bar' },
      h('button', { title: 'Add a folder outside the places Piper looks by itself. Adding does not load it.', onclick: () => addDialog(!l.can_approve) }, fi('plus'), 'Add folder…'),
      h('button', { title: 'Read the folders again (a new checkout, a changed manifest or git remote).', onclick: () => void refresh(true) }, 'Look again'),
      h('span', { class: 'mut small' }, 'Registered extensions, folders next to Piper and two levels under your projects.')));
    if (!l.can_approve) el.append(h('p', { class: 'server-list-note' }, 'Loading code runs it on your computer, so it is confirmed there. Turning an extension off works from here.'));
    const row = scroll ? el.querySelector<HTMLElement>('.ext-focus') : null;
    if (row) { row.scrollIntoView?.({ block: 'center' }); scroll = false; }
  }
  draw();
  return {
    el,
    /** every Settings redraw calls it: the listing is asked again only after a few seconds (discover() reads git) */
    show(hint = ''): void {
      if (hint !== focus) { focus = hint; scroll = !!hint; }   // the row a doctor action named stays marked; scrolled to once
      draw();
      if (hint || Date.now() - shownAt > 4000) { shownAt = Date.now(); void refresh(); }
    },
    refresh,
    count: () => reviewCount(listing),
  };
}
