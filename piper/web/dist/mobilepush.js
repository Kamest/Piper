let rpc = null;
let computer = 'this computer';
let subscriptionId = '';
let companion;
let pushScope = '/';
let syncing = null;
let pushGeneration = 0;
const ENABLED = 'piper.mobile.pushEnabled';
const OFFER = 'piper.mobile.pushOffer';
let offerCompleted = false;
export function configurePush(value, name = 'this computer', other, scope = '/') {
    rpc = value;
    computer = name;
    companion = other;
    pushScope = scope;
}
export function mobilePushClient() { return rpc !== null; }
export function pushComputer() { return computer; }
export function pushSupport() {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    if (ios && !standalone)
        return 'install';
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window))
        return 'unsupported';
    if (Notification.permission === 'denied')
        return 'blocked';
    return null;
}
export async function prepareMobilePush() {
    if (pushSupport() === 'unsupported' || !('serviceWorker' in navigator))
        return;
    await navigator.serviceWorker.register('/service-worker.js', { scope: pushScope, updateViaCache: 'none' });
}
async function localSubscription(scope = pushScope) {
    if (!('serviceWorker' in navigator))
        return null;
    const registration = await navigator.serviceWorker.getRegistration(scope);
    // getRegistration returns the root worker when a narrower scope has not been installed yet.
    if (scope !== '/' && registration?.scope !== new URL(scope, location.origin).href)
        return null;
    return registration?.pushManager?.getSubscription() ?? null;
}
async function pushRegistration(scope) {
    const registration = await navigator.serviceWorker.register('/service-worker.js', { scope, updateViaCache: 'none' });
    let timer = 0;
    const activated = scope === '/' ? navigator.serviceWorker.ready : registration.active ? Promise.resolve(registration) : new Promise((resolve, reject) => {
        const worker = registration.installing ?? registration.waiting;
        if (!worker) {
            reject(new Error('Notifications could not start. Try again.'));
            return;
        }
        const changed = () => {
            if (worker.state === 'activated' || worker.state === 'redundant') {
                worker.removeEventListener('statechange', changed);
                if (worker.state === 'activated')
                    resolve(registration);
                else
                    reject(new Error('Notifications could not start. Try again.'));
            }
        };
        worker.addEventListener('statechange', changed);
        changed();
    });
    return Promise.race([activated, new Promise((_, reject) => {
            timer = window.setTimeout(() => reject(new Error('Notifications could not start. Try again.')), 8000);
        })]).finally(() => window.clearTimeout(timer));
}
/** Separate worker scopes retain each computer’s own VAPID-bound subscription. */
async function subscribeComputer(target, scope) {
    const cycle = pushGeneration;
    const current = () => { if (cycle !== pushGeneration)
        throw new Error('Notification choice changed.'); };
    const server = await target({ action: 'status' });
    current();
    if (!server.available || !server.publicKey)
        throw new Error('Notifications are unavailable. Try again when Piper is online.');
    const registration = await pushRegistration(scope);
    current();
    const key = keyBytes(server.publicKey);
    let sub = await registration.pushManager.getSubscription();
    current();
    const old = sub?.options.applicationServerKey;
    if (sub && old && (old.byteLength !== key.length || !new Uint8Array(old).every((byte, i) => byte === key[i]))) {
        await sub.unsubscribe();
        sub = null;
    }
    current();
    sub ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    if (cycle !== pushGeneration) {
        await sub.unsubscribe();
        current();
    }
    return (await target({ action: 'subscribe', subscription: sub.toJSON() })).subscriptionId ?? '';
}
/** Best-effort reconciliation retries an offline companion on the next minute/load. No permission prompt. */
export function syncMobilePush() {
    if (syncing)
        return syncing;
    syncing = (async () => {
        if (pushSupport() || Notification.permission !== 'granted')
            return;
        const cycle = pushGeneration;
        let enabled = null;
        try {
            enabled = localStorage.getItem(ENABLED);
        }
        catch { /* existing subscription is still consent */ }
        if (enabled === '0' || (enabled !== '1' && !await localSubscription('/') && !await localSubscription('/backup-push/')))
            return;
        if (cycle !== pushGeneration)
            return;
        const targets = [rpc ? subscribeComputer(rpc, pushScope).then(id => { if (cycle === pushGeneration)
                subscriptionId = id; }) : Promise.resolve(),
            companion ? subscribeComputer(companion.rpc, companion.scope).then(() => { }) : Promise.resolve()];
        await Promise.allSettled(targets);
    })().finally(() => { syncing = null; });
    return syncing;
}
export async function pushStatus() {
    const support = pushSupport();
    const subscribed = !!await localSubscription();
    if (support)
        return { state: support, subscribed, showTitles: false };
    if (!rpc)
        return { state: 'unavailable', subscribed, showTitles: false };
    const server = await rpc({ action: 'status' });
    subscriptionId = server.subscriptionId ?? '';
    return { state: !server.available ? 'unavailable' : server.enabled && subscribed && Notification.permission === 'granted' ? 'on' : 'off', subscribed, showTitles: server.showTitles === true };
}
/** Remember an explicit choice/showing, never turn a permission check into a prompt. */
export function completePushOffer() {
    offerCompleted = true;
    try {
        localStorage.setItem(OFFER, '1');
        return true;
    }
    catch {
        return false;
    }
}
export async function pushOffer() {
    if (!rpc || offerCompleted)
        return null;
    try {
        if (localStorage.getItem(OFFER))
            return null;
    }
    catch { /* same-page guard still works */ }
    const support = pushSupport();
    if (support === 'install')
        return 'install';
    if (support) {
        completePushOffer();
        return null;
    }
    try {
        const state = (await pushStatus()).state;
        if (state === 'on')
            completePushOffer();
        return state === 'off' ? 'off' : null;
    }
    catch {
        return null;
    }
}
export async function setPushTitles(showTitles) {
    if (!rpc)
        throw new Error('Notifications are unavailable.');
    const server = await rpc({ action: 'preferences', showTitles });
    subscriptionId = server.subscriptionId ?? '';
}
function keyBytes(encoded) {
    const raw = atob(encoded.replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
}
export async function enablePush() {
    const cycle = pushGeneration;
    const support = pushSupport();
    if (support || !rpc)
        throw new Error('Notifications are unavailable on this phone.');
    completePushOffer();
    // Safari requires this exact user gesture, before any network/registration await.
    const permission = await Notification.requestPermission();
    if (permission !== 'granted')
        throw new Error('Notifications weren’t allowed. Change permission in the phone’s settings.');
    if (cycle !== pushGeneration)
        throw new Error('Notification choice changed.');
    const id = await subscribeComputer(rpc, pushScope);
    if (cycle !== pushGeneration)
        throw new Error('Notification choice changed.');
    subscriptionId = id;
    try {
        localStorage.setItem(ENABLED, '1');
    }
    catch { /* the browser subscription still records consent */ }
    if (companion)
        void subscribeComputer(companion.rpc, companion.scope).catch(() => { });
    document.dispatchEvent(new CustomEvent('piper-push-changed'));
}
export async function forgetBackupPush() {
    pushGeneration++;
    companion = undefined;
    await (await localSubscription('/backup-push/'))?.unsubscribe();
}
export async function stopLocalPush() {
    pushGeneration++;
    try {
        localStorage.setItem(ENABLED, '0');
    }
    catch { /* unsubscribe remains available */ }
    await Promise.all(['/', '/backup-push/'].map(async (scope) => { await (await localSubscription(scope))?.unsubscribe(); }));
}
export async function disablePush() {
    pushGeneration++;
    completePushOffer();
    try {
        localStorage.setItem(ENABLED, '0');
    }
    catch { /* unsubscribe remains available */ }
    const expected = subscriptionId;
    const other = companion;
    const otherSub = other ? await localSubscription(other.scope) : null;
    if (other) {
        void (async () => {
            try {
                const status = await other.rpc({ action: 'status' });
                await other.rpc({ action: 'unsubscribe', subscriptionId: status.subscriptionId ?? '' });
            }
            catch { /* local unsubscribe prevents delivery while the companion is offline */ }
            finally {
                await otherSub?.unsubscribe();
            }
        })().catch(() => { });
    }
    const sub = await localSubscription();
    let changed = false;
    try {
        if (rpc) {
            const server = await rpc({ action: 'unsubscribe', subscriptionId: expected });
            subscriptionId = server.subscriptionId ?? '';
            changed = server.enabled;
            if (changed)
                throw new Error('Notification subscription changed. Refresh and try again.');
        }
    }
    finally {
        if (!changed)
            await sub?.unsubscribe();
        document.dispatchEvent(new CustomEvent('piper-push-changed'));
    }
}
export async function testPush() {
    if (!rpc || !(await rpc({ action: 'test' })).sent)
        throw new Error('Test notification couldn’t be sent. Try again when Piper is online.');
}
//# sourceMappingURL=mobilepush.js.map