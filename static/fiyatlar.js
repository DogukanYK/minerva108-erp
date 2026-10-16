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
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
