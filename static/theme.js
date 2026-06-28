/* Minerva 108 — Theme controller
   - MOBİL (dar ekran / uygulama): DAİMA açık tema. Koyu tema asla uygulanmaz,
     kullanıcıya seçenek sunulmaz, toggle etkisizdir.
   - Masaüstü: localStorage('minerva-theme') tercihi → yoksa sistem (prefers-color-scheme).
   - <head> parse anında senkron çalışır (flash olmaması için).
   - window.toggleTheme() kenar çubuğu düğmesi için.
*/
(function () {
  const KEY = 'minerva-theme';
  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
  }

  // ── Mobil: her zaman açık tema ──────────────────────────────────────────
  const isMobile = window.matchMedia &&
    window.matchMedia('(max-width: 768px)').matches;
  if (isMobile) {
    apply('light');
    window.toggleTheme = function () { apply('light'); }; // etkisiz — hep açık
    return;
  }

  // ── Masaüstü: tercih / sistem ───────────────────────────────────────────
  function systemPref() {
    return (window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
      ? 'dark' : 'light';
  }
  const stored = localStorage.getItem(KEY);
  apply(stored || systemPref());

  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', e => {
        if (!localStorage.getItem(KEY)) apply(e.matches ? 'dark' : 'light');
      });
  }

  window.toggleTheme = function () {
    const curr = document.documentElement.getAttribute('data-theme');
    const next = curr === 'dark' ? 'light' : 'dark';
    localStorage.setItem(KEY, next);
    apply(next);
  };
})();
