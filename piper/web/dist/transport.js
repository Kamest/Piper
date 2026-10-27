let remote = null;
export function installTransport(value) { remote = value; }
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
export function notifyTransportReady() { for (const listener of [...readyListeners])
    listener(); }
export function terminalStreamVisibleOnly() { return remote?.visibleTerminalsOnly === true; }
//# sourceMappingURL=transport.js.map