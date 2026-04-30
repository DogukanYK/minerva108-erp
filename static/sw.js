/**
 * Minerva108 ERP — Service Worker
 * ─────────────────────────────────────────────────────────────────────────
 * Strategy:
 *   • /static/*   — cache-first  (CSS/JS/images: instant repeat-visit loads)
 *   • /api/*      — NEVER cached (ERP business data must always be fresh)
 *   • HTML pages  — NEVER cached (each page embeds per-user `window.PERMS`;
 *                   serving cached HTML to a different session would leak
 *                   permission state across users)
 *
 * On version bump (CACHE_NAME), old caches are dropped during `activate`.
 * Bump CACHE_NAME any time you ship a breaking static-asset change so users
 * pick it up on next reload instead of being stuck on stale CSS/JS.
 */
const CACHE_NAME = 'minerva108-v1';

const PRECACHE = [
  '/static/dark.css',
  '/static/theme.js',
  '/static/mobile.css',
  '/static/mobile.js',
  '/static/images/logo_sidebar.png',
  '/static/images/favicon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE).catch(() => null))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // ── Security: never intercept business data or per-user HTML ───────────
  if (url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate') return;
  const accept = request.headers.get('accept') || '';
  if (accept.includes('text/html')) return;

  // ── Static assets: cache-first ─────────────────────────────────────────
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(request, copy)).catch(() => null);
          }
          return res;
        });
      })
    );
  }
});
