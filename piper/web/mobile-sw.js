/* Push-only worker. Names are per-phone opt-in; messages/keys never belong here. No fetch cache. */
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let test = false, session = '';
  try {
    const value = event.data?.json();
    test = value?.v === 1 && value?.kind === 'test';
    if (value?.v === 1 && value?.kind === 'attention' && typeof value.session === 'string') {
      session = value.session.replace(/[\u0000-\u001f\u007f]/g, ' ')
        .replace(/[\u061c\u180e\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g, '')
        .replace(/[\u{e0000}-\u{e007f}]/gu, '').replace(/\s+/g, ' ').trim().slice(0, 100);
    }
  } catch { /* malformed/unknown payloads show only fixed generic text */ }
  event.waitUntil(self.registration.showNotification('Piper', {
    body: test ? 'Test notification from Piper.' : session
      ? `“${session}” needs your attention. Open Piper to see what’s waiting.`
      : 'Piper needs your attention. Open Piper to see what’s waiting.',
    icon: '/piper/icon-192.png', badge: '/piper/icon-192.png', tag: 'piper-attention',
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const home = new URL('/', self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const app = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (app) { await app.navigate(home); return app.focus(); }
    return self.clients.openWindow(home);
  })());
});
