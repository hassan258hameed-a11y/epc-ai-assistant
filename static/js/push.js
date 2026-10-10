/* Browser notifications for the student app.

   Loaded once by the chat shell. Panels arrive as HTML swapped in with
   innerHTML, so nothing here is wired to a particular element: buttons are
   handled by delegation, and the Settings card is repainted whenever a panel
   is painted (a MutationObserver on #panelHost) or the tab comes back.

   The card's state lives in one attribute, data-push-state, which the
   stylesheet reads to show the right words and buttons:
     checking · unsupported · needs-install · not-configured · blocked · off · on

   Nothing here decides who gets what — the server does. This only asks the
   browser for permission and hands its subscription over. */
(function () {
  'use strict';
  if (window.__pushClient) return;
  window.__pushClient = true;

  var SW_URL = '/sw.js';
  var API = {
    key: '/api/push/key/',
    subscribe: '/api/push/subscribe/',
    unsubscribe: '/api/push/unsubscribe/',
    test: '/api/push/test/'
  };

  /* ---------------- the browser ---------------- */
  function supported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window &&
      window.isSecureContext !== false;
  }

  function isIOS() {
    var ua = navigator.userAgent || '';
    return /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function isStandalone() {
    try {
      return navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
    } catch (e) {
      return false;
    }
  }

  function deviceName() {
    var ua = navigator.userAgent || '';
    var browser = /Edg(A|iOS)?\//.test(ua) ? 'Edge'
      : /OPR\//.test(ua) ? 'Opera'
      : /SamsungBrowser/.test(ua) ? 'Samsung Internet'
      : /Firefox\/|FxiOS/.test(ua) ? 'Firefox'
      : /Chrome\/|CriOS/.test(ua) ? 'Chrome'
      : /Safari\//.test(ua) ? 'Safari' : '';
    var system = /iPhone/.test(ua) ? 'iPhone'
      : (/iPad/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) ? 'iPad'
      : /Android/.test(ua) ? 'Android'
      : /Windows/.test(ua) ? 'Windows'
      : /CrOS/.test(ua) ? 'ChromeOS'
      : /Mac OS X/.test(ua) ? 'Mac'
      : /Linux/.test(ua) ? 'Linux' : '';
    if (isStandalone()) return 'The app on ' + (system || 'this device');
    if (browser && system) return browser + ' on ' + system;
    return browser || system || 'This browser';
  }

  function keyBytes(base64) {
    var padded = (base64 + '='.repeat((4 - base64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    var raw = window.atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function toBase64Url(buffer) {
    var bytes = new Uint8Array(buffer);
    var text = '';
    for (var i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
    return window.btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  /* A subscription made with an older server key can never receive again. */
  function sameKey(subscription, key) {
    try {
      var have = subscription.options && subscription.options.applicationServerKey;
      if (!have || !key) return true;
      return toBase64Url(have) === key.replace(/=+$/, '');
    } catch (e) {
      return true;
    }
  }

  function withTimeout(promise, ms, fallback) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { resolve(fallback); }, ms);
      promise.then(function (value) { clearTimeout(timer); resolve(value); },
                   function (error) { clearTimeout(timer); reject(error); });
    });
  }

  /* Rejects if the browser never answers — a push service it cannot reach
     can leave subscribe() pending indefinitely. */
  function withDeadline(promise, ms, message) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { reject(userError(message)); }, ms);
      promise.then(function (value) { clearTimeout(timer); resolve(value); },
                   function (error) { clearTimeout(timer); reject(error); });
    });
  }

  var registering = null;
  function registration() {
    if (!registering) {
      registering = navigator.serviceWorker.register(SW_URL, { scope: '/' })
        .then(function () { return navigator.serviceWorker.ready; })
        .catch(function (error) { registering = null; throw error; });
    }
    return registering;
  }

  /* The current subscription without registering anything new. */
  function currentSubscription() {
    if (!supported()) return Promise.resolve(null);
    return navigator.serviceWorker.getRegistration('/').then(function (reg) {
      return reg ? reg.pushManager.getSubscription() : null;
    }).catch(function () { return null; });
  }

  function askPermission() {
    // Called straight from the click: Safari only asks inside the gesture.
    return new Promise(function (resolve) {
      var answered = false;
      function done(value) { if (!answered) { answered = true; resolve(value || Notification.permission); } }
      try {
        var maybe = Notification.requestPermission(done);
        if (maybe && typeof maybe.then === 'function') maybe.then(done, function () { done(Notification.permission); });
      } catch (e) {
        done(Notification.permission);
      }
    });
  }

  /* ---------------- the server ---------------- */
  function csrf() {
    var field = document.querySelector('[data-push-card] [name=csrfmiddlewaretoken]') ||
                document.querySelector('[name=csrfmiddlewaretoken]');
    if (field && field.value) return field.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function post(url, body, extra) {
    var init = {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrf()
      },
      body: JSON.stringify(body || {})
    };
    if (extra && extra.keepalive) init.keepalive = true;
    return fetch(url, init).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        data = data || {};
        data.status = response.status;
        data.ok = response.ok && data.ok !== false;
        return data;
      });
    });
  }

  function serverKey() {
    return fetch(API.key, {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (response) { return response.ok ? response.json() : null; })
      .then(function (meta) { return meta && meta.configured ? meta.public_key : ''; })
      .catch(function () { return ''; });
  }

  /* ---------------- the card ---------------- */
  function cards() {
    return Array.prototype.slice.call(document.querySelectorAll('[data-push-card]'));
  }

  function detect(card) {
    if (card.getAttribute('data-push-configured') === '0') return Promise.resolve('not-configured');
    if (!supported()) return Promise.resolve(isIOS() && !isStandalone() ? 'needs-install' : 'unsupported');
    if (Notification.permission === 'denied') return Promise.resolve('blocked');
    if (Notification.permission !== 'granted') return Promise.resolve('off');
    return withTimeout(currentSubscription(), 6000, null).then(function (subscription) {
      return subscription ? 'on' : 'off';
    }, function () { return 'off'; });
  }

  function paint(card, state) {
    card.setAttribute('data-push-state', state);
    var device = card.querySelector('[data-push-device]');
    if (device) device.textContent = deviceName();
  }

  function say(card, text, tone) {
    var line = card.querySelector('[data-push-msg]');
    if (!line) return;
    line.textContent = text || '';
    line.setAttribute('data-tone', tone || '');
    line.hidden = !text;
  }

  function busy(card, on) {
    card.setAttribute('data-push-busy', on ? '1' : '0');
    card.querySelectorAll('[data-push-action]').forEach(function (button) {
      button.disabled = !!on;
      if (on) button.setAttribute('aria-busy', 'true'); else button.removeAttribute('aria-busy');
    });
  }

  function refreshCard(card) {
    if (card.getAttribute('data-push-busy') === '1') return Promise.resolve();
    return detect(card).then(function (state) { paint(card, state); });
  }

  function refresh() {
    cards().forEach(function (card) {
      if (card.getAttribute('data-push-painted') !== '1') {
        card.setAttribute('data-push-painted', '1');
        say(card, '');
      }
      refreshCard(card);
    });
  }
  window.pushRefresh = refresh;

  function friendly(error) {
    var name = error && error.name;
    if (name === 'NotAllowedError') return 'Your browser refused. Allow notifications for this site in its settings, then try again.';
    if (name === 'AbortError' || name === 'InvalidStateError') {
      return "Your browser couldn't set up notifications. Private and incognito windows can't receive them; " +
        'otherwise check your connection and try again.';
    }
    if (error && error.userFacing) return error.message;
    return 'Something went wrong turning notifications on. Please try again.';
  }

  function userError(message) {
    var error = new Error(message);
    error.userFacing = true;
    return error;
  }

  /* ---------------- actions ---------------- */
  function enable(card) {
    if (!supported()) { refreshCard(card); return; }
    var key = card.getAttribute('data-push-key') || '';
    var asking = askPermission();  // first, while the click still counts
    busy(card, true);
    say(card, 'Waiting for your answer in the browser prompt…', '');
    asking.then(function (permission) {
      if (permission !== 'granted') {
        paint(card, permission === 'denied' ? 'blocked' : 'off');
        say(card, permission === 'denied' ? '' : 'Nothing was changed. Press Turn on again whenever you like.', '');
        return null;
      }
      say(card, 'Turning on…', '');
      return withDeadline(registration().then(function (reg) {
        return reg.pushManager.getSubscription().then(function (existing) {
          if (existing && !sameKey(existing, key)) return existing.unsubscribe().then(function () { return null; });
          return existing;
        }).then(function (existing) {
          return existing || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
        });
      }), 30000, "Your browser didn't finish setting up notifications. Check your connection and try again.")
        .then(function (subscription) {
          return post(API.subscribe, { subscription: subscription.toJSON() }).then(function (result) {
            if (result.ok) {
              paint(card, 'on');
              say(card, 'Done — this device will get notifications, even with the site closed.', 'ok');
              return null;
            }
            // The server said no: leave the browser as it found it, so the
            // card never shows "on" for a device that will never hear anything.
            return subscription.unsubscribe().catch(function () {}).then(function () {
              throw userError(result.status === 403
                ? 'Your session has expired. Refresh the page and try again.'
                : (result.error || 'The server could not save this device. Please try again.'));
            });
          });
        });
    }).catch(function (error) {
      say(card, friendly(error), 'bad');
      return refreshCard(card);
    }).then(function () { busy(card, false); });
  }

  function disable(card) {
    busy(card, true);
    say(card, 'Turning off…', '');
    currentSubscription().then(function (subscription) {
      if (!subscription) return null;
      var endpoint = subscription.endpoint;
      return subscription.unsubscribe().catch(function () { return false; }).then(function () {
        return post(API.unsubscribe, { endpoint: endpoint });
      });
    }).then(function () {
      paint(card, 'off');
      say(card, 'Turned off for this device. Your other devices are not affected.', 'ok');
    }, function () {
      say(card, "That didn't work. Please try again.", 'bad');
      return refreshCard(card);
    }).then(function () { busy(card, false); });
  }

  function sendTest(card) {
    busy(card, true);
    say(card, 'Sending…', '');
    post(API.test, {}).then(function (result) {
      say(card, result.ok ? 'Sent — it should appear in a few seconds.' : (result.error || 'The test could not be sent.'),
          result.ok ? 'ok' : 'bad');
    }, function () {
      say(card, "We couldn't reach the server. Check your connection and try again.", 'bad');
    }).then(function () { busy(card, false); });
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-push-action]');
    if (!button) return;
    var card = button.closest('[data-push-card]');
    if (!card || button.disabled) return;
    event.preventDefault();
    var action = button.getAttribute('data-push-action');
    if (action === 'enable') enable(card);
    else if (action === 'disable') disable(card);
    else if (action === 'test') sendTest(card);
    else if (action === 'refresh') { say(card, ''); refreshCard(card); }
  });

  /* Signing out detaches this device from the account first, so the next
     person to use the browser never sees the last one's alerts. The browser
     keeps its subscription: whoever signs in next picks it up (below). */
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest('a[href]');
    if (!link || !supported() || Notification.permission !== 'granted') return;
    var url;
    try { url = new URL(link.href, location.href); } catch (e) { return; }
    if (url.origin !== location.origin || url.pathname !== '/logout/') return;
    event.preventDefault();
    var gone = false;
    function leave() { if (!gone) { gone = true; location.href = link.href; } }
    setTimeout(leave, 1500);
    currentSubscription().then(function (subscription) {
      if (subscription) return post(API.unsubscribe, { endpoint: subscription.endpoint }, { keepalive: true });
      return null;
    }).then(leave, leave);
  });

  /* ---------------- boot ---------------- */
  /* Tell the server about this browser's subscription on every visit. That
     is what makes a shared browser follow whoever signed in last, recovers a
     device the server dropped, and moves a subscription made with an old key
     onto the current one — all without asking again. */
  function resync() {
    if (!supported() || Notification.permission !== 'granted') return;
    registration().then(function (reg) {
      return reg.pushManager.getSubscription().then(function (subscription) {
        if (!subscription) return null;
        return serverKey().then(function (key) {
          if (!key) return null;
          if (sameKey(subscription, key)) return post(API.subscribe, { subscription: subscription.toJSON() });
          var old = subscription.endpoint;
          return subscription.unsubscribe().then(function () {
            return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
          }).then(function (fresh) {
            return post(API.subscribe, { subscription: fresh.toJSON(), old_endpoint: old });
          });
        });
      });
    }).catch(function () { /* next visit */ }).then(refresh);
  }

  function boot() {
    refresh();
    if (supported()) {
      registration().catch(function () {});
      resync();
    }
    var host = document.getElementById('panelHost');
    if (host && 'MutationObserver' in window) {
      new MutationObserver(function () {
        if (host.querySelector('[data-push-card]:not([data-push-painted="1"])')) refresh();
      }).observe(host, { childList: true });
    }
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && cards().length) refresh();
    });
    try {
      navigator.permissions.query({ name: 'notifications' }).then(function (status) {
        status.onchange = refresh;
      }).catch(function () {});
    } catch (e) { /* no Permissions API */ }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
