/* ─────────────────────────────────────────────────────────────────────────────
 * Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
 * Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Lotu başka karta taşı — paylaşılan pencere (06.10.2026).
 *
 * 05.10'da Naturalya'nın jojoba/portakal/lavanta numuneleri KRK GIDA ve İPEDA
 * kartlarına girilmişti; lab bu lotları doğru tedarikçinin kartına KENDİSİ
 * taşısın diye (kullanıcı kararı).  Giriş noktaları: Stoklar → lot detayı
 * "Taşı" (templates/ledger.html) ve Ürünler → Numune sekmesi "Karta taşı"
 * (templates/items.html).
 *
 *   window.openLotMoveModal({inventory_id, item_id, item_name, unit,
 *     lot_number, quantity, is_sample, supplier_id, supplier_name,
 *     category?, material_group_id?, reason?, onDone})
 *
 * API: POST /api/inventory/lots/{inventory_id}/move  (routers/inventory.py)
 *   {target_item_id, quantity?, lot_number?, reason}
 * Kurallar SUNUCUDADIR (core/stock_lots.move_lot) — burada yalnız liste
 * süzülür ve kullanıcıya önceden söylenir:
 *  • Numune lotu stok değildir: yalnız kart değişir, bütün olarak taşınır.
 *  • Stoktaki lot: kaynak −q / hedef +q "Lot taşındı" düzeltmesi (alım ya da
 *    tüketim DEĞİL); kısmi taşıma yalnız burada.  `inventory.adjust` ister.
 *  • Hedef: aynı tür (hammadde/ambalaj), aynı birim ailesi, Bitmiş Ürün asla.
 *    Liste aynı grup > lotun tedarikçisi + benzer ad > benzer ad sırasında.
 *  • 409 `lot_collision`: hedefte aynı lot no başka tedarikçiyle kayıtlı →
 *    yeni lot no sorulur, istek `lot_number` ile tekrarlanır.
 *  • Sınıf öneki `lm-`; `btn-*` adı YASAK (Bootstrap gölgeleme tuzağı).
 *  • Kendi katmanıdır (Bootstrap modal değil): açık bir Bootstrap modalı
 *    varsa önce gizlenir (odak tuzağı girişleri kilitlemesin), pencere
 *    kapanınca geri açılır.
 */
(function () {
  'use strict';

  // ─── Yardımcılar ───────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fold(s) {
    return window.trFold ? window.trFold(s)
      : String(s == null ? '' : s).toLocaleLowerCase('tr').split(/\s+/).filter(Boolean).join(' ');
  }
  function fmt(n) {
    return Number(n || 0).toLocaleString('tr-TR', { maximumFractionDigits: 4 });
  }
  function num(v) {
    if (v === '' || v == null) return null;
    var n = parseFloat(String(v).replace(',', '.'));
    return isFinite(n) ? n : null;
  }
  function toast(msg, type) {
    if (window.showToast) window.showToast(msg, type || 'error');
    else window.alert(msg);
  }
  function detailText(d, fallback) {
    if (d && typeof d.detail === 'string') return d.detail;
    if (d && Array.isArray(d.detail) && d.detail.length) {
      return 'Geçersiz giriş: ' + d.detail.slice(0, 2).map(function (x) { return x.msg || ''; }).join(' · ');
    }
    return fallback;
  }

  // core/purchase_plan.unit_norm'un karşılığı — farklı birim ailesini sunucu
  // zaten 400 ile reddeder; burada yalnız listeyi süzmek / uyarmak için.
  function unitKey(u) {
    u = String(u || '').trim().toLowerCase();
    if (u === 'g' || u === 'gr' || u === 'gram') return 'g';
    if (u === 'kg') return 'kg';
    if (u === 'ml') return 'ml';
    if (u === 'l' || u === 'lt' || u === 'litre' || u === 'liter') return 'l';
    return 'adet';
  }
  // core/stock_lots.lot_kind karşılığı.
  function lotKind(cat) {
    var c = String(cat || '').trim();
    if (c === 'Bitmiş Ürün') return 'finished';
    if (c === 'Ambalaj') return 'packaging';
    return 'raw';
  }
  // routers/inventory._names_similar karşılığı: parantez içi ("(NUMUNE)")
  // atılır; birinin sözcükleri diğerinde tamamen geçmeli, tek sözcükte en
  // az 5 harf ("YAĞ" her yağa uymasın).
  function nameTokens(s) {
    var f = fold(String(s || '').replace(/\([^)]*\)/g, ' '));
    var out = [];
    f.replace(/[^0-9a-z]+/g, ' ').split(' ').forEach(function (t) {
      if (t && out.indexOf(t) < 0) out.push(t);
    });
    return out;
  }
  function similar(a, b) {
    var as = [nameTokens(a.name), nameTokens(a.name_tr)];
    var bs = [nameTokens(b.name), nameTokens(b.name_tr)];
    for (var i = 0; i < as.length; i++) {
      for (var j = 0; j < bs.length; j++) {
        var x = as[i], y = bs[j];
        var small = x.length <= y.length ? x : y, big = small === x ? y : x;
        if (!small.length) continue;
        if (!small.every(function (t) { return big.indexOf(t) >= 0; })) continue;
        if (small.length >= 2 || small[0].length >= 5) return true;
      }
    }
    return false;
  }
  // Ürünler sayfası (çevirme penceresi, numune girişi ipucu) aynı kuralları
  // kullanır — ikinci bir kopya yazılmasın.
  window.lotMoveHelpers = { unitKey: unitKey, lotKind: lotKind, similar: similar, fold: fold };

  var CSS = [
    '.lm-overlay{position:fixed;inset:0;z-index:1085;background:rgba(15,23,42,.45);display:flex;align-items:center;justify-content:center;padding:16px;}',
    '.lm-box{background:var(--white,#fff);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.25);width:100%;max-width:640px;max-height:calc(100vh - 32px);display:flex;flex-direction:column;font-family:"Jost",sans-serif;}',
    '.lm-head{display:flex;align-items:center;justify-content:space-between;gap:.75rem;padding:1rem 1.25rem;border-bottom:1px solid #f0ece4;}',
    '.lm-head h5{margin:0;font-family:"Playfair Display",serif;font-size:1.08rem;font-weight:700;color:var(--navy,#232E6E);}',
    '.lm-x{border:none;background:transparent;font-size:1.4rem;line-height:1;color:#9ca3af;cursor:pointer;padding:0 .25rem;}',
    '.lm-x:hover{color:#374151;}',
    '.lm-body{padding:1rem 1.25rem;overflow-y:auto;}',
    '.lm-foot{display:flex;justify-content:flex-end;gap:.5rem;padding:.85rem 1.25rem;border-top:1px solid #f0ece4;}',
    '.lm-sum{padding:.6rem .85rem;border-radius:10px;background:#f9f7f3;border:1px solid #f0ece4;font-size:.86rem;margin-bottom:.6rem;}',
    '.lm-sum b{color:var(--navy,#232E6E);}',
    '.lm-smp{display:inline-block;background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe;border-radius:10px;padding:0 7px;font-size:.66rem;font-weight:700;margin-left:4px;vertical-align:middle;}',
    '.lm-note{font-size:.8rem;color:#6b7280;margin:0 0 .75rem;}',
    '.lm-label{display:block;font-size:.74rem;font-weight:600;color:#6b7280;letter-spacing:.06em;text-transform:uppercase;margin:.65rem 0 .3rem;}',
    '.lm-input{width:100%;border:1.5px solid #e5e7eb;border-radius:8px;padding:.5rem .75rem;font-family:inherit;font-size:.9rem;background:var(--white,#fff);color:inherit;}',
    '.lm-input:focus{outline:none;border-color:var(--navy,#232E6E);box-shadow:0 0 0 3px rgba(44,44,115,.10);}',
    '.lm-qty{max-width:200px;}',
    '.lm-list{border:1px solid #e5e7eb;border-radius:8px;margin-top:.4rem;max-height:250px;overflow-y:auto;}',
    '.lm-opt{display:flex;gap:.55rem;align-items:flex-start;padding:.45rem .7rem;border-bottom:1px solid #f3f0ea;cursor:pointer;margin:0;font-size:.86rem;}',
    '.lm-opt:last-child{border-bottom:none;}',
    '.lm-opt:hover{background:#faf8f4;}',
    '.lm-opt.lm-on{background:#f5f3ff;}',
    '.lm-opt.lm-off{cursor:not-allowed;opacity:.55;}',
    '.lm-opt input{margin-top:.25rem;accent-color:var(--navy,#232E6E);}',
    '.lm-opt-name{font-weight:600;}',
    '.lm-opt-meta{display:block;font-size:.74rem;color:#6b7280;}',
    '.lm-why{display:inline-block;background:#fef3c7;color:#92400e;border-radius:8px;padding:0 6px;font-size:.68rem;font-weight:600;margin-left:4px;}',
    '.lm-empty{padding:.8rem;color:#9ca3af;font-size:.84rem;text-align:center;}',
    '.lm-picked{font-size:.82rem;margin-top:.4rem;color:#15803d;}',
    '.lm-warn{margin-top:.75rem;padding:.6rem .85rem;border-radius:8px;background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;font-size:.82rem;}',
    '.lm-err{margin-top:.75rem;padding:.55rem .85rem;border-radius:8px;background:#fff1f1;border:1px solid #fca5a5;color:#b91c1c;font-size:.84rem;}',
    '.lm-go,.lm-cancel{padding:.55rem 1.3rem;border-radius:8px;font-family:inherit;font-size:.9rem;font-weight:600;cursor:pointer;}',
    '.lm-go{background:var(--navy,#232E6E);color:#fff;border:none;}',
    '.lm-go:disabled{opacity:.6;cursor:wait;}',
    '.lm-cancel{background:transparent;color:#6b7280;border:1.5px solid #e5e7eb;}',
    ':root[data-theme="dark"] .lm-sum{background:var(--surface-2,#232B45);border-color:var(--border,#2A3454);}',
    ':root[data-theme="dark"] .lm-head,:root[data-theme="dark"] .lm-foot,:root[data-theme="dark"] .lm-opt{border-color:var(--border,#2A3454);}',
    ':root[data-theme="dark"] .lm-input,:root[data-theme="dark"] .lm-list,:root[data-theme="dark"] .lm-cancel{border-color:var(--border,#2A3454);}',
    ':root[data-theme="dark"] .lm-opt:hover,:root[data-theme="dark"] .lm-opt.lm-on{background:var(--surface-2,#232B45);}',
    ':root[data-theme="dark"] .lm-head h5{color:var(--text,#E2E5EC);}',
    // dark.css span/div'i `color: inherit` yapar — pastel zeminler koyu karşılığa (aynı palet)
    ':root[data-theme="dark"] .lm-smp{background:#1A1F3F;color:#C7B5F9;border-color:#3B3F7A;}',
    ':root[data-theme="dark"] .lm-why,:root[data-theme="dark"] .lm-warn{background:#2D2114;color:#FCD34D;border-color:#5A4520;}',
    ':root[data-theme="dark"] .lm-err{background:#2E1414;color:#FCA5A5;border-color:#5A1F1F;}',
    ':root[data-theme="dark"] .lm-picked{color:#86EFAC;}',
    ':root[data-theme="dark"] .lm-opt input{accent-color:var(--gold);}',
    // dark.css .btn-save / .btn-cancel karşılıkları
    ':root[data-theme="dark"] .lm-go{background:var(--navy);color:var(--text,#E2E5EC);border:1px solid var(--gold);}',
    ':root[data-theme="dark"] .lm-go:hover{background:var(--gold);color:#1A1208;}',
    ':root[data-theme="dark"] .lm-cancel{background:var(--white);color:var(--text-muted,#8A93AB);}',
    ':root[data-theme="dark"] .lm-cancel:hover{border-color:var(--gold);color:var(--gold);}',
    '@media (max-width:576px){.lm-overlay{padding:8px;}.lm-box{max-height:calc(100vh - 16px);}.lm-qty{max-width:none;}}'
  ].join('\n');

  function ensureStyle() {
    if ($('lmStyle')) return;
    var st = document.createElement('style');
    st.id = 'lmStyle';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  // ─── Durum ─────────────────────────────────────────────────────────────────
  var S = null;   // açık pencere: {o, items, src, srcKind, targetId, collision, busy, prev}

  // Açık Bootstrap modalı (ör. Stoklar'daki lot detayı) önce gizlenir; yoksa
  // onun odak tuzağı bu penceredeki girişlere yazdırmaz.  Kapanınca geri açılır.
  function hidePrevModal() {
    var el = document.querySelector('.modal.show');
    if (!el || !window.bootstrap || !window.bootstrap.Modal) return null;
    var prev = { el: el, hidden: false };
    el.addEventListener('hidden.bs.modal', function () { prev.hidden = true; }, { once: true });
    window.bootstrap.Modal.getOrCreateInstance(el).hide();
    return prev;
  }
  function restorePrev(prev) {
    if (!prev) return;
    var inst = window.bootstrap.Modal.getOrCreateInstance(prev.el);
    if (prev.hidden) inst.show();
    else prev.el.addEventListener('hidden.bs.modal', function () { inst.show(); }, { once: true });
  }

  function onKey(e) {
    if (e.key === 'Escape' && S) {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  }

  function close() {
    if (!S) return;
    var prev = S.prev;
    S = null;
    document.removeEventListener('keydown', onKey, true);
    var ov = $('lmOverlay');
    if (ov) ov.remove();
    restorePrev(prev);
  }

  function showErr(msg) {
    var el = $('lmErr');
    if (!el) return;
    el.textContent = msg || '';
    el.hidden = !msg;
  }

  // ─── Hedef listesi ─────────────────────────────────────────────────────────
  var WHY = ['Aynı malzeme grubu', 'Lotun tedarikçisi · benzer ad', 'Benzer ad', 'Lotun tedarikçisi', ''];

  function ranked() {
    var o = S.o;
    var q = fold($('lmQ').value);
    var toks = q ? q.split(' ') : [];
    var uk = unitKey(o.unit);
    var gid = o.material_group_id || null;
    var lotSup = o.supplier_id || null;
    var out = [];
    S.items.forEach(function (it) {
      if (it.id === o.item_id) return;
      var k = lotKind(it.category);
      if (k === 'finished') return;
      if (S.srcKind && k !== S.srcKind) return;
      if (toks.length && !toks.every(function (t) { return it._hay.indexOf(t) >= 0; })) return;
      var sameGroup = !!gid && it.material_group_id === gid;
      var sameSup = !!lotSup && it.supplier_id === lotSup;
      var sim = similar(S.src, it);
      var tier = sameGroup ? 0 : (sameSup && sim) ? 1 : sim ? 2 : sameSup ? 3 : 4;
      if (!toks.length && tier > 2) return;      // aramasız: yalnız öneriler
      var unitOk = unitKey(it.unit) === uk;
      out.push({ it: it, tier: unitOk ? tier : 9, unitOk: unitOk });
    });
    out.sort(function (a, b) {
      return (a.tier - b.tier) || String(a.it.name).localeCompare(String(b.it.name), 'tr');
    });
    return out.slice(0, 60);
  }

  function renderList() {
    if (!S) return;
    var list = $('lmList');
    if (S.loadError) {
      list.innerHTML = '<div class="lm-empty">Kart listesi alınamadı — sayfayı yenileyip tekrar deneyin.</div>';
      return;
    }
    var rows = ranked();
    var q = $('lmQ').value.trim();
    if (!rows.length) {
      list.innerHTML = '<div class="lm-empty">' + (q
        ? 'Aramaya uyan kart yok.'
        : 'Önerilecek kart bulunamadı — hedef kartı adıyla arayın.') + '</div>';
      return;
    }
    var unit = S.o.unit || '';
    list.innerHTML = rows.map(function (r) {
      var it = r.it;
      var on = S.targetId === it.id;
      var meta = [];
      if (it.supplier_name) meta.push('🚚 ' + esc(it.supplier_name));
      meta.push('stok ' + fmt(it.current_stock) + ' ' + esc(it.unit || ''));
      if (Number(it.sample_qty || 0) > 0) meta.push('numune ' + fmt(it.sample_qty));
      if (it.material_group_name) meta.push('grup: ' + esc(it.material_group_name));
      var why = r.unitOk ? WHY[r.tier] : ('Birim farklı (' + esc(it.unit || '—') + ' ≠ ' + esc(unit || '—') + ')');
      return '<label class="lm-opt' + (on ? ' lm-on' : '') + (r.unitOk ? '' : ' lm-off') + '">' +
        '<input type="radio" name="lmTarget" value="' + it.id + '"' + (on ? ' checked' : '') +
        (r.unitOk ? '' : ' disabled') + '>' +
        '<span><span class="lm-opt-name">' + esc(it.name) + '</span>' +
        (it.name_tr ? ' <span style="color:#6b7280;">· ' + esc(it.name_tr) + '</span>' : '') +
        (why ? '<span class="lm-why">' + why + '</span>' : '') +
        '<span class="lm-opt-meta">#' + it.id + ' · ' + meta.join(' · ') + '</span></span>' +
        '</label>';
    }).join('');
  }

  function renderPicked() {
    var el = $('lmPicked');
    if (!el) return;
    var it = S.items.find(function (x) { return x.id === S.targetId; });
    el.innerHTML = it
      ? '<i class="bi bi-check2-circle"></i> Hedef: <b>' + esc(it.name) + '</b>' +
        (it.supplier_name ? ' — ' + esc(it.supplier_name) : '')
      : '';
  }

  function hideCollision() {
    S.collision = null;
    var w = $('lmLotWrap');
    if (w) w.hidden = true;
  }

  function showCollision(d) {
    var o = S.o;
    var ex = d.existing || {};
    S.collision = { lot_number: d.lot_number || o.lot_number };
    $('lmLotWrap').hidden = false;
    $('lmLotMsg').innerHTML =
      'Hedef kartta <b>' + esc(S.collision.lot_number) + '</b> lot numarası ' +
      (ex.supplier_name ? '<b>' + esc(ex.supplier_name) + '</b> tedarikçisiyle' : 'başka bir tedarikçiyle') +
      ' kayıtlı (' + fmt(ex.quantity) + ' ' + esc(o.unit || '') + '). İki farklı tedarikçinin malı aynı ' +
      'lot numarasında birleşemez — bu lot için yeni bir lot numarası girin.';
    var lotEl = $('lmLot');
    if (!lotEl.value) {
      var tag = String(o.supplier_name || '').trim().split(/\s+/)[0] || '2';
      lotEl.value = (S.collision.lot_number + '-' + tag).slice(0, 100);
    }
    showErr('');
    lotEl.focus();
    lotEl.select();
  }

  // ─── Gönder ────────────────────────────────────────────────────────────────
  async function submit() {
    if (!S || S.busy) return;
    var o = S.o;
    showErr('');
    if (!S.targetId) { showErr('Hedef kartı listeden seçin.'); return; }
    var reason = $('lmReason').value.trim();
    if (reason.length < 3) { showErr('Sebep zorunlu (en az 3 karakter).'); $('lmReason').focus(); return; }
    var body = { target_item_id: S.targetId, reason: reason.slice(0, 200) };
    var have = Number(o.quantity || 0);
    if (!o.is_sample) {
      var q = num($('lmQty').value);
      if (q == null || q <= 0) { showErr('Taşınacak miktar sıfırdan büyük olmalı.'); return; }
      if (q > have + 1e-9) { showErr('Lotta ' + fmt(have) + ' ' + (o.unit || '') + ' var; daha fazlası taşınamaz.'); return; }
      if (q < have - 1e-9) body.quantity = q;      // boş = lotun tamamı
    }
    if (S.collision) {
      var lot = $('lmLot').value.trim();
      if (!lot) { showErr('Yeni lot numarasını girin.'); return; }
      if (lot === S.collision.lot_number) { showErr('Hedefte zaten kayıtlı olmayan bir lot numarası girin.'); return; }
      body.lot_number = lot;
    }
    var go = $('lmGo');
    S.busy = true;
    go.disabled = true;
    go.textContent = 'Taşınıyor…';
    var cur = S;
    try {
      var r = await fetch('/api/inventory/lots/' + encodeURIComponent(o.inventory_id) + '/move', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      var d = {};
      try { d = await r.json(); } catch (e) { /* JSON değil */ }
      if (S !== cur) return;                       // kullanıcı bu arada kapattı
      if (r.status === 409 && d.code === 'lot_collision') { showCollision(d); return; }
      if (!r.ok) { showErr(detailText(d, 'Lot taşınamadı.')); return; }
      toast(d.message || 'Lot taşındı.', 'success');
      if (window.invalidateItemsCache) window.invalidateItemsCache();
      var cb = o.onDone;
      close();
      if (typeof cb === 'function') {
        try { cb(d); } catch (e) { console.error('lot-move onDone', e); }
      }
    } catch (e) {
      if (S === cur) showErr('Sunucuya bağlanılamadı.');
    } finally {
      if (S === cur) {
        S.busy = false;
        go.disabled = false;
        go.textContent = 'Taşı';
      }
    }
  }

  // ─── Pencere ───────────────────────────────────────────────────────────────
  function build(o) {
    var unit = esc(o.unit || '');
    var qty = fmt(o.quantity) + ' ' + unit;
    var note = o.is_sample
      ? 'Numune lotu stok değildir: yalnız bağlı olduğu kart değişir, stok ve hareket defteri ' +
        'etkilenmez. Numune bütün olarak taşınır (' + qty + ').'
      : 'Stoktaki lot: kaynak kartta −, hedef kartta + "Lot taşındı" düzeltmesi yazılır (alım ya da ' +
        'tüketim değildir). Bir kısmını taşırsanız kalanı bu kartta kalır.';
    var qtyBlock = o.is_sample ? '' :
      '<label class="lm-label" for="lmQty">Taşınacak miktar (' + unit + ')</label>' +
      '<input id="lmQty" type="number" class="lm-input lm-qty" min="0" step="any" max="' +
      Number(o.quantity || 0) + '" value="' + Number(o.quantity || 0) + '">' +
      '<div class="lm-note" style="margin-top:.25rem;">Lotta ' + qty + ' var.</div>';
    var ov = document.createElement('div');
    ov.className = 'lm-overlay';
    ov.id = 'lmOverlay';
    ov.setAttribute('role', 'dialog');
    ov.setAttribute('aria-modal', 'true');
    ov.setAttribute('aria-labelledby', 'lmTitle');
    ov.innerHTML =
      '<div class="lm-box">' +
        '<div class="lm-head"><h5 id="lmTitle">Lotu başka karta taşı</h5>' +
          '<button type="button" class="lm-x" data-lm="close" aria-label="Kapat">&times;</button></div>' +
        '<div class="lm-body">' +
          '<div class="lm-sum">Lot <b>' + esc(o.lot_number || '—') + '</b>' +
            (o.is_sample ? '<span class="lm-smp">NUMUNE</span>' : '') +
            ' · <b>' + qty + '</b>' +
            (o.supplier_name ? ' · 🚚 ' + esc(o.supplier_name) : '') +
            '<br>Şu an: <b>' + esc(o.item_name || '—') + '</b> kartında</div>' +
          '<p class="lm-note">' + note + '</p>' +
          '<label class="lm-label" for="lmQ">Hedef kart</label>' +
          '<input id="lmQ" type="text" class="lm-input" autocomplete="off" ' +
            'placeholder="Kart ara — ad, Türkçe ad ya da tedarikçi">' +
          '<div class="lm-list" id="lmList" role="radiogroup" aria-label="Hedef kart">' +
            '<div class="lm-empty">Kartlar yükleniyor…</div></div>' +
          '<div class="lm-picked" id="lmPicked"></div>' +
          qtyBlock +
          '<div class="lm-warn" id="lmLotWrap" hidden>' +
            '<div id="lmLotMsg"></div>' +
            '<label class="lm-label" for="lmLot">Yeni lot numarası</label>' +
            '<input id="lmLot" type="text" class="lm-input" maxlength="100" autocomplete="off">' +
          '</div>' +
          '<label class="lm-label" for="lmReason">Sebep *</label>' +
          '<input id="lmReason" type="text" class="lm-input" maxlength="200" autocomplete="off" ' +
            'placeholder="Örn: Naturalya numunesi yanlışlıkla KRK GIDA kartına girilmiş">' +
          '<div class="lm-err" id="lmErr" hidden></div>' +
        '</div>' +
        '<div class="lm-foot">' +
          '<button type="button" class="lm-cancel" data-lm="close">Vazgeç</button>' +
          '<button type="button" class="lm-go" id="lmGo">Taşı</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(ov);
    if (o.reason) $('lmReason').value = String(o.reason).slice(0, 200);

    ov.addEventListener('click', function (e) {
      if (e.target.closest('[data-lm="close"]')) close();
    });
    $('lmGo').addEventListener('click', submit);
    $('lmQ').addEventListener('input', renderList);
    $('lmList').addEventListener('change', function (e) {
      if (!S || e.target.name !== 'lmTarget') return;
      var id = parseInt(e.target.value, 10);
      if (id !== S.targetId) hideCollision();     // çakışma hedefe özgü
      S.targetId = id;
      renderList();
      renderPicked();
    });
    $('lmReason').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); submit(); }
    });
    document.addEventListener('keydown', onKey, true);
  }

  async function loadTargets(cur) {
    var items = null;
    try {
      items = window.fetchItems ? await window.fetchItems()
        : await fetch('/api/items').then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)); });
    } catch (e) { items = null; }
    if (S !== cur) return;
    if (!items) { S.loadError = true; renderList(); return; }
    S.items = items.map(function (it) {
      return Object.assign({}, it, {
        _hay: fold([it.name, it.name_tr, it.supplier_name, it.material_group_name, '#' + it.id].join(' ')),
      });
    });
    var src = S.items.find(function (x) { return x.id === S.o.item_id; });
    S.src = { name: S.o.item_name || (src && src.name) || '', name_tr: (src && src.name_tr) || '' };
    var cat = S.o.category != null ? S.o.category : (src ? src.category : null);
    S.srcKind = cat != null ? lotKind(cat) : null;   // pasif kaynak kart listede yok → süzme yok
    if (!S.o.material_group_id && src) S.o.material_group_id = src.material_group_id || null;
    if (S.o.preselect_target_id && S.items.some(function (x) { return x.id === S.o.preselect_target_id; })) {
      S.targetId = S.o.preselect_target_id;
    }
    renderList();
    renderPicked();
  }

  window.openLotMoveModal = function (opts) {
    if (!opts || !opts.inventory_id) return;
    if (S) close();
    ensureStyle();
    var o = Object.assign({}, opts);
    S = { o: o, items: [], src: { name: o.item_name || '', name_tr: '' }, srcKind: null,
          targetId: null, collision: null, busy: false, loadError: false, prev: hidePrevModal() };
    build(o);
    setTimeout(function () { var q = $('lmQ'); if (q) q.focus(); }, 30);
    loadTargets(S);
  };
})();
