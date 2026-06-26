(function () {
  'use strict';

  var sidebar = document.querySelector('.sidebar');
  var topnav  = document.querySelector('.topnav');

  /* Pages without sidebar (login) — nothing to do */
  if (!sidebar || !topnav) return;

  /* ── Hamburger button ──────────────────────────── */
  var hamburger = document.createElement('button');
  hamburger.className   = 'btn-hamburger';
  hamburger.setAttribute('aria-label', 'Menüyü Aç/Kapat');
  hamburger.setAttribute('type', 'button');
  hamburger.innerHTML   = '<i class="bi bi-list"></i>';
  topnav.insertBefore(hamburger, topnav.firstChild);

  /* ── Overlay ──────────────────────────────────── */
  var overlay = document.createElement('div');
  overlay.className = 'sidebar-overlay';
  document.body.appendChild(overlay);

  /* ── Open / close helpers ─────────────────────── */
  function openSidebar() {
    sidebar.classList.add('mobile-open');
    overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
    hamburger.innerHTML = '<i class="bi bi-x-lg"></i>';
    hamburger.setAttribute('aria-label', 'Menüyü Kapat');
  }

  function closeSidebar() {
    sidebar.classList.remove('mobile-open');
    overlay.classList.remove('active');
    document.body.style.overflow = '';
    hamburger.innerHTML = '<i class="bi bi-list"></i>';
    hamburger.setAttribute('aria-label', 'Menüyü Aç/Kapat');
  }

  hamburger.addEventListener('click', function () {
    if (sidebar.classList.contains('mobile-open')) closeSidebar();
    else openSidebar();
  });

  overlay.addEventListener('click', closeSidebar);

  /* Close when a nav link is tapped (mobile navigation) */
  sidebar.querySelectorAll('.nav-link-item').forEach(function (link) {
    link.addEventListener('click', closeSidebar);
  });

  /* Close on Escape key */
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeSidebar();
  });

  /* ── Wrap bare tables in scrollable divs ──────── */
  /*
   * Some pages (e.g. index.html) place <table> directly inside
   * .table-card without a .table-responsive wrapper.
   * This ensures every table can scroll horizontally on mobile.
   */
  document.querySelectorAll('.table-card table, .panel table').forEach(function (table) {
    var parent = table.parentElement;
    if (parent && !parent.classList.contains('table-responsive')) {
      var wrap = document.createElement('div');
      wrap.className = 'table-responsive';
      parent.insertBefore(wrap, table);
      wrap.appendChild(table);
    }
  });

})();


/* ── PWA: "Uygulamayı Yükle" — masaüstü (Chrome/Edge) + Android + iOS ipucu ──
 * Siteyi iOS'taki "Ana Ekrana Ekle" gibi masaüstüne/uygulamaya kurulabilir
 * yapar.  Manifest + service worker zaten hazır; bu sadece kurulumu görünür
 * kılar (yüzen "Yükle" butonu + iOS Safari için tek seferlik ipucu). */
(function () {
  function isStandalone() {
    return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
        || window.navigator.standalone === true;
  }
  if (isStandalone()) return;   // zaten uygulama olarak açık

  var deferredPrompt = null;
  var btn = null;

  function showInstallBtn() {
    if (btn) return;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'pwaInstallBtn';
    btn.innerHTML = '<i class="bi bi-download"></i> Uygulamayı Yükle';
    btn.style.cssText = [
      'position:fixed', 'left:16px', 'bottom:16px', 'z-index:9998',
      'display:inline-flex', 'align-items:center', 'gap:0.45rem',
      'padding:0.62rem 1.05rem', 'background:#232E6E', 'color:#fff', 'border:none',
      'border-radius:999px', "font-family:'Jost',sans-serif", 'font-size:0.86rem',
      'font-weight:600', 'cursor:pointer', 'box-shadow:0 6px 22px rgba(35,46,110,0.35)'
    ].join(';');
    btn.addEventListener('click', function () {
      if (!deferredPrompt) return;
      btn.disabled = true;
      deferredPrompt.prompt();
      var choice = deferredPrompt.userChoice;
      if (choice && choice.then) {
        choice.then(function () { deferredPrompt = null; removeInstallBtn(); });
      } else {
        deferredPrompt = null; removeInstallBtn();
      }
    });
    if (document.body) document.body.appendChild(btn);
  }
  function removeInstallBtn() { if (btn) { btn.remove(); btn = null; } }

  // Chrome / Edge (masaüstü + Android): kurulabilir olunca tetiklenir
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    showInstallBtn();
  });
  window.addEventListener('appinstalled', function () {
    deferredPrompt = null;
    removeInstallBtn();
    try { if (window.showToast) showToast('Uygulama yüklendi.', 'success'); } catch (_e) {}
  });

  // iOS Safari: beforeinstallprompt YOK → tek seferlik "Ana Ekrana Ekle" ipucu
  (function () {
    var ua = window.navigator.userAgent || '';
    var isIOS = /iPhone|iPad|iPod/.test(ua) && !window.MSStream;
    var isSafari = /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);
    if (!isIOS || !isSafari) return;
    try { if (localStorage.getItem('pwaIosHintDismissed')) return; } catch (_e) {}
    function render() {
      var d = document.createElement('div');
      d.style.cssText = 'position:fixed;left:12px;right:12px;bottom:12px;z-index:9998;background:#232E6E;color:#fff;padding:0.75rem 0.9rem;border-radius:12px;font-size:0.85rem;box-shadow:0 8px 24px rgba(0,0,0,0.25);display:flex;align-items:center;gap:0.6rem;';
      d.innerHTML = '<span style="flex:1;">Uygulama gibi kullan: <b>Paylaş</b> <i class="bi bi-box-arrow-up"></i> &rarr; <b>Ana Ekrana Ekle</b></span>'
        + '<button type="button" style="background:rgba(255,255,255,0.22);border:none;color:#fff;border-radius:8px;padding:0.4rem 0.7rem;cursor:pointer;font-weight:600;">Tamam</button>';
      d.querySelector('button').addEventListener('click', function () {
        try { localStorage.setItem('pwaIosHintDismissed', '1'); } catch (_e) {}
        d.remove();
      });
      document.body.appendChild(d);
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', render);
    } else { render(); }
  })();
})();
