/**
 * Minerva108 — Türkçe alfabetik sıralama helper'ı
 * ================================================
 *
 * Browser'ın Intl.Collator API'sini 'tr' locale ile başlatır.
 * Çç, Ğğ, İi/Iı, Öö, Şş, Üü harflerini doğru sıralar (ASCII sort'tan farklı).
 *
 * Kullanım:
 *   const sorted = window.trSort(items, i => i.name);
 *   // veya alanı string olarak ver:
 *   const sorted = window.trSortBy(items, 'name');
 *
 * Helper, orijinal diziyi mutate etmez — yeni dizi döner.
 */
(function () {
  'use strict';

  // sensitivity: 'base' → büyük/küçük harf ve aksan farklarını yutar
  //   ("Argan" === "argan" === "ARGAN" sıralamada eşit kabul edilir)
  // numeric: true → "100ml" < "200ml" < "1000ml" sıralanır (string değil)
  const collator = new Intl.Collator('tr', {
    sensitivity: 'base',
    numeric: true,
  });

  window.trCompare = collator.compare.bind(collator);

  window.trSort = function (arr, keyFn) {
    if (!Array.isArray(arr)) return arr;
    const copy = [...arr];
    copy.sort(function (a, b) {
      const ka = (keyFn ? keyFn(a) : a) || '';
      const kb = (keyFn ? keyFn(b) : b) || '';
      return collator.compare(String(ka), String(kb));
    });
    return copy;
  };

  window.trSortBy = function (arr, fieldName) {
    return window.trSort(arr, function (x) { return x[fieldName]; });
  };

  // ── Türkçe-katlanmış arama anahtarı ──────────────────────────────────────
  // `core/supplier_prices.py::normalize`'ın (_TR_FOLD tablosu) BİREBİR JS
  // karşılığı — sunucu ile istemci aynı ürünü aynı anahtarla bulsun diye.
  // Sıradan `.toLowerCase()` "İ"yi katlamaz (U+0130 → "i̇", noktalı bileşik
  // karakter kalır) — 24.08.2026'da stajyerin "BADEM YAGI" araması "BADEM
  // YAĞI" kartını bulamamasının sebeplerinden biri buydu (diğeri: arama
  // yalnız `name` alanına bakıyordu, `name_tr`'ye değil).
  const _TR_FOLD_MAP = {
    'ç': 'c', 'Ç': 'c', 'ğ': 'g', 'Ğ': 'g', 'ı': 'i', 'İ': 'i',
    'ö': 'o', 'Ö': 'o', 'ş': 's', 'Ş': 's', 'ü': 'u', 'Ü': 'u', 'I': 'i',
  };
  window.trFold = function (s) {
    if (s === null || s === undefined) return '';
    let out = '';
    for (const ch of String(s)) {
      out += _TR_FOLD_MAP[ch] !== undefined ? _TR_FOLD_MAP[ch] : ch.toLowerCase();
    }
    return out.split(/\s+/).filter(Boolean).join(' ');
  };
})();
