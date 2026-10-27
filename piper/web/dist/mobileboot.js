// Standalone mobile entry point. The normal desktop GUI never imports this module.
import { createMobileConnection } from './mobileworkerclient.js';
import { notifyTransportReady } from './transport.js';
import { setToken } from './api.js';
import { h } from './dom.js';
import { exchange, mobileTransport, parsePairingLink, parseTicket } from './mobiletransport.js';
import { clearMobileState, installMobileViewport, mobileBrowserInstall, mobileOffline, showMobileState } from './mobileui.js';
import { installTransport } from './transport.js';
import { applyChoice } from './theme.js';
import { configurePush, prepareMobilePush, stopLocalPush } from './mobilepush.js';
const STORE = 'piper.mobile.profile';
let profile = null;
let ticket = null;
let active = null;
let launched = false;
let revoked = false;
let attemptController = null;
let recovering = false;
let tentative = false;
let generation = 0;
let transport = null;
let backgrounded = false;
let reconnectTimer = 0;
let reconnectAttempt = 0;
let healthTimer = 0;
function stopReconnect() {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = 0;
}
/** Retry only the authenticated connection, never a request or terminal input. */
function scheduleReconnect() {
    if (reconnectTimer || !launched || !profile?.paired || revoked || document.hidden)
        return;
    const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempt++, 4), 15000);
    reconnectTimer = window.setTimeout(() => {
        reconnectTimer = 0;
        if (!document.hidden && !revoked)
            void connect().catch(() => { });
    }, delay);
}
function retireConnection() {
    window.clearInterval(healthTimer);
    healthTimer = 0;
    const old = active;
    active = null;
    attemptController?.abort(new Error('Mobile connection replaced'));
    attemptController = null;
    void old?.then(c => c.close()).catch(() => { });
}
/** Resume with the same paired identity; pending mutations are cancelled, never replayed. */
function resume() {
    if (!launched || !profile?.paired || revoked || document.hidden || recovering)
        return;
    // Safari can focus and run GUI work before visibilitychange. Only this
    // coordinator releases the suspended generation for requests/terminals.
    backgrounded = false;
    recovering = true;
    stopReconnect();
    reconnectAttempt = 0;
    const cycle = ++generation;
    retireConnection();
    transport?.reset();
    mobileOffline(true);
    void connect().catch(() => { }).finally(() => { if (generation === cycle)
        recovering = false; });
}
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        backgrounded = true;
        stopReconnect();
        if (launched && profile?.paired && !revoked) {
            generation++;
            recovering = false;
            retireConnection();
            transport?.reset();
            mobileOffline(true);
        }
    }
    else if (backgrounded) {
        backgrounded = false;
        resume();
    }
});
window.addEventListener('pageshow', e => { if (e.persisted)
    resume(); });
window.addEventListener('online', resume);
function linkCode(value) { return parsePairingLink(value, location.origin, document.querySelector('meta[name="piper-mobile-relay"]')?.content ?? ''); }
function validLink(value) { try {
    linkCode(value);
    return true;
}
catch {
    return false;
} }
let browserOnly = false;
try {
    browserOnly = localStorage.getItem('piper.mobile.browserOnly') === '1';
}
catch { /* memory-only choice if storage is blocked */ }
function chooseBrowser(value) {
    browserOnly = value;
    try {
        if (value)
            localStorage.setItem('piper.mobile.browserOnly', '1');
        else
            localStorage.removeItem('piper.mobile.browserOnly');
    }
    catch { /* choice remains usable for this page */ }
    unpaired();
}
function unpaired() {
    if (mobileBrowserInstall() && !browserOnly) {
        showMobileState('install', 'Piper', { browser: () => chooseBrowser(true) });
    }
    else
        showMobileState('unpaired', 'Piper', { openLink, validLink, install: () => chooseBrowser(false) });
}
function expired() { showMobileState('expired', 'Piper', { openLink, validLink }); }
async function openLink(value) {
    try {
        const encoded = linkCode(value);
        stopReconnect();
        generation++;
        retireConnection();
        ticket = null;
        await boot(encoded);
        return null;
    }
    catch {
        return 'This isn’t a Piper pairing link. Copy it again on the computer.';
    }
}
function save(value) {
    // Refuse to grant access if this browser cannot retain the device key.
    if (!tentative)
        localStorage.setItem(STORE, JSON.stringify(value));
    profile = value;
}
function stored() {
    try {
        const value = JSON.parse(localStorage.getItem(STORE) ?? 'null');
        if (!value)
            return null;
        parseTicket(btoa(JSON.stringify({ v: value.v, endpoint: value.endpoint, relay: value.relay })).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_'));
        if (typeof value.key !== 'string' || atob(value.key).length !== 32 || typeof value.computer !== 'string' || typeof value.paired !== 'boolean')
            return null;
        return value;
    }
    catch {
        return null;
    }
}
async function forget() {
    document.getElementById('mobile-push-welcome')?.remove();
    await stopLocalPush().catch(() => { });
    stopReconnect();
    generation++;
    revoked = true;
    transport?.reset();
    retireConnection();
    localStorage.removeItem(STORE);
    // A reload also disposes the old GUI's timers and sockets.
    location.replace(location.pathname);
}
function removed() {
    document.getElementById('mobile-push-welcome')?.remove();
    void stopLocalPush().catch(() => { });
    stopReconnect();
    generation++;
    revoked = true;
    retireConnection();
    transport?.reset();
    document.querySelector('.mobile-paired-toast')?.remove();
    mobileOffline(false);
    showMobileState('revoked', profile?.computer, { forget: () => void forget() }, launched);
    throw new Error('This phone no longer has Piper access');
}
function guarded(raw) {
    const owner = active;
    let invalidated = false;
    let checking = null;
    const failed = () => {
        if (invalidated)
            return;
        invalidated = true;
        if (active === owner) {
            generation++;
            retireConnection();
            transport?.reset();
            if (launched && !revoked) {
                mobileOffline(true);
                scheduleReconnect();
            }
        }
        void raw.close().catch(() => { });
    };
    return {
        invalidate: failed,
        check() {
            if (invalidated || checking || active !== owner)
                return;
            // hello bypasses the local HTTP server. One coalesced probe distinguishes
            // a slow API response from a stalled connection, without replaying writes.
            checking = (async () => {
                try {
                    const response = await exchange(raw, { op: 'hello' }, undefined, AbortSignal.timeout(5000));
                    // A full request budget is an answered probe, not a dead transport.
                    if (response.status === 503)
                        return;
                    if (!response.ok || (await response.json()).paired !== true)
                        throw new Error('Mobile peer unavailable');
                }
                catch {
                    failed();
                }
                finally {
                    checking = null;
                }
            })();
        },
        async open() {
            let stream;
            try {
                stream = await raw.open();
            }
            catch (e) {
                failed();
                throw e;
            }
            // QUIC streams are independent: a terminal ending/resetting does not end its connection.
            return stream;
        },
        close: () => raw.close(),
    };
}
function startHealth(connection) {
    window.clearInterval(healthTimer);
    healthTimer = 0;
    if (!launched || !profile?.paired || revoked || document.hidden)
        return;
    healthTimer = window.setInterval(() => connection.check?.(), 5000);
}
async function openConnection(signal) {
    if (!profile || revoked)
        throw new Error('Phone is not paired');
    const attemptGeneration = generation;
    const relay = document.querySelector('meta[name="piper-mobile-relay"]')?.content;
    if (new URL(profile.relay).origin !== relay)
        throw new Error('Mobile link uses a different relay');
    signal.throwIfAborted();
    const key = Uint8Array.from(atob(profile.key), (c) => c.charCodeAt(0));
    let connection;
    try {
        connection = await createMobileConnection(profile.endpoint, profile.relay, key, signal);
    }
    finally {
        key.fill(0);
    }
    try {
        if (attemptGeneration !== generation)
            throw new Error('Mobile connection replaced');
        const hello = await exchange(connection, { op: 'hello' }, undefined, signal);
        if (!hello.ok)
            throw new Error('Computer not reachable');
        const value = await hello.json();
        if (attemptGeneration !== generation)
            throw new Error('Mobile connection replaced');
        if (!value || typeof value !== 'object' || !('computer' in value) || typeof value.computer !== 'string'
            || value.computer.length > 80 || !('paired' in value) || typeof value.paired !== 'boolean')
            throw new Error('Invalid computer response');
        const wasPaired = profile.paired;
        // Only the pinned computer can prove this key is already paired. Until then,
        // a link must not replace a stored key or relay capability.
        if (value.paired)
            tentative = false;
        save({ ...profile, computer: value.computer, paired: value.paired });
        if (!value.paired && (!ticket?.code || launched)) {
            if (wasPaired || launched)
                removed();
            revoked = true;
            unpaired();
            throw new Error('Phone is not paired');
        }
        stopReconnect();
        reconnectAttempt = 0;
        mobileOffline(false);
        notifyTransportReady();
        const result = guarded(connection);
        startHealth(result);
        return result;
    }
    catch (error) {
        void connection.close().catch(() => { });
        throw error;
    }
}
function connect() {
    // GUI polling can arrive after visibility handlers retired the endpoint.
    // Gate the shared entry point as well as explicit recovery/heartbeat paths.
    if (launched && (document.hidden || backgrounded))
        return Promise.reject(new Error('Mobile connection suspended'));
    if (!active) {
        const controller = new AbortController();
        attemptController = controller;
        const timer = window.setTimeout(() => controller.abort(new Error('Computer not reachable')), 15000);
        const attempt = openConnection(controller.signal);
        active = attempt;
        void attempt.then(() => window.clearTimeout(timer), () => window.clearTimeout(timer));
        void attempt.catch(() => {
            if (active !== attempt)
                return;
            active = null;
            if (launched && !revoked) {
                mobileOffline(true);
                scheduleReconnect();
            }
        });
    }
    return active;
}
async function launch(pairedNow = false) {
    if (!profile)
        return;
    transport?.reset();
    const currentTransport = mobileTransport(connect);
    transport = currentTransport;
    installTransport({ ...currentTransport, async fetch(path, init) {
            const response = await currentTransport.fetch(path, init);
            if (response.status === 401) {
                // A local bridge credential problem must never ask the phone for a desktop bearer.
                throw new Error('Piper bridge authentication failed. Restart the mobile transport on the computer.');
            }
            return response;
        } });
    // A UI marker only; the real bearer exists exclusively on the computer's loopback bridge.
    setToken('iroh-' + profile.endpoint);
    configurePush(async (value) => {
        if (!profile?.paired || revoked)
            throw new Error('This phone no longer has Piper access');
        const connection = { async open() { return (await connect()).open(); }, async close() { } };
        const response = await exchange(connection, { op: 'push', ...value }, new Uint8Array(), AbortSignal.timeout(12000));
        if (!response.ok)
            throw new Error('Notifications are unavailable. Try again.');
        return response.json();
    }, profile.computer);
    void prepareMobilePush().catch(() => { });
    clearMobileState();
    launched = true;
    void connect().then(startHealth).catch(() => { });
    await import('./main.js');
    if (pairedNow) {
        const toast = h('div', { class: 'toast mobile-paired-toast', role: 'status' }, h('strong', {}, `Paired with ${profile.computer}`), h('p', {}, 'This phone has full Piper access. Remove it on the computer in Settings › Mobile access.'));
        toast.addEventListener('click', () => toast.remove());
        document.body.append(toast);
        window.setTimeout(() => toast.remove(), 6000);
    }
}
async function pair() {
    if (!profile || !ticket?.code)
        return;
    showMobileState('pairing', profile.computer);
    // Never automatically retry pairing or any API mutation after a lost response.
    try {
        const response = await exchange(await connect(), { op: 'pair', code: ticket.code }, undefined, AbortSignal.timeout(8000));
        ticket = null;
        if (!response.ok) {
            expired();
            return;
        }
        tentative = false;
        save({ ...profile, paired: true });
        await launch(true);
    }
    catch {
        ticket = null;
        retireConnection();
        showMobileState('offline', profile.computer, { retry: () => void start() }, false, profile.paired);
    }
}
async function start() {
    if (!profile) {
        unpaired();
        return;
    }
    revoked = false;
    showMobileState('connecting', profile.computer);
    try {
        await connect();
        if (profile.paired) {
            ticket = null;
            await launch();
            return;
        }
        showMobileState('pair', profile.computer, {
            pair: () => void pair(),
            cancel: () => { generation++; ticket = null; retireConnection(); unpaired(); },
        }, false, true, browserOnly);
    }
    catch {
        if (revoked)
            return;
        active = null;
        showMobileState('offline', profile.computer, { retry: () => void start() }, false, profile.paired);
    }
}
async function boot(pasted) {
    installMobileViewport();
    applyChoice(window.__PIPER__?.themes, 'auto');
    const encoded = pasted ?? new URLSearchParams(location.hash.slice(1)).get('iroh');
    history.replaceState(null, '', location.pathname);
    try {
        profile = stored();
        if (encoded) {
            ticket = parseTicket(encoded);
            const relay = document.querySelector('meta[name="piper-mobile-relay"]')?.content;
            if (new URL(ticket.relay).origin !== relay)
                throw new Error('Mobile link uses a different relay');
            if (profile?.paired && profile.endpoint !== ticket.endpoint) {
                showMobileState('identity', profile.computer, { forget: () => void forget() });
                return;
            }
            tentative = true;
            if (!profile || profile.endpoint !== ticket.endpoint) {
                const key = crypto.getRandomValues(new Uint8Array(32));
                save({ v: 1, endpoint: ticket.endpoint, relay: ticket.relay, key: btoa(String.fromCharCode(...key)), computer: 'Piper', paired: false });
                key.fill(0);
            }
            else if (profile.relay !== ticket.relay)
                save({ ...profile, relay: ticket.relay });
        }
        await start();
    }
    catch {
        expired();
    }
}
void boot();
//# sourceMappingURL=mobileboot.js.map