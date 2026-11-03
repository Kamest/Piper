// Phone pairing. Shared Piper controls and theme only.
import { fi, h } from './dom.js';
import { markBidi } from './text.js';
import { readQRPhoto, startQRScanner } from './mobilescanner.js';

export type MobileState = 'install' | 'unpaired' | 'scan' | 'camera-denied' | 'camera-unavailable' | 'camera-interrupted' | 'invalid-qr' | 'install-qr' | 'photo-empty' | 'pair' | 'pairing' | 'connecting' | 'expired' | 'revoked' | 'offline' | 'identity' | 'approve-wait' | 'setup-ended';
export interface MobileActions { browser?: () => void; install?: () => void; pair?: () => void; cancel?: () => void; forget?: () => void; retry?: () => void; openLink?: (link: string) => Promise<string | null>; validLink?: (link: string) => boolean; setup?: boolean; number?: string; reason?: string }
/** iOS browser and Home Screen app have separate identities. Existing paired browsers keep working. */
export function mobileBrowserInstall(): boolean {
  return !window.matchMedia('(display-mode: standalone)').matches && !(navigator as Navigator & { standalone?: boolean }).standalone;
}

let stage: HTMLElement | null = null;
let clock = 0;
let viewportBound = false;
let disposeScanner: (() => void) | null = null;

/** Text field focused = the on-screen keyboard may be up. */
function editing(): boolean {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === 'TEXTAREA' || tag === 'SELECT' || (tag === 'INPUT' && !['button', 'checkbox', 'radio', 'submit', 'reset', 'range', 'color', 'file', 'image'].includes((el as HTMLInputElement).type));
}

/** Safari's keyboard and browser chrome resize the visible area, including non-terminal views. */
export function installMobileViewport(): void {
  if (viewportBound) return;
  viewportBound = true;
  // iOS zooms into any field under 16px on focus and leaves the page zoomed; maximum-scale stops only that
  // auto-zoom (Safari still allows pinch zoom; the standalone app unverified). Fonts stay the shared app ones.
  const apple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]');
  if (apple && meta && !/maximum-scale/.test(meta.content)) meta.content += ', maximum-scale=1';
  const update = () => {
    const viewport = window.visualViewport;
    const zoomed = !!viewport && Math.abs(viewport.scale - 1) > .01;
    if (!editing()) {
      // No keyboard: the visible area, or when zoomed the whole layout viewport - a keyboard-sized height must not stay.
      const height = !zoomed && viewport && viewport.height > 0 ? viewport.height : document.documentElement.clientHeight || window.innerHeight;
      document.documentElement.style.setProperty('--mobile-viewport-height', `${height}px`);
      document.documentElement.style.setProperty('--mobile-viewport-top', '0px');
      return;
    }
    if (zoomed) return; // preserve native pinch zoom/panning
    const height = viewport && viewport.height > 0 ? viewport.height : window.innerHeight;
    const top = Math.max(0, viewport?.offsetTop ?? 0);
    document.documentElement.style.setProperty('--mobile-viewport-height', `${height}px`);
    document.documentElement.style.setProperty('--mobile-viewport-top', `${top}px`);
  };
  // the keyboard hides after blur; Safari does not always send a resize for it
  const settle = () => { update(); window.setTimeout(update, 350); };
  window.addEventListener('resize', update);
  window.visualViewport?.addEventListener('resize', update);
  window.visualViewport?.addEventListener('scroll', update);
  document.addEventListener('focusin', settle);
  document.addEventListener('focusout', settle);
  update();
}

export function clearMobileState(): void {
  disposeScanner?.(); disposeScanner = null;
  window.clearInterval(clock);
  stage?.remove(); stage = null;
  const app = document.getElementById('app');
  if (app) app.inert = false;
}

export function showMobileState(state: MobileState, computer = 'Piper', actions: MobileActions = {}, inGui = false, paired = true, browserOnly = false): void {
  clearMobileState();
  const app = document.getElementById('app');
  if (app) app.inert = true;
  const button = (label: string, action: (() => void) | undefined, primary = false) =>
    h('button', { ...(primary ? { class: 'primary' } : {}), onclick: () => action?.() }, label);
  const title = (text: string, icon = 'lock') => h('h2', {}, fi(icon), text);
  const step = (n: number, text: string) => h('div', { class: 'mobile-step' }, h('span', {}, String(n)), h('div', {}, text));
  const show = (next: MobileState) => showMobileState(next, computer, actions, inGui, paired, browserOnly);
  const scan = () => show('scan');
  let startScanner: (() => void) | null = null;
  const acceptQR = async (value: string) => {
    disposeScanner?.(); disposeScanner = null;
    if (!actions.validLink?.(value)) {
      let installation = false;
      try { const url = new URL(value); installation = url.origin === location.origin && url.pathname === '/' && !url.hash && !url.search; } catch { /* unknown QR */ }
      show(installation ? 'install-qr' : 'invalid-qr'); return;
    }
    if (await actions.openLink?.(value)) show('invalid-qr');
  };
  const photo = () => {
    const input = h('input', { type: 'file', accept: 'image/*', hidden: true, 'aria-label': 'Choose a photo of the code' });
    const choose = h('button', { onclick: () => {
      disposeScanner?.(); disposeScanner = null;
      if (state === 'scan') {
        const status = card.querySelector('[role="status"]');
        if (status) status.textContent = 'Camera paused. Choose a photo or scan again.';
        if (!card.querySelector('.mobile-scan-resume')) card.append(h('button', { class: 'mobile-scan-resume', onclick: scan }, 'Scan again'));
      }
      input.click();
    } }, 'Choose a photo of the code');
    input.addEventListener('change', () => {
      const file = input.files?.[0]; input.value = '';
      if (!file) return;
      const owner = stage;
      choose.disabled = true; choose.textContent = 'Reading photo…';
      void readQRPhoto(file).then(value => {
        if (stage !== owner) return;
        if (value) void acceptQR(value); else show('photo-empty');
      }, () => { if (stage === owner) show('photo-empty'); });
    });
    return h('div', { class: 'mobile-photo' }, input, choose);
  };
  const paste = (app = false) => {
    const input = h('textarea', { id: 'mobile-link', rows: '3', maxlength: '4096', autocomplete: 'off', autocorrect: 'off', autocapitalize: 'none', inputmode: 'url', spellcheck: 'false', placeholder: 'Paste the link copied on the computer' });
    const error = h('p', { class: 'bad small', role: 'alert', hidden: true });
    const next = h('button', { class: 'primary', disabled: true, onclick: async () => {
      if (!actions.openLink) return;
      next.disabled = true;
      const value = input.value.trim(); input.value = '';
      try {
        const problem = await actions.openLink(value);
        if (problem) { error.textContent = markBidi(problem); error.hidden = false; input.focus(); }
      } catch { error.textContent = 'Couldn’t open this link. Copy a new one on the computer.'; error.hidden = false; }
    } }, 'Continue');
    input.addEventListener('input', () => {
      const value = input.value.trim(), valid = actions.validLink?.(value) ?? false;
      next.disabled = !valid; error.hidden = !value || valid;
      error.textContent = 'This isn’t a Piper pairing link. Copy it again on the computer.';
      input.setAttribute('aria-invalid', String(!!value && !valid));
    });
    return h('div', { class: 'mobile-paste' }, app ? null : h('div', { class: 'mobile-or' }, 'or paste the link'), h('label', { for: 'mobile-link' }, 'Pairing link'), input,
      app ? null : h('p', { class: 'mut small' }, 'On the computer choose Copy pairing link, then paste it here.'), error, next);
  };
  const apple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const card = h('section', { class: 'modal', role: 'dialog', 'aria-modal': 'true', tabindex: '-1' });
  if (state === 'install') {
    card.classList.add('mobile-onboarding');
    card.append(title('Install Piper', 'phone'),
      h('p', { class: 'mut' }, actions.setup ? 'Add Piper to your Home Screen. It connects to your computer when you open it.' : 'Add Piper to your Home Screen. You’ll pair it with your computer from there.'),
      step(1, apple ? 'Tap Share in this browser.' : 'Open the browser menu.'),
      ...(apple ? [h('p', { class: 'mut small mobile-gui-path' }, 'In Chrome it’s next to the address bar. If you don’t see it, tap More first. No need to switch browsers.')] : []),
      step(2, apple ? 'Choose Add to Home Screen.' : 'Choose Add to Home screen or Install app.'),
      ...(apple ? [h('p', { class: 'mut small mobile-gui-path' }, 'If Open as Web App is shown, leave it on.')] : []),
      step(3, actions.setup ? 'Open Piper from your Home Screen. Then approve it on your computer.' : 'Open Piper from your Home Screen. Then tap Scan pairing code.'),
      actions.setup ? h('p', { class: 'mut small' }, fi('lock'), ' Nothing is shared until you approve this phone on your computer.')
        : h('div', { class: 'mobile-where' }, h('strong', {}, 'Pair once, inside the app'), h('p', { class: 'mut small' }, 'On the computer, scan the code shown in Pair phone inside Piper.')),
      h('div', { class: 'mobile-onboarding-actions' }, button('Use Piper in this browser instead', actions.browser)));
  } else if (state === 'unpaired') {
    const browser = mobileBrowserInstall();
    card.classList.add('mobile-onboarding');
    card.append(h('div', { class: 'mobile-scan-art', 'aria-hidden': 'true' }, h('span', {}, fi('grid'))),
      title(browser ? 'Pair this browser with Piper' : 'Connect to your computer', 'phone'),
      h('p', { class: 'mut' }, 'Scan the code shown on your computer. It’s the same code you scanned before.'),
      h('p', { class: 'mut small' }, 'No code on screen? On your computer open Settings › Mobile access › Pair phone.'),
      ...(browser ? [h('div', { class: 'mobile-where' }, h('strong', {}, 'Pairs only this browser'), h('p', { class: 'mut small' }, 'Each browser and the Home Screen app keep their own pairing. A pairing made here won’t carry over to the Home Screen app.'), button('Add to Home Screen instead', actions.install))] : []),
      h('div', { class: 'mobile-onboarding-actions' }, button('Scan pairing code', scan, true), photo(),
        h('p', { class: 'mut small' }, 'Camera is used only while you scan.')),
      h('details', { class: 'mobile-link-fallback' }, h('summary', {}, 'Paste a pairing link instead'), paste(true)));
  } else if (state === 'scan') {
    card.classList.add('mobile-onboarding', 'mobile-scanner');
    const video = h('video', { playsinline: true, muted: true, 'aria-label': 'Pairing QR camera preview' });
    video.muted = true;
    card.append(h('div', { class: 'row' }, button('Cancel', () => show('unpaired')), title('Scan pairing code', 'phone')),
      h('div', { class: 'mobile-camera' }, video, h('span', { class: 'mobile-camera-guide', 'aria-hidden': 'true' })),
      h('p', { role: 'status' }, 'Point at the code on your computer screen'), h('p', { class: 'mut small' }, 'Scanning starts automatically'),
      h('div', { class: 'mobile-onboarding-actions' }, photo(), h('p', { class: 'mut small' }, 'Keep the code on your computer screen while you scan.')));
    startScanner = () => { disposeScanner = startQRScanner(video, value => void acceptQR(value),
      kind => show(kind === 'denied' ? 'camera-denied' : kind === 'interrupted' ? 'camera-interrupted' : 'camera-unavailable')); };
  } else if (['camera-denied', 'camera-unavailable', 'camera-interrupted', 'invalid-qr', 'install-qr', 'photo-empty'].includes(state)) {
    const copy: Record<string, [string, string]> = {
      'camera-denied': ['Camera access is off', 'Allow camera access for Piper in this phone’s settings, then try again. You can also choose a photo of the code.'],
      'camera-unavailable': ['Camera isn’t available here', 'Choose a photo of the code instead, or close other apps using the camera and try again.'],
      'camera-interrupted': ['Scanning paused', 'The camera stopped when Piper went into the background. Tap Scan again when you’re ready.'],
      'invalid-qr': ['That’s not a pairing code', 'Scan the code shown under Pair in Settings › Mobile access › Pair phone on your computer.'],
      'install-qr': ['That’s the install code', 'Piper is already installed. On your computer, click Piper is installed to show the pairing code.'],
      'photo-empty': ['No pairing code found in that photo', 'Make sure the whole code is visible and sharp. Choose a photo under 20 MB.'],
    };
    const [heading, description] = copy[state]!;
    card.classList.add('mobile-onboarding');
    card.append(title(heading, 'phone'), h('p', { class: 'mut' }, description),
      h('div', { class: 'mobile-onboarding-actions' }, button(state === 'camera-denied' || state === 'camera-unavailable' ? 'Try again' : 'Scan again', scan, true), photo(), button('Back', () => show('unpaired'))));
  } else if (state === 'approve-wait') {
    card.classList.add('mobile-onboarding');
    card.append(h('p', { class: 'mut small' }, `Connecting to ${computer}`),
      title('Approve on your computer', 'phone'),
      h('p', { class: 'mut' }, 'Your computer shows a code. If it matches, click Approve there.'),
      h('div', { class: 'mobile-approval-number', 'aria-label': 'Code on your phone' }, actions.number ?? ''),
      h('p', { role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), ' Waiting for approval…'),
      h('p', { class: 'mut small' }, 'Keep Piper open. Nothing is shared until you approve.'),
      h('div', { class: 'mobile-onboarding-actions' }, button('Cancel', actions.cancel)));
  } else if (state === 'setup-ended') {
    const stopped = actions.reason === 'stopped', denied = actions.reason === 'denied';
    card.classList.add('mobile-onboarding');
    card.append(title(stopped ? 'Stopped for safety' : denied ? 'Not approved' : 'This code expired', 'phone'),
      h('p', { class: 'mut' }, stopped ? 'Another device used this code. Show a new code on your computer and scan it here.'
        : denied ? 'This phone wasn’t approved on your computer. To try again, show a new code there and scan it here.'
          : 'On your computer, click Show new code. Then scan it here.'),
      h('div', { class: 'mobile-onboarding-actions' }, button('Scan pairing code', scan, true), photo()));
  } else if (state === 'pair') {
    const browser = mobileBrowserInstall();
    card.append(title(browser ? `Pair this browser with ${computer}?` : `Pair with ${computer}?`), h('p', { class: 'mobile-verified mut' }, fi('check'), 'Computer verified · end-to-end encrypted'),
      browser && !browserOnly ? h('div', { class: 'mobile-where' }, h('strong', {}, 'Want the Home Screen app instead?'), h('p', { class: 'mut small' }, 'This pairs only this browser: the Home Screen app won’t get it. Tap Cancel, open Piper from the Home Screen and paste the link there.')) : document.createTextNode(''),
      h('div', { class: 'protected-callout' }, h('strong', {}, 'Full access to Piper'),
        h('p', {}, `${browser ? 'This browser' : 'This phone'} can do everything you can do in Piper on this computer:`),
        h('ul', {}, h('li', {}, 'start, stop and type into agent sessions and host terminals'),
          h('li', {}, 'read and change project files and databases'), h('li', {}, 'approve agent requests, commit and push')),
        h('p', { class: 'mut' }, 'Anyone who can unlock this phone gets the same access. Remove it on the computer in Settings › Mobile access.')),
      h('div', { class: 'row mobile-pair-actions' }, button('Cancel', actions.cancel), button('Pair with full access', actions.pair, true)));
  } else if (state === 'connecting' || state === 'pairing') {
    card.className = 'progress';
    const time = h('span', { class: 'startup-clock' }, '0:00');
    const start = Date.now();
    clock = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - start) / 1000);
      time.textContent = markBidi(`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`);
    }, 1000);
    const pairing = state === 'pairing';
    card.append(h('div', { class: 'startup-head' }, h('span', { class: 'spinner' }),
      h('div', { class: 'startup-heading' }, h('div', { class: 'startup-eyebrow' }, pairing ? 'PAIRING' : 'CONNECTING'), h('strong', {}, computer)), time),
      h('p', { class: 'startup-hint' }, pairing ? "Sending this phone's key. Keep this page open." : 'End-to-end encrypted through the relay. The relay cannot read your session.'),
      h('ol', { class: 'startup-steps' }, ...[pairing ? 'Computer verified' : 'Relay', pairing ? 'Phone added' : 'Computer verified', 'Piper']
        .map((name, i) => h('li', { class: 'step ' + (i === 0 ? 'done' : i === 1 ? 'now' : '') }, name))));
  } else if (state === 'expired') {
    card.classList.add('mobile-onboarding');
    card.append(title('This pairing code is no longer valid'), h('p', { class: 'mut' }, 'Codes last 60 seconds and work once. On the computer, click Generate new code, then scan again.'),
      h('div', { class: 'mobile-onboarding-actions' }, button('Scan again', scan, true), photo(), button('Back', () => show('unpaired'))),
      h('details', { class: 'mobile-link-fallback' }, h('summary', {}, 'Paste a pairing link instead'), paste(true)));
  } else if (state === 'revoked') {
    if (inGui) card.className = 'panel bad mobile-revoked';
    card.append(title('Access removed', 'alert'), h('p', {}, `This phone was removed in Settings › Mobile access on ${computer}. The connection is closed.`),
      h('p', { class: 'mut' }, 'To use Piper here again, pair it on the computer: Settings › Mobile access › Pair phone.'),
      h('div', { class: 'row' }, button(`Forget ${computer}`, actions.forget)));
  } else if (state === 'identity') {
    card.append(title('Not connected', 'alert'), h('div', { class: 'protected-callout' }, h('strong', {}, "Computer identity doesn't match"),
      h('p', {}, `The computer that answered is not the ${computer} this phone was paired with. Nothing was sent.`),
      h('p', { class: 'mut' }, 'If Piper was reinstalled or its identity was reset, forget this computer and pair again with a fresh link.')),
      h('div', { class: 'row' }, button(`Forget ${computer}`, actions.forget)));
  } else {
    let remaining = 8;
    const retry = h('span', { class: 'mut' }, 'Retrying in 0:08');
    clock = window.setInterval(() => {
      remaining -= 1;
      retry.textContent = markBidi(`Retrying in 0:${String(remaining).padStart(2, '0')}`);
      if (remaining <= 0) { window.clearInterval(clock); actions.retry?.(); }
    }, 1000);
    card.append(title(`Can't reach ${computer}`), h('p', { class: 'mut' }, 'The computer is asleep or offline, or Piper is not running on it. '
      + (paired ? 'This phone stays paired.' : 'Piper opens only on a phone paired from your computer.')),
      h('div', { class: 'row' }, retry, button('Retry now', actions.retry), actions.cancel ? button('Cancel', actions.cancel) : null));
  }
  card.setAttribute('aria-label', markBidi(card.querySelector('h2, strong')?.textContent ?? 'Mobile connection'));
  stage = h('div', { class: 'mobile-stage' + (inGui ? ' mobile-over-gui' : ''), 'data-state': state }, card);
  document.body.append(stage);
  card.focus({ preventScroll: true });
  startScanner?.();
}

let offlineActive = false;
let reconnectNotice = 0;
let offlineNotice = 0;
let offlineComputer = 'Piper';
let retryConnection: (() => Promise<void>) | undefined;

/** Mobile recovery: one delayed notice; verified transport + state dismiss it. */
export function mobileOffline(on: boolean, computer = 'Piper', retry?: () => Promise<void>): void {
  const app = document.getElementById('app');
  if (app) app.inert = on || stage !== null;
  // The desktop poll's old banner must never reappear over a recovered phone.
  document.getElementById('banner')?.remove();
  offlineComputer = computer; retryConnection = retry;
  if (!on) {
    offlineActive = false;
    window.clearTimeout(reconnectNotice); reconnectNotice = 0;
    window.clearTimeout(offlineNotice); offlineNotice = 0;
    document.getElementById('mobile-offline')?.remove();
    return;
  }
  if (document.hidden) {
    // Background time never turns the first foreground frame into a red outage.
    offlineActive = false;
    window.clearTimeout(reconnectNotice); reconnectNotice = 0;
    window.clearTimeout(offlineNotice); offlineNotice = 0;
    document.getElementById('mobile-offline')?.remove();
    return;
  }
  if (offlineActive) return;
  offlineActive = true;
  reconnectNotice = window.setTimeout(() => {
    reconnectNotice = 0;
    if (!offlineActive || document.hidden) return;
    const banner = h('div', { id: 'mobile-offline', 'data-state': 'reconnecting', role: 'status', 'aria-live': 'polite' },
      h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', {}, `Reconnecting to ${offlineComputer}…`));
    document.body.prepend(banner);
    offlineNotice = window.setTimeout(() => {
      offlineNotice = 0;
      if (!offlineActive || document.hidden) return;
      banner.dataset.state = 'offline';
      banner.setAttribute('role', 'alert');
      banner.removeAttribute('aria-live');
      const retry = h('button', { onclick: async () => {
        retry.disabled = true; retry.textContent = 'Retrying…';
        try { await retryConnection?.(); } catch { /* automatic recovery remains active */ }
        finally { retry.disabled = false; retry.textContent = 'Retry now'; }
      } }, 'Retry now');
      banner.replaceChildren(h('strong', {}, fi('alert'), `Can't reach ${offlineComputer}`),
        h('p', {}, 'Piper keeps retrying. Changes you sent just before may or may not have run; check them before repeating.'),
        h('div', { class: 'mobile-retry' }, retry));
    }, 13000);
  }, 2000);
}
