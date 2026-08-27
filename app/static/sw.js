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
  // Declarative `actions` carry a `navigate` URL each; the classic
  // Notification API has no such field, so the URLs are stashed on `data`
  // keyed by action id and looked up again in notificationclick below.
  const actions = Array.isArray(n.actions) ? n.actions : [];
  const routes = {};
  actions.forEach((a) => {
    if (a && a.action && a.navigate) routes[a.action] = a.navigate;
  });
  const shown = self.registration.showNotification(n.title || 'Project Portal', {
    body: n.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    actions: actions.map((a) => ({ action: a.action, title: a.title })),
    data: { navigate: n.navigate || '/', routes: routes },
  });
  // The badge is part of the declarative payload the OS applies for itself on
  // iOS; here it has to be set by hand. Not fatal if it is unavailable.
  if (typeof n.app_badge === 'number' && navigator.setAppBadge) {
    try {
      const badged = n.app_badge > 0 ? navigator.setAppBadge(n.app_badge) : navigator.clearAppBadge();
      if (badged && badged.catch) badged.catch(() => {});
    } catch (err) {
      /* badging unsupported - the notification itself still showed */
    }
  }
  event.waitUntil(shown);
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const info = event.notification.data || {};
  // A tapped action button goes where that button pointed; tapping the body
  // of the notification (no action) goes to the notification's own target.
  const url = (event.action && info.routes && info.routes[event.action]) || info.navigate || '/';
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
