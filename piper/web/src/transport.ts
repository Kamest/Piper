// The local GUI uses ordinary HTTP/WebSocket. Only the exported mobile client installs a transport.
export type TerminalSocket = Pick<WebSocket, 'binaryType' | 'readyState' | 'onopen' | 'onmessage' | 'onerror' | 'onclose' | 'send' | 'close'>;
export interface Transport {
  /** Mobile: retained off-screen terminals must not consume network/replay capacity. */
  visibleTerminalsOnly?: boolean;
  fetch(path: string, init: RequestInit): Promise<Response>;
  socket(url: string): TerminalSocket;
  /** Mobile owns one connection notice, including state-poll failures. */
  stateChanged?(connected: boolean): void;
}
let remote: Transport | null = null;
let revision = 0;
export function installTransport(value: Transport): void { remote = value; revision++; }
export function transportRevision(): number { return revision; }
/** Ignore state results from a retired connection; desktop keeps its ordinary banner. */
export function reportTransportState(connected: boolean, owner: number): boolean {
  if (!remote?.stateChanged) return false;
  if (owner === revision) remote.stateChanged(connected);
  return true;
}
export function notifyTransportUnavailable(): void { revision++; }
export function transportFetch(path: string, init: RequestInit): Promise<Response> {
  return remote ? remote.fetch(path, init) : fetch(path, init);
}
export function terminalSocket(url: string): TerminalSocket {
  return remote ? remote.socket(url) : new WebSocket(url);
}

const readyListeners = new Set<() => void>();
/** The mobile manager renews a shared connection; terminals must not wait out an old retry. */
export function onTransportReady(listener: () => void): () => void {
  readyListeners.add(listener);
  return () => { readyListeners.delete(listener); };
}
export function notifyTransportReady(): void { revision++; for (const listener of [...readyListeners]) listener(); }

export function terminalStreamVisibleOnly(): boolean { return remote?.visibleTerminalsOnly === true; }
