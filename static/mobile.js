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
