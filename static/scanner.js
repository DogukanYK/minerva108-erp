/**
 * Minerva108 — Reusable barcode scanner (auto-camera variant).
 *
 * Wraps the html5-qrcode library's LOW-LEVEL Html5Qrcode API rather than
 * Html5QrcodeScanner — that lets us bypass the built-in camera picker UI
 * and start the optimal camera directly.
 *
 *     openBarcodeScanner((scannedText) => {
 *         // do something with scannedText
 *     });
 *
 * Camera-pick logic
 * ─────────────────
 * 1. Try Html5Qrcode.getCameras() to enumerate.
 *      • Filter out anything labeled "front", "selfie", "user", "ön".
 *      • Among rear-facing devices, prefer one labeled "macro" / "makro"
 *        (best at close-range high-density barcodes / QR labels).
 *      • If no macro, take the LAST back-cam (multi-cam phones tend to
 *        list main → tele → wide → ultrawide; the last entry is empirically
 *        the highest-resolution close-focus option).
 * 2. If enumeration fails or returns 0 devices, fall back to the
 *      `{ facingMode: "environment" }` constraint so the browser picks
 *      ANY rear camera. Still no picker shown.
 *
 * Teardown: closing the modal (any cause) calls Html5Qrcode.stop() so the
 * green camera light goes off cleanly.
 */
(function () {
  'use strict';

  let modalEl = null;
  let bsModal = null;
  let scannerInstance = null;       // Html5Qrcode instance (or null)

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
          <div class="modal-body" style="padding:0;">
            <div id="barcodeReader" style="width:100%;min-height:320px;background:#000;"></div>
            <div id="barcodeScanCamLabel"
                 style="text-align:center;color:#9ca3af;font-size:0.74rem;margin:0.45rem 0.6rem 0;font-family:monospace;"></div>
            <div id="barcodeScanHint"
                 style="text-align:center;color:#6b7280;font-size:0.82rem;margin:0.4rem 0.6rem 1rem;">
              Kamerayı barkoda doğrultun. Tarama başarılı olduğunda otomatik kapanır.
            </div>
            <div id="barcodeScanError"
                 style="display:none;color:#dc2626;text-align:center;font-size:0.85rem;margin:0 0.6rem 1rem;font-weight:600;"></div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(modalEl);

    // Always tear down the camera stream when the modal hides
    modalEl.addEventListener('hidden.bs.modal', () => {
      if (scannerInstance) {
        try { scannerInstance.stop().catch(() => {}); } catch (_e) { /* ignore */ }
        scannerInstance = null;
      }
      // Wipe the reader DOM so the next open starts clean
      const r = document.getElementById('barcodeReader');
      if (r) r.innerHTML = '';
      const lbl = document.getElementById('barcodeScanCamLabel');
      if (lbl) lbl.textContent = '';
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

  function setCamLabel(label, picked) {
    const el = document.getElementById('barcodeScanCamLabel');
    if (!el) return;
    const tag = picked === 'macro'  ? '🔍 Makro kamera'
              : picked === 'rear'   ? '📷 Arka kamera'
              : picked === 'fallback' ? '📷 Kamera'
              : '📷';
    el.textContent = `${tag} · ${label || '—'}`;
  }

  /**
   * Scores enumerated cameras and returns the best one for barcode work.
   * @returns {{ id: string, label: string, picked: 'macro'|'rear'|'fallback' }|null}
   */
  async function pickBestCamera() {
    if (typeof Html5Qrcode === 'undefined' || !Html5Qrcode.getCameras) return null;
    let cams;
    try {
      cams = await Html5Qrcode.getCameras();
    } catch (_e) {
      return null;
    }
    if (!Array.isArray(cams) || cams.length === 0) return null;

    const lc = (s) => (s || '').toLowerCase();
    const isFront = (c) => /front|selfie|user|facetime|ön/.test(lc(c.label));
    const isMacro = (c) => /macro|makro|micro|mikro|close[\s-]?up/.test(lc(c.label));

    const back = cams.filter(c => !isFront(c));
    const macro = back.find(isMacro);
    if (macro)         return { id: macro.id, label: macro.label, picked: 'macro' };
    if (back.length)   return { id: back[back.length - 1].id, label: back[back.length - 1].label, picked: 'rear' };
    return { id: cams[cams.length - 1].id, label: cams[cams.length - 1].label, picked: 'fallback' };
  }

  /**
   * @param {(text: string) => void} callback
   */
  window.openBarcodeScanner = async function (callback) {
    if (!('mediaDevices' in navigator) || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      alert('Bu cihaz / tarayıcı kamera erişimini desteklemiyor.');
      return;
    }
    if (typeof Html5Qrcode === 'undefined') {
      alert('Tarayıcı kütüphanesi yüklenemedi. İnternet bağlantınızı kontrol edip sayfayı yenileyin.');
      return;
    }

    const el = ensureModal();
    const errEl = document.getElementById('barcodeScanError');
    if (errEl) errEl.style.display = 'none';

    if (!bsModal) bsModal = new bootstrap.Modal(el);
    bsModal.show();

    // Wait for the modal's reader div to be visible before starting the camera
    await new Promise(r => setTimeout(r, 200));

    const onDecoded = (decodedText) => {
      // Stop + close — single-shot scan
      if (scannerInstance) {
        scannerInstance.stop().catch(() => {});
        scannerInstance = null;
      }
      bsModal.hide();
      if (typeof callback === 'function') {
        try { callback(String(decodedText || '').trim()); }
        catch (e) { console.error('[scanner] callback error:', e); }
      }
    };
    const onFrameErr = () => { /* per-frame "no QR found" — silent */ };
    const cfg = {
      fps: 10,
      qrbox: { width: 260, height: 160 },
      aspectRatio: 1.4,
      // Help with poorly-lit shipping labels
      experimentalFeatures: { useBarCodeDetectorIfSupported: true },
    };

    try {
      scannerInstance = new Html5Qrcode('barcodeReader', /* verbose */ false);

      const pick = await pickBestCamera();

      if (pick) {
        setCamLabel(pick.label, pick.picked);
        try {
          await scannerInstance.start(pick.id, cfg, onDecoded, onFrameErr);
          return;
        } catch (e1) {
          console.warn('[scanner] start with picked id failed:', e1);
          // Fall through to facingMode constraint
        }
      }

      // Fallback: ask the browser for "rear, no preference"
      setCamLabel('environment-mode', 'fallback');
      try {
        await scannerInstance.start({ facingMode: 'environment' }, cfg, onDecoded, onFrameErr);
      } catch (e2) {
        // Last try: any camera (some laptops / VMs don't honor environment)
        try {
          await scannerInstance.start({ facingMode: 'user' }, cfg, onDecoded, onFrameErr);
        } catch (e3) {
          showError('Kamera başlatılamadı: ' + (e3 && e3.message ? e3.message : e3));
          if (scannerInstance) {
            scannerInstance = null;
          }
        }
      }
    } catch (e) {
      showError('Tarayıcı başlatılamadı: ' + (e && e.message ? e.message : e));
    }
  };
})();
