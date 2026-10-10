/* Service worker: browser notifications, nothing else.

   It shows the notifications the server pushes and opens the right page when
   one is clicked. It deliberately has no fetch handler and caches nothing —
   every page still comes from the network, exactly as without it. */
'use strict';

var ICON = {{ icon_js|safe }};
var BADGE = {{ badge_js|safe }};
var APP_NAME = {{ app_name_js|safe }};

self.addEventListener('install', function () {
  // A new version takes over at once; there is no cache to migrate.
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

function readPush(event) {
  if (!event.data) return {};
  try {
    var data = event.data.json();
    return data && typeof data === 'object' ? data : {};
  } catch (e) {
    return { body: event.data.text() };
  }
}

self.addEventListener('push', function (event) {
  var data = readPush(event);
  var options = {
    body: String(data.body || ''),
    icon: ICON,
    badge: BADGE,
    data: { url: data.url || '/chat/' },
    timestamp: Date.now()
  };
  if (data.tag) {
    // Same tag replaces the earlier one ("your answer is ready" per chat);
    // renotify makes the replacement buzz again instead of arriving silently.
    options.tag = String(data.tag);
    options.renotify = true;
  }
  event.waitUntil(self.registration.showNotification(String(data.title || APP_NAME), options));
});

function targetUrl(notification) {
  var raw = (notification.data && notification.data.url) || '/chat/';
  try {
    return new URL(raw, self.location.origin);
  } catch (e) {
    return new URL('/chat/', self.location.origin);
  }
}

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var target = targetUrl(event.notification);
  var sameOrigin = target.origin === self.location.origin;

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (tabs) {
      if (sameOrigin) {
        var wanted = target.pathname + target.search;
        // 1. A tab already on that page: bring it forward.
        for (var i = 0; i < tabs.length; i++) {
          var url = new URL(tabs[i].url);
          if (url.origin === target.origin && url.pathname + url.search === wanted && 'focus' in tabs[i]) {
            return tabs[i].focus();
          }
        }
        // 2. Any tab of the app (not the console): take it there.
        for (var j = 0; j < tabs.length; j++) {
          var tab = tabs[j];
          var path = new URL(tab.url).pathname;
          if (new URL(tab.url).origin !== target.origin || path.indexOf('/admin/') === 0) continue;
          if ('navigate' in tab && 'focus' in tab) {
            return tab.focus().then(function (focused) {
              return (focused || tab).navigate(target.href);
            }).catch(function () {
              return self.clients.openWindow(target.href);
            });
          }
        }
      }
      // 3. Nothing open: a new window.
      return self.clients.openWindow(target.href);
    })
  );
});

/* The push service replaced this browser's subscription (keys rotated, or it
   expired). Subscribe again and tell the server, so notifications keep coming
   without the student having to notice. */
function keyBytes(base64) {
  var padded = (base64 + '='.repeat((4 - base64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  var raw = atob(padded);
  var bytes = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

self.addEventListener('pushsubscriptionchange', function (event) {
  var old = event.oldSubscription;
  event.waitUntil(
    fetch('/api/push/key/', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (meta) {
        if (!meta || !meta.configured || !meta.public_key) return null;
        var fresh = event.newSubscription
          ? Promise.resolve(event.newSubscription)
          : self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(meta.public_key) });
        return fresh.then(function (subscription) {
          return fetch('/api/push/subscribe/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
              'Content-Type': 'application/json',
              'Accept': 'application/json',
              'X-Requested-With': 'XMLHttpRequest',
              'X-CSRFToken': meta.csrf || ''
            },
            body: JSON.stringify({ subscription: subscription.toJSON(), old_endpoint: old ? old.endpoint : '' })
          });
        });
      })
      .catch(function () { /* the next visit to the app re-syncs */ })
  );
});
