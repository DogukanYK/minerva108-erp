/* ─────────────────────────────────────────────────────────────────────────────
 * Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * TR tarih/saat girişi.
 *
 * Sorun: <input type="date"> ve <input type="time"> TARAYICI DİLİNE göre
 * render edilir — tarayıcı İngilizceyse kullanıcı "08/04/2026" ve "04:30 PM"
 * görür.  Bu bir CSS/HTML meselesi değildir; lang="tr" de değiştirmez.
 *
 * Çözüm: input'u text'e çevirip TR maskesi uygularız (gg.aa.yyyy · ss:dd,
 * 24 saat).  ÖNEMLİ: `el.value` property'si override edilir ve DAİMA ISO
 * ("2026-08-04" / "16:30") döndürür — böylece mevcut JS'in tamamı
 * (`getElementById('x').value`, `.value = iso`) hiç değişmeden çalışır,
 * sunucuya giden veri de aynı kalır.  Görünen metin TR, taşınan değer ISO.
 *
 * Dinamik eklenen input'lar MutationObserver ile yakalanır (PDKS program
 * tablosu satırları innerHTML ile basılıyor).  data-tr-dt guard'ı sayesinde
 * aynı element iki kez sarmalanmaz.
 */
(function () {
  'use strict';

  var proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
  if (!proto) return;
  var NATIVE = Object.getOwnPropertyDescriptor(proto, 'value');
  if (!NATIVE || !NATIVE.get || !NATIVE.set) return;   // desteklenmiyorsa sessizce çık

  /* ── Biçim çevirimleri ── */
  function isoToTrDate(v) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v || ''));
    return m ? m[3] + '.' + m[2] + '.' + m[1] : '';
  }
  function trToIsoDate(v) {
    var m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(String(v || '').trim());
    if (!m) return '';
    var d = +m[1], mo = +m[2], y = +m[3];
    var dt = new Date(y, mo - 1, d);
    // Takvimsel geçerlilik: 31.02.2026 gibi girdiler ISO'ya çevrilmez.
    if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return '';
    return m[3] + '-' + m[2] + '-' + m[1];
  }
  function maskDate(v) {
    var d = String(v || '').replace(/\D/g, '').slice(0, 8);
    if (d.length <= 2) return d;
    if (d.length <= 4) return d.slice(0, 2) + '.' + d.slice(2);
    return d.slice(0, 2) + '.' + d.slice(2, 4) + '.' + d.slice(4);
  }
  function maskTime(v) {
    var d = String(v || '').replace(/\D/g, '').slice(0, 4);
    return d.length <= 2 ? d : d.slice(0, 2) + ':' + d.slice(2);
  }
  function normTime(v) {
    var m = /^(\d{1,2}):(\d{2})$/.exec(String(v || '').trim());
    if (!m) return '';
    var h = +m[1], mi = +m[2];
    if (h > 23 || mi > 59) return '';
    return (h < 10 ? '0' + h : '' + h) + ':' + m[2];
  }

  /* ── Sarmalama ── */
  function wrap(el, kind) {
    if (el.dataset.trDt) return;
    el.dataset.trDt = kind;

    var isDate = kind === 'date';
    var initial = NATIVE.get.call(el);          // type değişmeden ÖNCE oku
    var iso = isDate ? String(initial || '').slice(0, 10) : normTime(initial);

    el.type = 'text';
    el.setAttribute('inputmode', 'numeric');
    el.setAttribute('autocomplete', 'off');
    el.maxLength = isDate ? 10 : 5;
    if (!el.placeholder) el.placeholder = isDate ? 'gg.aa.yyyy' : 'ss:dd';
    if (!el.title) el.title = isDate ? 'Gün.Ay.Yıl' : 'Saat:Dakika (24 saat)';
    NATIVE.set.call(el, isDate ? isoToTrDate(iso) : iso);

    Object.defineProperty(el, 'value', {
      configurable: true,
      // Dışarıya DAİMA ISO — mevcut JS ve sunucu sözleşmesi değişmez.
      get: function () { return iso; },
      set: function (v) {
        iso = isDate ? String(v || '').slice(0, 10) : normTime(v);
        NATIVE.set.call(el, isDate ? isoToTrDate(iso) : iso);
      },
    });

    el.addEventListener('input', function () {
      var raw = NATIVE.get.call(el);
      var masked = isDate ? maskDate(raw) : maskTime(raw);
      if (masked !== raw) {
        NATIVE.set.call(el, masked);
        // İmleci sona al — maske noktayı araya eklediğinde geri sıçramasın.
        try { el.setSelectionRange(masked.length, masked.length); } catch (e) { /* yoksay */ }
      }
      iso = isDate ? trToIsoDate(masked) : normTime(masked);
    });

    // Odaktan çıkınca yarım kalan girdiyi temizle (yarım tarih ISO üretmez;
    // ekranda kalırsa kullanıcı geçerli sanır).
    el.addEventListener('blur', function () {
      var raw = NATIVE.get.call(el);
      if (!raw) { iso = ''; return; }
      var ok = isDate ? trToIsoDate(raw) : normTime(raw);
      if (!ok) {
        NATIVE.set.call(el, '');
        iso = '';
      } else {
        NATIVE.set.call(el, isDate ? isoToTrDate(ok) : ok);
        iso = ok;
      }
    });
  }

  function scan(root) {
    var scope = (root && root.querySelectorAll) ? root : document;
    scope.querySelectorAll('input[type="date"]').forEach(function (el) { wrap(el, 'date'); });
    scope.querySelectorAll('input[type="time"]').forEach(function (el) { wrap(el, 'time'); });
  }

  function boot() {
    scan(document);
    // innerHTML ile sonradan basılan satırlar (ör. PDKS haftalık program).
    if (window.MutationObserver) {
      new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          if (muts[i].addedNodes && muts[i].addedNodes.length) { scan(document); return; }
        }
      }).observe(document.documentElement, { childList: true, subtree: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // Görüntüleme yardımcıları — tablo/etiket basan kodlar da kullanabilsin.
  window.trDate = isoToTrDate;
  window.trDateToIso = trToIsoDate;
})();
