'use strict';
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:' && location.pathname.endsWith('/about.html')) {
  contextBridge.exposeInMainWorld('piperAbout', {
    resize: height => ipcRenderer.send('about:resize', height),
    getState: () => ipcRenderer.invoke('about:state'),
    action: action => ipcRenderer.invoke('about:action', action),
    subscribe: callback => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on('about:state', listener);
      return () => ipcRenderer.removeListener('about:state', listener);
    }
  });
} else if (location.protocol === 'file:') {
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
  contextBridge.exposeInMainWorld('__piperNative', {
    platform: process.platform,
    pickDirectory: initial => ipcRenderer.invoke('file:pick-directory', initial),
    file: (action, path) => ipcRenderer.invoke('file:action', action, path),
    drag: path => ipcRenderer.send('file:drag', path),
    workspace: (action, slot) => ipcRenderer.invoke('workspace:action', action, slot),
    workspaces: callback => { const listener = (_event, slots) => callback(slots); ipcRenderer.on('workspace:changed', listener); return () => ipcRenderer.removeListener('workspace:changed', listener); }
  });
  // A renderer-owned screen survives an outage; only its explicit Start Piper / Show log clicks have effects.
  contextBridge.exposeInMainWorld('__piperRestart', {
    visible: value => ipcRenderer.send('restart:visible', value === true),
    start: () => ipcRenderer.invoke('restart:action', 'ensure'),
    log: () => ipcRenderer.invoke('restart:action', 'log'),
    listen: callback => {
      const listener = (_event, text) => { if (typeof text === 'string' && text.length <= 8000) callback(text); };
      ipcRenderer.on('restart:result', listener);
      return () => ipcRenderer.removeListener('restart:result', listener);
    }
  });
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
