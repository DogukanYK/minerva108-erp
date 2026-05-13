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
})();
