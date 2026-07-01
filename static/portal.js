/* Distribütör Sipariş Portalı — katalog, sepet, siparişler. */
(function () {
  'use strict';

  var PRODUCTS = [];        // [{item_id,name,name_tr,sku,unit,unit_price,currency,available}]
  var CART = {};            // item_id → qty
  var CURRENCY = 'TRY';

  function notify(m, t) { if (window.showToast) window.showToast(m, t || 'info'); else if (t === 'error') alert(m); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }
  function el(id) { return document.getElementById(id); }
  function money(v) { try { return new Intl.NumberFormat('tr-TR', { style: 'currency', currency: CURRENCY }).format(v || 0); } catch (_e) { return (v || 0).toFixed(2) + ' ' + CURRENCY; } }

  window.logout = function () { fetch('/api/logout', { method: 'POST' }).finally(function () { location.href = '/login'; }); };

  window.switchDomain = function (dom) {
    if (dom === window.DOMAIN) return;
    fetch('/api/domain/switch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ domain: dom }) })
      .then(function () { location.reload(); });
  };

  window.showTab = function (t) {
    document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('on', x.getAttribute('data-tab') === t); });
    el('tab-catalog').classList.toggle('hidden', t !== 'catalog');
    el('tab-orders').classList.toggle('hidden', t !== 'orders');
    if (t === 'orders') loadOrders();
  };

  // ── Katalog ──────────────────────────────────────────────────────────────
  async function loadPortal() {
    // panel pill'i işaretle
    document.querySelectorAll('.dom-pill').forEach(function (p) { p.classList.toggle('on', p.getAttribute('data-dom') === window.DOMAIN); });
    try {
      var me = await (await fetch('/api/portal/me')).json();
      if (me && me.company_name) el('coName').textContent = me.company_name;
    } catch (_e) {}
    try {
      var r = await fetch('/api/portal/products');
      var data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Katalog yüklenemedi.');
      CURRENCY = data.currency || 'TRY';
      PRODUCTS = data.products || [];
      renderCatalog();
    } catch (e) { el('catalogList').innerHTML = '<div class="empty">' + esc(e.message) + '</div>'; }
  }

  window.renderCatalog = function () {
    var q = (el('search').value || '').toLowerCase().trim();
    var list = PRODUCTS.filter(function (p) {
      return !q || (p.name || '').toLowerCase().indexOf(q) >= 0 || (p.name_tr || '').toLowerCase().indexOf(q) >= 0 || (p.sku || '').toLowerCase().indexOf(q) >= 0;
    });
    if (!PRODUCTS.length) { el('catalogList').innerHTML = '<div class="empty">Bu panelde size tanımlı ürün yok.<br>Fiyat listeniz için Minerva ile iletişime geçin.</div>'; return; }
    if (!list.length) { el('catalogList').innerHTML = '<div class="empty">Sonuç yok.</div>'; return; }
    el('catalogList').innerHTML = list.map(function (p) {
      var qty = CART[p.item_id] || 0;
      var av = p.available ? '<span class="avail yes">Stokta var</span>' : '<span class="avail no">Tükendi</span>';
      return '<div class="prod">' +
        '<div class="prod-info">' +
          '<div class="prod-name">' + esc(p.name_tr || p.name) + '</div>' +
          '<div class="prod-meta">' + esc(p.sku || '') + ' · ' + esc(p.unit || '') + ' ' + av + '</div>' +
          '<div class="prod-price">' + money(p.unit_price) + '</div>' +
        '</div>' +
        '<div class="stepper">' +
          '<button onclick="chgQty(' + p.item_id + ',-1)">−</button>' +
          '<input type="number" min="0" step="1" value="' + qty + '" data-item="' + p.item_id + '" oninput="setQty(' + p.item_id + ',this.value)" />' +
          '<button onclick="chgQty(' + p.item_id + ',1)">+</button>' +
        '</div>' +
      '</div>';
    }).join('');
  };

  window.chgQty = function (id, delta) {
    var cur = CART[id] || 0;
    setQty(id, cur + delta);
    var inp = document.querySelector('.prod input[data-item="' + id + '"]');
    if (inp) inp.value = CART[id] || 0;
  };
  window.setQty = function (id, val) {
    var n = Math.max(0, Math.floor(parseFloat(val) || 0));
    if (n > 0) CART[id] = n; else delete CART[id];
    updateCartBar();
  };

  function updateCartBar() {
    var ids = Object.keys(CART);
    var total = 0;
    ids.forEach(function (id) {
      var p = PRODUCTS.find(function (x) { return String(x.item_id) === String(id); });
      if (p) total += p.unit_price * CART[id];
    });
    el('cartCount').textContent = ids.length;
    el('cartTotal').textContent = money(total);
    el('cartbar').classList.toggle('show', ids.length > 0);
    el('btnOrder').disabled = ids.length === 0;
  }

  window.submitOrder = async function () {
    var items = Object.keys(CART).map(function (id) { return { item_id: parseInt(id, 10), quantity: CART[id] }; });
    if (!items.length) return;
    el('btnOrder').disabled = true;
    try {
      var r = await fetch('/api/portal/orders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: items }) });
      var data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Sipariş gönderilemedi.');
      notify(data.message || 'Siparişiniz alındı.', 'success');
      CART = {};
      updateCartBar();
      renderCatalog();
      showTab('orders');
    } catch (e) { notify(e.message, 'error'); el('btnOrder').disabled = false; }
  };

  // ── Siparişlerim ─────────────────────────────────────────────────────────
  var STATUS_TR = { PENDING: 'Onay Bekliyor', CONFIRMED: 'Onaylandı', REJECTED: 'Reddedildi' };
  async function loadOrders() {
    el('ordersList').innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      var rows = await (await fetch('/api/portal/orders')).json();
      if (!rows.length) { el('ordersList').innerHTML = '<div class="empty">Henüz siparişiniz yok.</div>'; return; }
      el('ordersList').innerHTML = rows.map(function (o) {
        var meta = o.created_at;
        if (o.status === 'CONFIRMED' && o.confirmed_at) meta = 'Onay: ' + o.confirmed_at;
        else if (o.status === 'REJECTED') meta = 'Red: ' + (o.rejected_at || '') + (o.reject_reason ? ' · ' + esc(o.reject_reason) : '');
        return '<div class="order">' +
          '<div class="order-h"><span class="order-no">' + esc(o.order_number) + '</span>' +
            '<span class="pill ' + o.status + '">' + (STATUS_TR[o.status] || o.status) + '</span></div>' +
          '<div class="order-h" style="margin-top:6px;"><span class="order-meta">' + o.item_count + ' kalem · ' + esc(meta) + '</span>' +
            '<span class="order-tot">' + orderMoney(o.total_amount, o.currency) + '</span></div>' +
        '</div>';
      }).join('');
    } catch (e) { el('ordersList').innerHTML = '<div class="empty">Siparişler yüklenemedi.</div>'; }
  }
  function orderMoney(v, cur) { try { return new Intl.NumberFormat('tr-TR', { style: 'currency', currency: cur || CURRENCY }).format(v || 0); } catch (_e) { return (v || 0).toFixed(2) + ' ' + (cur || CURRENCY); } }

  document.addEventListener('DOMContentLoaded', loadPortal);
})();
