/**
 * Minerva108 — Web Push subscription helpers (client side).
 *
 * Two flows:
 *   • window.setupPushNotifications()
 *       Explicit user gesture (click a "Bildirimleri Aç" button). Asks
 *       permission, subscribes, posts to /api/notifications/subscribe.
 *       Returns true on success, false on denial / unsupported / error.
 *
 *   • window.refreshPushSubscription()
 *       Silent — auto-runs on every page load. If the user has already
 *       granted permission AND a subscription exists, re-syncs the DB
 *       record (last_used_at + reassignment if logged-in user changed).
 *       Never prompts. No-op when permission was never granted.
 *
 * Design notes
 * ────────────
 * • Browsers REQUIRE a user gesture for Notification.requestPermission().
 *   That's why setup is exposed as a function — wire it to a button.
 * • Both helpers are idempotent: re-subscribing returns the existing
 *   subscription on the browser side; the server endpoint upserts.
 * • Falls back gracefully on unsupported browsers (warns to console only).
 */
(function () {
  'use strict';

  // ── Helpers ──────────────────────────────────────────────────────────
  function urlBase64ToUint8Array(base64) {
    const padding = '='.repeat((4 - (base64.length % 4)) % 4);
    const base = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function fetchVapidKey() {
    const r = await fetch('/api/notifications/vapid-public-key', {
      credentials: 'same-origin',
    });
    if (!r.ok) throw new Error('VAPID public key fetch failed: ' + r.status);
    const j = await r.json();
    if (!j.configured || !j.key) {
      throw new Error('Push not configured on server (set VAPID_PUBLIC_KEY)');
    }
    return j.key;
  }

  async function postSubscription(sub) {
    return fetch('/api/notifications/subscribe', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub),
    });
  }

  function isSupported() {
    return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
  }

  // ── UX polish: show the topnav "Bildirimleri Aç" button only when there's
  //     actually something for the user to do. State machine:
  //       granted   → hide (already subscribed; clean UI for the common case)
  //       denied    → hide (browser-level block; clicking the button can't fix it)
  //       default   → show (call-to-action)
  //       no support → hide
  function updateEnablePushBtnVisibility() {
    const btn = document.getElementById('enablePushBtn');
    if (!btn) return;
    const showCTA = isSupported() && Notification.permission === 'default';
    btn.style.display = showCTA ? 'inline-flex' : 'none';
  }

  // ── Public: explicit setup (user-gesture entry point) ─────────────────
  async function setupPushNotifications() {
    if (!isSupported()) {
      console.warn('[push] not supported in this browser');
      return false;
    }
    try {
      const reg = await navigator.serviceWorker.ready;

      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') {
          console.warn('[push] permission denied:', perm);
          return false;
        }
        const vapidKey = await fetchVapidKey();
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidKey),
        });
      }
      const r = await postSubscription(sub);
      if (!r.ok) {
        console.warn('[push] backend subscribe failed:', r.status);
        return false;
      }
      console.info('[push] subscribed');
      updateEnablePushBtnVisibility();         // permission is now 'granted' → hide CTA
      return true;
    } catch (e) {
      console.warn('[push] setup error:', e);
      updateEnablePushBtnVisibility();         // re-evaluate (denial / failure)
      return false;
    }
  }

  // ── Public: silent refresh (every page load) ──────────────────────────
  async function refreshPushSubscription() {
    if (!isSupported()) return;
    if (Notification.permission !== 'granted') return;     // never asked → don't prompt
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) await postSubscription(sub);
    } catch (e) {
      // Network hiccups, expired subs, etc. — silent on the auto-path.
      console.debug('[push] refresh skipped:', e && e.message);
    }
  }

  // ── Boot: silent DB refresh + topnav button visibility ────────────────
  function _boot() {
    updateEnablePushBtnVisibility();
    refreshPushSubscription();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }

  // Expose for explicit user-gesture wiring + ad-hoc button refresh.
  window.setupPushNotifications        = setupPushNotifications;
  window.refreshPushSubscription       = refreshPushSubscription;
  window.updateEnablePushBtnVisibility = updateEnablePushBtnVisibility;
})();
