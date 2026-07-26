/* Project Portal service worker.
 *
 * Exists for two reasons only: browsers without Declarative Web Push (Chrome,
 * mainly) need a `push` handler to display a notification at all, and a
 * registered worker is what makes Chrome treat the portal as installable.
 * On iOS 18.4+ the OS displays the portal's application/notification+json
 * payloads itself and this file never runs for a push.
 *
 * No caching, no offline: the portal is a live dashboard on the LAN and a
 * stale cached copy of it is worse than a connection error.
 */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (err) {
    data = { notification: { title: 'Project Portal', body: event.data ? event.data.text() : '' } };
  }
  // The declarative payload shape ({web_push: 8030, notification: {...}}) is
  // what the portal always sends; tolerate a bare notification object too.
  const n = data.notification || data;
  event.waitUntil(
    self.registration.showNotification(n.title || 'Project Portal', {
      body: n.body || '',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { navigate: n.navigate || '/' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.navigate) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const win of wins) {
        if ('focus' in win) {
          win.navigate(url);
          return win.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
