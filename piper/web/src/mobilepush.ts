/** Web Push controls only; private subscriptions travel over the authenticated mobile transport. */
export interface PushServerStatus { available: boolean; enabled: boolean; publicKey: string; showTitles?: boolean; subscriptionId?: string }
export type PushRPC = (value: { action: string; subscription?: PushSubscriptionJSON; showTitles?: boolean; subscriptionId?: string }) => Promise<PushServerStatus & { sent?: boolean }>;
export type PushState = 'off' | 'on' | 'blocked' | 'unsupported' | 'install' | 'unavailable';
export interface PushStatus { state: PushState; subscribed: boolean; showTitles: boolean }
let rpc: PushRPC | null = null;
let computer = 'this computer';
let subscriptionId = '';
const OFFER = 'piper.mobile.pushOffer';
let offerCompleted = false;
export function configurePush(value: PushRPC, name = 'this computer'): void { rpc = value; computer = name; }
export function mobilePushClient(): boolean { return rpc !== null; }
export function pushComputer(): string { return computer; }

export function pushSupport(): PushState | null {
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = matchMedia('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone;
  if (ios && !standalone) return 'install';
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return 'unsupported';
  if (Notification.permission === 'denied') return 'blocked';
  return null;
}

export async function prepareMobilePush(): Promise<void> {
  if (pushSupport() === 'unsupported' || !('serviceWorker' in navigator)) return;
  await navigator.serviceWorker.register('/service-worker.js', { scope: '/', updateViaCache: 'none' });
}

async function localSubscription(): Promise<PushSubscription | null> {
  if (!('serviceWorker' in navigator)) return null;
  return (await navigator.serviceWorker.getRegistration('/'))?.pushManager?.getSubscription() ?? null;
}

export async function pushStatus(): Promise<PushStatus> {
  const support = pushSupport();
  const subscribed = !!await localSubscription();
  if (support) return { state: support, subscribed, showTitles: false };
  if (!rpc) return { state: 'unavailable', subscribed, showTitles: false };
  const server = await rpc({ action: 'status' });
  subscriptionId = server.subscriptionId ?? '';
  return { state: !server.available ? 'unavailable' : server.enabled && subscribed && Notification.permission === 'granted' ? 'on' : 'off', subscribed, showTitles: server.showTitles === true };
}

/** Remember an explicit choice/showing, never turn a permission check into a prompt. */
export function completePushOffer(): boolean {
  offerCompleted = true;
  try { localStorage.setItem(OFFER, '1'); return true; } catch { return false; }
}

export async function pushOffer(): Promise<'off' | 'install' | null> {
  if (!rpc || offerCompleted) return null;
  try { if (localStorage.getItem(OFFER)) return null; } catch { /* same-page guard still works */ }
  const support = pushSupport();
  if (support === 'install') return 'install';
  if (support) { completePushOffer(); return null; }
  try {
    const state = (await pushStatus()).state;
    if (state === 'on') completePushOffer();
    return state === 'off' ? 'off' : null;
  } catch { return null; }
}

export async function setPushTitles(showTitles: boolean): Promise<void> {
  if (!rpc) throw new Error('Notifications are unavailable.');
  const server = await rpc({ action: 'preferences', showTitles });
  subscriptionId = server.subscriptionId ?? '';
}

function keyBytes(encoded: string): Uint8Array<ArrayBuffer> {
  const raw = atob(encoded.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

export async function enablePush(): Promise<void> {
  const support = pushSupport();
  if (support || !rpc) throw new Error('Notifications are unavailable on this phone.');
  completePushOffer();
  // Safari requires this exact user gesture, before any network/registration await.
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') throw new Error('Notifications weren’t allowed. Change permission in the phone’s settings.');
  const server = await rpc({ action: 'status' });
  if (!server.available || !server.publicKey) throw new Error('Notifications are unavailable. Try again when Piper is online.');
  await prepareMobilePush();
  let timer = 0;
  const ready = await Promise.race([navigator.serviceWorker.ready, new Promise<never>((_, reject) => {
    timer = window.setTimeout(() => reject(new Error('Notifications could not start. Try again.')), 8000);
  })]).finally(() => window.clearTimeout(timer));
  const key = keyBytes(server.publicKey);
  let sub = await ready.pushManager.getSubscription();
  const old = sub?.options.applicationServerKey;
  if (sub && old && (old.byteLength !== key.length || !new Uint8Array(old).every((byte, i) => byte === key[i]))) { await sub.unsubscribe(); sub = null; }
  sub ??= await ready.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  const installed = await rpc({ action: 'subscribe', subscription: sub.toJSON() });
  subscriptionId = installed.subscriptionId ?? '';
  document.dispatchEvent(new CustomEvent('piper-push-changed'));
}

export async function stopLocalPush(): Promise<void> { await (await localSubscription())?.unsubscribe(); }
export async function disablePush(): Promise<void> {
  completePushOffer();
  const expected = subscriptionId;
  const sub = await localSubscription();
  let changed = false;
  try {
    if (rpc) {
      const server = await rpc({ action: 'unsubscribe', subscriptionId: expected });
      subscriptionId = server.subscriptionId ?? '';
      changed = server.enabled;
      if (changed) throw new Error('Notification subscription changed. Refresh and try again.');
    }
  } finally {
    if (!changed) await sub?.unsubscribe();
    document.dispatchEvent(new CustomEvent('piper-push-changed'));
  }
}
export async function testPush(): Promise<void> {
  if (!rpc || !(await rpc({ action: 'test' })).sent) throw new Error('Test notification couldn’t be sent. Try again when Piper is online.');
}
