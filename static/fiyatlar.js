/* ─────────────────────────────────────────────────────────────────────────────
 * Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
 * Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Malzeme Fiyatları — /fiyatlar sayfasının istemcisi (templates/fiyatlar.html).
 *
 * API: GET /api/supplier-prices/overview?currency&q&category&has_price&source
 * (routers/reports.py → core/price_overview.build_overview).  Düzen:
 * "aynı malzeme" grubu → kart → fiyat satırları.  Kurallar SUNUCUDADIR:
 * tercih / bitirilecek bayrakları (satın alma planıyla aynı), elle fiyatın
 * gizlediği Excel satırları, net fiyatın seçilen para birimine çevrimi, en
 * ucuz ve planın seçeceği teklif.  TR sayı biçimi de sunucuda (`*_text`) —
 * burada yeniden biçimlenmez.
 *
 *  • Bütün metin `textContent` / `title` ile yazılır (XSS) — HTML dizesi kurulmaz.
 *  • Ekle / düzenle / sil `suppliers.prices` (Manager + LabLead) —
 *    ortak form static/supplier-price-form.js (SupplierPriceForm.mount).
 *  • `?q=` adres parametresi aramayı doldurur (Satın Alma Planı'nın "Fiyat gir →"
 *    bağlantısı) — o zaman fiyat durumu "Hepsi" başlar ki fiyatsız kart da görünsün.
 *  • Para birimi tarayıcıda hatırlanır (localStorage, try/catch — özel pencere).
 *  • Sınıf öneki `fp-`; `btn-*` adı YASAK (Bootstrap gölgeleme tuzağı).
 *  • Paraşüt onay kuyruğu (#fpParasut): GET /api/supplier-prices/parasut-queue
 *    (reports.view) → satır başına kart seçici (items-cache), firma, ambalaj
 *    (FİYAT BİRİMİNDE), birim, net önizleme; Onayla / Atla yalnız
 *    suppliers.prices (POST …/{id}/approve | skip).  Hesap SUNUCUDA; buradaki
 *    önizleme yalnız bilgi.  Paraşüt'e hiçbir şey yazılmaz.
 */
(function () {
  'use strict';

  var API = '/api/supplier-prices/overview';
  var CUR_KEY = 'fiyatlar.currency';
  var PAGE = 120;                    // bir seferde çizilen blok sayısı
  var CAN_EDIT = !!(window.can && window.can('suppliers', 'prices'));

  var state = { data: null, shown: 0, seq: 0, timer: null };

  function $(id) { return document.getElementById(id); }
  function SPF() { return window.SupplierPriceForm; }
  function toast(msg, type) {
    if (window.showToast) window.showToast(msg, type || 'error');
    else window.alert(msg);
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null && text !== '') e.textContent = String(text);
    return e;
  }
  function pill(text, kind, title) {
    var p = el('span', 'fp-pill fp-pill-' + kind, text);
    if (title) p.title = title;
    return p;
  }
  function icon(name) { return el('i', 'bi bi-' + name); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function getCurrency() {
    try {
      var c = window.localStorage.getItem(CUR_KEY);
      return (c === 'USD' || c === 'EUR' || c === 'TRY') ? c : 'USD';
    } catch (e) { return 'USD'; }
  }
  function setCurrency(c) {
    try { window.localStorage.setItem(CUR_KEY, c); } catch (e) { /* özel pencere */ }
  }

  function params() {
    var p = new URLSearchParams();
    p.set('currency', $('fpCur').value || 'USD');
    var q = $('fpQ').value.trim();
    if (q) p.set('q', q.slice(0, 100));
    p.set('category', $('fpCat').value || 'all');
    p.set('has_price', $('fpHas').value || 'priced');
    p.set('source', $('fpSrc').value || 'all');
    return p;
  }
  // Arama adreste kalsın (yenileyince / paylaşınca aynı görünüm)
  function syncUrl() {
    try {
      var q = $('fpQ').value.trim();
      var u = new URL(window.location.href);
      if (q) u.searchParams.set('q', q); else u.searchParams.delete('q');
      window.history.replaceState(null, '', u.pathname + (u.search || ''));
    } catch (e) { /* eski tarayıcı */ }
  }

  function load() {
    var seq = ++state.seq;
    $('fpSummary').textContent = 'Yükleniyor…';
    return fetch(API + '?' + params().toString())
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; });
      })
      .then(function (res) {
        if (seq !== state.seq) return;                 // daha yeni istek var
        if (!res.r.ok) {
          var msg = (res.d && typeof res.d.detail === 'string') ? res.d.detail : 'Fiyatlar yüklenemedi.';
          $('fpSummary').textContent = msg;
          renderEmpty(msg);
          return;
        }
        state.data = res.d;
        render();
      })
      .catch(function () {
        if (seq !== state.seq) return;
        $('fpSummary').textContent = 'Fiyatlar yüklenemedi.';
        renderEmpty('Fiyatlar yüklenemedi.');
      });
  }
  function reload() { return load(); }

  function renderEmpty(msg) {
    var list = $('fpList');
    clear(list);
    var box = el('div', 'fp-empty');
    box.appendChild(icon('tags'));
    box.appendChild(document.createTextNode(msg));
    list.appendChild(box);
    $('fpMoreWrap').hidden = true;
  }

  function render() {
    var d = state.data || {};
    $('fpSummary').textContent = d.summary_text || '';
    var fx = d.fx || {};
    $('fpFx').textContent = fx.text ? fx.text + (fx.rates_text ? ' (' + fx.rates_text + ')' : '') : '';
    var warn = $('fpFxWarn');
    warn.hidden = !fx.warning;
    warn.textContent = fx.warning ? 'Döviz kuru: ' + fx.warning + ' Çevrilmiş fiyatlar yaklaşıktır.' : '';
    var list = $('fpList');
    clear(list);
    state.shown = 0;
    var blocks = d.blocks || [];
    if (!blocks.length) {
      var f = d.filters || {};
      var why = f.q ? 'Aramaya uyan malzeme yok.'
        : (f.has_price === 'unpriced' ? 'Fiyatı olmayan hammadde / ambalaj kartı yok.' : 'Gösterilecek fiyat yok.');
      renderEmpty(why);
      return;
    }
    more();
  }

  function more() {
    var blocks = (state.data && state.data.blocks) || [];
    var list = $('fpList');
    var end = Math.min(blocks.length, state.shown + PAGE);
    for (var i = state.shown; i < end; i++) list.appendChild(blockNode(blocks[i]));
    state.shown = end;
    var left = blocks.length - end;
    $('fpMoreWrap').hidden = left <= 0;
    if (left > 0) $('fpMore').textContent = 'Daha fazla göster (' + left + ' kalem daha)';
  }

  function blockNode(b) {
    if (!b.group) return cardNode(b.cards[0], false);
    var g = el('section', 'fp-group');
    var h = el('div', 'fp-group-h');
    h.appendChild(icon('link-45deg'));
    h.appendChild(el('span', null, 'Aynı malzeme:'));
    h.appendChild(el('b', null, b.group.name));
    h.appendChild(el('span', null, '· ' + b.cards.length + ' kart — fiyatları birlikte karşılaştırın'));
    g.appendChild(h);
    b.cards.forEach(function (c) { g.appendChild(cardNode(c, true)); });
    return g;
  }

  function cardNode(c, inGroup) {
    var card = el('article', 'fp-card');
    card.dataset.itemId = c.item_id;
    var head = el('div', 'fp-head');
    var left = el('div');
    var nm = el('div', 'fp-name', c.name);
    left.appendChild(nm);
    var sub = el('div', 'fp-sub');
    if (c.bucket_label) sub.appendChild(pill(c.bucket_label, 'cat'));
    var bits = [];
    if (c.name_tr && c.name_tr !== c.name) bits.push(c.name_tr);
    if (c.unit) bits.push('kart birimi ' + c.unit + ' · fiyat ' + ({ kg: 'kg', l: 'litre', adet: 'adet' }[c.purchase_unit] || c.purchase_unit) + ' başına');
    if (c.card_supplier) bits.push('kart tedarikçisi: ' + c.card_supplier);
    if (!inGroup && c.group_name) bits.push('grup: ' + c.group_name);
    sub.appendChild(document.createTextNode(bits.join(' · ')));
    left.appendChild(sub);
    if (c.pref_text) left.appendChild(el('div', 'fp-sub', c.pref_text));
    head.appendChild(left);
    var right = el('div', 'fp-head-right');
    var host = el('div', 'fp-host');
    host.hidden = true;
    if (CAN_EDIT) {
      var add = el('button', 'fp-b fp-b-sm');
      add.type = 'button';
      add.appendChild(icon('plus-lg'));
      add.appendChild(document.createTextNode('Fiyat ekle'));
      add.addEventListener('click', function () {
        mountForm(host, { mode: 'create', item: { id: c.item_id, name: c.name, unit: c.unit } });
      });
      right.appendChild(add);
    }
    head.appendChild(right);
    card.appendChild(head);
    card.appendChild(host);

    var rows = c.rows || [];
    if (!rows.length) {
      card.appendChild(el('div', 'fp-none', c.priced ? 'Süzgece uyan fiyat satırı yok.'
        : (c.twin_priced ? 'Bu kartta fiyat yok — planda birleştiği kartın fiyatı kullanılır.' : 'Fiyat yok — teklif alınmalı.')));
    } else {
      card.appendChild(tableNode(c, rows, host));
    }
    var notes = [];
    if (c.merge_text) notes.push([c.merge_text, '']);
    if (c.compare_note) notes.push([c.compare_note, 'warn']);
    if (c.hidden_text) notes.push([c.hidden_text, '']);
    if (c.filtered_text) notes.push([c.filtered_text, '']);
    if (notes.length) {
      var foot = el('div', 'fp-foot');
      notes.forEach(function (n) { foot.appendChild(el('div', n[1], n[0])); });
      card.appendChild(foot);
    }
    return card;
  }

  function tableNode(c, rows, host) {
    var cur = (state.data && state.data.currency) || 'USD';
    var wrap = el('div', 'fp-scroll');
    var t = el('table', 'fp-tbl');
    var thead = el('thead');
    var hr = el('tr');
    [['Tedarikçi · durum', ''], ['Ambalaj', 'num'], ['Fiyat (liste)', 'num'], ['Net (' + cur + ')', 'num'],
     ['Tarih', ''], ['Kaynak', ''], ['Not', ''], ['', '']].forEach(function (x) {
      hr.appendChild(el('th', x[1] || null, x[0]));
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tb = el('tbody');
    var twins = (c.merged_with || []).map(function (t) { return t.name; });
    var many = rows.length > 1 || twins.length > 0;
    var pickTip = twins.length
      ? 'Satın Alma Planı bu kartı ' + twins.join(', ') + ' ile tek satırda birleştirir ve bu teklifi seçer (tercih → tercih edilen firma → en ucuz; pasif / bitirilecek / "alma" hariç)'
      : 'Satın Alma Planı bu kartta bu teklifi seçer (tercih → tercih edilen firma → en ucuz; pasif / bitirilecek / "alma" hariç)';
    rows.forEach(function (r) {
      var tr = el('tr', (r.status === 'inactive' || r.eligible === false) ? 'fp-off' : (r.cheapest ? 'fp-best' : null));
      // Tedarikçi + durum
      var td = el('td');
      var sn = el('span', 'fp-sup', r.supplier_name);
      td.appendChild(sn);
      if (!r.matched) {
        var w = el('span', 'fp-loose', ' ⚠');
        w.title = 'Tedarikçi sistemde eşleşmedi — serbest metin';
        td.appendChild(w);
      }
      var pills = el('div');
      if (r.status === 'inactive') pills.appendChild(pill('pasif tedarikçi', 'off', 'Firma pasife alınmış — satın almada seçilmez'));
      else if (r.status_blocked) pills.appendChild(pill(r.status_blocked, 'warn', r.status_reason || 'Satın Alma Planı bu teklifi seçmez'));
      (r.status_tags || []).forEach(function (tg) { pills.appendChild(pill(tg, 'ok')); });
      if (r.cheapest) pills.appendChild(pill('en ucuz', 'ok', 'Karşılaştırılabilir teklifler içinde en düşük net fiyat'));
      if (r.plan_pick && many) pills.appendChild(pill('planda seçilir', 'info', pickTip));
      if (pills.firstChild) td.appendChild(pills);
      tr.appendChild(td);
      // Ambalaj (fiyat biriminde)
      tr.appendChild(el('td', 'num', r.package_text || '—'));
      // Liste fiyatı + KDV etiketi
      td = el('td', 'num');
      td.appendChild(document.createTextNode(r.orig_text || '—'));
      if (r.vat_label) td.appendChild(el('span', 'fp-small warn', r.vat_label));
      tr.appendChild(td);
      // Net — seçilen para biriminde, alım biriminde
      td = el('td', 'num fp-net');
      td.appendChild(document.createTextNode(r.net_text || '—'));
      if (r.net_note) td.appendChild(el('span', 'fp-small warn', r.net_note));
      if (r.litre_kg) td.appendChild(el('span', 'fp-small', '1 l ≈ 1 kg kabulüyle'));
      tr.appendChild(td);
      // Tarih
      tr.appendChild(el('td', 'fp-date', r.date_text || '—'));
      // Kaynak
      var spf = SPF();
      var src = el('td', 'fp-src' + (r.source === 'manual' ? ' manual' : ''),
        spf ? spf.sourceText(r) : (r.source_label || r.source || '—'));
      var ttl = spf ? spf.sourceTitle(r) : '';
      if (ttl) src.title = ttl;
      tr.appendChild(src);
      // Not
      tr.appendChild(el('td', 'fp-note', r.note || ''));
      // Eylemler
      td = el('td', 'fp-act');
      if (CAN_EDIT) {
        if (r.matched) {
          var e = el('button', 'fp-ic');
          e.type = 'button'; e.title = 'Düzenle'; e.setAttribute('aria-label', 'Düzenle');
          e.appendChild(icon('pencil'));
          e.addEventListener('click', function () { mountForm(host, { mode: 'edit', row: r }); });
          td.appendChild(e);
        }
        var x = el('button', 'fp-ic del', '×');
        x.type = 'button'; x.title = 'Sil'; x.setAttribute('aria-label', 'Sil');
        x.addEventListener('click', function () { removeRow(c, r); });
        td.appendChild(x);
      }
      tr.appendChild(td);
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    wrap.appendChild(t);
    return wrap;
  }

  // Form kart içinde açılır; aynı anda tek form (diğerleri kapanır)
  var openHost = null;
  function mountForm(host, opts) {
    var spf = SPF();
    if (!spf || !CAN_EDIT) return;
    if (openHost && openHost !== host) spf.unmount(openHost);
    openHost = host;
    spf.mount(host, Object.assign({
      onSaved: function () { openHost = null; reload(); },
      onCancel: function () { openHost = null; },
    }, opts));
    try { host.scrollIntoView({ block: 'nearest' }); } catch (e) { /* eski tarayıcı */ }
  }

  function removeRow(c, r) {
    if (!CAN_EDIT) return;
    if (!window.confirm('«' + c.name + '» — ' + r.supplier_name + ' fiyat satırı silinsin mi?')) return;
    fetch('/api/supplier-prices/' + encodeURIComponent(r.id), { method: 'DELETE' })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (d) {
          if (res.ok) { toast('Silindi.', 'success'); reload(); }
          else toast(typeof d.detail === 'string' ? d.detail : 'Silinemedi.');
        });
      })
      .catch(function () { toast('Silinemedi.'); });
  }

  // ─── Paraşüt onay kuyruğu ───────────────────────────────────────────────
  var PQ_API = '/api/supplier-prices/parasut-queue';
  var pq = { items: null, byLabel: {}, loaded: false };

  function pqItemLabel(it) { return it.name + (it.sku ? ' · ' + it.sku : ''); }
  function pqLoadItems() {
    if (pq.items) return Promise.resolve(pq.items);
    var p = window.fetchItems ? window.fetchItems()
      : fetch('/api/items').then(function (r) { return r.ok ? r.json() : []; });
    return p.then(function (list) {
      pq.items = (list || []).filter(function (i) { return i.category !== 'Bitmiş Ürün' && i.is_active !== false; });
      pq.items = window.trSortBy ? window.trSortBy(pq.items, 'name') : pq.items;
      pq.byLabel = {};
      var dl = $('fpPqItems');
      if (dl) {
        clear(dl);
        pq.items.forEach(function (it) {
          var lbl = pqItemLabel(it);
          pq.byLabel[lbl] = it;
          var o = document.createElement('option');
          o.value = lbl;
          dl.appendChild(o);
        });
      }
      return pq.items;
    }).catch(function () { pq.items = []; return pq.items; });
  }
  function pqFindItem(text) {
    var t = String(text || '').trim();
    if (!t) return null;
    if (pq.byLabel[t]) return pq.byLabel[t];
    var hit = null, n = 0;
    (pq.items || []).forEach(function (it) { if (it.name === t) { hit = it; n++; } });
    return n === 1 ? hit : null;
  }
  function pqNum(v, d) {
    if (v == null || v === '') return '—';
    var n = Number(v);
    return isFinite(n) ? n.toLocaleString('tr-TR', { maximumFractionDigits: d == null ? 4 : d }) : '—';
  }
  function pqUnitsFor(itemUnit) {
    var spf = SPF();
    if (spf && spf.priceUnitsFor) { try { return spf.priceUnitsFor(itemUnit); } catch (e) { /* düş */ } }
    var u = String(itemUnit || '').trim().toLowerCase();
    if (u === 'g' || u === 'gr' || u === 'kg' || u === 'ml' || u === 'l' || u === 'lt') return ['kg', 'l'];
    return ['adet'];
  }
  function pqLoad() {
    if (!$('fpParasut')) return Promise.resolve();
    return fetch(PQ_API).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; });
    }).then(function (res) {
      if (!res.r.ok) { $('fpParasut').hidden = true; return; }
      return pqRender(res.d || {});
    }).catch(function () { $('fpParasut').hidden = true; });
  }
  function pqRender(d) {
    var sec = $('fpParasut');
    var count = d.count || 0;
    if (!count && !d.enabled) { sec.hidden = true; return; }
    sec.hidden = false;
    $('fpPqTitle').textContent = 'Onay bekleyen fatura fiyatları (' + count + ')';
    var list = $('fpPqList');
    clear(list);
    $('fpPqEmpty').hidden = count > 0;
    var rows = d.pending || [];
    if (!rows.length) return;
    var p = CAN_EDIT ? pqLoadItems() : Promise.resolve([]);
    return p.then(function () {
      rows.forEach(function (ln) { list.appendChild(pqLineNode(ln)); });
      if (count > rows.length) list.appendChild(el('div', 'fp-pq-ro', '… ve ' + (count - rows.length) + ' satır daha (onayladıkça gelir).'));
    });
  }
  function pqLineNode(ln) {
    var box = el('div', 'fp-pq-line');
    box.dataset.lineId = ln.id;
    var b = ln.bill || {};
    var cur = ln.currency || b.currency || 'TRY';
    box.appendChild(el('div', 'fp-pq-name', ln.product_name || ln.description || '—'));
    var meta = [];
    if (b.supplier_name) meta.push(b.supplier_name);
    if (b.invoice_no) meta.push('fatura ' + b.invoice_no);
    if (b.date_text) meta.push(b.date_text);
    if (ln.description && ln.description !== ln.product_name) meta.push(ln.description);
    var m = el('div', 'fp-pq-meta', meta.join(' · '));
    box.appendChild(m);
    var pills = el('div');
    if (ln.expense_hint) pills.appendChild(pill('gider kalemi?', 'warn', 'Kargo / seyahat / kiralama gibi görünüyor — malzeme değilse Atla'));
    if (ln.stale) pills.appendChild(pill('eski fatura (12 aydan eski)', 'off', 'Onaylansa da satın alma planı bu fiyatı seçmez'));
    if (ln.suggestion) pills.appendChild(pill('öneri: ' + ln.suggestion.name, 'info', 'Kart önerisi (' + pqNum(ln.suggestion.score, 0) + ' puan) — onaylamadan kontrol edin'));
    if (ln.supplier) pills.appendChild(pill('firma: ' + ln.supplier.name, 'ok', 'Faturadaki firma bu IMS kartıyla eşleşti'));
    if (pills.firstChild) box.appendChild(pills);
    var num = pqNum(ln.quantity) + ' ' + (ln.product_unit || 'adet') + ' × ' + pqNum(ln.unit_price) + ' ' + cur;
    if (ln.discount_pct) num += ' · indirim %' + pqNum(ln.discount_pct, 2);
    if (ln.vat_rate != null) num += ' · KDV %' + pqNum(ln.vat_rate, 2) + ' hariç';
    if (ln.net_total != null) num += ' · satır net ' + pqNum(ln.net_total, 2) + ' ' + cur;
    box.appendChild(el('div', 'fp-pq-num', num));
    if (!CAN_EDIT) {
      box.appendChild(el('div', 'fp-pq-ro', 'Onay / atlama: yönetici ya da lab sorumlusu.'));
      return box;
    }
    box.appendChild(pqForm(ln, box));
    return box;
  }
  function pqForm(ln, box) {
    var b = ln.bill || {};
    var cur = ln.currency || b.currency || 'TRY';
    var form = el('div', 'fp-pq-form');
    // Kart
    var fCard = el('div', 'fp-field fp-pq-card');
    var lab = el('label', 'fp-lbl', 'IMS kartı');
    var inp = el('input', 'fp-in');
    inp.type = 'text'; inp.setAttribute('list', 'fpPqItems'); inp.autocomplete = 'off';
    inp.placeholder = 'Kart adı yazın…'; inp.maxLength = 160;
    var sugItem = ln.suggestion ? pqFindItem(ln.suggestion.name) : null;
    if (!sugItem && ln.suggestion) sugItem = (pq.items || []).filter(function (i) { return i.id === ln.suggestion.id; })[0] || null;
    if (!sugItem && ln.pack && ln.pack.item_id) sugItem = (pq.items || []).filter(function (i) { return i.id === ln.pack.item_id; })[0] || null;
    if (sugItem) inp.value = pqItemLabel(sugItem);
    lab.htmlFor = inp.id = 'fpPqCard' + ln.id;
    fCard.appendChild(lab); fCard.appendChild(inp);
    form.appendChild(fCard);
    // Firma
    var fSup = el('div', 'fp-field');
    var labS = el('label', 'fp-lbl', 'Firma');
    var sel = el('select', 'fp-sel');
    labS.htmlFor = sel.id = 'fpPqSup' + ln.id;
    if (ln.supplier) {
      var o1 = document.createElement('option'); o1.value = 'id:' + ln.supplier.id; o1.textContent = ln.supplier.name + ' (IMS kartı)';
      sel.appendChild(o1);
    }
    var o2 = document.createElement('option'); o2.value = 'name'; o2.textContent = (b.supplier_name || '—') + ' (faturadaki ad)';
    sel.appendChild(o2);
    fSup.appendChild(labS); fSup.appendChild(sel);
    form.appendChild(fSup);
    // Ambalaj + birim
    var fPkg = el('div', 'fp-field');
    var labP = el('label', 'fp-lbl', 'Ambalaj (fiyat biriminde)');
    var pkg = el('input', 'fp-in');
    pkg.type = 'number'; pkg.step = 'any'; pkg.min = '0'; pkg.placeholder = 'ör. 25';
    labP.htmlFor = pkg.id = 'fpPqPkg' + ln.id;
    if (ln.pack && ln.pack.package_qty) pkg.value = String(ln.pack.package_qty);
    fPkg.appendChild(labP); fPkg.appendChild(pkg);
    form.appendChild(fPkg);
    var fUnit = el('div', 'fp-field');
    var labU = el('label', 'fp-lbl', 'Birim');
    var unit = el('select', 'fp-sel');
    labU.htmlFor = unit.id = 'fpPqUnit' + ln.id;
    fUnit.appendChild(labU); fUnit.appendChild(unit);
    form.appendChild(fUnit);
    var preview = el('span', 'fp-pq-preview');
    form.appendChild(preview);
    var act = el('div', 'fp-pq-act');
    var ok = el('button', 'fp-b fp-b-main fp-b-sm', 'Onayla'); ok.type = 'button';
    var skip = el('button', 'fp-b fp-b-sm', 'Atla'); skip.type = 'button';
    act.appendChild(ok); act.appendChild(skip);
    form.appendChild(act);

    function fillUnits() {
      var it = pqFindItem(inp.value);
      var units = pqUnitsFor(it ? it.unit : null);
      var keep = unit.value;
      clear(unit);
      units.forEach(function (u) {
        var o = document.createElement('option'); o.value = u; o.textContent = ({ kg: 'kg', l: 'litre', adet: 'adet' })[u] || u;
        unit.appendChild(o);
      });
      var want = (ln.pack && ln.pack.price_unit) || keep;
      if (want && units.indexOf(want) >= 0) unit.value = want;
      else if (/\b(lt|l|ml|litre)\b/i.test(String(ln.product_name || '')) && units.indexOf('l') >= 0) unit.value = 'l';
    }
    function refresh() {
      var it = pqFindItem(inp.value);
      var q = Number(pkg.value);
      inp.classList.toggle('is-invalid', !!inp.value && !it);
      if (!(q > 0) || ln.unit_price == null) { preview.textContent = ''; return; }
      var net = Number(ln.unit_price) * (1 - Number(ln.discount_rate || 0)) / q;
      preview.textContent = '≈ ' + pqNum(net, 4) + ' ' + cur + '/' + unit.value + ' (KDV hariç)';
      preview.classList.toggle('warn', !it);
    }
    inp.addEventListener('input', function () { fillUnits(); refresh(); });
    inp.addEventListener('change', function () { fillUnits(); refresh(); });
    pkg.addEventListener('input', refresh);
    unit.addEventListener('change', refresh);
    fillUnits(); refresh();

    ok.addEventListener('click', function () {
      var it = pqFindItem(inp.value);
      if (!it) { toast('Önce listeden bir IMS kartı seçin.'); inp.focus(); return; }
      var q = Number(pkg.value);
      if (!(q > 0)) { toast('Ambalaj miktarını yazın (ör. 25 kg\'lık bidon → 25).'); pkg.focus(); return; }
      var body = { item_id: it.id, package_qty: q, price_unit: unit.value };
      if (sel.value.indexOf('id:') === 0) body.supplier_id = Number(sel.value.slice(3));
      else body.supplier_name = b.supplier_name || '';
      ok.disabled = skip.disabled = true;
      fetch(PQ_API + '/' + encodeURIComponent(ln.id) + '/approve', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (r.ok) { toast(d.message || 'Fiyat yazıldı.', 'success'); box.classList.add('fp-pq-done'); pqLoad(); reload(); }
          else { toast(typeof d.detail === 'string' ? d.detail : 'Onaylanamadı.'); ok.disabled = skip.disabled = false; }
        });
      }).catch(function () { toast('Onaylanamadı.'); ok.disabled = skip.disabled = false; });
    });
    skip.addEventListener('click', function () {
      if (!window.confirm('«' + (ln.product_name || ln.description || '—') + '» satırı atlansın mı? (fiyat yazılmaz)')) return;
      ok.disabled = skip.disabled = true;
      fetch(PQ_API + '/' + encodeURIComponent(ln.id) + '/skip', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (r.ok) { toast('Satır atlandı.', 'success'); box.classList.add('fp-pq-done'); pqLoad(); }
          else { toast(typeof d.detail === 'string' ? d.detail : 'Atlanamadı.'); ok.disabled = skip.disabled = false; }
        });
      }).catch(function () { toast('Atlanamadı.'); ok.disabled = skip.disabled = false; });
    });
    return form;
  }

  function init() {
    var q = '';
    try { q = new URLSearchParams(window.location.search).get('q') || ''; } catch (e) { q = ''; }
    $('fpQ').value = q.slice(0, 100);
    $('fpHas').value = q ? 'all' : 'priced';
    $('fpCur').value = getCurrency();

    $('fpQ').addEventListener('input', function () {
      clearTimeout(state.timer);
      state.timer = setTimeout(function () { syncUrl(); load(); }, 300);
    });
    ['fpCat', 'fpHas', 'fpSrc'].forEach(function (id) { $(id).addEventListener('change', load); });
    $('fpCur').addEventListener('change', function () { setCurrency($('fpCur').value); load(); });
    $('fpMore').addEventListener('click', more);
    var add = $('fpAdd');
    if (add && CAN_EDIT) add.addEventListener('click', function () { mountForm($('fpForm'), { mode: 'create' }); });
    load();
    pqLoad();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
