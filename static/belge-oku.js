/**
 * Minerva108 — Belge Oku istemcisi (fatura / proforma / irsaliye / CoA → form)
 * ============================================================================
 *
 *   window.M108Doc.mount(container, {
 *     context: 'page' | 'receiving',   // /asistan/belge ya da Mal Kabul sayfası
 *     onUseLine(doc, line, choice),    // "Forma aktar" (receiving); page'de varsayılan:
 *                                      //  sessionStorage + /receiving'e git
 *     docId: <id>                      // açılışta bu belgeyi yükle (GET /documents/{id})
 *   })
 *
 * POST /api/assistant/documents (multipart `file`) → NDJSON `meta` → `ping` →
 * `done {document, reused}` | `error {code,msg}`; ön kontroller JSON (413/415/
 * 400/402/409/429/503).  Asistan HİÇBİR kayıt yazmaz: satır eylemleri mevcut
 * formları doldurur, kaydı kullanıcı kendi yetkisiyle yapar —
 *   • Mal Kabul'e aktar  (inventory.receive): receiving formu (recvItem/Supplier/
 *     Lot/Expiry/Qty) dolar.
 *   • SKT'yi düzelt      (inventory.adjust, lot sistemde varsa): İzlenebilirlik'e
 *     `?lot=&item_id=&inv=&skt=` — orada öneri çubuğu + "Kaydet" (PATCH, audit'li).
 *   • Fiyatı kaydet      (suppliers.prices, SupplierPriceForm yüklüyse): ortak
 *     fiyat formu `prefill` ile satır içinde açılır.
 *
 * Güvenlik: DOM yalnız createElement + textContent (belge metni modelden
 * gelir — XSS kapısı olmasın); bağlantılar yalnız iç yol; sınıf öneki
 * `m108-doc-`; `btn-*` adı YOK (Bootstrap gölgeleme tuzağı).  Koyu tema:
 * dark.css div/span rengini `inherit`e zorlar → renkli metin `c-*` yardımcı
 * sınıfıyla (koyu temada :root[data-theme] önekli kopyası var).
 */
(function () {
  'use strict';
  if (window.M108Doc) return;

  var P = 'm108-doc-';
  var API = '/api/assistant/documents';
  var SS_RECV = 'm108doc.recv';
  var ACCEPT = 'application/pdf,image/jpeg,image/png';
  var MAX_MB = 10;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls.split(' ').map(function (c) { return P + c; }).join(' ');
    if (text != null) n.textContent = String(text);
    return n;
  }
  function icon(name) {
    var i = document.createElement('i');
    if (/^bi-[a-z0-9-]+$/.test(name || '')) i.className = 'bi ' + name;
    i.setAttribute('aria-hidden', 'true');
    return i;
  }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function can(cat, act) { return typeof window.can === 'function' && window.can(cat, act); }
  function toast(msg, lvl) { if (typeof window.showToast === 'function') window.showToast(msg, lvl || 'error'); }
  function num(v) {
    if (v == null || v === '') return '—';
    return Number(v).toLocaleString('tr-TR', { maximumFractionDigits: 4 });
  }
  function money(v) {
    if (v == null || v === '') return '—';
    return Number(v).toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }
  function trDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    return m ? m[3] + '.' + m[2] + '.' + m[1] : (iso || '');
  }
  function button(cls, text, iconName) {
    var b = el('button', cls);
    b.type = 'button';
    if (iconName) b.appendChild(icon(iconName));
    if (text) b.appendChild(el('span', null, text));
    return b;
  }
  function isInternalHref(href) {
    if (typeof href !== 'string' || !/^\/(?![\/\\])/.test(href) || href.indexOf('\\') !== -1) return false;
    try { return new URL(href, location.origin).origin === location.origin; } catch (_) { return false; }
  }

  function injectStyles() {
    if (document.getElementById(P + 'style')) return;
    var css = [
      ':root{--m108-doc-ink:#1f2937;--m108-doc-muted:#6b7280;--m108-doc-faint:#9aa0ab;--m108-doc-line:#e8e3d9;',
      '--m108-doc-line2:#d8d0c0;--m108-doc-soft:#faf8f3;--m108-doc-surface:#fff;--m108-doc-accent:#232E6E;--m108-doc-on-accent:#fff;',
      '--m108-doc-gold:#C9AC6F;--m108-doc-ok:#15803d;--m108-doc-warn:#8a5a00;--m108-doc-warn-bg:#fbf4e4;--m108-doc-bad:#a61b1b;--m108-doc-tint:rgba(35,46,110,.06)}',
      ':root[data-theme="dark"]{--m108-doc-ink:#e2e5ec;--m108-doc-muted:#9aa3ba;--m108-doc-faint:#6b7593;--m108-doc-line:#2a3454;',
      '--m108-doc-line2:#36426a;--m108-doc-soft:#151b30;--m108-doc-surface:#1a2138;--m108-doc-accent:#d9be85;--m108-doc-on-accent:#131b40;',
      '--m108-doc-gold:#d9be85;--m108-doc-ok:#6ee7b7;--m108-doc-warn:#e6cfa0;--m108-doc-warn-bg:rgba(217,190,133,.12);--m108-doc-bad:#fca5a5;--m108-doc-tint:rgba(217,190,133,.10)}',
      '.m108-doc-c-muted{color:var(--m108-doc-muted)}.m108-doc-c-faint{color:var(--m108-doc-faint)}.m108-doc-c-ok{color:var(--m108-doc-ok)}',
      '.m108-doc-c-warn{color:var(--m108-doc-warn)}.m108-doc-c-bad{color:var(--m108-doc-bad)}.m108-doc-c-ink{color:var(--m108-doc-ink)}',
      ':root[data-theme="dark"] .m108-doc-c-muted{color:var(--m108-doc-muted)}:root[data-theme="dark"] .m108-doc-c-faint{color:var(--m108-doc-faint)}',
      ':root[data-theme="dark"] .m108-doc-c-ok{color:var(--m108-doc-ok)}:root[data-theme="dark"] .m108-doc-c-warn{color:var(--m108-doc-warn)}',
      ':root[data-theme="dark"] .m108-doc-c-bad{color:var(--m108-doc-bad)}:root[data-theme="dark"] .m108-doc-c-ink{color:var(--m108-doc-ink)}',
      '.m108-doc-root{font-family:"Jost",system-ui,sans-serif;color:var(--m108-doc-ink)}',
      '.m108-doc-drop{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;padding:1.1rem 1.2rem;border:1.5px dashed var(--m108-doc-line2);',
      'border-radius:12px;background:var(--m108-doc-soft);color:var(--m108-doc-ink);transition:border-color .15s,background .15s}',
      '.m108-doc-drop.m108-doc-over{border-color:var(--m108-doc-gold);background:var(--m108-doc-tint)}',
      '.m108-doc-dicon{flex:none;width:44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;',
      'background:var(--m108-doc-surface);border:1px solid var(--m108-doc-line);color:var(--m108-doc-accent);font-size:1.25rem}',
      '.m108-doc-dtext{flex:1;min-width:200px}',
      '.m108-doc-dtitle{margin:0;font:600 .95rem/1.35 "Jost",system-ui,sans-serif;color:var(--m108-doc-ink)}',
      '.m108-doc-dsub{font-size:.78rem;margin-top:.15rem}',
      '.m108-doc-actions{display:flex;gap:.5rem;flex-wrap:wrap}',
      '.m108-doc-b{display:inline-flex;align-items:center;gap:.4rem;height:36px;padding:0 .8rem;border:1px solid var(--m108-doc-line2);',
      'border-radius:8px;background:var(--m108-doc-surface);color:var(--m108-doc-ink);font:500 .82rem "Jost",system-ui,sans-serif;',
      'cursor:pointer;text-decoration:none;white-space:nowrap}',
      '.m108-doc-b:hover{border-color:var(--m108-doc-gold);color:var(--m108-doc-ink);text-decoration:none}',
      '.m108-doc-b:disabled{opacity:.45;cursor:not-allowed}',
      '.m108-doc-main{background:var(--m108-doc-accent);border-color:var(--m108-doc-accent);color:var(--m108-doc-on-accent)}',
      '.m108-doc-main:hover{color:var(--m108-doc-on-accent);filter:brightness(1.08)}',
      '.m108-doc-sm{height:30px;padding:0 .6rem;font-size:.76rem}',
      '.m108-doc-b:focus-visible,.m108-doc-sel:focus-visible,.m108-doc-link:focus-visible{outline:2px solid var(--m108-doc-gold);outline-offset:2px}',
      '.m108-doc-busy{display:flex;align-items:center;gap:.7rem;margin-top:.9rem;padding:.8rem 1rem;border:1px solid var(--m108-doc-line);',
      'border-radius:10px;background:var(--m108-doc-surface);font-size:.86rem;color:var(--m108-doc-ink)}',
      '.m108-doc-spin{flex:none;width:14px;height:14px;border-radius:50%;border:2px solid var(--m108-doc-line2);',
      'border-top-color:var(--m108-doc-gold);animation:m108-doc-rot .8s linear infinite}',
      '@keyframes m108-doc-rot{to{transform:rotate(360deg)}}',
      '@media (prefers-reduced-motion:reduce){.m108-doc-spin{animation:none;border-color:var(--m108-doc-gold)}}',
      '.m108-doc-msg{margin-top:.9rem;padding:.6rem .8rem;border:1px solid var(--m108-doc-line);border-left:3px solid var(--m108-doc-bad);',
      'border-radius:8px;font-size:.84rem;color:var(--m108-doc-bad);background:var(--m108-doc-surface)}',
      '.m108-doc-card{margin-top:1rem;border:1px solid var(--m108-doc-line);border-radius:12px;background:var(--m108-doc-surface);',
      'color:var(--m108-doc-ink);overflow:hidden}',
      '.m108-doc-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;flex-wrap:wrap;padding:1rem 1.2rem;',
      'border-bottom:1px solid var(--m108-doc-line)}',
      '.m108-doc-title{margin:0;font:600 1rem/1.35 "Jost",system-ui,sans-serif;color:var(--m108-doc-ink)}',
      '.m108-doc-meta{display:flex;gap:.35rem 1rem;flex-wrap:wrap;margin-top:.3rem;font-size:.8rem}',
      '.m108-doc-sup{display:inline-flex;align-items:center;gap:.35rem;font-size:.84rem}',
      '.m108-doc-badge{display:inline-flex;align-items:center;gap:.3rem;padding:.12rem .55rem;border-radius:999px;font-size:.72rem;',
      'border:1px solid var(--m108-doc-line);background:var(--m108-doc-soft)}',
      '.m108-doc-warns{margin:0;padding:.7rem 1.2rem .7rem 2.4rem;background:var(--m108-doc-warn-bg);color:var(--m108-doc-warn);',
      'font-size:.8rem;border-bottom:1px solid var(--m108-doc-line)}',
      '.m108-doc-warns li{margin:.1rem 0}',
      '.m108-doc-tablewrap{overflow-x:auto}',
      '.m108-doc-table{width:100%;border-collapse:collapse;font-size:.83rem}',
      '.m108-doc-table th{text-align:left;font-size:.68rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;',
      'color:var(--m108-doc-muted);background:var(--m108-doc-soft);padding:.55rem .75rem;white-space:nowrap}',
      '.m108-doc-table td{border-top:1px solid var(--m108-doc-line);padding:.65rem .75rem;vertical-align:top;color:var(--m108-doc-ink)}',
      '.m108-doc-table .m108-doc-num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}',
      '.m108-doc-mat{font-weight:500}',
      '.m108-doc-small{font-size:.74rem;margin-top:.15rem}',
      '.m108-doc-sel{width:100%;min-width:180px;max-width:280px;height:32px;padding:0 .5rem;border:1px solid var(--m108-doc-line2);',
      'border-radius:7px;background:var(--m108-doc-surface);color:var(--m108-doc-ink);font:400 .8rem "Jost",system-ui,sans-serif}',
      '.m108-doc-lw{margin:.3rem 0 0;padding:0;list-style:none;font-size:.72rem}',
      '.m108-doc-lw li::before{content:"• "}',
      '.m108-doc-rowact{display:flex;flex-direction:column;gap:.35rem;align-items:stretch;min-width:150px}',
      '.m108-doc-done{border-color:var(--m108-doc-ok);color:var(--m108-doc-ok)}',
      '.m108-doc-pricehost{padding:0 .75rem .75rem}',
      '.m108-doc-foot{display:flex;align-items:center;justify-content:space-between;gap:.75rem;flex-wrap:wrap;padding:.8rem 1.2rem;font-size:.84rem;',
      'border-top:1px solid var(--m108-doc-line);background:var(--m108-doc-soft)}',
      '.m108-doc-coa summary{cursor:pointer;padding:.7rem 1.2rem;font-size:.84rem;font-weight:500;color:var(--m108-doc-ink);border-top:1px solid var(--m108-doc-line)}',
      '.m108-doc-recent{margin-top:1.2rem}',
      '.m108-doc-rtitle{margin:0 0 .4rem;font-size:.68rem;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--m108-doc-muted)}',
      '.m108-doc-rlist{display:flex;flex-direction:column;gap:.35rem}',
      '.m108-doc-ritem{display:flex;align-items:center;gap:.6rem;width:100%;padding:.55rem .7rem;border:1px solid var(--m108-doc-line);',
      'border-radius:9px;background:var(--m108-doc-surface);color:var(--m108-doc-ink);font:400 .84rem "Jost",system-ui,sans-serif;text-align:left;cursor:pointer}',
      '.m108-doc-ritem:hover{border-color:var(--m108-doc-gold)}',
      '.m108-doc-sr{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;padding:0;margin:-1px}',
      '@media (max-width:720px){.m108-doc-table thead{display:none}.m108-doc-table,.m108-doc-table tbody,.m108-doc-table tr,.m108-doc-table td{display:block;width:100%}',
      '.m108-doc-table tr{border-top:1px solid var(--m108-doc-line);padding:.4rem 0}.m108-doc-table td{border:0;padding:.3rem .9rem}',
      '.m108-doc-table .m108-doc-num{text-align:left}.m108-doc-table td[data-label]::before{content:attr(data-label);display:block;',
      'font-size:.66rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--m108-doc-muted)}',
      '.m108-doc-sel{max-width:none}.m108-doc-rowact{flex-direction:row;flex-wrap:wrap}}'
    ].join('');
    var st = document.createElement('style');
    st.id = P + 'style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  // ─── Yardımcılar: NDJSON, birim, fiyat ───────────────────────────────────

  function readNdjson(res, onEvent) {
    var reader = res.body.getReader();
    var dec = new TextDecoder('utf-8');
    var buf = '';
    function emit(line) {
      line = line.trim();
      if (!line) return;
      var ev = null;
      try { ev = JSON.parse(line); } catch (_) { ev = null; }
      if (ev) onEvent(ev);
    }
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) { buf += dec.decode(); emit(buf); return; }
        buf += dec.decode(r.value, { stream: true });
        var i;
        while ((i = buf.indexOf('\n')) >= 0) { emit(buf.slice(0, i)); buf = buf.slice(i + 1); }
        return pump();
      });
    }
    return pump();
  }

  // core/assistant/documents.unit_family karşılığı (kg↔l ÇEVRİLMEZ)
  function unitFam(u) {
    u = String(u || '').trim().toLowerCase();
    if (['g', 'gr', 'gram', 'grams', 'grs'].indexOf(u) >= 0) return 'g';
    if (['kg', 'kgs', 'kilogram', 'kilograms'].indexOf(u) >= 0) return 'kg';
    if (u === 'ml') return 'ml';
    if (['l', 'lt', 'ltr', 'litre', 'liter', 'liters', 'litres'].indexOf(u) >= 0) return 'l';
    return 'adet';
  }
  var FACTORS = { 'g>kg': 0.001, 'kg>g': 1000, 'ml>l': 0.001, 'l>ml': 1000 };
  function convertQty(q, from, to) {
    if (q == null) return { qty: null, note: '' };
    var a = unitFam(from), b = unitFam(to);
    if (!from || a === b) return { qty: q, note: '' };
    var f = FACTORS[a + '>' + b];
    if (f) {
      var v = Math.round(q * f * 1e6) / 1e6;
      return { qty: v, note: num(q) + ' ' + a + ' = ' + num(v) + ' ' + b };
    }
    return { qty: null, note: 'Birim farklı: belgede ' + from + ', kartta ' + to + ' — miktarı elle çevirin.' };
  }
  // Fiyat formu kg | l | adet ister; belgede g / ml başına fiyat varsa ×1000
  function pricePrefill(doc, line, item) {
    var allowed = window.SupplierPriceForm ? window.SupplierPriceForm.priceUnitsFor(item.unit) : ['kg', 'l', 'adet'];
    var u = unitFam(line.price_unit || line.unit);
    var price = line.unit_price, note = '';
    if (u === 'g' || u === 'ml') {
      price = Math.round(price * 1000 * 1e6) / 1e6;
      note = ' (belgede ' + num(line.unit_price) + ' / ' + u + ')';
      u = u === 'g' ? 'kg' : 'l';
    }
    var cur = String(doc.currency || '').toUpperCase();
    return {
      unit_price: price,
      currency: ['USD', 'EUR', 'TRY'].indexOf(cur) >= 0 ? cur : null,
      price_unit: allowed.indexOf(u) >= 0 ? u : null,
      quoted_at: doc.document_date || null,
      vat_included: doc.vat_included, vat_rate: doc.vat_rate,
      note: ('Belgeden: ' + (doc.file_name || '') + (doc.document_no ? ' · ' + doc.document_no : '') + note).slice(0, 2000),
      source_label: ('Belge okuma — ' + (doc.file_name || '')).slice(0, 120)
    };
  }

  // ─── Mount ───────────────────────────────────────────────────────────────

  function mount(container, opts) {
    opts = opts || {};
    injectStyles();
    var ctx = opts.context === 'receiving' ? 'receiving' : 'page';
    clear(container);
    var root = el('div', 'root');
    container.appendChild(root);

    var drop = el('section', 'drop');
    var dicon = el('div', 'dicon');
    dicon.appendChild(icon('bi-file-earmark-arrow-up'));
    var dtext = el('div', 'dtext');
    dtext.appendChild(el('h3', 'dtitle', ctx === 'receiving' ? 'Belgeden doldur' : 'Belge yükleyin'));
    dtext.appendChild(el('div', 'dsub c-muted',
      'Fatura, proforma, irsaliye ya da analiz sertifikası (CoA) · PDF, JPG, PNG · en çok ' + MAX_MB + ' MB, 20 sayfa'));
    var acts = el('div', 'actions');
    var pick = button('b main', 'Dosya seç', 'bi-upload');
    var cam = button('b', 'Fotoğraf çek', 'bi-camera');
    var fileIn = document.createElement('input');
    fileIn.type = 'file'; fileIn.accept = ACCEPT; fileIn.hidden = true;
    fileIn.setAttribute('aria-label', 'Belge dosyası seç');
    var camIn = document.createElement('input');
    camIn.type = 'file'; camIn.accept = 'image/*'; camIn.hidden = true;
    camIn.setAttribute('capture', 'environment');
    camIn.setAttribute('aria-label', 'Belgenin fotoğrafını çek');
    acts.appendChild(pick); acts.appendChild(cam);
    drop.appendChild(dicon); drop.appendChild(dtext); drop.appendChild(acts);
    drop.appendChild(fileIn); drop.appendChild(camIn);
    var status = el('div', null);
    status.setAttribute('role', 'status');
    var cardHost = el('div', null);
    var recentHost = el('div', null);
    root.appendChild(drop); root.appendChild(status); root.appendChild(cardHost); root.appendChild(recentHost);

    var busy = false, timer = null, current = null;

    pick.addEventListener('click', function () { if (!busy) fileIn.click(); });
    cam.addEventListener('click', function () { if (!busy) camIn.click(); });
    fileIn.addEventListener('change', function () { if (fileIn.files && fileIn.files[0]) upload(fileIn.files[0]); fileIn.value = ''; });
    camIn.addEventListener('change', function () { if (camIn.files && camIn.files[0]) upload(camIn.files[0]); camIn.value = ''; });
    drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add(P + 'over'); });
    drop.addEventListener('dragleave', function () { drop.classList.remove(P + 'over'); });
    drop.addEventListener('drop', function (e) {
      e.preventDefault();
      drop.classList.remove(P + 'over');
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) upload(f);
    });

    function setBusy(name) {
      clear(status);
      var b = el('div', 'busy');
      var sp = el('span', 'spin'); sp.setAttribute('aria-hidden', 'true');
      var t = el('span', null, name + ' okunuyor…');
      var secs = el('span', 'c-faint', '');
      b.appendChild(sp); b.appendChild(t); b.appendChild(secs);
      status.appendChild(b);
      var start = Date.now();
      clearInterval(timer);
      timer = setInterval(function () {
        var s = Math.round((Date.now() - start) / 1000);
        secs.textContent = s + ' sn · okuma genelde 10–40 sn sürer, sayfadan ayrılmayın';
      }, 1000);
      pick.disabled = cam.disabled = true;
    }
    function stopBusy() {
      clearInterval(timer);
      clear(status);
      pick.disabled = cam.disabled = false;
      busy = false;
    }
    function showError(msg) {
      clear(status);
      status.appendChild(el('div', 'msg', msg));
    }

    function upload(file) {
      if (busy || !file) return;
      if (file.size > MAX_MB * 1024 * 1024) { showError('Dosya en fazla ' + MAX_MB + ' MB olabilir.'); return; }
      busy = true;
      setBusy(file.name || 'Belge');
      var fd = new FormData();
      fd.append('file', file, file.name || 'belge');
      fetch(API, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/x-ndjson' } })
        .then(function (res) {
          if (!res.ok) {
            return res.json().catch(function () { return {}; }).then(function (d) {
              var msg = (d && typeof d.detail === 'string') ? d.detail : ('Belge okunamadı (' + res.status + ').');
              if (res.status === 429 && d && d.retry_after_seconds) msg += ' ' + Math.ceil(d.retry_after_seconds) + ' sn sonra tekrar deneyin.';
              throw new Error(msg);
            });
          }
          var result = null, err = null;
          return readNdjson(res, function (ev) {
            if (ev.t === 'done') result = ev;
            else if (ev.t === 'error') err = ev.msg || 'Belge okunamadı.';
          }).then(function () {
            if (err) throw new Error(err);
            if (!result) throw new Error('Bağlantı kesildi; belge okunmuş olabilir — "Son okunan belgeler"den açın.');
            return result;
          });
        })
        .then(function (result) {
          stopBusy();
          render(result.document, !!result.reused);
          loadRecent();
        })
        .catch(function (e) { stopBusy(); showError((e && e.message) || 'Belge okunamadı.'); });
    }

    function load(id) {
      return fetch(API + '/' + encodeURIComponent(id), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) { render(d, false); return d; })
        .catch(function () { showError('Belge açılamadı (silinmiş ya da başka panelde olabilir).'); return null; });
    }

    // ─── Sonuç kartı ────────────────────────────────────────────────────────

    function render(doc, reused) {
      current = doc;
      clear(cardHost);
      clear(status);
      var card = el('section', 'card');
      card.setAttribute('aria-label', 'Belge çıkarımı');
      var head = el('header', 'head');
      var left = el('div', null);
      left.appendChild(el('h3', 'title', (doc.doc_type_label || 'Belge') + (doc.document_no ? ' · ' + doc.document_no : '')));
      var meta = el('div', 'meta c-muted');
      if (doc.document_date || doc.document_date_raw) meta.appendChild(el('span', null, 'Tarih: ' + (trDate(doc.document_date) || doc.document_date_raw)));
      meta.appendChild(el('span', null, doc.file_name + (doc.pages ? ' · ' + doc.pages + ' sayfa' : '')));
      if (reused) meta.appendChild(el('span', 'badge', 'Daha önce okundu — yeniden ücret yok'));
      left.appendChild(meta);
      var sup = doc.supplier || {};
      var supLine = el('div', 'sup');
      var supChoice = { id: sup.match ? sup.match.id : null, name: sup.match ? sup.match.name : '' };
      supLine.appendChild(el('span', 'c-muted', 'Tedarikçi:'));
      if (sup.match) {
        var ok = el('span', 'c-ok');
        ok.appendChild(icon('bi-check2-circle'));
        ok.appendChild(document.createTextNode(' ' + sup.match.name));
        supLine.appendChild(ok);
        if (sup.text && sup.text !== sup.match.name) supLine.appendChild(el('span', 'c-faint', '(belgede: ' + sup.text + ')'));
      } else if (sup.text) {
        supLine.appendChild(el('span', 'c-warn', sup.text + ' — sistemde kesin eşleşmedi'));
        if ((sup.candidates || []).length) {
          var ss = el('select', 'sel');
          ss.setAttribute('aria-label', 'Tedarikçi kartı');
          var o0 = document.createElement('option'); o0.value = ''; o0.textContent = '— tedarikçi seçin —';
          ss.appendChild(o0);
          sup.candidates.forEach(function (c) {
            var o = document.createElement('option'); o.value = String(c.id);
            o.textContent = c.name + (c.active ? '' : ' (pasif)');
            ss.appendChild(o);
          });
          ss.addEventListener('change', function () {
            var c = (sup.candidates || []).find(function (x) { return String(x.id) === ss.value; });
            supChoice.id = c ? c.id : null; supChoice.name = c ? c.name : '';
          });
          supLine.appendChild(ss);
        }
      } else {
        supLine.appendChild(el('span', 'c-faint', 'belgede okunamadı'));
      }
      left.appendChild(supLine);
      head.appendChild(left);
      var tot = el('div', 'meta c-muted');
      var cur = doc.currency || '';
      if (doc.subtotal != null) tot.appendChild(el('span', null, 'Ara toplam: ' + money(doc.subtotal) + ' ' + cur));
      if (doc.vat_amount != null) tot.appendChild(el('span', null, 'KDV' + (doc.vat_rate != null ? ' %' + num(doc.vat_rate) : '') + ': ' + money(doc.vat_amount) + ' ' + cur));
      if (doc.total != null) tot.appendChild(el('span', 'c-ink', 'Toplam: ' + money(doc.total) + ' ' + cur));
      head.appendChild(tot);
      card.appendChild(head);

      if ((doc.warnings || []).length) {
        var ul = el('ul', 'warns');
        doc.warnings.forEach(function (w) { ul.appendChild(el('li', null, w)); });
        card.appendChild(ul);
      }

      var wrap = el('div', 'tablewrap');
      var table = el('table', 'table');
      var thead = document.createElement('thead');
      var hr = document.createElement('tr');
      ['Belgedeki malzeme', 'Sistemdeki kart', 'Lot · SKT', 'Miktar', 'Birim fiyat', ''].forEach(function (h) {
        var th = document.createElement('th'); th.textContent = h; hr.appendChild(th);
      });
      thead.appendChild(hr); table.appendChild(thead);
      var tbody = document.createElement('tbody');
      (doc.lines || []).forEach(function (line) { renderLine(tbody, doc, line, supChoice); });
      if (!(doc.lines || []).length) {
        var tr0 = document.createElement('tr'); var td0 = document.createElement('td');
        td0.colSpan = 6; td0.textContent = 'Belgede malzeme satırı bulunamadı.'; tr0.appendChild(td0); tbody.appendChild(tr0);
      }
      table.appendChild(tbody); wrap.appendChild(table); card.appendChild(wrap);

      if ((doc.coa_results || []).length) {
        var det = el('details', 'coa');
        var sm = document.createElement('summary');
        sm.textContent = 'CoA test sonuçları (' + doc.coa_results.length + ')';
        det.appendChild(sm);
        var cw = el('div', 'tablewrap');
        var ct = el('table', 'table');
        var ch = document.createElement('thead'); var chr = document.createElement('tr');
        ['Test', 'Spesifikasyon', 'Sonuç', 'Uygun mu'].forEach(function (h) { var th = document.createElement('th'); th.textContent = h; chr.appendChild(th); });
        ch.appendChild(chr); ct.appendChild(ch);
        var cb = document.createElement('tbody');
        doc.coa_results.forEach(function (r) {
          var tr = document.createElement('tr');
          [r.test, r.spec || '—', r.result || '—'].forEach(function (v, k) {
            var td = document.createElement('td'); td.textContent = v;
            td.setAttribute('data-label', ['Test', 'Spesifikasyon', 'Sonuç'][k]); tr.appendChild(td);
          });
          var tp = document.createElement('td'); tp.setAttribute('data-label', 'Uygun mu');
          tp.appendChild(el('span', r.pass === true ? 'c-ok' : r.pass === false ? 'c-bad' : 'c-faint',
            r.pass === true ? 'Uygun' : r.pass === false ? 'Uygun değil' : '—'));
          tr.appendChild(tp); cb.appendChild(tr);
        });
        ct.appendChild(cb); cw.appendChild(ct); det.appendChild(cw); card.appendChild(det);
      }

      var foot = el('footer', 'foot');
      foot.appendChild(el('span', 'c-muted', 'Alanları kontrol edin — kaydı ilgili formda siz yaparsınız; asistan kayıt yazmaz.'));
      var fa = el('div', 'actions');
      if (ctx === 'page') {
        var askLink = el('a', 'b');
        askLink.href = '/asistan?doc=' + encodeURIComponent(doc.id);
        askLink.appendChild(icon('bi-chat-left-text'));
        askLink.appendChild(el('span', null, 'Bu belge hakkında sor'));
        fa.appendChild(askLink);
      }
      var again = button('b', 'Yeni belge', 'bi-plus-lg');
      again.addEventListener('click', function () { clear(cardHost); current = null; pick.focus(); });
      fa.appendChild(again);
      foot.appendChild(fa);
      card.appendChild(foot);
      cardHost.appendChild(card);
      if (typeof opts.onRendered === 'function') opts.onRendered(doc);
    }

    function renderLine(tbody, doc, line, supChoice) {
      var tr = document.createElement('tr');
      var choice = {};
      function setItem(it) {
        choice.itemId = it ? it.id : null;
        choice.itemName = it ? it.name : '';
        choice.itemUnit = it ? (it.unit || '') : '';
        var c = it ? convertQty(line.quantity, line.unit, it.unit) : { qty: null, note: '' };
        choice.qty = c.qty;
        qtyNote.textContent = c.note || '';
      }

      var td1 = document.createElement('td');
      td1.setAttribute('data-label', 'Belgedeki malzeme');
      td1.appendChild(el('div', 'mat', line.material));
      var sub = [line.material_code, line.package].filter(Boolean).join(' · ');
      if (sub) td1.appendChild(el('div', 'small c-muted', sub));
      if ((line.warnings || []).length) {
        var lw = el('ul', 'lw c-warn');
        line.warnings.forEach(function (w) { lw.appendChild(el('li', null, w)); });
        td1.appendChild(lw);
      }

      var td2 = document.createElement('td');
      td2.setAttribute('data-label', 'Sistemdeki kart');
      var sel = el('select', 'sel');
      sel.setAttribute('aria-label', 'Satır ' + (line.index + 1) + ' için kart');
      var none = document.createElement('option'); none.value = ''; none.textContent = '— kart seçin —';
      sel.appendChild(none);
      var pool = (line.candidates || []).slice();
      if (line.item && !pool.some(function (c) { return c.id === line.item.id; })) pool.unshift(line.item);
      pool.forEach(function (c) {
        var o = document.createElement('option'); o.value = String(c.id);
        o.textContent = c.name + (c.unit ? ' (' + c.unit + ')' : '');
        sel.appendChild(o);
      });
      var qtyNote = el('div', 'small c-muted', '');
      td2.appendChild(sel);
      td2.appendChild(qtyNote);
      sel.value = line.item ? String(line.item.id) : '';
      setItem(line.item || null);
      sel.addEventListener('change', function () {
        setItem(pool.find(function (c) { return String(c.id) === sel.value; }) || null);
      });

      var td3 = document.createElement('td');
      td3.setAttribute('data-label', 'Lot · SKT');
      td3.appendChild(el('div', 'mat', line.lot || '—'));
      var skt = line.expiry_iso ? trDate(line.expiry_iso) : (line.expiry_raw || '');
      td3.appendChild(el('div', 'small c-muted', skt ? 'SKT ' + skt : 'SKT yok'));
      if (line.retest_date) td3.appendChild(el('div', 'small c-faint', 'Retest ' + trDate(line.retest_date)));
      if (line.lot_exists) td3.appendChild(el('div', 'small c-warn', 'Sistemde kayıtlı lot' + (line.lot_expiry ? ' · SKT ' + trDate(line.lot_expiry) : '')));

      var td4 = document.createElement('td');
      td4.className = P + 'num';
      td4.setAttribute('data-label', 'Miktar');
      td4.textContent = line.quantity != null ? num(line.quantity) + ' ' + (line.unit || '') : '—';

      var td5 = document.createElement('td');
      td5.className = P + 'num';
      td5.setAttribute('data-label', 'Birim fiyat');
      td5.textContent = line.unit_price != null
        ? money(line.unit_price) + ' ' + (doc.currency || '') + (line.price_unit || line.unit ? ' / ' + (line.price_unit || line.unit) : '')
        : '—';
      if (line.line_total != null) td5.appendChild(el('div', 'small c-muted', 'Tutar ' + money(line.line_total) + (doc.currency ? ' ' + doc.currency : '')));

      var td6 = document.createElement('td');
      var act = el('div', 'rowact');
      var priceHostRow = null;
      if (ctx === 'receiving' || can('inventory', 'receive')) {
        var use = button('b sm' + (ctx === 'receiving' ? ' main' : ''), ctx === 'receiving' ? 'Forma aktar' : "Mal Kabul'e aktar",
          ctx === 'receiving' ? 'bi-box-arrow-in-down' : 'bi-box-arrow-in-down');
        use.addEventListener('click', function () {
          var payload = { itemId: choice.itemId, itemName: choice.itemName, itemUnit: choice.itemUnit, qty: choice.qty,
                          supplierId: supChoice.id, supplierName: supChoice.name };
          if (typeof opts.onUseLine === 'function') {
            opts.onUseLine(doc, line, payload);
            use.classList.add(P + 'done');
            clear(use); use.appendChild(icon('bi-check2')); use.appendChild(el('span', null, 'Aktarıldı'));
          } else {
            try { sessionStorage.setItem(SS_RECV, JSON.stringify({ doc: doc.id, line: line.index, choice: payload })); } catch (_) { /* özel mod */ }
            location.href = '/receiving';
          }
        });
        act.appendChild(use);
      }
      if (line.lot_exists && line.lot_inventory_id && line.expiry_iso && can('inventory', 'adjust') &&
          String(line.lot_expiry || '') !== line.expiry_iso) {
        var fix = el('a', 'b sm');
        var href = '/traceability?lot=' + encodeURIComponent(line.lot) + '&item_id=' + encodeURIComponent(line.item ? line.item.id : '') +
          '&inv=' + encodeURIComponent(line.lot_inventory_id) + '&skt=' + encodeURIComponent(line.expiry_iso);
        if (isInternalHref(href)) fix.href = href;
        fix.appendChild(icon('bi-calendar-check'));
        fix.appendChild(el('span', null, "SKT'yi düzelt"));
        fix.title = 'İzlenebilirlik sayfasında lotun SKT\'sini belgedeki tarihle kaydet';
        act.appendChild(fix);
      }
      if (line.unit_price != null && can('suppliers', 'prices') && window.SupplierPriceForm) {
        var pr = button('b sm', 'Fiyatı kaydet', 'bi-tag');
        pr.addEventListener('click', function () {
          if (!choice.itemId) { toast('Önce satırın kartını seçin.'); return; }
          if (priceHostRow) { priceHostRow.parentNode.removeChild(priceHostRow); priceHostRow = null; return; }
          priceHostRow = document.createElement('tr');
          var ptd = document.createElement('td'); ptd.colSpan = 6;
          var host = el('div', 'pricehost');
          ptd.appendChild(host); priceHostRow.appendChild(ptd);
          tr.parentNode.insertBefore(priceHostRow, tr.nextSibling);
          var item = { id: choice.itemId, name: choice.itemName, unit: choice.itemUnit };
          window.SupplierPriceForm.mount(host, {
            mode: 'create', item: item,
            supplierId: supChoice.id || undefined, supplierName: supChoice.name || undefined,
            prefill: pricePrefill(doc, line, item),
            onSaved: function () {
              if (priceHostRow && priceHostRow.parentNode) priceHostRow.parentNode.removeChild(priceHostRow);
              priceHostRow = null;
              pr.classList.add(P + 'done');
              clear(pr); pr.appendChild(icon('bi-check2')); pr.appendChild(el('span', null, 'Fiyat kaydedildi'));
            },
            onCancel: function () {
              if (priceHostRow && priceHostRow.parentNode) priceHostRow.parentNode.removeChild(priceHostRow);
              priceHostRow = null;
            }
          });
        });
        act.appendChild(pr);
      }
      td6.appendChild(act);
      [td1, td2, td3, td4, td5, td6].forEach(function (td) { tr.appendChild(td); });
      tbody.appendChild(tr);
    }

    function loadRecent() {
      if (ctx !== 'page') return;
      fetch(API, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (rows) {
          clear(recentHost);
          if (!rows || !rows.length) return;
          var box = el('section', 'recent');
          box.appendChild(el('h4', 'rtitle', 'Son okunan belgeler'));
          var list = el('div', 'rlist');
          rows.forEach(function (d) {
            var b = el('button', 'ritem'); b.type = 'button';
            b.appendChild(icon(d.doc_type === 'coa' ? 'bi-clipboard2-check' : 'bi-file-earmark-text'));
            b.appendChild(el('span', null, d.file_name));
            b.appendChild(el('span', 'c-faint', (d.doc_type_label || '') + ' · ' + (d.created_at || '')));
            b.addEventListener('click', function () { load(d.id).then(function () { cardHost.scrollIntoView({ behavior: 'smooth', block: 'start' }); }); });
            list.appendChild(b);
          });
          box.appendChild(list);
          recentHost.appendChild(box);
        })
        .catch(function () { /* liste opsiyonel */ });
    }

    loadRecent();
    var api = { load: load, upload: upload, current: function () { return current; } };
    if (opts.docId) load(opts.docId).then(function (d) { if (d && typeof opts.onLoaded === 'function') opts.onLoaded(d, api); });
    return api;
  }

  // Mal Kabul'e aktar → /receiving: bekleyen satırı bir kez okuyup sil
  function takePendingReceive() {
    var v = null;
    try { v = JSON.parse(sessionStorage.getItem(SS_RECV) || 'null'); sessionStorage.removeItem(SS_RECV); } catch (_) { v = null; }
    return v && v.doc ? v : null;
  }

  window.M108Doc = { mount: mount, takePendingReceive: takePendingReceive, convertQty: convertQty };
})();
