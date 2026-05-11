/**
 * Minerva108 — Paylaşılan toast bildirim helper'ı (R7 / XSS-safe)
 * =============================================================
 *
 * Eskiden 13 ayrı template'te `function showToast(...)` tanımı vardı; her
 * birinde `innerHTML = ...${message}...` kullanılıyordu — eğer message API
 * hatasından geldi ve içinde `<script>` varsa XSS yaratır.
 *
 * Bu dosya tek noktadan tanımlar, XSS-safe `createElement + textContent`
 * pattern'ı kullanır.  Her template HEAD'inde:
 *
 *     <script src="/static/toast.js" defer></script>
 *
 * include edilir.  Local function tanımları kaldırıldı — tüm template'ler
 * artık `window.showToast(...)` veya kısa adıyla `showToast(...)` çağırır.
 *
 * Tüm toast div'leri sayfanın altındaki `<div id="toast-container">` içine
 * dökülür.  Container yoksa otomatik yaratılır (eski template'ler için
 * geri uyumlu — sıralı body'nin altına ekler).
 */
(function () {
  'use strict';

  function ensureContainer() {
    var c = document.getElementById('toast-container');
    if (c) return c;
    c = document.createElement('div');
    c.id = 'toast-container';
    // Her template'in kendi CSS'inde toast-container'ı pozisyonladığı için
    // bu fallback minimal stil tutar (orijinal stiller üstüne biner)
    c.style.cssText = 'position:fixed;bottom:1.5rem;right:1.5rem;z-index:9999;' +
                       'display:flex;flex-direction:column;gap:0.6rem;';
    document.body.appendChild(c);
    return c;
  }

  /**
   * Toast göster.
   * @param {string} message  Görüntülenecek metin (her zaman text olarak).
   * @param {'error'|'success'} [type='error']  Renk + ikon.
   * @param {number} [duration=3500]  Otomatik kapanma süresi (ms).
   */
  window.showToast = function (message, type, duration) {
    type = type || 'error';
    duration = (typeof duration === 'number') ? duration : 3500;

    var container = ensureContainer();
    var icon = (type === 'error') ? '✕' : '✓';

    // CSS sınıfları her template'te zaten tanımlı (.toast-msg, .toast-error,
    // .toast-success).  Onları kullan ki görsel tutarlı kalsın.
    var el = document.createElement('div');
    el.className = 'toast-msg toast-' + type;

    var iconSpan = document.createElement('span');
    iconSpan.textContent = icon;
    el.appendChild(iconSpan);

    var msgSpan = document.createElement('span');
    msgSpan.textContent = String(message == null ? '' : message);
    el.appendChild(msgSpan);

    container.appendChild(el);

    setTimeout(function () {
      el.style.transition = 'opacity 0.4s';
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 400);
    }, duration);
  };
})();
