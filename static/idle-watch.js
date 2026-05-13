/**
 * Minerva108 — Idle timeout watcher
 * =================================
 *
 * Sayfa yüklendiğinde /api/me/session-policy'den kullanıcının idle limitini
 * çeker (default 5 dk).  Bu süre boyunca mouse/keyboard/touch/scroll
 * hareketi yoksa otomatik /api/logout çağırır ve /login'e yönlendirir.
 *
 * Aktivite olayları "activity" olarak sayılır:
 *   - mousemove (throttled)
 *   - keydown
 *   - click
 *   - touchstart
 *   - scroll
 *
 * Sayım performans için throttle'lanır (1 sn'de en fazla 1 reset).
 *
 * Login sayfası yüklemez (zaten oturum yok).
 */
(function () {
  'use strict';
  if (window.location.pathname === '/login') return;

  let idleLimitMs = 5 * 60 * 1000;   // fallback: 5 dk
  let lastActivity = Date.now();
  let warningShown = false;
  let banner = null;

  function fetchPolicy() {
    fetch('/api/me/session-policy')
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (d) {
        if (d && d.idle_timeout_minutes) {
          idleLimitMs = d.idle_timeout_minutes * 60 * 1000;
        }
      })
      .catch(function () { /* default'a düş */ });
  }

  function recordActivity() {
    lastActivity = Date.now();
    if (warningShown && banner) {
      banner.remove();
      banner = null;
      warningShown = false;
    }
  }

  // mousemove çok sık tetiklenebilir — 2sn throttle
  let throttle = 0;
  function throttledActivity() {
    const now = Date.now();
    if (now - throttle < 2000) return;
    throttle = now;
    recordActivity();
  }

  ['mousedown', 'keydown', 'click', 'touchstart', 'scroll'].forEach(function (ev) {
    document.addEventListener(ev, recordActivity, { passive: true, capture: true });
  });
  document.addEventListener('mousemove', throttledActivity, { passive: true, capture: true });

  function showWarning(secondsLeft) {
    if (warningShown) return;
    warningShown = true;
    banner = document.createElement('div');
    banner.style.cssText = `
      position:fixed; top:0; left:0; right:0; z-index:99998;
      background:#fef3c7; color:#92400e; padding:10px 16px;
      border-bottom:2px solid #f59e0b; text-align:center;
      font-family:'Jost',sans-serif; font-size:0.9rem; font-weight:600;
    `;
    banner.textContent =
      '⏱ ' + Math.ceil(secondsLeft) + ' saniye sonra inaktiflik nedeniyle çıkış yapılacak. ' +
      'Devam etmek için fare/klavye hareketi yapın.';
    document.body.appendChild(banner);
  }

  function tick() {
    const elapsed = Date.now() - lastActivity;
    const remaining = idleLimitMs - elapsed;

    if (remaining <= 0) {
      // Limit doldu — çıkış yap
      fetch('/api/logout', { method: 'POST' })
        .finally(function () {
          window.location.href = '/login?next=' + encodeURIComponent(
            window.location.pathname + window.location.search
          );
        });
      return;
    }

    // Son 30 saniyede uyarı göster
    if (remaining < 30 * 1000) {
      showWarning(remaining / 1000);
    }
  }

  // Setup
  fetchPolicy();
  // Her 5 sn'de bir kontrol — kısa idle limitleri için yeterli granulometri
  setInterval(tick, 5000);
  // Sayfa görünür olunca aktivite say (sekme değişimi)
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) recordActivity();
  });
})();
