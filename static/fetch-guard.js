/**
 * Minerva108 — Global fetch guard
 * ===============================
 *
 * Tüm sayfalardaki window.fetch çağrılarını sarar.  Cookie tabanlı oturum
 * süresi doldurduğunda backend 401 döner; pek çok sayfa bunu sessizce
 * yutuyor ("Yükleniyor..." takılı kalmasının başlıca sebebi).  Bu wrapper:
 *
 *   1. Her fetch yanıtını kontrol eder.
 *   2. 401 dönerse bir kerelik banner gösterip /login'e yönlendirir.
 *   3. Login sayfası bu dosyayı yüklemez (sonsuz redirect'i önler).
 *
 * Çağıran kodlar değişmeye gerek yok — wrapper transparent çalışır.
 * Sonuç hâlâ fetch() Response'u; çağıran kod kendi data işlemesini yapar
 * AMA redirect zaten başlatıldığı için sayfa yenilenecek.
 */
(function () {
  'use strict';

  // Login sayfasında redirect loop'a girmesin
  if (window.location.pathname === '/login') return;

  let redirecting = false;

  function handleUnauthorized() {
    if (redirecting) return;
    redirecting = true;
    // Kullanıcıya kısa bir bilgi göster, sonra yönlendir
    const banner = document.createElement('div');
    banner.setAttribute('role', 'alert');
    banner.style.cssText = `
      position:fixed; top:0; left:0; right:0; z-index:99999;
      background:#fef3c7; color:#92400e; padding:12px 16px;
      border-bottom:2px solid #f59e0b; text-align:center;
      font-family:'Jost',sans-serif; font-size:0.92rem; font-weight:600;
      box-shadow:0 4px 12px rgba(0,0,0,0.15);
    `;
    banner.textContent = '⚠ Oturum süresi doldu — giriş sayfasına yönlendiriliyorsunuz…';
    document.body.appendChild(banner);
    setTimeout(function () {
      // Tarayıcı geri zinciri için redirect param'ı taşı
      var next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = '/login?next=' + next;
    }, 1500);
  }

  var _origFetch = window.fetch.bind(window);
  window.fetch = function () {
    var args = arguments;
    return _origFetch.apply(window, args).then(function (response) {
      // Sadece API çağrılarında uygula; static / HTML normalde 401 dönmez
      try {
        var url = (typeof args[0] === 'string') ? args[0] : (args[0] && args[0].url) || '';
        // /api/login muaf — login form'u 401'i kendi handle eder
        if (response.status === 401 && url.indexOf('/api/') !== -1 && url.indexOf('/api/login') === -1) {
          handleUnauthorized();
        }
      } catch (e) { /* paranoia */ }
      return response;
    });
  };
})();
