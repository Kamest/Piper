let remote = null;
let revision = 0;
export function installTransport(value) { remote = value; revision++; }
export function transportRevision() { return revision; }
/** Ignore state results from a retired connection; desktop keeps its ordinary banner. */
export function reportTransportState(connected, owner) {
    if (!remote?.stateChanged)
        return false;
    if (owner === revision)
        remote.stateChanged(connected);
    return true;
}
export function notifyTransportUnavailable() { revision++; }
export function transportFetch(path, init) {
    return remote ? remote.fetch(path, init) : fetch(path, init);
}
export function terminalSocket(url) {
    return remote ? remote.socket(url) : new WebSocket(url);
}
const readyListeners = new Set();
/** The mobile manager renews a shared connection; terminals must not wait out an old retry. */
export function onTransportReady(listener) {
    readyListeners.add(listener);
    return () => { readyListeners.delete(listener); };
}
export function notifyTransportReady() { revision++; for (const listener of [...readyListeners])
    listener(); }
export function terminalStreamVisibleOnly() { return remote?.visibleTerminalsOnly === true; }
//# sourceMappingURL=transport.js.map