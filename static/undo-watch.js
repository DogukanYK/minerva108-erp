/**
 * Minerva108 — Ctrl+Z / Undo helper
 * ==================================
 *
 * Sayfaya inject edilen küçük helper:
 *  1) Sağ-üst köşeye sabit "↶" buton koyar (varsa).
 *  2) Ctrl+Z (Mac: Cmd+Z) kısayolunu yakalar — form input'larında değilse.
 *  3) /api/undo/peek ile son undoable hareket var mı kontrol eder; button
 *     enable/disable + tooltip ona göre.
 *  4) Undo tetiklenince /api/undo'yu çağırır, toast'la sonuç gösterir,
 *     sayfayı reload eder (her sayfanın data state'i temiz başlasın).
 *
 * Bağımlılıklar:
 *  - window.showToast(msg, level)  — static/toast.js'ten gelir
 *  - Login durumu: /api/undo/peek 401 dönerse buton gizli kalır
 *
 * Login dışı sayfalarda (/login) çağrılırsa session-policy gibi sessiz failure.
 */
(function () {
  'use strict';

  // Login sayfasındaysa skip — peek API'sini boşuna çağırmayalım
  if (/\/login\b/.test(location.pathname)) return;

  let lastPeek = null;        // son cevap; tooltip için
  let inFlight = false;       // double-undo'yu engelle

  function toast(msg, lvl) {
    if (typeof window.showToast === 'function') window.showToast(msg, lvl);
    else console.log('[undo]', lvl || 'info', msg);
  }

  async function peek() {
    try {
      const res = await fetch('/api/undo/peek', { credentials: 'same-origin' });
      if (!res.ok) return null;
      return await res.json();
    } catch (_) {
      return null;
    }
  }

  function refreshButtonState() {
    const btn = document.getElementById('undoFloatBtn');
    if (!btn) return;
    const tip = document.getElementById('undoFloatTip');
    if (lastPeek && lastPeek.available) {
      btn.disabled = false;
      btn.style.opacity = '1';
      btn.style.cursor = 'pointer';
      btn.title = `Geri al (Ctrl+Z): ${lastPeek.description}`;
      if (tip) tip.textContent = lastPeek.description;
    } else {
      btn.disabled = true;
      btn.style.opacity = '0.35';
      btn.style.cursor = 'not-allowed';
      btn.title = 'Geri alınacak bir hareket yok';
      if (tip) tip.textContent = '';
    }
  }

  async function performUndo() {
    if (inFlight) return;
    if (!lastPeek || !lastPeek.available) {
      toast('Geri alınacak bir hareket yok.', 'info');
      return;
    }
    inFlight = true;
    const btn = document.getElementById('undoFloatBtn');
    if (btn) btn.style.opacity = '0.5';
    try {
      const res = await fetch('/api/undo', { method: 'POST', credentials: 'same-origin' });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        toast(data.message || 'Geri alındı.', 'success');
        // 0.5sn bekle ki kullanıcı toast'u okusun, sonra reload — sayfa
        // verisinin tutarlı görünmesi için (örneğin items tablosu yeniden çekilsin)
        setTimeout(() => location.reload(), 700);
      } else if (res.status === 409) {
        toast(data.detail || 'Veri değişmiş, geri alınamaz.', 'warning');
      } else if (res.status === 410) {
        toast(data.detail || 'Hedef kayıt artık yok.', 'warning');
      } else {
        toast(data.detail || 'Geri alma başarısız.', 'error');
      }
    } catch (e) {
      toast('Bağlantı hatası — geri alınamadı.', 'error');
    } finally {
      inFlight = false;
      // refresh peek + button state
      lastPeek = await peek();
      refreshButtonState();
    }
  }

  function injectButton() {
    // Eğer sayfada zaten "logoutBtn" yan yana toolbar varsa onun yanına otursun;
    // yoksa floating buton.
    if (document.getElementById('undoFloatBtn')) return;
    const btn = document.createElement('button');
    btn.id = 'undoFloatBtn';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Geri al');
    btn.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i>';
    btn.style.cssText = [
      'position:fixed', 'bottom:1.25rem', 'right:1.25rem',
      'width:48px', 'height:48px', 'border-radius:50%', 'border:none',
      'background:#1f2937', 'color:#fff', 'font-size:1.3rem',
      'box-shadow:0 6px 16px rgba(0,0,0,0.18)', 'z-index:9999',
      'display:flex', 'align-items:center', 'justify-content:center',
      'transition:opacity 0.15s, transform 0.1s',
    ].join(';');
    btn.addEventListener('click', performUndo);
    btn.addEventListener('mousedown', () => btn.style.transform = 'scale(0.94)');
    btn.addEventListener('mouseup',   () => btn.style.transform = 'scale(1)');
    btn.addEventListener('mouseleave',() => btn.style.transform = 'scale(1)');
    document.body.appendChild(btn);
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function bindShortcut() {
    document.addEventListener('keydown', function (e) {
      // Ctrl+Z (Win/Linux) veya Cmd+Z (Mac), Shift OLMASIN (Shift+Ctrl+Z = redo)
      const isUndo = (e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === 'z' || e.key === 'Z');
      if (!isUndo) return;
      // Form input'larındaki text-undo'ya dokunma
      if (isTypingTarget(e.target)) return;
      e.preventDefault();
      performUndo();
    });
  }

  async function init() {
    injectButton();
    bindShortcut();
    lastPeek = await peek();
    refreshButtonState();
    // Periyodik refresh: başka sekmede bir işlem yapıldıysa button güncellenir.
    // 20sn aralık — hafif yük.
    setInterval(async () => {
      lastPeek = await peek();
      refreshButtonState();
    }, 20000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
