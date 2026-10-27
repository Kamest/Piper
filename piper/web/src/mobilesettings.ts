/** The phone's Settings section. */
import { ApiError, call } from './api.js';
import { clear, fi, h } from './dom.js';
import { markBidi } from './text.js';
import qrcode from '../vendor/qrcode/qrcode.js';
import { completePushOffer, disablePush, enablePush, mobilePushClient, pushComputer, pushOffer, pushStatus, pushSupport, setPushTitles, testPush, type PushState } from './mobilepush.js';

interface Device { id: string; label: string; at: number }
interface Status { ready: boolean; configured: boolean; computer: string; install_url: string | null; devices: Device[]; pending: { id: string; expires: number } | null; last_pairing: string | null }
interface Invitation { id: string; url: string; expires: number }
interface Deps { modal(title: string, ...content: HTMLElement[]): void; closeModal(): void; toast(text: string): void }
const request = async <T>(method: string, action = '', body?: unknown) => (await call<T>(method, '/api/mobile' + action, body, undefined, 5000, '')).data!;
const errorText = (e: unknown) => e instanceof ApiError ? e.message : 'Mobile access is unavailable. Try again.';

/** Build the QR locally, without HTML interpolation or third-party requests. */
export function pairingQR(link: string, label = 'Pairing QR code'): SVGSVGElement {
  const qr = qrcode(0, 'M'); qr.addData(link); qr.make();
  const n = qr.getModuleCount(), size = n + 8;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', markBidi(label));
  svg.setAttribute('shape-rendering', 'crispEdges');
  const background = document.createElementNS(svg.namespaceURI, 'rect');
  background.setAttribute('width', String(size)); background.setAttribute('height', String(size)); background.setAttribute('fill', '#fff');
  const pixels = document.createElementNS(svg.namespaceURI, 'path');
  let d = '';
  for (let row = 0; row < n; row++) for (let column = 0; column < n; column++) if (qr.isDark(row, column)) d += `M${column + 4},${row + 4}h1v1h-1z`;
  pixels.setAttribute('d', d); pixels.setAttribute('fill', '#000'); svg.append(background, pixels);
  return svg;
}

function warning(short = false): HTMLElement {
  return h('div', { class: 'protected-callout' }, h('strong', {}, short ? 'Full access · keep it private' : 'Full access to Piper'),
    h('p', {}, short ? 'Whoever scans this code or opens this link gets full access to Piper on this computer. Don’t send it to anyone.'
      : 'Whoever scans the code or opens the link first gets everything you can do in Piper on this computer:'),
    short ? null : h('ul', {}, h('li', {}, 'agent sessions and host terminals'), h('li', {}, 'project files and databases'), h('li', {}, 'approvals, commits and pushes')),
    short ? null : h('p', { class: 'mut small' }, 'Scan it only with your own phone. Don’t send the code or link to anyone or paste it into a chat or ticket.'));
}

/** Push welcome: one non-modal strip, never an automatic OS prompt. */
export async function showMobilePushWelcome(deps: Deps, showSettings: () => void): Promise<void> {
  if (!mobilePushClient()) return;
  let interacted = false;
  const touch = () => { interacted = true; };
  document.addEventListener('pointerdown', touch, { capture: true, once: true });
  document.addEventListener('keydown', touch, { capture: true, once: true });
  const cleanTouch = () => { document.removeEventListener('pointerdown', touch, true); document.removeEventListener('keydown', touch, true); };
  const kind = await pushOffer();
  if (!kind || interacted) { cleanTouch(); return; }
  let busy = false, error = '', stopped = false;
  const el = h('section', { id: 'mobile-push-welcome', class: 'mpw', 'data-kind': kind === 'off' ? 'offer' : 'install',
    role: 'region', 'aria-labelledby': 'mpw-title' });
  const cleanWatch = () => { observer.disconnect(); document.removeEventListener('visibilitychange', show); cleanTouch(); };
  const remove = () => {
    stopped = true; cleanWatch();
    const focused = el.contains(document.activeElement);
    el.remove(); document.removeEventListener('piper-push-changed', enabled);
    if (focused) [...document.querySelectorAll<HTMLElement>('#app button:not([disabled]), #app [tabindex="0"]')]
      .find(node => node.getClientRects().length)?.focus({ preventScroll: true });
  };
  const enabled = () => { if (!busy) remove(); };
  const enable = async () => {
    if (busy) return;
    const pending = enablePush(); // preserves the real click before any await
    busy = true; error = ''; draw();
    try { await pending; remove(); deps.toast('Notifications turned on for this phone'); }
    catch {
      if (Notification.permission !== 'granted') remove();
      else { error = 'Couldn’t turn on notifications. Try again.'; busy = false; draw(); }
    }
  };
  function draw(): void {
    clear(el); el.dataset.state = busy ? 'busy' : error ? 'error' : 'idle';
    el.append(h('div', { class: 'mpw-head' }, fi(kind === 'off' ? 'bell' : 'phone'),
      h('div', { class: 'mpw-text' }, h('strong', { id: 'mpw-title' }, kind === 'off' ? 'Turn on notifications?' : 'Notifications need the Home Screen app'),
        h('p', { class: 'mut small' }, kind === 'off'
          ? 'Get a notification on this phone when an agent needs you, even with Piper closed. Session names are off by default; messages are never shown.'
          : 'On iPhone, Piper can notify you only when it’s opened from the Home Screen. Add it there and pair the app once; then remove this browser on the computer.'))));
    el.append(h('div', { class: 'row mpw-actions' }, h('button', { disabled: busy, onclick: remove }, 'Not now'),
      h('button', { class: 'primary', disabled: busy, onclick: kind === 'off' ? () => void enable() : () => { remove(); showSettings(); } },
        kind === 'off' ? busy ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : fi('bell') : null,
        kind === 'off' ? busy ? 'Turning on…' : 'Turn on notifications' : 'Show steps')));
    if (error) el.append(h('p', { class: 'bad small', role: 'alert' }, error));
    if (kind === 'off') el.append(h('p', { class: 'mpw-note small' }, `Works while ${pushComputer()} is online. Turn on session names or change this anytime in Settings › Mobile access.`));
  }
  function show(): void {
    if (interacted) { remove(); return; } // do not move content beneath someone already using the app
    if (stopped || document.hidden || document.querySelector('.mobile-stage, #mobile-offline, #overlay.open')) return;
    const app = document.getElementById('app');
    if (!app) return;
    cleanWatch(); stopped = true;
    if (!completePushOffer()) { remove(); return; } // storage blocked: do not risk a prompt on every load
    draw(); app.before(el);
  }
  const observer = new MutationObserver(show);
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'inert'] });
  document.addEventListener('visibilitychange', show);
  document.addEventListener('piper-push-changed', enabled);
  window.addEventListener('pagehide', cleanWatch, { once: true });
  el.addEventListener('keydown', event => { if (event.key === 'Escape' && !busy) { event.stopPropagation(); remove(); } });
  show();
}

/** Push: opt-in controls in the phone's existing Settings section. */
function createNotificationPanel(deps: Deps) {
  const el = h('div', { class: 'mobile-push' });
  let state: PushState = 'unavailable', loaded = false, busy = '', error = '';
  let showTitles = false, pendingTitles: boolean | null = null, titleError = '';
  async function refresh(): Promise<void> {
    if (busy) return;
    try { const status = await pushStatus(); state = status.state; showTitles = status.showTitles; error = ''; titleError = ''; }
    catch { state = 'unavailable'; }
    loaded = true; draw();
  }
  const run = async (kind: 'enable' | 'disable' | 'test') => {
    if (busy) return;
    // Start first: enablePush requests permission synchronously from this click.
    const pending = kind === 'enable' ? enablePush() : kind === 'disable' ? disablePush() : testPush();
    busy = kind; error = ''; titleError = ''; draw();
    try {
      await pending;
      if (kind === 'test') {
        const toast = h('div', { class: 'toast mobile-push-toast', role: 'status' }, h('strong', {}, 'Test notification sent'),
          h('p', {}, 'Nothing showing? Check Focus and notification settings on this phone.'));
        document.body.append(toast); window.setTimeout(() => toast.remove(), 6000);
      } else {
        state = kind === 'enable' ? 'on' : 'off';
        if (kind === 'disable') deps.toast('Notifications turned off on this phone');
      }
    } catch {
      state = pushSupport() ?? state;
      error = kind === 'enable' ? 'Couldn’t turn on notifications. Try again.' : kind === 'test' ? 'Couldn’t send the test. Try again.' : 'Couldn’t update notifications. Try again.';
    } finally { busy = ''; draw(); }
  };
  const saveTitles = async (value: boolean) => {
    if (busy) return;
    busy = 'titles'; pendingTitles = value; error = ''; titleError = ''; draw();
    try { await setPushTitles(value); showTitles = value; }
    catch { titleError = 'Couldn’t save this setting. Try again.'; }
    finally { busy = ''; pendingTitles = null; draw(); }
  };
  function draw(): void {
    clear(el);
    if (!loaded) { el.append(h('p', { class: 'mut small', role: 'status' }, 'Checking notifications…')); return; }
    const copy: Record<PushState, [string, string]> = {
      off: ['Off on this phone', 'Get a notification when an agent needs you, even with Piper closed. Your phone may ask for permission.'],
      on: ['On for this phone', 'When an agent needs you, this phone gets a notification. Open Piper to see what it is.'],
      install: ['Add Piper to the Home Screen first', 'On iPhone, notifications work only in Piper opened from the Home Screen.'],
      blocked: ['Blocked on this phone', 'Notifications for Piper are turned off in the phone’s settings. Allow them in Settings › Notifications › Piper, then come back.'],
      unsupported: ['Not supported here', 'This browser can’t show notifications from Piper. On iPhone, update iOS and open Piper from the Home Screen.'],
      unavailable: ['Notifications aren’t available', `Piper on ${pushComputer()} can’t set up notifications right now. Your choice is kept.`],
    };
    el.dataset.state = state;
    const [title, description] = copy[state];
    const dot = state === 'on' ? 'server-dot online' : state === 'off' ? 'server-dot' : ['blocked', 'unavailable'].includes(state) ? 'server-error-dot' : 'server-unknown-dot';
    el.append(h('div', { class: 'mobile-status', role: 'status' }, h('div', { class: 'row left' },
      h('span', { class: dot, 'aria-hidden': 'true' }), h('strong', {}, title)), h('p', { class: 'mut small' }, description)));
    if (state === 'install') {
      el.append(h('ol', { class: 'mobile-push-steps' }, ...[
        'Tap Share, then Add to Home Screen.',
        'Open Piper from the Home Screen. On the computer choose Pair phone › Next: pair the app, copy the link and paste it in the app.',
        'Turn on notifications there, in Settings › Mobile access.',
        'On the computer, remove this browser from Paired phones.',
      ].map((text, index) => h('li', { class: 'mobile-step' }, h('span', {}, String(index + 1)), h('div', {}, text)))));
    }
    const button = (text: string, kind: 'enable' | 'disable' | 'test', primary = false) => h('button', {
      class: primary ? 'primary' : '', disabled: !!busy, onclick: () => void run(kind),
    }, busy === kind ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : primary ? fi('bell') : null,
    busy === 'enable' && kind === 'enable' ? 'Turning on…' : text);
    const actions = h('div', { class: 'row mobile-push-actions' });
    if (state === 'off') actions.append(button('Turn on notifications', 'enable', true));
    if (state === 'on') actions.append(button('Send test', 'test'), button('Turn off', 'disable'));
    if (state === 'blocked' || state === 'unavailable') actions.append(h('button', { disabled: !!busy, onclick: () => void refresh() }, state === 'blocked' ? 'Check again' : 'Retry'));
    if (actions.childElementCount) el.append(actions);
    if (error) el.append(h('p', { class: 'bad small', role: 'alert' }, error));
    if (state === 'on') {
      const input = h('input', { type: 'checkbox', role: 'switch', 'aria-labelledby': 'mobile-push-names-label',
        'aria-describedby': 'mobile-push-names-help', disabled: !!busy }) as HTMLInputElement;
      input.checked = pendingTitles ?? showTitles;
      input.addEventListener('change', () => void saveTitles(input.checked));
      el.append(h('label', { class: 'switch mobile-push-names', 'aria-busy': String(busy === 'titles') },
        h('span', { class: 'mobile-push-names-text' }, h('strong', { id: 'mobile-push-names-label' }, 'Show session names'),
          h('p', { id: 'mobile-push-names-help', class: 'mut small' }, 'Notifications say which session needs you, e.g. “HTTP”. Names can appear on your lock screen. Messages are never included.')),
        busy === 'titles' ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : null, input, h('span', { class: 'track', 'aria-hidden': 'true' })));
      if (titleError) el.append(h('p', { class: 'bad small', role: 'alert' }, titleError));
    }
    el.append(h('p', { class: 'mobile-push-privacy' }, fi('lock'),
      h('span', {}, showTitles ? 'Notifications show the session name, never messages. Details appear after Piper opens a secure connection.'
        : 'Notifications say only that Piper needs you — no session names or messages. Details appear after Piper opens a secure connection.')));
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && el.isConnected) void refresh(); });
  document.addEventListener('piper-push-changed', () => { if (mobilePushClient()) void refresh(); });
  draw();
  return { el, show: () => { if (!loaded) void refresh(); } };
}

export function createMobileSettings(deps: Deps) {
  const el = h('div', { class: 'mobile-settings' });
  const notifications = createNotificationPanel(deps);
  let state: Status | null = null, loaded = false, loading = false, error = '', localOnly = false;
  async function refresh(): Promise<void> {
    if (loading) return;
    loading = true;
    try { state = await request<Status>('GET'); error = ''; localOnly = false; }
    catch (e) { error = errorText(e); localOnly = e instanceof ApiError && e.status === 403; }
    finally { loading = false; loaded = true; draw(); el.dispatchEvent(new CustomEvent('mobile-loaded', { bubbles: true })); }
  }
  function draw(): void {
    clear(el);
    if (!loaded) { el.append(h('p', { class: 'mut', role: 'status' }, 'Checking mobile access…')); return; }
    if (localOnly) {
      if (mobilePushClient()) {
        el.append(h('h3', { class: 'server-group' }, 'Notifications'), notifications.el,
          h('p', { class: 'server-list-note mobile-push-note' }, `Notifications arrive only while ${pushComputer()} is online and Piper is running on it.`),
          h('h3', { class: 'server-group' }, 'Phones'));
        notifications.show();
      }
      el.append(h('div', { class: 'server-empty mobile-remote' }, fi('monitor'), h('b', {}, 'Manage phones on the computer'), h('p', { class: 'mut small' }, 'Pairing and removing phones works only in Piper on the computer itself, in Settings › Mobile access. A phone can’t add or remove phones.'))); return; }
    const ready = !error && state?.ready;
    el.append(h('div', { class: 'mobile-status', role: 'status' },
      h('div', { class: 'row left' }, h('span', { class: ready ? 'server-dot online' : 'server-error-dot', 'aria-hidden': 'true' }),
        h('span', { class: 'grow' }, ready ? 'Ready · end-to-end encrypted' : state?.configured || error ? 'Mobile access is unavailable' : 'Mobile access isn’t set up'),
        ready ? null : h('button', { onclick: () => void refresh() }, 'Retry')),
      h('p', { class: 'mut small' }, ready ? 'Phones reach this computer through a relay that can’t read your sessions.'
        : error || (state?.configured ? 'The mobile bridge is not running. Paired phones stay paired but can’t connect until it’s back.' : 'This Piper install has no relay settings, so phones can’t connect. Nothing is shared.'))));
    if (!state) return;
    if (!state.configured) return;
    if (!state.devices.length) {
      el.append(h('div', { class: 'server-empty' }, h('b', {}, 'No phones yet'),
        h('p', { class: 'mut small' }, 'Pair a phone to follow sessions, answer agents and approve requests away from this computer. A paired phone gets full access to Piper here.'),
        h('button', { class: 'primary', disabled: !ready, onclick: pairName }, fi('plus'), 'Pair phone')));
      return;
    }
    el.append(h('h3', { class: 'server-group' }, 'Paired phones'), h('ul', { class: 'server-list mobile-phones', 'aria-label': 'Paired phones' },
      ...[...state.devices].sort((a, b) => b.at - a.at).map(device => h('li', { class: 'mobile-phone' }, fi('phone'),
        h('div', { class: 'grow' }, h('b', { title: device.label }, device.label), h('time', { class: 'mut small', datetime: new Date(device.at * 1000).toISOString() }, 'Added ' + new Date(device.at * 1000).toLocaleString([], { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }))),
        h('button', { onclick: () => remove(device), 'aria-label': `Remove ${device.label}` }, 'Remove…')))),
      h('button', { class: 'server-add', disabled: !ready, onclick: pairName }, fi('plus'), 'Pair phone'),
      h('p', { class: 'server-list-note' }, ready ? 'Remove a phone you no longer use or have lost. It loses access at once and any open connection closes.' : 'Pairing needs mobile access. Removing a phone still works and takes effect at once.'));
  }
  function remove(device: Device): void {
    const message = h('p', { class: 'bad small', role: 'alert', hidden: true });
    const cancel = h('button', { onclick: deps.closeModal }, 'Cancel');
    const confirm = h('button', { class: 'danger', onclick: async () => {
      confirm.disabled = true;
      try { await request('POST', '/revoke', { id: device.id }); deps.closeModal(); deps.toast('Phone removed'); await refresh(); }
      catch (e) { message.hidden = false; message.textContent = markBidi(errorText(e)); confirm.disabled = false; }
    } }, 'Remove phone');
    deps.modal(`Remove ${device.label}?`, h('p', {}, `${device.label} loses access to Piper at once. If it’s connected now, the connection closes.`),
      h('p', { class: 'mut' }, 'To use it again, pair it again.'), message, h('div', { class: 'row' }, cancel, confirm));
    cancel.focus();
  }
  let suggestedName = 'iPhone';
  function pairName(): void {
    const label = h('input', { id: 'mobile-label', value: suggestedName, maxlength: '80', autocomplete: 'off', spellcheck: 'false' });
    const error = h('p', { class: 'bad small', role: 'alert', hidden: true });
    const root = h('div', { class: 'mobile-name mobile-install' });
    const generate = async (browser: boolean) => {
      const name = label.value.trim();
      if (!name || /[\x00-\x1f]/.test(name)) { error.hidden = false; error.textContent = 'Enter a phone name (1–80 characters).'; label.focus(); return; }
      suggestedName = name; next.disabled = browserOnly.disabled = true;
      try {
        const invitation = await request<Invitation>('POST', '/invite', { label: name });
        if (!root.isConnected) { await request('POST', '/cancel', { id: invitation.id }); return; }
        pairCode(name, invitation, browser);
      } catch (e) { error.hidden = false; error.textContent = markBidi(errorText(e)); next.disabled = browserOnly.disabled = false; }
    };
    const next = h('button', { class: 'primary', onclick: () => void generate(false) }, 'Next: pair the app');
    const browserOnly = h('button', { onclick: () => void generate(true) }, 'Use in a browser only');
    const qr = h('div', { class: 'mobile-install-qr' }, h('div', { class: 'mobile-qr' }, pairingQR(state?.install_url ?? 'https://piper.kamest.dev/', 'Installation QR code')),
      h('p', { class: 'mut small' }, 'Opens Piper · gives no access'));
    root.append(h('p', { class: 'startup-eyebrow' }, 'STEP 1 OF 2 · INSTALL'), h('h2', {}, 'Add Piper to the phone’s Home Screen'),
      h('p', { class: 'mut' }, 'Install first, then pair once inside the app. A browser and the Home Screen app keep separate sign-ins, so a pairing made in the browser doesn’t carry over.'),
      h('div', { class: 'mobile-pair-body' }, qr, h('div', { class: 'mobile-pair-side' }, h('ol', {},
        h('li', {}, h('div', {}, 'Scan this code with the phone’s camera and open the link.', h('p', { class: 'mut small' }, 'Any browser works, Chrome or Safari. No need to switch.'))),
        h('li', {}, h('div', {}, 'Tap Share, then Add to Home Screen.', h('p', { class: 'mut small' }, 'In Chrome, Share is next to the address bar. Don’t see Add to Home Screen? Scroll the list or tap More.'))),
        h('li', {}, h('div', {}, 'Open Piper from the Home Screen.', h('p', { class: 'mut small' }, 'It asks to pair with your computer. Then come back here.')))))),
      h('label', { for: 'mobile-label' }, 'Phone name'), label, h('p', { class: 'mut small' }, 'Shown in the list of paired phones. 1–80 characters.'),
      error, h('div', { class: 'row mobile-install-actions' }, browserOnly, h('span', { class: 'grow' }), h('button', { onclick: deps.closeModal }, 'Cancel'), next));
    label.addEventListener('input', () => { next.disabled = browserOnly.disabled = !label.value.trim(); });
    label.addEventListener('keydown', e => { if (e.key === 'Enter' && !next.disabled) { e.preventDefault(); next.click(); } });
    deps.modal('Add Piper to the phone’s Home Screen', root); label.focus(); label.select();
  }
  function pairCode(label: string, first: Invitation, browser = false): void {
    const root = h('div', { class: 'mobile-pair' + (browser ? '' : ' mobile-pair-app'), 'aria-live': 'polite' });
    let invitation: Invitation | null = first, checking = false, ended = false, renewing = false, expiredShown = false;
    let timer = 0;
    let remaining: HTMLElement | null = null;
    const message = h('p', { class: 'bad small', role: 'alert', hidden: true });
    const title = (text: string) => {
      const modal = root.closest('.modal');
      modal?.setAttribute('aria-label', markBidi(text));
      const heading = modal?.querySelector('h2'); if (heading) heading.textContent = markBidi(text);
    };
    const cancel = async () => {
      if (!invitation) { deps.closeModal(); return; }
      try { await request('POST', '/cancel', { id: invitation.id }); if (invitation) invitation.url = ''; invitation = null; ended = true; deps.closeModal(); void refresh(); }
      catch (e) { message.hidden = false; message.textContent = markBidi(errorText(e)); }
    };
    const render = (expired = false, paired = false) => {
      clear(root); root.classList.toggle('paired', paired); message.hidden = true;
      remaining = null;
      const heading = paired ? `${label} paired` : expired ? browser ? 'QR code expired' : 'Pairing link expired' : browser ? `Scan with ${label}` : `Pair Piper on ${label}`;
      title(heading);
      if (paired) {
        root.append(h('p', {}, `${label} can use Piper now. To take its access away, remove it in this list.`),
          h('div', { class: 'row' }, h('button', { class: 'primary', onclick: deps.closeModal }, 'Done')));
        root.querySelector('button')?.focus(); return;
      }
      const qr = h('div', { class: 'mobile-qr' + (expired ? ' expired' : '') });
      if (invitation && !expired) qr.append(pairingQR(invitation.url));
      else qr.append(fi('clock'), h('b', {}, 'Expired'));
      const side = h('div', { class: 'mobile-pair-side' });
      if (expired) side.append(h('p', {}, browser ? 'A code works for 60 seconds and only once. Nothing was paired.' : 'A link works for 60 seconds and only once. Nothing was paired.'), h('p', { class: 'mut small' }, `A new ${browser ? 'code' : 'link'} pairs the same phone name, ${label}.`));
      else {
        const left = Math.max(0, Math.ceil((invitation?.expires ?? 0) - Date.now() / 1000));
        remaining = h('span', { class: 'startup-clock', 'aria-hidden': 'true' }, `· expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`);
        const copy = h('button', { class: browser ? '' : 'primary', onclick: async () => {
          if (!invitation || invitation.expires <= Date.now() / 1000) return;
          try { await navigator.clipboard.writeText(invitation.url); copy.replaceChildren(fi('check'), 'Copied'); copy.classList.remove('primary'); deps.toast('Pairing link copied · keep it private'); }
          catch { message.hidden = false; message.textContent = 'Can’t copy the link. Allow clipboard access and try again.'; }
        } }, fi('copy'), 'Copy pairing link');
        if (browser) side.append(h('ol', {}, h('li', {}, `Open the camera on ${label} and scan the code.`), h('li', {}, 'Open the link and tap Pair with full access.')),
          copy);
        else side.append(h('ol', {},
          h('li', {}, h('div', {}, 'Copy the pairing link.', h('p', { class: 'mut small' }, 'On a Mac signed in to the same Apple Account as the phone, it’s ready to paste there right away.'), copy)),
          h('li', {}, 'In Piper on the Home Screen, paste it and tap Continue.'),
          h('li', {}, 'Check the computer name and tap Pair with full access.')));
        side.append(h('p', { class: 'mut small mobile-wait' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Waiting for the phone ', remaining));
      }
      const generate = h('button', { class: 'primary', onclick: async () => {
        if (renewing) return;
        renewing = true; generate.disabled = true;
        try {
          const next = await request<Invitation>('POST', '/invite', { label });
          if (!root.isConnected) { await request('POST', '/cancel', { id: next.id }); return; }
          invitation = next; ended = expiredShown = false; render();
        } catch (e) { message.hidden = false; message.textContent = markBidi(errorText(e)); }
        finally { renewing = false; generate.disabled = false; }
      } }, browser ? 'Generate new code' : 'Generate new link');
      root.append(h('p', { class: 'startup-eyebrow' }, browser ? 'BROWSER ONLY' : 'STEP 2 OF 2 · PAIR THE APP'), h('h2', {}, heading), h('div', { class: 'mobile-pair-body' }, browser ? qr : null, side));
      if (browser && !expired) root.append(h('div', { class: 'mobile-where' }, h('strong', {}, 'Pairs only the browser that opens the link'), h('p', { class: 'mut small' }, 'If you add Piper to the Home Screen later, the app needs its own pairing. Pair it with Pair phone, then remove this browser’s entry here.')));
      if (!browser && !expired) root.append(h('div', { class: 'mobile-where' }, h('strong', {}, fi('info'), 'Don’t use the Camera app for this step'), h('p', { class: 'mut small' }, 'A code scanned with the camera opens a browser, which would get paired instead of the Home Screen app. Can’t paste on the phone? Go Back and choose Use in a browser only.')));
      if (!expired) root.append(browser ? warning(true) : h('div', { class: 'protected-callout' }, h('strong', {}, 'Full access · keep it private'), h('p', {}, 'Whoever opens this link first gets full access to Piper on this computer. Paste it only into Piper on your own phone, never into a chat or ticket.')));
      root.append(message,
        h('div', { class: 'row' }, h('button', { onclick: async () => {
          try { if (invitation) await request('POST', '/cancel', { id: invitation.id }); invitation = null; ended = true; pairName(); }
          catch (e) { message.hidden = false; message.textContent = markBidi(errorText(e)); }
        } }, 'Back'), h('span', { class: 'grow' }), h('button', { onclick: () => void cancel() }, expired ? 'Close' : 'Cancel'), expired ? generate : null));
    };
    const detached = () => {
      if (root.isConnected) return;
      window.clearInterval(timer); observer.disconnect();
      if (invitation) { const id = invitation.id; invitation.url = ''; invitation = null; void request('POST', '/cancel', { id }).catch(() => {}); }
      el.querySelector<HTMLButtonElement>('button.server-add, .server-empty button')?.focus();
    };
    const observer = new MutationObserver(detached);
    deps.modal(`Scan with ${label}`, root); render();
    observer.observe(root.closest('#overlay') ?? document.body, { childList: true, subtree: true });
    const tick = async () => {
      if (!root.isConnected) {
        detached();
        return;
      }
      if (ended || !invitation) return;
      const left = Math.max(0, Math.ceil(invitation.expires - Date.now() / 1000));
      if (remaining) remaining.textContent = markBidi(`· expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`);
      if (!left && !expiredShown) { expiredShown = true; invitation.url = ''; render(true); }
      if (checking) return;
      checking = true;
      const current = invitation;
      try {
        const result = await request<Status>('GET');
        if (!root.isConnected || invitation !== current) return;
        state = result;
        if (result.last_pairing === current.id) { current.url = ''; invitation = null; ended = true; render(false, true); draw(); el.dispatchEvent(new CustomEvent('mobile-loaded', { bubbles: true })); }
        else if (!left || result.pending?.id !== current.id) { invitation = null; ended = true; render(true); }
      } catch {
        // An unreadable status is never success. Hide the secret at its local deadline too.
        if (!left && invitation === current) { invitation = null; ended = true; render(true); }
      } finally { checking = false; }
    };
    timer = window.setInterval(() => void tick(), 1000);
    void tick();
  }
  draw();
  return { el, show: () => { if (!loaded) void refresh(); }, refresh, count: () => state?.devices.length ?? 0 };
}
