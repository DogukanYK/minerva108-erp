/**
 * Minerva108 — /api/items sessionStorage cache
 * =============================================
 *
 * /api/items 545 satır × 18 alan ≈ 50KB JSON.  Şu an her sayfa (items,
 * recipes, receiving, production) ayrı ayrı çekiyor — sayfa değişimi 50KB
 * ekstra trafik.
 *
 * Bu helper sessionStorage'da 30 saniye TTL'li bir cache tutar:
 *   - Cache fresh ise direkt onu döner (network yok)
 *   - Stale veya yoksa /api/items çağırır, cache'e koyar
 *   - Mutation event'leri (item create/edit/delete) cache'i temizler:
 *     window.invalidateItemsCache() çağırılabilir
 *
 * sessionStorage seçildi çünkü:
 *   - Sekme kapanınca silinir (cross-session leak yok)
 *   - localStorage kullanıcı çıkış yapsa bile kalır (gizlilik riski)
 *
 * Kullanım:
 *   const items = await window.fetchItems();        // cache'li
 *   const items = await window.fetchItems(true);    // forceRefresh
 *   window.invalidateItemsCache();                  // create/edit/delete sonrası
 */
(function () {
  'use strict';

  const TTL_MS = 30 * 1000;          // 30 saniye fresh
  const CACHE_KEY = 'minerva_items_cache_v1';
  const CACHE_TS  = 'minerva_items_cache_ts_v1';

  function readCache() {
    try {
      const ts = parseInt(sessionStorage.getItem(CACHE_TS) || '0', 10);
      if (!ts || (Date.now() - ts) > TTL_MS) return null;
      const raw = sessionStorage.getItem(CACHE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function writeCache(items) {
    try {
      sessionStorage.setItem(CACHE_KEY, JSON.stringify(items));
      sessionStorage.setItem(CACHE_TS, String(Date.now()));
    } catch (e) { /* QuotaExceeded? sessizce devam */ }
  }

  window.invalidateItemsCache = function () {
    try {
      sessionStorage.removeItem(CACHE_KEY);
      sessionStorage.removeItem(CACHE_TS);
    } catch (e) { /* */ }
  };

  /**
   * /api/items'ı 30sn cache ile çek.
   * @param {boolean} [forceRefresh=false] — cache'i atla, taze çek
   * @returns {Promise<Array>}  items array
   */
  window.fetchItems = async function (forceRefresh) {
    if (!forceRefresh) {
      const cached = readCache();
      if (cached) return cached;
    }
    const res = await fetch('/api/items');
    if (!res.ok) {
      // 401'i fetch-guard handle eder; biz sadece hatayı yukarı fırlat
      throw new Error('Items fetch HTTP ' + res.status);
    }
    const items = await res.json();
    writeCache(items);
    return items;
  };
})();
