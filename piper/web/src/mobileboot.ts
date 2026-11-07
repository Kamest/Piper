// Standalone mobile entry point. The normal desktop GUI never imports this module.
import { createMobileConnection } from './mobileworkerclient.js';
import { notifyTransportReady, notifyTransportUnavailable } from './transport.js';
import { setToken } from './api.js';
import { h } from './dom.js';
import { exchange, mobileTransport, parsePairingLink, parseTicket, parseSetupCookie, type QuicConnection, type QuicStream, type Ticket } from './mobiletransport.js';
import { clearMobileState, installMobileViewport, mobileBrowserInstall, mobileOffline, showMobileBackup, showMobileState } from './mobileui.js';
import { configureMobileComputers, installTransport } from './transport.js';
import { applyChoice } from './theme.js';
import { configurePush, prepareMobilePush, stopLocalPush, syncMobilePush, forgetBackupPush, type PushServerStatus } from './mobilepush.js';

interface Computer { endpoint: string; relay: string; key: string; computer: string; paired: boolean }
interface Profile { v: 2; primary: Computer; fallback?: Computer }
const STORE = 'piper.mobile.profile';
let profile: Computer | null = null;
let profiles: Profile | null = null;
const ACTIVE_STORE = 'piper.mobile.active';
let activeProfile: 'primary' | 'fallback' = 'primary';
let pairingBackup = false;
let backupConfirmed = false;
let switching = false;
let primaryProbe = 0;
let probeRunning = false;
let fallbackTimer = 0;
let waitingPrimary = false;
let ticket: Ticket | null = null;
let active: Promise<QuicConnection> | null = null;
let launched = false;
let revoked = false;
let attemptController: AbortController | null = null;
let recovering = false;
let tentative = false;
let generation = 0;
let transport: ReturnType<typeof mobileTransport> | null = null;
let backgrounded = false;
let reconnectTimer = 0;
let reconnectAttempt = 0;
let healthTimer = 0;
let transportConnected = false;
let stateAnswered = false;
const SETUP_STORE = 'piper.mobile.setup';
let setup: Ticket | null = null;
let setupTimer = 0;
let setupController: AbortController | null = null;
let setupRunning: number | null = null;
let setupNumber = '';
let setupExpires = 0;

function connectionStatus(): void {
  if (!launched || revoked || switching) return;
  publishComputers();
  mobileOffline(!transportConnected || !stateAnswered, profile?.computer, async () => {
    resume();
    await active?.catch(() => {});
  }, activeProfile === 'primary' && profiles?.fallback ? { computer: profiles.fallback.computer, use: () => switchProfile('fallback') } : undefined);
}

function stopReconnect(): void {
  window.clearTimeout(reconnectTimer); reconnectTimer = 0;
}
/** Retry only the authenticated connection, never a request or terminal input. */
function scheduleReconnect(): void {
  if (reconnectTimer || !launched || !profile?.paired || revoked || document.hidden) return;
  const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempt++, 4), 15000);
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = 0;
    if (!document.hidden && !revoked) void connect().catch(() => {});
  }, delay);
}

function retireConnection(): void {
  transportConnected = false; stateAnswered = false;
  notifyTransportUnavailable();
  window.clearInterval(healthTimer); healthTimer = 0;
  const old = active; active = null;
  attemptController?.abort(new Error('Mobile connection replaced'));
  attemptController = null;
  void old?.then(c => c.close()).catch(() => {});
}

/** Resume with the same paired identity; pending mutations are cancelled, never replayed. */
function resume(): void {
  if (setup && !launched) { void requestSetup(); return; }
  if (!launched || !profile?.paired || revoked || document.hidden || recovering) return;
  // Safari can focus and run GUI work before visibilitychange. Only this
  // coordinator releases the suspended generation for requests/terminals.
  backgrounded = false;
  recovering = true;
  stopReconnect(); reconnectAttempt = 0;
  const cycle = ++generation;
  retireConnection();
  transport?.reset();
  connectionStatus();
  void connect().catch(() => {}).finally(() => { if (generation === cycle) recovering = false; });
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    backgrounded = true; stopReconnect();
    if (setup) { generation++; window.clearTimeout(setupTimer); setupController?.abort(); }
    if (launched && profile?.paired && !revoked) {
      generation++; recovering = false; retireConnection(); transport?.reset(); mobileOffline(true);
    }
  }
  else if (backgrounded) { backgrounded = false; resume(); }
});
window.addEventListener('pageshow', e => { if ((e as PageTransitionEvent).persisted) resume(); });
window.addEventListener('online', resume);

function linkCode(value: string): string { return parsePairingLink(value, location.origin, document.querySelector<HTMLMetaElement>('meta[name="piper-mobile-relay"]')?.content ?? ''); }
function validLink(value: string): boolean { try { linkCode(value); return true; } catch { return false; } }
let browserOnly = false;
let browserInvitation: string | null = null;
try { browserOnly = localStorage.getItem('piper.mobile.browserOnly') === '1'; } catch { /* memory-only choice if storage is blocked */ }
function chooseBrowser(value: boolean): void {
  browserOnly = value;
  try { if (value) localStorage.setItem('piper.mobile.browserOnly', '1'); else localStorage.removeItem('piper.mobile.browserOnly'); } catch { /* choice remains usable for this page */ }
  if (value && browserInvitation) {
    const encoded = browserInvitation; browserInvitation = null;
    void boot(encoded);
  } else unpaired();
}
function unpaired(): void {
  if (mobileBrowserInstall() && !browserOnly) {
    showMobileState('install', 'Piper', { browser: () => chooseBrowser(true), setup: !!browserInvitation && !!parseTicket(browserInvitation).setup });
  } else showMobileState('unpaired', 'Piper', { openLink, validLink, install: () => chooseBrowser(false) });
}
function expired(): void { showMobileState('expired', 'Piper', { openLink, validLink }); }
async function openLink(value: string): Promise<string | null> {
  try {
    const encoded = linkCode(value);
    stopReconnect();
    generation++;
    if (setup) clearSetup();
    retireConnection();
    ticket = null;
    await boot(encoded);
    return null;
  } catch { return 'This isn’t a Piper pairing link. Copy it again on the computer.'; }
}

/** Persist one identity without replacing the other computer or retaining invitations. */
function save(value: Computer): void {
  const next: Profile = profiles ? { ...profiles } : { v: 2, primary: value };
  if (pairingBackup) {
    if (value.paired) next.fallback = value;
  } else next[activeProfile] = value;
  // A candidate backup must never displace the primary before explicit pairing.
  if (!tentative && (!pairingBackup || value.paired)) localStorage.setItem(STORE, JSON.stringify(next));
  if (!tentative && (!pairingBackup || value.paired)) profiles = next;
  profile = value;
}
function validComputer(value: unknown): Computer | null {
  try {
    if (!value || typeof value !== 'object') return null;
    const c = value as Computer;
    parseTicket(btoa(JSON.stringify({ v: 1, endpoint: c.endpoint, relay: c.relay })).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_'));
    if (typeof c.key !== 'string' || !/^[A-Za-z0-9+/]{43}=$/.test(c.key) || atob(c.key).length !== 32
        || typeof c.computer !== 'string' || c.computer.length > 80 || typeof c.paired !== 'boolean') return null;
    return { endpoint: c.endpoint, relay: c.relay, key: c.key, computer: c.computer, paired: c.paired };
  } catch { return null; }
}
function stored(): Profile | null {
  try {
    const value = JSON.parse(localStorage.getItem(STORE) ?? 'null');
    if (!value || ![1, 2].includes(value.v)) return null;
    const primary = validComputer(value.v === 1 ? value : value.primary);
    if (!primary) return null;
    const fallback = value.v === 2 ? validComputer(value.fallback) : null;
    return { v: 2, primary, ...(primary.paired && fallback?.paired && fallback.endpoint !== primary.endpoint ? { fallback } : {}) };
  } catch { return null; }
}

/** A full reload disposes GUI polls, cards, sockets and callbacks before changing identity. */
function switchProfile(target: 'primary' | 'fallback', fallingBack = false): void {
  if (switching || (target === 'fallback' && !profiles?.fallback)) return;
  sessionStorage.setItem(ACTIVE_STORE, target);
  if (fallingBack) sessionStorage.setItem('piper.mobile.fallingBack', '1');
  else if (activeProfile === 'fallback' && target === 'primary') sessionStorage.setItem('piper.mobile.fallingBack', 'wait');
  else sessionStorage.removeItem('piper.mobile.fallingBack');
  switching = true; generation++; stopReconnect();
  window.clearTimeout(fallbackTimer); window.clearInterval(primaryProbe);
  clearSetup(); transport?.reset(); retireConnection(); mobileOffline(false);
  location.replace(location.pathname);
}
function forgetBackup(): void {
  if (!profiles) return;
  const next: Profile = { v: 2, primary: profiles.primary };
  localStorage.setItem(STORE, JSON.stringify(next)); profiles = next;
  window.clearInterval(primaryProbe); primaryProbe = 0;
  void forgetBackupPush().catch(() => {});
  publishComputers();
}
function publishComputers(): void {
  if (!profiles) return;
  configureMobileComputers({ primary: profiles.primary.computer, fallback: profiles.fallback?.computer ?? null,
    limited: activeProfile === 'fallback', connected: transportConnected && stateAnswered, forgetBackup, scanBackup });
}
/** A valid code restarts boot with it: the paired primary profile then leads to the pair-backup confirmation. */
function scanBackup(): void {
  showMobileState('scan', 'Piper', { backup: true, validLink, cancel: clearMobileState,
    openLink: async value => {
      let encoded: string;
      try { encoded = linkCode(value); } catch { return 'This isn’t a Piper pairing link.'; }
      location.hash = 'iroh=' + encoded; location.reload(); return null;
    } });
}

/** One short hello per minute, never a GUI connection or an automatic switch. */
async function probePrimary(): Promise<void> {
  if (probeRunning || document.hidden || switching || revoked || activeProfile !== 'fallback' || !profiles) return;
  probeRunning = true;
  const cycle = generation;
  try {
    const value = await computerRequest<{ paired: boolean }>(profiles.primary, { op: 'hello' });
    if (cycle !== generation || switching || revoked) return;
    showMobileBackup(profiles.primary.computer, profile!.computer, value.paired === true, () => switchProfile('primary'));
  } catch { if (cycle === generation && !switching && !revoked) showMobileBackup(profiles.primary.computer, profile!.computer, false, () => switchProfile('primary')); }
  finally { probeRunning = false; }
}
async function computerRequest<T>(computer: Computer, value: Record<string, unknown>): Promise<T> {
  const relay = document.querySelector<HTMLMetaElement>('meta[name="piper-mobile-relay"]')?.content;
  if (new URL(computer.relay).origin !== relay) throw new Error('Mobile link uses a different relay');
  const key = Uint8Array.from(atob(computer.key), c => c.charCodeAt(0));
  let connection: QuicConnection | null = null;
  try {
    const signal = AbortSignal.timeout(8000);
    connection = await createMobileConnection(computer.endpoint, computer.relay, key, signal);
    const hello = await exchange(connection, { op: 'hello' }, undefined, signal);
    if (!hello.ok) throw new Error('Computer not reachable');
    const identity = await hello.json();
    if (identity.paired !== true) throw new Error('Phone is not paired');
    if (value.op === 'hello') return identity;
    const response = await exchange(connection, value, new Uint8Array(), signal);
    if (!response.ok) throw new Error('Notifications are unavailable. Try again.');
    return response.json();
  } finally { key.fill(0); await connection?.close().catch(() => {}); }
}
async function forget(): Promise<void> {
  document.getElementById('mobile-push-welcome')?.remove();
  await stopLocalPush().catch(() => {});
  stopReconnect();
  generation++;
  clearSetup();
  revoked = true;
  transport?.reset();
  retireConnection();
  sessionStorage.removeItem(ACTIVE_STORE);
  localStorage.removeItem(STORE);
  // A reload also disposes the old GUI's timers and sockets.
  location.replace(location.pathname);
}
function removed(): never {
  document.getElementById('mobile-push-welcome')?.remove();
  void stopLocalPush().catch(() => {});
  stopReconnect();
  generation++;
  revoked = true;
  window.clearTimeout(fallbackTimer); window.clearInterval(primaryProbe);
  retireConnection();
  transport?.reset();
  document.querySelector('.mobile-paired-toast')?.remove();
  mobileOffline(false);
  showMobileState('revoked', profile?.computer, { forget: () => void forget() }, launched);
  throw new Error('This phone no longer has Piper access');
}

function guarded(raw: QuicConnection): QuicConnection {
  const owner = active;
  let invalidated = false;
  let checking: Promise<void> | null = null;
  let received = 0;
  let missedProbes = 0;
  const failed = () => {
    if (invalidated) return;
    invalidated = true;
    if (active === owner) {
      generation++;
      retireConnection();
      transport?.reset();
      if (launched && !revoked) { connectionStatus(); scheduleReconnect(); }
    }
    void raw.close().catch(() => {});
  };
  return {
    invalidate: failed,
    check() {
      if (invalidated || checking || active !== owner) return;
      // hello bypasses the local HTTP server. One coalesced probe distinguishes
      // a slow API response from a stalled connection, without replaying writes.
      const before = received;
      checking = (async () => {
        try {
          const response = await exchange(raw, { op: 'hello' }, undefined, AbortSignal.timeout(5000));
          // A full request budget is an answered probe, not a dead transport.
          if (response.status === 503) { missedProbes = 0; return; }
          if (!response.ok || (await response.json()).paired !== true) failed();
          else missedProbes = 0;
        } catch (error) {
          // Fresh decrypted stream data disproves a dead connection even when
          // the independent probe is delayed. Never interrupt a working terminal.
          if (received !== before) missedProbes = 0;
          else if (!(error && typeof error === 'object' && 'name' in error && error.name === 'TimeoutError') || ++missedProbes >= 2) failed();
        }
        finally { checking = null; }
      })();
    },
    async open() {
      let stream: QuicStream;
      try { stream = await raw.open(); } catch (e) { failed(); throw e; }
      // QUIC streams are independent: a terminal ending/resetting does not end its connection.
      return {
        ...stream,
        async read(count) {
          const data = await stream.read(count);
          if (data.byteLength) { received++; missedProbes = 0; }
          return data;
        },
        async read_to_end() {
          const data = await stream.read_to_end();
          if (data.byteLength) { received++; missedProbes = 0; }
          return data;
        },
      };
    },
    close: () => raw.close(),
  };
}

function startHealth(connection: QuicConnection): void {
  window.clearInterval(healthTimer); healthTimer = 0;
  if (!launched || !profile?.paired || revoked || document.hidden) return;
  healthTimer = window.setInterval(() => connection.check?.(), 5000);
}

async function openConnection(signal: AbortSignal): Promise<QuicConnection> {
  if (!profile || revoked) throw new Error('Phone is not paired');
  const attemptGeneration = generation;
  const relay = document.querySelector<HTMLMetaElement>('meta[name="piper-mobile-relay"]')?.content;
  if (new URL(profile.relay).origin !== relay) throw new Error('Mobile link uses a different relay');
  signal.throwIfAborted();
  const key = Uint8Array.from(atob(profile.key), (c) => c.charCodeAt(0));
  let connection: QuicConnection;
  try { connection = await createMobileConnection(profile.endpoint, profile.relay, key, signal); }
  finally { key.fill(0); }
  try {
    if (attemptGeneration !== generation) throw new Error('Mobile connection replaced');
    const hello = await exchange(connection, { op: 'hello' }, undefined, signal);
    if (!hello.ok) throw new Error('Computer not reachable');
    const value: unknown = await hello.json();
    if (attemptGeneration !== generation) throw new Error('Mobile connection replaced');
    if (!value || typeof value !== 'object' || !('computer' in value) || typeof value.computer !== 'string'
      || value.computer.length > 80 || !('paired' in value) || typeof value.paired !== 'boolean') throw new Error('Invalid computer response');
    const wasPaired = profile.paired;
    // Only the pinned computer can prove this key is already paired. Until then,
    // a link must not replace a stored key or relay capability.
    if (value.paired && (!pairingBackup || backupConfirmed)) tentative = false;
    save({ ...profile, computer: value.computer, paired: value.paired });
    if (!value.paired && ((!ticket?.code && !(pairingBackup && ticket?.setup)) || launched)) {
      if (pairingBackup) throw new Error('Backup pairing was not confirmed');
      if (wasPaired || launched) removed();
      revoked = true;
      unpaired();
      throw new Error('Phone is not paired');
    }
    stopReconnect(); reconnectAttempt = 0;
    transportConnected = true; stateAnswered = false;
    connectionStatus();
    notifyTransportReady();
    const result = guarded(connection);
    startHealth(result);
    return result;
  } catch (error) { void connection.close().catch(() => {}); throw error; }
}

function connect(): Promise<QuicConnection> {
  // GUI polling can arrive after visibility handlers retired the endpoint.
  // Gate the shared entry point as well as explicit recovery/heartbeat paths.
  if (launched && (document.hidden || backgrounded)) return Promise.reject(new Error('Mobile connection suspended'));
  if (!active) {
    const controller = new AbortController();
    attemptController = controller;
    const timer = window.setTimeout(() => controller.abort(new Error('Computer not reachable')), !launched && activeProfile === 'primary' && profiles?.fallback && !pairingBackup ? 10000 : 15000);
    const attempt = openConnection(controller.signal);
    active = attempt;
    void attempt.then(() => window.clearTimeout(timer), () => window.clearTimeout(timer));
    void attempt.catch(() => {
      if (active !== attempt) return;
      active = null;
      if (launched && !revoked) { connectionStatus(); scheduleReconnect(); }
    });
  }
  return active;
}

async function launch(pairedNow = false): Promise<void> {
  if (!profile) return;
  if (pairingBackup) { switchProfile('primary'); return; }
  window.clearTimeout(fallbackTimer);
  sessionStorage.removeItem('piper.mobile.fallingBack');
  publishComputers();
  transport?.reset();
  const currentTransport = mobileTransport(connect);
  transport = currentTransport;
  installTransport({ ...currentTransport, stateChanged(connected) {
    stateAnswered = connected;
    connectionStatus();
  }, async fetch(path, init) {
    const response = await currentTransport.fetch(path, init);
    if (response.status === 401) {
      // A local bridge credential problem must never ask the phone for a desktop bearer.
      throw new Error('Piper bridge authentication failed. Restart the mobile transport on the computer.');
    }
    return response;
  } });
  // A UI marker only; the real bearer exists exclusively on the computer's loopback bridge.
  setToken('iroh-' + profile.endpoint);
  configurePush(async value => {
    if (!profile?.paired || revoked) throw new Error('This phone no longer has Piper access');
    const connection = { async open() { return (await connect()).open(); }, async close() {} };
    const response = await exchange(connection, { op: 'push', ...value }, new Uint8Array(), AbortSignal.timeout(12000));
    if (!response.ok) throw new Error('Notifications are unavailable. Try again.');
    return response.json();
  }, profile.computer, profiles?.fallback ? {
    rpc: value => computerRequest<PushServerStatus & { sent?: boolean }>(activeProfile === 'primary' ? profiles!.fallback! : profiles!.primary, { op: 'push', ...value }),
    scope: activeProfile === 'primary' ? '/backup-push/' : '/',
  } : undefined, activeProfile === 'fallback' ? '/backup-push/' : '/');
  void syncMobilePush().catch(() => {});
  void prepareMobilePush().catch(() => {});
  clearMobileState();
  launched = true;
  void connect().then(startHealth).catch(() => {});
  if (activeProfile === 'fallback' && profiles) {
    showMobileBackup(profiles.primary.computer, profile.computer, false, () => switchProfile('primary'));
  }
  if (profiles?.fallback) primaryProbe = window.setInterval(() => {
    if (document.hidden || revoked || switching) return;
    void probePrimary(); void syncMobilePush().catch(() => {});
  }, 60000);
  await import('./main.js');
  if (pairedNow) {
    const toast = h('div', { class: 'toast mobile-paired-toast', role: 'status' }, h('strong', {}, `Paired with ${profile.computer}`),
      h('p', {}, 'This phone has full Piper access. Remove it on the computer in Settings › Mobile access.'));
    toast.addEventListener('click', () => toast.remove());
    document.body.append(toast);
    window.setTimeout(() => toast.remove(), 6000);
  }
}

async function pair(): Promise<void> {
  if (!profile || !ticket?.code) return;
  if (pairingBackup) backupConfirmed = true;
  showMobileState('pairing', profile.computer);
  // Never automatically retry pairing or any API mutation after a lost response.
  try {
    const response = await exchange(await connect(), { op: 'pair', code: ticket.code }, undefined, AbortSignal.timeout(8000));
    ticket = null;
    if (!response.ok) { backupConfirmed = false; expired(); return; }
    tentative = false;
    save({ ...profile, paired: true });
    await launch(true);
  } catch {
    ticket = null; retireConnection();
    showMobileState('offline', profile.computer, { retry: () => void start() }, false, profile.paired);
  }
}

function fallingBack(): void {
  if (!profiles?.fallback || waitingPrimary || switching || pairingBackup || launched) return;
  switchProfile('fallback', true);
}

async function start(): Promise<void> {
  if (!profile) { unpaired(); return; }
  revoked = false;
  const falling = activeProfile === 'fallback' && sessionStorage.getItem('piper.mobile.fallingBack') === '1';
  showMobileState(falling ? 'falling-back' : 'connecting', profile.computer, falling && profiles ? {
    primary: profiles.primary.computer, cancel: () => switchProfile('primary'),
  } : {});
  if (!launched && activeProfile === 'primary' && profiles?.fallback && !pairingBackup && !waitingPrimary) {
    window.clearTimeout(fallbackTimer);
    fallbackTimer = window.setTimeout(fallingBack, 10000);
  }
  try {
    await connect();
    if (switching) return;
    window.clearTimeout(fallbackTimer);
    if (profile.paired && (!pairingBackup || backupConfirmed)) { ticket = null; await launch(); return; }
    showMobileState(pairingBackup ? 'pair-backup' : 'pair', profile.computer, {
      primary: profiles?.primary.computer, replacement: pairingBackup ? profiles?.fallback?.computer : undefined, number: ticket?.code ? 'Verified' : undefined,
      pair: () => void pair(),
      cancel: () => { if (pairingBackup) { switchProfile('primary'); return; } generation++; ticket = null; retireConnection(); unpaired(); },
    }, false, true, browserOnly);
  } catch {
    if (revoked || switching) return;
    active = null;
    if (activeProfile === 'primary' && profiles?.fallback && !pairingBackup && !waitingPrimary) return;
    if (falling) {
      stopReconnect(); reconnectTimer = window.setTimeout(() => { reconnectTimer = 0; void start(); }, 8000);
      return;
    }
    showMobileState('offline', profile.computer, { retry: () => void start() }, false, profile.paired);
  }
}

function clearSetup(): void {
  setup = null; setupNumber = ''; setupExpires = 0;
  window.clearTimeout(setupTimer);
  setupController?.abort(); setupController = null;
  localStorage.removeItem(SETUP_STORE);
  document.cookie = '__Host-piper-setup=; Max-Age=0; Path=/; Secure; SameSite=Strict';
}

async function cancelSetup(): Promise<void> {
  const current = setup, saved = profile;
  generation++; clearSetup();
  if (pairingBackup) { switchProfile('primary'); return; }
  if (!profile?.paired) { localStorage.removeItem(STORE); profile = null; }
  unpaired();
  if (!current?.setup || !saved) return;
  const key = Uint8Array.from(atob(saved.key), c => c.charCodeAt(0));
  let conn: QuicConnection | null = null;
  try {
    const signal = AbortSignal.timeout(8000);
    conn = await createMobileConnection(current.endpoint, current.relay, key, signal);
    await exchange(conn, { op: 'withdraw_pairing', setup: current.setup }, undefined, signal);
  } catch { /* A failed cancellation cannot grant access; the PC's local deadline still ends it. */ }
  finally { key.fill(0); await conn?.close().catch(() => {}); }
}

/** Idempotent pre-approval requests use short connections; no unpaired idle slot is held. */
async function requestSetup(): Promise<void> {
  if (!setup?.setup || !profile || setupRunning === generation || document.hidden) return;
  window.clearTimeout(setupTimer);
  const current = setup, saved = profile, cycle = generation;
  setupRunning = cycle;
  const controller = new AbortController(); setupController = controller;
  const deadline = window.setTimeout(() => controller.abort(), 12000);
  const key = Uint8Array.from(atob(saved.key), c => c.charCodeAt(0));
  let conn: QuicConnection | null = null, retry = 2000;
  const stillCurrent = () => cycle === generation && setup === current;
  try {
    conn = await createMobileConnection(current.endpoint, current.relay, key, controller.signal);
    const hello = await exchange(conn, { op: 'hello' }, undefined, controller.signal);
    if (!hello.ok) throw new Error('Computer unavailable');
    const value = await hello.json();
    if (!stillCurrent()) return;
    if (typeof value.computer !== 'string' || value.computer.length > 80 || typeof value.paired !== 'boolean') throw new Error('Invalid computer');
    if (value.paired) {
      save({ ...saved, computer: value.computer, paired: true });
      clearSetup();
      await conn.close(); conn = null;
      await launch(true);
      return;
    }
    const response = await exchange(conn, { op: 'request_pairing', setup: current.setup }, undefined, controller.signal);
    const result = await response.json();
    if (!stillCurrent()) return;
    if (!response.ok) {
      if (response.status !== 409 || !['denied', 'stopped', 'withdrawn', 'expired'].includes(result.reason)) throw new Error('Approval status unavailable');
      const reason = result.reason;
      clearSetup();
      if (pairingBackup) { switchProfile('primary'); return; }
      showMobileState(reason === 'withdrawn' ? 'unpaired' : 'setup-ended', 'Piper', { openLink, validLink, reason });
      return;
    }
    if (result.approved === true) { retry = 0; return; } // Verify the grant with a fresh authenticated hello.
    if (typeof result.number !== 'string' || !/^[0-9]{6}$/.test(result.number)
        || typeof result.expires !== 'number' || !Number.isFinite(result.expires)) throw new Error('Invalid approval');
    save({ ...saved, computer: value.computer, paired: false });
    setupExpires = result.expires;
    if (setupNumber !== result.number) {
      setupNumber = result.number;
      showMobileState('approve-wait', value.computer, { number: result.number, cancel: () => void cancelSetup() });
    }
  } catch {
    if (stillCurrent() && !document.hidden) {
      retry = 8000;
      // A missed status poll does not invalidate a code already confirmed by the
      // pinned computer. Keep Claude's approval screen while retrying; only an
      // explicit outcome or the known deadline can end this waiting state.
      if (!setupNumber || setupExpires <= Date.now() / 1000) {
        setupNumber = '';
        showMobileState('offline', saved.computer, { retry: () => void requestSetup(), cancel: () => void cancelSetup() }, false, false, browserOnly);
      }
    }
  } finally {
    key.fill(0); window.clearTimeout(deadline);
    await conn?.close().catch(() => {});
    if (setupController === controller) setupController = null;
    if (setupRunning === cycle) setupRunning = null;
    if (stillCurrent() && !document.hidden) setupTimer = window.setTimeout(() => void requestSetup(), retry);
  }
}

async function boot(pasted?: string): Promise<void> {
  installMobileViewport();
  applyChoice(window.__PIPER__?.themes, 'auto');
  const explicit = pasted ?? new URLSearchParams(location.hash.slice(1)).get('iroh');
  history.replaceState(null, '', location.pathname);
  try {
    profiles = stored(); pairingBackup = false; backupConfirmed = false;
    activeProfile = sessionStorage.getItem(ACTIVE_STORE) === 'fallback' && profiles?.fallback ? 'fallback' : 'primary';
    waitingPrimary = sessionStorage.getItem('piper.mobile.fallingBack') === 'wait';
    profile = profiles?.[activeProfile] ?? null;
    const relayOrigin = document.querySelector<HTMLMetaElement>('meta[name="piper-mobile-relay"]')?.content ?? '';
    const restored = !profile?.paired ? parseSetupCookie(document.cookie ?? '', relayOrigin)
      ?? parseSetupCookie('__Host-piper-setup=' + (localStorage.getItem(SETUP_STORE) ?? ''), relayOrigin) : null;
    const encoded = explicit ?? restored;
    if (encoded) {
      ticket = parseTicket(encoded);
      const relay = document.querySelector<HTMLMetaElement>('meta[name="piper-mobile-relay"]')?.content;
      if (new URL(ticket.relay).origin !== relay) throw new Error('Mobile link uses a different relay');
      if (profile?.paired && profile.endpoint !== ticket.endpoint) {
        if (activeProfile === 'fallback' || ticket.endpoint === profiles?.fallback?.endpoint || (!ticket.code && !ticket.setup)) {
          showMobileState('identity', profile.computer, { forget: () => void forget() }); return;
        }
        pairingBackup = true;
      }
      // A Camera-app scan opens a browser on iOS, not the installed app.
      // Installation must never create a browser grant as an accidental first step.
      if (!profile?.paired && mobileBrowserInstall() && !browserOnly) {
        browserInvitation = ticket.setup
          ? btoa(JSON.stringify(ticket)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_') : encoded;
        if (ticket.setup) document.cookie = `__Host-piper-setup=${browserInvitation}; Max-Age=600; Path=/; Secure; SameSite=Strict`;
        ticket = null;
        unpaired();
        return;
      }
      if (ticket.setup && pairingBackup) {
        const invitation = ticket;
        profile = { endpoint: invitation.endpoint, relay: invitation.relay, key: btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))), computer: 'Piper', paired: false };
        tentative = true;
        showMobileState('connecting', profile.computer);
        try { await connect(); } catch { showMobileState('offline', profile.computer, { retry: () => void boot(encoded), cancel: () => switchProfile('primary') }); return; }
        showMobileState('pair-backup', profile.computer, { primary: profiles!.primary.computer, replacement: profiles?.fallback?.computer, number: 'Approve on the computer',
          pair: () => { generation++; retireConnection(); setup = invitation; ticket = null; tentative = false; void requestSetup(); }, cancel: () => switchProfile('primary') });
        return;
      }
      if (ticket.setup && !profile?.paired) {
        setup = ticket; ticket = null; setupNumber = ''; setupExpires = 0;
        const key = profile?.endpoint === setup.endpoint ? profile.key : btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32))));
        // Retain the same tentative key across app closure, before registering the request.
        tentative = false;
        save({ endpoint: setup.endpoint, relay: setup.relay, key, computer: 'Piper', paired: false });
        localStorage.setItem(SETUP_STORE, btoa(JSON.stringify(setup)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_'));
        document.cookie = '__Host-piper-setup=; Max-Age=0; Path=/; Secure; SameSite=Strict';
        showMobileState('connecting', 'Piper');
        await requestSetup();
        return;
      }
      tentative = true;
      if (!profile || profile.endpoint !== ticket.endpoint) {
        const key = crypto.getRandomValues(new Uint8Array(32));
        save({ endpoint: ticket.endpoint, relay: ticket.relay, key: btoa(String.fromCharCode(...key)), computer: 'Piper', paired: false });
        key.fill(0);
      } else if (profile.relay !== ticket.relay) save({ ...profile, relay: ticket.relay });
    }
    await start();
  } catch { expired(); }
}
void boot();
