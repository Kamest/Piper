// The local GUI uses ordinary HTTP/WebSocket. Only the exported mobile client installs a transport.
export type TerminalSocket = Pick<WebSocket, 'binaryType' | 'readyState' | 'onopen' | 'onmessage' | 'onerror' | 'onclose' | 'send' | 'close'>;
export interface Transport {
  /** Mobile: retained off-screen terminals must not consume network/replay capacity. */
  visibleTerminalsOnly?: boolean;
  fetch(path: string, init: RequestInit): Promise<Response>;
  socket(url: string): TerminalSocket;
}
let remote: Transport | null = null;
export function installTransport(value: Transport): void { remote = value; }
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
export function notifyTransportReady(): void { for (const listener of [...readyListeners]) listener(); }

export function terminalStreamVisibleOnly(): boolean { return remote?.visibleTerminalsOnly === true; }
