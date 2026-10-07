/* ─────────────────────────────────────────────────────────────────────────────
 * Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
 * Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Elle tedarikçi fiyatı — paylaşılan form (07.10.2026).
 *
 * Lab fiyatı kendisi girer (Songül Hanım; yetki `suppliers.prices` — Manager
 * + LabLead).  İki giriş noktası aynı formu kullanır, doğrulama tek yerde:
 *   • Tedarikçiler → firma → "Fiyatlar" penceresi (tedarikçi sabit)
 *   • Raporlar → Tedarikçi Fiyatları paneli → "Fiyat ekle" / satır kalemi
 *
 *   SupplierPriceForm.mount(container, {mode: 'create'|'edit', row?,
 *     supplierId?, supplierName?, item?: {id, name, unit}, onSaved(row), onCancel()})
 *   `item` verilirse malzeme sabittir (Ürünler → kart → "Tedarikçi tercihi"
 *   satırındaki "fiyat ekle" kısayolu: kart + tedarikçi baştan belli).
 *
 * API (routers/reports.py):
 *   POST /api/supplier-prices {item_id, supplier_id, unit_price, currency,
 *     price_unit, package_size?, quoted_at?, note?}  → 201 satır
 *     409 {code:'price_exists', id} → "zaten var, güncellensin mi?" → PUT
 *   PUT  /api/supplier-prices/{id}  (kısmi) — Excel satırı düzenlenince
 *     'elle' olur, sonraki içe aktarma onu silmez.  Düzenlemedeki 409
 *     (birim/firma başka satırla çakışıyor) köprüye GİRMEZ — mesaj gösterilir.
 * Kurallar SUNUCUDADIR; burada birim seçenekleri malzemenin birim ailesine
 * göre süzülür (core/supplier_prices.price_unit_ok): adetli → adet,
 * kütle/hacim → kg | l.
 *  • Form SAYFA İÇİNDE çizilir (Bootstrap modal içinde ikinci modal yok).
 *  • Sınıf öneki `spf-`; `btn-*` adı YASAK (Bootstrap gölgeleme tuzağı).
 *  • Defer DEĞİL — şablonlar bootstrap.bundle'dan hemen sonra yükler; inline
 *    script biçimlendiricileri (priceText, sourceText) parse anında kullanır.
 */
(function () {
  'use strict';

  var SYM = { USD: '$', EUR: '€', TRY: '₺' };
  var CUR_KEY = 'spf.currency';

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fold(s) {
    return window.trFold ? window.trFold(s)
      : String(s == null ? '' : s).toLocaleLowerCase('tr').split(/\s+/).filter(Boolean).join(' ');
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
    if (d && Array.isArray(d.detail)) return 'Geçersiz değer.';
    return fallback;
  }
  // core/supplier_prices._unit_family karşılığı
  function unitFamily(u) {
    u = String(u || '').trim().toLowerCase();
    if (u === 'g' || u === 'gr' || u === 'kg') return 'mass';
    if (u === 'ml' || u === 'l' || u === 'lt') return 'volume';
    return 'count';
  }
  function priceUnitsFor(u) {
    var f = unitFamily(u);
    if (f === 'count') return ['adet'];
    return f === 'volume' ? ['l', 'kg'] : ['kg', 'l'];
  }
  // Birim fiyat: 1'in altında 4 hane (ambalaj/etiket — 0,1193 $/adet), değilse 2.
  function money(v) {
    if (v == null || v === '') return '—';
    var n = Number(v), d = (n !== 0 && Math.abs(n) < 1) ? 4 : 2;
    return n.toLocaleString('tr-TR', { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function priceText(r) {
    var cur = String(r.currency || '').toUpperCase();
    var sym = SYM[cur] || cur;
    return money(r.unit_price) + (sym ? ' ' + esc(sym) : '') + (r.price_unit ? '/' + esc(r.price_unit) : '');
  }
  function fmtDate(iso) {
    var d = String(iso || '').split('-');
    return d.length === 3 ? d[2] + '.' + d[1] + '.' + d[0] : '';
  }
  // "Elle — Songül Arslan" / "Stok Son Durum" (etiket title'da)
  function sourceText(r) {
    if (r.source === 'manual') {
      var who = r.updated_by || r.created_by;
      return 'Elle' + (who ? ' — ' + who : '');
    }
    if (r.source === 'stok_son_durum') return 'Stok Son Durum';
    return r.source_label || '—';
  }
  function sourceTitle(r) {
    var parts = [];
    if (r.source_label) parts.push('Kaynak: ' + r.source_label);
    if (r.quoted_at) parts.push('Teklif tarihi: ' + fmtDate(r.quoted_at));
    if (r.created_by) parts.push('Giren: ' + r.created_by);
    if (r.updated_by) parts.push('Son düzenleyen: ' + r.updated_by + (r.updated_at ? ' (' + r.updated_at + ')' : ''));
    return parts.join(' · ');
  }
  function todayIso() {
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function lastCurrency() {
    try { return window.localStorage.getItem(CUR_KEY) || 'USD'; } catch (e) { return 'USD'; }
  }
  function rememberCurrency(c) {
    try { window.localStorage.setItem(CUR_KEY, c); } catch (e) { /* özel pencere */ }
  }

  var CSS = [
    '.spf-box{border:1px solid #e5e7eb;border-radius:12px;padding:.9rem 1rem;background:#fbfaf7;margin:.4rem 0 .8rem;font-family:"Jost",sans-serif;}',
    '.spf-title{font-weight:700;color:var(--navy,#232E6E);font-size:.92rem;margin:0 0 .55rem;}',
    '.spf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.55rem .75rem;}',
    '.spf-wide{grid-column:1/-1;}',
    '.spf-label{display:block;font-size:.7rem;font-weight:600;color:#6b7280;letter-spacing:.05em;text-transform:uppercase;margin:0 0 .25rem;}',
    '.spf-input{width:100%;border:1.5px solid #e5e7eb;border-radius:8px;padding:.42rem .6rem;font-family:inherit;font-size:.86rem;background:var(--white,#fff);color:inherit;}',
    '.spf-input:focus{outline:none;border-color:var(--navy,#232E6E);box-shadow:0 0 0 3px rgba(44,44,115,.10);}',
    '.spf-list{width:100%;border:1.5px solid #e5e7eb;border-radius:8px;margin-top:.35rem;font-size:.84rem;background:var(--white,#fff);color:inherit;}',
    '.spf-fixed{font-size:.88rem;padding:.42rem 0;font-weight:600;}',
    '.spf-hint{font-size:.76rem;color:#6b7280;margin:.45rem 0 0;}',
    '.spf-warn{font-size:.78rem;color:#92400e;background:#fef3c7;border-radius:8px;padding:.35rem .6rem;margin:.1rem 0 .5rem;}',
    '.spf-actions{display:flex;justify-content:flex-end;gap:.5rem;margin-top:.75rem;}',
    '.spf-save{padding:.45rem 1.1rem;background:var(--navy,#232E6E);color:#fff;border:none;border-radius:8px;font-family:inherit;font-size:.86rem;font-weight:600;cursor:pointer;}',
    '.spf-save:disabled{opacity:.6;cursor:wait;}',
    '.spf-cancel{padding:.45rem 1rem;background:transparent;color:#6b7280;border:1.5px solid #e5e7eb;border-radius:8px;font-family:inherit;font-size:.86rem;cursor:pointer;}',
    ':root[data-theme="dark"] .spf-box{background:var(--surface-2,#232B45);border-color:var(--border,#2A3454);}',
    ':root[data-theme="dark"] .spf-input,:root[data-theme="dark"] .spf-list{background:var(--white,#1A2138);border-color:var(--border,#2A3454);color:var(--text,#E2E5EC);}',
    ':root[data-theme="dark"] .spf-title{color:var(--gold2,#E6CFA0);}',
    ':root[data-theme="dark"] .spf-warn{background:#3b2f12;color:#fcd34d;}',
  ].join('\n');
  function injectCss() {
    if (document.getElementById('spf-css')) return;
    var st = document.createElement('style');
    st.id = 'spf-css';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  var _items = null, _suppliers = null;
  function loadItems() {
    if (_items) return Promise.resolve(_items);
    var p = window.fetchItems ? window.fetchItems()
      : fetch('/api/items').then(function (r) { return r.ok ? r.json() : []; });
    return p.then(function (list) {
      _items = (list || []).filter(function (i) { return i.category !== 'Bitmiş Ürün'; });
      _items = window.trSortBy ? window.trSortBy(_items, 'name') : _items;
      return _items;
    }).catch(function () { return []; });
  }
  function loadSuppliers() {
    if (_suppliers) return Promise.resolve(_suppliers);
    return fetch('/api/suppliers').then(function (r) { return r.ok ? r.json() : []; })
      .then(function (list) {
        _suppliers = (list || []).filter(function (s) { return s.is_active !== false; });
        _suppliers = window.trSortBy ? window.trSortBy(_suppliers, 'name') : _suppliers;
        return _suppliers;
      }).catch(function () { return []; });
  }

  function unmount(container) {
    if (container) { container.innerHTML = ''; container.hidden = true; }
  }

  function mount(container, opts) {
    opts = opts || {};
    injectCss();
    var edit = opts.mode === 'edit' && opts.row;
    var row = edit ? opts.row : {};
    var lockedSup = edit ? { id: row.supplier_id, name: row.supplier_name }
      : (opts.supplierId ? { id: opts.supplierId, name: opts.supplierName || '' } : null);
    var lockedItem = !edit && opts.item && opts.item.id ? opts.item : null;
    var state = { item: edit ? { id: row.item_id, name: row.material, unit: row.unit } : lockedItem };
    var fixedItem = state.item;

    container.hidden = false;
    container.innerHTML =
      '<div class="spf-box">' +
        '<p class="spf-title">' + (edit ? 'Fiyatı düzenle' : 'Fiyat ekle') + '</p>' +
        (edit && row.source !== 'manual'
          ? '<div class="spf-warn">Excel’den gelen satır — kaydedince “elle” olur; sonraki içe aktarma bu satırı silmez.</div>'
          : '') +
        '<div class="spf-grid">' +
          '<div class="spf-wide"><span class="spf-label">Malzeme</span>' +
            (fixedItem
              ? '<div class="spf-fixed">' + esc(fixedItem.name || '—') + (fixedItem.unit ? ' <span style="color:#9ca3af;font-weight:400;">(' + esc(fixedItem.unit) + ')</span>' : '') + '</div>'
              : '<input type="text" class="spf-input" data-f="search" placeholder="Malzeme ara… (TR/EN ad)" autocomplete="off">' +
                '<select class="spf-list" data-f="item" size="6"><option value="" disabled>Yükleniyor…</option></select>') +
          '</div>' +
          '<div class="spf-wide"><span class="spf-label">Tedarikçi</span>' +
            (lockedSup
              ? '<div class="spf-fixed">' + esc(lockedSup.name || '—') + '</div>'
              : '<select class="spf-input" data-f="supplier"><option value="">— Tedarikçi seçin —</option></select>') +
          '</div>' +
          '<div><span class="spf-label">Birim fiyat</span><input type="number" step="any" min="0" class="spf-input" data-f="price"></div>' +
          '<div><span class="spf-label">Para birimi</span><select class="spf-input" data-f="currency">' +
            '<option value="USD">USD ($)</option><option value="EUR">EUR (€)</option><option value="TRY">TRY (₺)</option></select></div>' +
          '<div><span class="spf-label">Fiyat birimi</span><select class="spf-input" data-f="unit"></select></div>' +
          '<div><span class="spf-label" data-f="pkglabel">Alınabilecek miktar</span><input type="number" step="any" min="0" class="spf-input" data-f="pkg" placeholder="isteğe bağlı"></div>' +
          '<div><span class="spf-label">Teklif tarihi</span><input type="date" class="spf-input" data-f="date"></div>' +
          '<div class="spf-wide"><span class="spf-label">Not</span><input type="text" maxlength="2000" class="spf-input" data-f="note" placeholder="ör. telefonla teyit, 25 kg bidon"></div>' +
        '</div>' +
        '<p class="spf-hint">Adet birimli malzemenin fiyatı adet başınadır; g/kg ve ml/l malzemelerde kg ya da litre başına.</p>' +
        '<div class="spf-actions"><button type="button" class="spf-cancel" data-f="cancel">Vazgeç</button>' +
          '<button type="button" class="spf-save" data-f="save">Kaydet</button></div>' +
      '</div>';

    function f(name) { return container.querySelector('[data-f="' + name + '"]'); }

    function fillUnits() {
      var sel = f('unit');
      var units = state.item ? priceUnitsFor(state.item.unit) : ['kg', 'l', 'adet'];
      var keep = sel.value || (edit ? row.price_unit : '');
      sel.innerHTML = units.map(function (u) {
        return '<option value="' + u + '">' + (u === 'adet' ? 'adet başına' : u === 'l' ? 'litre başına' : 'kg başına') + '</option>';
      }).join('');
      if (keep && units.indexOf(keep) >= 0) sel.value = keep;
      f('pkglabel').textContent = 'Alınabilecek miktar' + (state.item && state.item.unit ? ' (' + state.item.unit + ')' : '');
    }

    // Başlangıç değerleri
    f('price').value = edit && row.unit_price != null ? row.unit_price : '';
    f('currency').value = edit ? (row.currency || 'USD') : lastCurrency();
    if (!f('currency').value) f('currency').value = 'USD';
    f('pkg').value = edit && row.package_size != null ? row.package_size : '';
    f('date').value = edit ? (row.quoted_at || '') : todayIso();
    f('note').value = edit ? (row.note || '') : '';
    fillUnits();

    if (!fixedItem) {
      var itemSel = f('item'), search = f('search');
      var renderItems = function () {
        var q = fold(search.value);
        var list = (_items || []).filter(function (i) {
          return !q || (i._fold || (i._fold = fold(i.name) + ' ' + fold(i.name_tr) + ' ' + fold(i.supplier_name))).indexOf(q) >= 0;
        }).slice(0, 300);
        itemSel.innerHTML = list.length ? list.map(function (i) {
          var lbl = i.name + (i.name_tr ? ' · ' + i.name_tr : '') +
            (i.supplier_name ? ' — ' + i.supplier_name : '') + ' (' + (i.unit || '?') + ')';
          return '<option value="' + i.id + '">' + esc(lbl) + '</option>';
        }).join('') : '<option value="" disabled>Eşleşen malzeme yok</option>';
        if (state.item) {
          itemSel.value = String(state.item.id);
          // Seçili kart süzülmüş listede yoksa seçim düşer: ekranda görünmeyen
          // karta fiyat yazılmasın (görünen seçim = kaydedilen kart).
          if (itemSel.value !== String(state.item.id)) { state.item = null; fillUnits(); }
        }
      };
      search.addEventListener('input', renderItems);
      itemSel.addEventListener('change', function () {
        var id = parseInt(itemSel.value, 10);
        state.item = (_items || []).find(function (i) { return i.id === id; }) || null;
        fillUnits();
        // Tedarikçi boşsa kartın kendi tedarikçisi önerilir (aktifse listede vardır)
        var ss = f('supplier');
        if (ss && !ss.value && state.item && state.item.supplier_id &&
            [].some.call(ss.options, function (o) { return o.value === String(state.item.supplier_id); })) {
          ss.value = String(state.item.supplier_id);
        }
      });
      loadItems().then(renderItems);
      setTimeout(function () { search.focus(); }, 50);
    } else {
      setTimeout(function () { f('price').focus(); }, 50);
    }
    if (!lockedSup) {
      loadSuppliers().then(function (list) {
        var ss = f('supplier');
        if (!ss) return;
        ss.innerHTML = '<option value="">— Tedarikçi seçin —</option>' + list.map(function (s) {
          var tag = s.purchase_status === 'phase_out' ? ' — bitirilecek' : s.purchase_status === 'preferred' ? ' — tercih edilen' : '';
          return '<option value="' + s.id + '">' + esc(s.name + tag) + '</option>';
        }).join('');
      });
    }

    f('cancel').addEventListener('click', function () {
      unmount(container);
      if (opts.onCancel) opts.onCancel();
    });

    var saving = false;
    f('save').addEventListener('click', function () {
      if (saving) return;
      var price = num(f('price').value);
      if (price == null || price <= 0) { toast('Birim fiyat sıfırdan büyük olmalı.'); return; }
      var pkg = num(f('pkg').value);
      if (f('pkg').value.trim() && (pkg == null || pkg <= 0)) { toast('Alınabilecek miktar sıfırdan büyük olmalı (ya da boş).'); return; }
      var body = {
        unit_price: price,
        currency: f('currency').value || 'USD',
        price_unit: f('unit').value,
        package_size: pkg,
        quoted_at: f('date').value || null,
        note: f('note').value.trim() || null,
      };
      var url, method;
      if (edit) {
        url = '/api/supplier-prices/' + row.id; method = 'PUT';
      } else {
        if (!state.item || (!fixedItem && f('item').value !== String(state.item.id))) {
          toast('Malzeme seçin.'); return;
        }
        var sid = lockedSup ? lockedSup.id : parseInt((f('supplier') || {}).value, 10);
        if (!sid) { toast('Tedarikçi seçin.'); return; }
        body.item_id = state.item.id;
        body.supplier_id = sid;
        url = '/api/supplier-prices'; method = 'POST';
      }
      saving = true;
      var btn = f('save'); btn.disabled = true; btn.textContent = 'Kaydediliyor…';
      var done = function () { saving = false; btn.disabled = false; btn.textContent = 'Kaydet'; };
      var send = function (u, m, b) {
        return fetch(u, { method: m, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) })
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; }); });
      };
      send(url, method, body).then(function (res) {
        // "O satır güncellensin mi?" köprüsü YALNIZ yeni fiyatta: düzenlemede
        // diğer satırı güncellemek, düzenlenen satırı bayat bırakırdı — orada
        // sunucunun 409 mesajı gösterilir, form açık kalır.
        if (!edit && res.r.status === 409 && res.d && res.d.code === 'price_exists' && res.d.id) {
          var msg = (res.d.detail || 'Bu malzeme + tedarikçi + birim için fiyat zaten kayıtlı.') +
            '\n\nGirdiğiniz değerlerle o satır güncellensin mi?';
          if (!window.confirm(msg)) return null;
          var upd = Object.assign({}, body); delete upd.item_id; delete upd.supplier_id;
          return send('/api/supplier-prices/' + res.d.id, 'PUT', upd);
        }
        return res;
      }).then(function (res) {
        if (!res) { done(); return; }
        if (!res.r.ok) { toast(detailText(res.d, 'Fiyat kaydedilemedi.')); done(); return; }
        rememberCurrency(body.currency);
        toast(edit ? 'Fiyat güncellendi.' : 'Fiyat kaydedildi.', 'success');
        unmount(container);
        if (opts.onSaved) opts.onSaved(res.d);
      }).catch(function () { toast('Fiyat kaydedilemedi.'); done(); });
    });
  }

  window.SupplierPriceForm = {
    mount: mount, unmount: unmount, unitFamily: unitFamily, priceUnitsFor: priceUnitsFor,
    money: money, priceText: priceText, sourceText: sourceText, sourceTitle: sourceTitle,
    fmtDate: fmtDate, esc: esc, fold: fold,
    // Sayfa tedarikçi/kart değiştirince önbelleği boşalt
    reset: function () { _items = null; _suppliers = null; },
  };
})();
