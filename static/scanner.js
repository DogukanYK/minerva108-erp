/**
 * Minerva108 — Reusable barcode scanner.
 *
 * Wraps html5-qrcode in a single global function. Any page that wants to
 * accept barcode input calls:
 *
 *     openBarcodeScanner((scannedText) => {
 *         // do something with scannedText
 *     });
 *
 * The function:
 *   • Lazily creates the modal+reader DOM the first time it's used
 *   • Asks for camera permission via the standard browser prompt
 *   • Tries QR + EAN + Code-128 + UPC by default (Html5QrcodeScanner default)
 *   • Calls back with the decoded text on first success, then auto-closes
 *   • Tears down the camera stream cleanly when the modal hides (cancel
 *     OR successful read), so no green-light camera leak
 *
 * The library exposes `Html5QrcodeScanner` globally; if it failed to
 * load (offline, blocked CDN), we surface a friendly error.
 */
(function () {
  'use strict';

  let modalEl = null;
  let scannerInstance = null;
  let bsModal = null;

  function ensureModal() {
    if (modalEl) return modalEl;

    modalEl = document.createElement('div');
    modalEl.id = 'barcodeScannerModal';
    modalEl.className = 'modal fade';
    modalEl.tabIndex = -1;
    modalEl.setAttribute('aria-hidden', 'true');
    modalEl.innerHTML = `
      <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title" style="display:flex;align-items:center;gap:0.5rem;">
              <i class="bi bi-qr-code-scan"></i> Barkod Tara
            </h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Kapat"></button>
          </div>
          <div class="modal-body">
            <div id="barcodeReader" style="width:100%;min-height:300px;"></div>
            <div id="barcodeScanHint"
                 style="text-align:center;color:#6b7280;font-size:0.82rem;margin-top:0.6rem;">
              Kamerayı barkoda doğrultun. Tarama başarılı olduğunda otomatik kapanır.
            </div>
            <div id="barcodeScanError"
                 style="display:none;color:#dc2626;text-align:center;font-size:0.85rem;margin-top:0.6rem;font-weight:600;"></div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(modalEl);

    // Camera teardown on modal close (any cause)
    modalEl.addEventListener('hidden.bs.modal', () => {
      if (scannerInstance) {
        try { scannerInstance.clear(); } catch (_e) { /* ignore */ }
        scannerInstance = null;
      }
    });

    return modalEl;
  }

  function showError(msg) {
    const err = document.getElementById('barcodeScanError');
    if (err) {
      err.textContent = msg;
      err.style.display = 'block';
    }
  }

  /**
   * @param {(text: string) => void} callback  — invoked with the scanned text
   */
  window.openBarcodeScanner = function (callback) {
    if (!('mediaDevices' in navigator) || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      alert('Bu cihaz / tarayıcı kamera erişimini desteklemiyor.');
      return;
    }
    if (typeof Html5QrcodeScanner === 'undefined') {
      alert('Tarayıcı kütüphanesi yüklenemedi. İnternet bağlantınızı kontrol edin.');
      return;
    }

    const el = ensureModal();
    const errEl = document.getElementById('barcodeScanError');
    if (errEl) errEl.style.display = 'none';

    if (!bsModal) bsModal = new bootstrap.Modal(el);
    bsModal.show();

    // The reader div needs to be visible before Html5QrcodeScanner sizes itself.
    setTimeout(() => {
      try {
        scannerInstance = new Html5QrcodeScanner(
          'barcodeReader',
          {
            fps: 10,
            qrbox: { width: 260, height: 160 },
            aspectRatio: 1.45,
            rememberLastUsedCamera: true,
            showTorchButtonIfSupported: true,
          },
          /* verbose */ false
        );

        scannerInstance.render(
          // SUCCESS — single shot, then close
          (decodedText) => {
            if (typeof callback === 'function') {
              try { callback(String(decodedText || '').trim()); }
              catch (e) { console.error('[scanner] callback error:', e); }
            }
            bsModal.hide();
          },
          // FRAME ERRORS — silent (per-frame "no QR found" noise)
          (_frameErr) => { /* no-op */ }
        );
      } catch (e) {
        showError('Kamera başlatılamadı: ' + (e && e.message ? e.message : e));
      }
    }, 250);
  };
})();
