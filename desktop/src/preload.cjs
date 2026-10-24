'use strict';
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('piperDesktop', {
    getState: () => ipcRenderer.invoke('setup:state'),
    action: (name, value) => ipcRenderer.invoke('setup:action', name, value),
    subscribe: callback => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on('setup:state', listener);
      return () => ipcRenderer.removeListener('setup:state', listener);
    }
  });
} else if (location.protocol === 'http:' && location.hostname === '127.0.0.1') {
  contextBridge.exposeInMainWorld('__piperNotifications', {
    show: data => ipcRenderer.send('notification:show', data),
    close: id => ipcRenderer.send('notification:close', id),
    listen: callback => ipcRenderer.on('notification:event', (_event, data) => callback(data))
  });
  // Run before the GUI module scripts. Preserve onclick and EventTarget semantics.
  contextBridge.executeInMainWorld({ func: () => {
    const bridge = window.__piperNotifications;
    const active = new Map(); let seq = 0;
    class PiperNotification extends EventTarget {
      static get permission() { return 'granted'; }
      static requestPermission(callback) { callback?.('granted'); return Promise.resolve('granted'); }
      constructor(title, options = {}) {
        super(); this.id = String(++seq); this.title = String(title); this.body = String(options.body || ''); this.tag = String(options.tag || '');
        active.set(this.id, this); bridge.show({ id: this.id, title: this.title, body: this.body, tag: this.tag });
      }
      close() { bridge.close(this.id); active.delete(this.id); }
    }
    bridge.listen(({ id, type }) => {
      const n = active.get(id); if (!n) return;
      const event = new Event(type); n.dispatchEvent(event); n['on' + type]?.(event);
      if (type === 'close') active.delete(id);
    });
    window.Notification = PiperNotification;
  } });
}
