/* Minerva 108 — Trendyol (Faz 1, yalnız okuma).
   Ürün adları dış kaynaktan (Trendyol) geldiği için DOM createElement +
   textContent ile kurulur; innerHTML ataması YOK.  Yetki kararı sunucuda:
   düğmeler yalnız TY_CFG izin verdiğinde çizilir, uçlar ayrıca denetler. */
(function () {
  'use strict';

  const API = '/api/trendyol';
  const CFG = window.TY_CFG || {};
  const $ = id => document.getElementById(id);
  const state = { tab: 'orders', page: 0, size: 50, pollTimer: null, statusLabels: {},
                  map: { barcode: null, comps: [], manual: false, title: '' }, searchTimer: null };

  // ─── Yardımcılar ──────────────────────────────────────────────────────────
  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        const v = props[k];
        if (v == null || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'style') el.style.cssText = v;
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      });
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, kid) {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(k => append(el, k)); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function fill(host, node) { host.replaceChildren(); append(host, node); }
  const num = (v, d) => v == null ? '—' : Number(v).toLocaleString('tr-TR', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  const money = (v, cur) => v == null ? '—' : Number(v).toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ' + (!cur || cur === 'TRY' ? '₺' : cur);
  function errText(d) {
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map(x => x && x.msg).filter(Boolean).join(' ') || 'Alanları kontrol edin.';
    return '';
  }
  async function request(path, opts) {
    const o = opts || {};
    const init = { method: o.method || 'GET', credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (o.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(o.body); }
    const res = await fetch(API + path, init);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(errText(data.detail) || ('İşlem tamamlanamadı (' + res.status + ').'));
      err.status = res.status; err.data = data; throw err;
    }
    return data;
  }
  function toast(msg, type) { if (typeof window.showToast === 'function') window.showToast(msg, type || 'success', 4500); }
  function badge(text, kind, title) { return h('span', { class: 'os-badge' + (kind ? ' os-badge-' + kind : ''), title: title }, text); }
  function empty(text) { return h('div', { class: 'os-empty' }, text); }
  function table(headers, rows) {
    return h('table', { class: 'os-table' },
      h('thead', null, h('tr', null, headers.map(c => h('th', { class: c.num ? 'num' : null }, c.t || c)))),
      h('tbody', null, rows));
  }
  function td(content, cls) { return h('td', { class: cls || null }, content); }
  function isoDay(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function modal(id) { return window.bootstrap ? window.bootstrap.Modal.getOrCreateInstance($(id)) : null; }

  const STATUS_KIND = { Delivered: 'good', Cancelled: 'bad', UnSupplied: 'bad', Returned: 'warn', UnDelivered: 'warn' };
  const MATCH_KIND = { matched: 'good', mapped: 'good', ambiguous: 'bad', unmatched: 'warn', invalid_map: 'bad' };

  function matchCell(res) {
    if (!res) return '—';
    const names = (res.components || []).map(c => (c.qty && c.qty !== 1 ? num(c.qty, 0) + ' × ' : '') + (c.name || ('#' + c.id)));
    const wrap = h('div', null, badge(res.label, MATCH_KIND[res.status]));
    if (names.length) wrap.appendChild(h('div', { class: 'os-muted', style: 'margin-top:.2rem' }, names.join(' + ')));
    if (res.suggestion) wrap.appendChild(h('div', { class: 'os-muted', style: 'margin-top:.2rem' }, 'Öneri (eski barkod): ' + res.suggestion.name));
    if (res.candidates && res.candidates.length) wrap.appendChild(h('div', { class: 'os-muted', style: 'margin-top:.2rem' }, 'Aynı barkodlu ' + res.candidates.length + ' kart: ' + res.candidates.map(c => c.name).join(', ')));
    return wrap;
  }

  // ─── Sekmeler ─────────────────────────────────────────────────────────────
  function showTab(name) {
    state.tab = name;
    document.querySelectorAll('[data-ty-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tyTab === name)));
    $('tyOrdersPanel').hidden = name !== 'orders';
    $('tyCatalogPanel').hidden = name !== 'catalog';
    $('tyConnPanel').hidden = name !== 'connection';
    try { localStorage.setItem('ty.tab', name); } catch (e) { /* yoksay */ }
    if (name === 'orders') loadOrders();
    else if (name === 'catalog') loadCatalog();
    else loadStatus();
  }

  // ─── Siparişler ───────────────────────────────────────────────────────────
  function orderQuery() {
    const p = new URLSearchParams();
    const s = $('tyStart').value, e = $('tyEnd').value, st = $('tyStatus').value, pn = $('tyPanel').value, q = $('tyQ').value.trim();
    if (s) p.set('start', s);
    if (e) p.set('end', e);
    if (st) p.set('status', st);
    if (pn && pn !== 'all') p.set('panel', pn);
    if (q) p.set('q', q);
    p.set('page', String(state.page));
    p.set('size', String(state.size));
    return p.toString();
  }
  async function loadOrders() {
    try {
      const d = await request('/orders?' + orderQuery());
      state.statusLabels = d.status_labels || {};
      renderStatusFilter(d.status_counts || {});
      renderOrderStats(d);
      renderOrders(d);
    } catch (err) {
      fill($('tyOrdersHost'), empty(err.message));
    }
  }
  function renderStatusFilter(counts) {
    const sel = $('tyStatus'), cur = sel.value;
    const opts = [h('option', { value: '' }, 'Tümü')];
    Object.keys(counts).sort().forEach(k => opts.push(h('option', { value: k }, (state.statusLabels[k] || k) + ' (' + counts[k] + ')')));
    if (cur && !counts[cur]) opts.push(h('option', { value: cur }, state.statusLabels[cur] || cur));
    sel.replaceChildren(...opts);
    sel.value = cur;
  }
  function renderOrderStats(d) {
    const c = d.status_counts || {};
    const sum = keys => keys.reduce((a, k) => a + (c[k] || 0), 0);
    const total = Object.keys(c).filter(k => k !== 'UnPacked').reduce((a, k) => a + c[k], 0);
    const stats = [[total, 'Paket (süzgeçte)'], [sum(['Delivered']), 'Teslim edildi'],
      [sum(['Created', 'Picking', 'Invoiced', 'Shipped', 'AtCollectionPoint', 'Awaiting', 'Verified']), 'Yolda / hazırlanıyor'],
      [sum(['Returned', 'UnDelivered']), 'İade / teslim edilemedi'], [sum(['Cancelled', 'UnSupplied']), 'İptal']];
    fill($('tyOrderStats'), stats.map(s => h('div', { class: 'os-stat' }, h('strong', null, num(s[0])), h('span', null, s[1]))));
  }
  function renderOrders(d) {
    $('tyOrdersCount').textContent = num(d.total) + ' paket';
    const rows = (d.orders || []).map(o => h('tr', null,
      td(o.order_date || '—'),
      td(h('span', { class: 'os-code' }, o.order_number)),
      td(badge(o.status_label || o.status || '—', STATUS_KIND[o.status])),
      td(o.lines.map(l => h('div', null, num(l.quantity) + ' × ' + (l.product_name || l.barcode || '—'),
        l.resolution && l.resolution.status !== 'matched' && l.resolution.status !== 'mapped' ? [' ', badge(l.resolution.label, MATCH_KIND[l.resolution.status])] : null)), 'ty-wide'),
      td(o.panel_label || '—'),
      td(money(o.total_price, o.currency), 'num'),
      td(o.cargo_provider || '—'),
      td(h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => openOrder(o.order_number) }, 'Detay ', h('i', { class: 'bi bi-arrow-right' })))));
    fill($('tyOrdersHost'), rows.length ? table(['Tarih', 'Sipariş', 'Durum', 'Ürünler', 'Panel', { t: 'Tutar (net)', num: true }, 'Kargo', ''], rows)
      : empty('Bu süzgeçte paket yok. Bağlantı kurulduysa "Bağlantı / Durum" sekmesinden senkronu başlatın.'));
    const pages = Math.max(1, Math.ceil((d.total || 0) / state.size));
    fill($('tyPager'), [
      h('button', { type: 'button', class: 'os-b os-b-sm', disabled: state.page <= 0, onclick: () => { state.page--; loadOrders(); } }, h('i', { class: 'bi bi-chevron-left' })),
      h('span', { class: 'os-muted' }, (state.page + 1) + ' / ' + pages),
      h('button', { type: 'button', class: 'os-b os-b-sm', disabled: state.page + 1 >= pages, onclick: () => { state.page++; loadOrders(); } }, h('i', { class: 'bi bi-chevron-right' })),
    ]);
  }
  async function openOrder(no) {
    $('tyOrderTitle').textContent = 'Sipariş ' + no;
    fill($('tyOrderBody'), empty('Yükleniyor…'));
    const m = modal('tyOrderModal'); if (m) m.show();
    try {
      const d = await request('/orders/' + encodeURIComponent(no));
      fill($('tyOrderBody'), renderOrderDetail(d));
    } catch (err) { fill($('tyOrderBody'), empty(err.message)); }
  }
  function kv(label, value) { return h('div', null, h('span', null, label), value == null || value === '' ? '—' : value); }
  function renderOrderDetail(d) {
    const parts = [];
    d.packages.forEach(p => {
      parts.push(h('h3', { style: 'font-size:1rem;margin:.4rem 0 .6rem' }, 'Paket ', h('span', { class: 'os-code' }, String(p.package_id)), ' ', badge(p.status_label || p.status, STATUS_KIND[p.status])));
      parts.push(h('div', { class: 'ty-kv' }, kv('Sipariş tarihi', p.order_date), kv('Panel', p.panel_label), kv('Kargo', p.cargo_provider),
        kv('Takip no', p.cargo_tracking), kv('Brüt', money(p.gross_amount, p.currency)), kv('Satıcı indirimi', money(p.seller_discount, p.currency)),
        kv('Trendyol indirimi', money(p.ty_discount, p.currency)), kv('Net (müşteri ödedi)', money(p.total_price, p.currency)),
        p.origin_package_ids ? kv('Bölünen paket', p.origin_package_ids) : null, p.cancel_reason ? kv('İptal nedeni', p.cancel_reason) : null));
      const showCom = p.lines.some(l => 'commission_rate' in l);
      parts.push(h('div', { class: 'os-table-wrap' }, table(
        ['Barkod', 'Ürün', { t: 'Adet', num: true }, { t: 'Birim net', num: true }, { t: 'Satır net', num: true }, { t: 'KDV %', num: true }].concat(showCom ? [{ t: 'Komisyon %', num: true }] : []).concat(['IMS kartı']),
        p.lines.map(l => h('tr', null, td(h('span', { class: 'os-code' }, l.barcode || '—')), td(l.product_name || '—'), td(num(l.quantity), 'num'),
          td(money(l.unit_price, p.currency), 'num'), td(money(l.net_total, p.currency), 'num'), td(num(l.vat_rate), 'num'),
          showCom ? td(num(l.commission_rate, 2), 'num') : null, td(matchCell(l.resolution)))))));
    });
    if (d.finance) {
      parts.push(h('h3', { style: 'font-size:1rem;margin:1.2rem 0 .6rem' }, 'Finans kayıtları (cari hesap ekstresi)'));
      parts.push(d.finance.length ? h('div', { class: 'os-table-wrap' }, table(
        ['Tarih', 'Tür', 'Etiket', 'Barkod', { t: 'Borç', num: true }, { t: 'Alacak', num: true }, { t: 'Komisyon', num: true }, { t: 'Satıcı hakedişi', num: true }],
        d.finance.map(f => h('tr', null, td(f.date || '—'), td(f.type + (f.sub_type ? ' · ' + f.sub_type : '')), td(f.label || '—'),
          td(f.barcode || '—'), td(num(f.debt, 2), 'num'), td(num(f.credit, 2), 'num'), td(num(f.commission_amount, 2), 'num'), td(num(f.seller_revenue, 2), 'num')))))
        : h('p', { class: 'os-muted' }, 'Henüz finans kaydı yok — Trendyol kayıtları teslimattan sonra oluşturur.'));
      parts.push(h('h3', { style: 'font-size:1rem;margin:1.2rem 0 .6rem' }, 'Kargo faturası kalemleri'));
      parts.push(d.cargo && d.cargo.length ? h('div', { class: 'os-table-wrap' }, table(
        ['Fatura', 'Tür', { t: 'Desi', num: true }, { t: 'Tutar', num: true }],
        d.cargo.map(c => h('tr', null, td(h('span', { class: 'os-code' }, c.invoice_serial)), td(c.kind || '—'), td(num(c.desi), 'num'), td(money(c.amount), 'num')))))
        : h('p', { class: 'os-muted' }, 'Henüz kargo faturası kalemi yok.'));
    }
    return parts;
  }

  // ─── Eşleştirme & Stok ────────────────────────────────────────────────────
  async function loadCatalog() {
    const p = new URLSearchParams();
    const pn = $('tyCatPanel').value, q = $('tyCatQ').value.trim();
    if (pn && pn !== 'all') p.set('panel', pn);
    if (q) p.set('q', q);
    if ($('tyCatProblems').checked) p.set('problems', 'true');
    try {
      renderCatalog(await request('/catalog?' + p.toString()));
    } catch (err) { fill($('tyCatalogHost'), empty(err.message)); }
  }
  function renderCatalog(d) {
    const s = d.summary || {};
    fill($('tyCatalogStats'), [[d.total, 'Trendyol ürünü'], [(s.matched || 0) + (s.mapped || 0), 'Eşleşti'], [s.unmatched || 0, 'Eşleşmedi'],
      [s.ambiguous || 0, 'Belirsiz barkod'], [s.invalid_map || 0, 'Geçersiz eşleme'], [s.over || 0, 'Trendyol stoğu IMS\'ten fazla']]
      .map(x => h('div', { class: 'os-stat' }, h('strong', null, num(x[0])), h('span', null, x[1]))));
    $('tyCatInfo').textContent = d.last_sync ? ('Son katalog senkronu: ' + d.last_sync + '. Kırmızı satır: Trendyol\'daki stok IMS stoğundan fazla — satılırsa karşılanamayabilir.')
      : 'Katalog henüz çekilmedi. "Bağlantı / Durum" sekmesinden senkronu başlatın.';
    const manage = !!CFG.can_manage;
    const rows = (d.rows || []).map(r => {
      const flags = [];
      if (r.on_sale === false) flags.push(badge('Satışta değil', 'warn'));
      if (r.archived) flags.push(badge('Arşivde'));
      if (r.locked) flags.push(badge('Kilitli', 'bad'));
      if (r.blacklisted) flags.push(badge('Kara liste', 'bad'));
      if (r.stale) flags.push(badge('Son senkronda yok', 'warn'));
      return h('tr', { class: r.over ? 'ty-row-over' : null },
        td([h('div', { style: 'font-weight:600' }, r.title || '—'), h('div', { class: 'os-muted' }, [r.brand, r.stock_code].filter(Boolean).join(' · '))], 'ty-wide'),
        td(h('span', { class: 'os-code' }, r.barcode)),
        td([money(r.sale_price), r.list_price && r.list_price !== r.sale_price ? h('div', { class: 'os-muted' }, 'Liste: ' + money(r.list_price)) : null], 'num'),
        td(num(r.quantity), 'num'),
        td(matchCell(r.resolution), 'ty-wide'),
        td(r.resolution ? r.resolution.panel_label : '—'),
        td(num(r.ims_stock), 'num'),
        td(r.diff == null ? '—' : (r.diff > 0 ? '+' : '') + num(r.diff), 'num'),
        td(flags.length ? flags : '—'),
        manage ? td(h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => openMap(r.barcode, r.title, r.resolution) }, h('i', { class: 'bi bi-link-45deg' }), 'Eşle')) : null);
    });
    const headers = ['Trendyol ürünü', 'Barkod', { t: 'Satış fiyatı', num: true }, { t: 'Trendyol stok', num: true }, 'IMS kartı', 'Panel',
      { t: 'IMS stok', num: true }, { t: 'Fark', num: true }, 'Durum'].concat(manage ? [''] : []);
    fill($('tyCatalogHost'), rows.length ? table(headers, rows) : empty(d.total ? 'Bu süzgeçte ürün yok.' : 'Katalog boş.'));
    const nic = d.not_in_catalog || [];
    fill($('tyNotInCatalogHost'), nic.length ? table(['Barkod', 'Ürün (siparişten)', { t: 'Satır', num: true }, 'IMS kartı'].concat(manage ? [''] : []),
      nic.map(r => h('tr', null, td(h('span', { class: 'os-code' }, r.barcode)), td(r.product_name || '—'), td(num(r.lines), 'num'), td(matchCell(r.resolution)),
        manage ? td(h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => openMap(r.barcode, r.product_name, r.resolution) }, 'Eşle')) : null)))
      : empty('Yok — siparişlerdeki tüm barkodlar katalogda.'));
    const miss = d.ims_missing || [];
    fill($('tyImsMissingHost'), miss.length ? table(['IMS kartı', 'Barkod', 'Panel', { t: 'IMS stok', num: true }],
      miss.map(r => h('tr', null, td(r.name), td(h('span', { class: 'os-code' }, r.barcode)), td(r.domain === 'supplement' ? 'Food Supplement' : 'Kozmetik'), td(num(r.current_stock), 'num'))))
      : empty(d.total ? 'Yok — barkodlu tüm bitmiş ürünler Trendyol kataloğunda eşleşiyor.' : '—'));
  }

  // ─── Elle eşleme penceresi ────────────────────────────────────────────────
  function openMap(barcode, title, res) {
    state.map = { barcode: barcode, title: title || '', manual: !!(res && (res.status === 'mapped' || res.status === 'invalid_map')),
                  comps: (res && (res.status === 'mapped' || res.status === 'invalid_map' || res.status === 'matched') ? res.components : [])
                    .map(c => ({ item_id: c.id, name: c.name, qty: c.qty || 1 })) };
    fill($('tyMapInfo'), [kv('Barkod', h('span', { class: 'os-code' }, barcode)), kv('Trendyol ürünü', title || '—'), kv('Şu anki durum', res ? res.label : '—')]);
    $('tyMapSearch').value = '';
    $('tyMapNote').value = '';
    $('tyMapResults').hidden = true;
    $('tyMapDelete').hidden = !state.map.manual;
    const quick = [];
    if (res && res.suggestion) quick.push(res.suggestion);
    (res && res.candidates || []).forEach(c => quick.push(c));
    if (quick.length) showResults(quick, 'Öneriler');
    renderComps();
    const m = modal('tyMapModal'); if (m) m.show();
  }
  function renderComps() {
    const host = $('tyMapComps');
    if (!state.map.comps.length) { fill(host, h('p', { class: 'os-muted' }, 'Henüz kart seçilmedi — yukarıdan arayın.')); return; }
    fill(host, state.map.comps.map((c, i) => h('div', { class: 'ty-comp' },
      h('div', null, c.name || ('#' + c.item_id)),
      h('input', { type: 'number', class: 'os-input', min: '0.01', max: '100', step: '1', value: String(c.qty), 'aria-label': 'Adet',
        oninput: e => { c.qty = parseFloat(e.target.value) || 0; } }),
      h('button', { type: 'button', class: 'os-b os-b-sm os-b-danger', 'aria-label': 'Çıkar', onclick: () => { state.map.comps.splice(i, 1); renderComps(); } }, h('i', { class: 'bi bi-x' })))));
  }
  function showResults(items, heading) {
    const host = $('tyMapResults');
    host.hidden = false;
    fill(host, [heading ? h('div', { class: 'os-muted', style: 'padding:.4rem .7rem' }, heading) : null,
      items.length ? items.map(it => h('button', { type: 'button', onclick: () => addComp(it) },
        h('strong', null, it.name), ' ', h('span', { class: 'os-muted' }, [it.barcode || 'barkodsuz', it.domain === 'supplement' ? 'Food Supplement' : 'Kozmetik', 'stok ' + num(it.current_stock)].join(' · '))))
        : h('div', { class: 'os-muted', style: 'padding:.5rem .7rem' }, 'Sonuç yok.')]);
  }
  function addComp(it) {
    if (state.map.comps.some(c => c.item_id === it.id)) { toast('Bu kart zaten ekli.', 'info'); return; }
    state.map.comps.push({ item_id: it.id, name: it.name, qty: 1 });
    renderComps();
  }
  function searchItems() {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(async () => {
      const q = $('tyMapSearch').value.trim();
      if (!q) { $('tyMapResults').hidden = true; return; }
      try { showResults((await request('/items?q=' + encodeURIComponent(q))).items || []); } catch (err) { toast(err.message, 'error'); }
    }, 250);
  }
  async function saveMap() {
    const comps = state.map.comps.filter(c => c.qty > 0);
    if (!comps.length) { toast('En az bir kart seçin.', 'error'); return; }
    try {
      await request('/mappings', { method: 'PUT', body: { barcode: state.map.barcode, note: $('tyMapNote').value.trim() || null,
        components: comps.map(c => ({ item_id: c.item_id, qty: c.qty })) } });
      toast('Eşleme kaydedildi.');
      const m = modal('tyMapModal'); if (m) m.hide();
      loadCatalog();
    } catch (err) { toast(err.message, 'error'); }
  }
  async function deleteMap() {
    if (!window.confirm('Bu barkodun elle eşlemesi kaldırılsın mı? Otomatik eşleşme (barkod birebir tutuyorsa) geri gelir.')) return;
    try {
      await request('/mappings?barcode=' + encodeURIComponent(state.map.barcode), { method: 'DELETE' });
      toast('Elle eşleme kaldırıldı.');
      const m = modal('tyMapModal'); if (m) m.hide();
      loadCatalog();
    } catch (err) { toast(err.message, 'error'); }
  }

  // ─── Bağlantı / Durum ─────────────────────────────────────────────────────
  async function loadStatus() {
    try { renderStatus(await request('/status')); } catch (err) { fill($('tyStreamsHost'), empty(err.message)); }
  }
  function renderStatus(d) {
    const host = $('tyConfigHost');
    if (!d.configured) {
      fill(host, h('div', { class: 'os-notice os-notice-danger' },
        h('strong', null, 'Trendyol bağlantısı yapılandırılmamış.'), ' Sunucunun .env dosyasına şu değişkenler eklenmeli: ',
        h('span', { class: 'os-code' }, (d.missing || []).join(', ')), '. Değerler Trendyol satıcı panelinde (yalnız ana kullanıcı) Hesap Bilgilerim → Entegrasyon Bilgileri altındadır. Anahtarları sohbete ya da e-postaya yapıştırmayın.'));
    } else {
      fill(host, h('div', { class: 'os-notice' }, badge('Yapılandırıldı', 'good'), ' Anahtarlar sunucu ortamından okunuyor. ',
        d.ts_tr_wallclock ? 'Zaman damgası TR duvar saati olarak yorumlanıyor.' : '',
        d.first_order_at ? ' İlk sipariş kaydı: ' + d.first_order_at + '.' : ''));
    }
    $('tyActions').hidden = !(CFG.can_manage && d.configured);
    $('tyEnabled').checked = !!d.enabled;
    $('tyCalibCard').hidden = !CFG.can_profit;
    $('tyProbeCard').hidden = !(CFG.is_superadmin && d.configured);
    fill($('tyStreamsHost'), table(['Akış', 'Zamanlama', 'Son çalışma', 'Son başarı', 'Durum', { t: 'Ardışık hata', num: true }, 'Kapsanan (sipariş)'],
      (d.streams || []).map(s => h('tr', null, td(h('strong', null, s.label)), td(s.schedule), td(s.last_run_at || '—'), td(s.last_ok_at || '—'),
        td(s.last_error ? h('span', { class: 'os-badge os-badge-bad', title: s.last_error }, (s.last_status || 'HATA').slice(0, 90)) : (s.last_status || '—')),
        td(num(s.consecutive_failures), 'num'), td(s.stream === 'orders' ? (s.watermark || '—') : '—')))));
    const c = d.counts || {};
    fill($('tyCountsHost'), h('div', { class: 'ty-kv' }, kv('Sipariş paketi', num(c.packages)), kv('Sipariş satırı', num(c.lines)), kv('Finans kaydı', num(c.fin_rows)),
      kv('Kargo faturası kalemi', num(c.cargo_items)), kv('Katalog ürünü', num(c.products)), kv('Elle eşlenen barkod', num(c.mappings))));
    renderProgress(d.progress || {}, d.running);
  }
  function renderProgress(p, running) {
    const box = $('tyProgress');
    if (running || p.running) {
      box.hidden = false;
      fill(box, h('div', { class: 'ty-progress' }, h('span', { class: 'spinner-border spinner-border-sm', role: 'status', 'aria-hidden': 'true' }),
        h('span', null, 'Senkron çalışıyor' + (p.step ? ' — ' + p.step : '') + (p.detail ? ': ' + p.detail : '') + (p.started_at ? ' (başlangıç ' + p.started_at + ')' : ''))));
      ['tySyncBtn', 'tyBackfillBtn'].forEach(id => { $(id).disabled = true; });
      schedulePoll();
      return;
    }
    ['tySyncBtn', 'tyBackfillBtn'].forEach(id => { $(id).disabled = false; });
    if (p.result && p.finished_at) {
      box.hidden = false;
      fill(box, h('div', null, h('strong', null, 'Son çalıştırma (' + p.finished_at + '): '),
        Object.keys(p.result).map(k => k + ' → ' + p.result[k]).join(' · ') || (p.error || 'sonuç yok')));
    } else if (p.error) {
      box.hidden = false; fill(box, h('div', null, p.error));
    } else box.hidden = true;
  }
  function schedulePoll() {
    if (state.pollTimer) return;
    state.pollTimer = setTimeout(async () => {
      state.pollTimer = null;
      try {
        const d = await request('/status');
        if (state.tab === 'connection') renderStatus(d);
        if (d.running) schedulePoll();
        else { toast('Trendyol senkronu bitti.', 'success'); if (state.tab === 'connection') renderStatus(d); }
      } catch (err) { /* sonraki yenilemede */ }
    }, 3000);
  }
  async function startSync(body, label) {
    try {
      await request('/sync', { method: 'POST', body: body });
      toast(label + ' başlatıldı — bu sayfada ilerleme görünür.', 'info');
      setTimeout(loadStatus, 800);
    } catch (err) { toast(err.message, 'error'); if (err.status === 409) loadStatus(); }
  }
  async function testConn() {
    $('tyTestBtn').disabled = true;
    try {
      const d = await request('/test', { method: 'POST' });
      toast('Bağlantı çalışıyor — katalogda ' + num(d.catalog_total) + ' ürün görünüyor.');
    } catch (err) { toast(err.message, 'error'); }
    finally { $('tyTestBtn').disabled = false; }
  }
  async function toggleEnabled(e) {
    try {
      const d = await request('/config', { method: 'PUT', body: { enabled: e.target.checked } });
      toast(d.enabled ? 'Otomatik senkron açık.' : 'Otomatik senkron duraklatıldı (elle senkron çalışır).');
    } catch (err) { toast(err.message, 'error'); e.target.checked = !e.target.checked; }
  }
  async function loadCalibration() {
    const p = new URLSearchParams();
    if ($('tyCalStart').value) p.set('start', $('tyCalStart').value);
    if ($('tyCalEnd').value) p.set('end', $('tyCalEnd').value);
    try {
      const d = await request('/calibration?' + p.toString());
      const groups = d.groups || [], inv = d.cargo_invoices || [];
      fill($('tyCalHost'), [
        groups.length ? table(['Kaynak', 'Tür (istenen)', 'Trendyol etiketi', { t: 'Kayıt', num: true }, { t: 'Borç', num: true }, { t: 'Alacak', num: true }, { t: 'Komisyon', num: true }, { t: 'Satıcı hakedişi', num: true }],
          groups.map(g => h('tr', null, td(g.source === 'settlement' ? 'Hakediş' : 'Diğer'), td(g.type + (g.sub_type ? ' · ' + g.sub_type : '')), td(g.label || '—'),
            td(num(g.count), 'num'), td(num(g.debt, 2), 'num'), td(num(g.credit, 2), 'num'), td(num(g.commission_amount, 2), 'num'), td(num(g.seller_revenue, 2), 'num'))))
          : empty('Bu aralıkta finans kaydı yok.'),
        inv.length ? h('div', { style: 'margin-top:1rem' }, table(['Kargo faturası', 'Tarih', { t: 'Fatura tutarı', num: true }, { t: 'Kalemler toplamı', num: true }, { t: 'Oran', num: true }, 'Kalemler'],
          inv.map(i => h('tr', null, td(h('span', { class: 'os-code' }, i.serial)), td(i.date || '—'), td(num(i.invoice_total, 2), 'num'), td(num(i.items_total, 2), 'num'),
            td(i.ratio == null ? '—' : num(i.ratio, 4), 'num'), td(i.fetched ? 'çekildi' : 'bekliyor'))))) : null]);
    } catch (err) { fill($('tyCalHost'), empty(err.message)); }
  }
  async function runProbe() {
    $('tyProbeOut').textContent = 'Yükleniyor…';
    try {
      const d = await request('/probe?kind=' + encodeURIComponent($('tyProbeKind').value));
      $('tyProbeOut').textContent = JSON.stringify(d, null, 2);
    } catch (err) { $('tyProbeOut').textContent = err.message; }
  }

  // ─── Açılış ───────────────────────────────────────────────────────────────
  function init() {
    const now = new Date();
    $('tyEnd').value = isoDay(now);
    $('tyStart').value = isoDay(new Date(now.getTime() - 29 * 864e5));
    const first = new Date(now.getFullYear(), now.getMonth() - 1, 1), last = new Date(now.getFullYear(), now.getMonth(), 0);
    $('tyCalStart').value = isoDay(first);
    $('tyCalEnd').value = isoDay(last);

    document.querySelectorAll('[data-ty-tab]').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tyTab)));
    $('tyOrdersRefresh').addEventListener('click', () => { state.page = 0; loadOrders(); });
    ['tyStatus', 'tyPanel'].forEach(id => $(id).addEventListener('change', () => { state.page = 0; loadOrders(); }));
    $('tyQ').addEventListener('keydown', e => { if (e.key === 'Enter') { state.page = 0; loadOrders(); } });
    $('tyCatRefresh').addEventListener('click', loadCatalog);
    ['tyCatPanel', 'tyCatProblems'].forEach(id => $(id).addEventListener('change', loadCatalog));
    $('tyCatQ').addEventListener('keydown', e => { if (e.key === 'Enter') loadCatalog(); });
    $('tyMapSearch').addEventListener('input', searchItems);
    $('tyMapSave').addEventListener('click', saveMap);
    $('tyMapDelete').addEventListener('click', deleteMap);
    $('tyTestBtn').addEventListener('click', testConn);
    $('tySyncBtn').addEventListener('click', () => startSync({ kind: 'all' }, 'Senkron'));
    $('tyBackfillBtn').addEventListener('click', () => {
      const days = Math.max(1, Math.min(365, parseInt($('tyBackfillDays').value, 10) || 90));
      if (!window.confirm(days + ' günlük geçmiş çekilecek (siparişler en fazla 89 gün). Bu birkaç dakika sürebilir; aynı kayıtlar tekrar yazılmaz. Devam edilsin mi?')) return;
      startSync({ kind: 'all', backfill_days: days }, 'Geçmiş çekme');
    });
    $('tyEnabled').addEventListener('change', toggleEnabled);
    $('tyCalBtn').addEventListener('click', loadCalibration);
    $('tyProbeBtn').addEventListener('click', runProbe);
    $('tyLogout').addEventListener('click', async () => {
      try { await fetch('/api/logout', { method: 'POST', headers: { 'X-Requested-With': '1' } }); } catch (e) { /* yoksay */ }
      window.location.href = '/login';
    });

    let tab = 'orders';
    try { tab = localStorage.getItem('ty.tab') || 'orders'; } catch (e) { /* yoksay */ }
    if (!['orders', 'catalog', 'connection'].includes(tab)) tab = 'orders';
    showTab(tab);
    // Bağlantı kurulmamışsa ya da henüz hiç veri çekilmemişse kullanıcıyı
    // yapılacak işin olduğu sekmeye götür.
    request('/status').then(d => {
      const c = d.counts || {};
      const noData = !c.packages && !c.products;
      if ((!d.configured || noData) && state.tab !== 'connection') showTab('connection');
      if (d.running) schedulePoll();
    }).catch(() => {});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
