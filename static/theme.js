/* Minerva 108 — Theme controller
   - Reads stored preference from localStorage('minerva-theme')
   - Falls back to system (prefers-color-scheme)
   - Applies BEFORE first paint to avoid flash
   - Exposes window.toggleTheme() for the sidebar button
*/
(function () {
  const KEY = 'minerva-theme';
  function systemPref() {
    return (window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
      ? 'dark' : 'light';
  }
  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
  }
  // Boot — synchronous, runs at <head> parse time
  const stored = localStorage.getItem(KEY);
  apply(stored || systemPref());

  // React to system changes if user hasn't picked manually
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', e => {
        if (!localStorage.getItem(KEY)) apply(e.matches ? 'dark' : 'light');
      });
  }

  // Public toggle
  window.toggleTheme = function () {
    const curr = document.documentElement.getAttribute('data-theme');
    const next = curr === 'dark' ? 'light' : 'dark';
    localStorage.setItem(KEY, next);
    apply(next);
  };
})();
