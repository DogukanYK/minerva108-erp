/* Distribütör yönetimi (personel) — liste, hesap CRUD, fiyat listesi. */
(function () {
  'use strict';

  var DISTS = {};        // id → distributor
  var PRICE_DIST = null; // fiyat modalındaki distribütör id

  function notify(msg, type) { if (window.showToast) window.showToast(msg, type || 'info'); else if (type === 'error') alert(msg); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }
  function el(id) { return document.getElementById(id); }
  window.closeModal = function (id) { el(id).style.display = 'none'; };
  window.logout = function () { fetch('/api/logout', { method: 'POST' }).finally(function () { location.href = '/login'; }); };

  async function api(url, opts) {
    var r = await fetch(url, opts);
    var data = null; try { data = await r.json(); } catch (_e) {}
    if (!r.ok) throw new Error((data && data.detail) || ('Hata (' + r.status + ')'));
    return data;
  }

  async function loadDistributors() {
    var body = el('distBody');
    try {
      var rows = await api('/api/distributors');
      DISTS = {};
      if (!rows.length) { body.innerHTML = '<tr><td colspan="6" class="muted" style="text-align:center;padding:2rem;">Henüz distribütör yok.</td></tr>'; return; }
      body.innerHTML = rows.map(function (d) {
        DISTS[d.id] = d;
        var st = d.is_active ? '<span class="badge-on">Aktif</span>' : '<span class="badge-off">Pasif</span>';
        var canEdit = window.can('distributors', 'edit');
        var canPrices = window.can('distributors', 'prices');
        return '<tr>' +
          '<td><b>' + esc(d.company_name) + '</b>' + (d.contact_name ? '<div class="muted">' + esc(d.contact_name) + '</div>' : '') + '</td>' +
          '<td>' + esc(d.username || '—') + '</td>' +
          '<td><span class="badge-cur">' + esc(d.currency) + '</span></td>' +
          '<td>' + (d.order_count || 0) + '</td>' +
          '<td>' + st + '</td>' +
          '<td style="text-align:right;white-space:nowrap;">' +
            (canPrices ? '<button class="btn-sm-soft" onclick="openPrices(' + d.id + ')"><i class="bi bi-tags"></i> Fiyatlar</button> ' : '') +
            (canEdit ? '<button class="btn-sm-soft" onclick="openEdit(' + d.id + ')"><i class="bi bi-pencil"></i></button>' : '') +
          '</td></tr>';
      }).join('');
    } catch (e) { body.innerHTML = '<tr><td colspan="6" class="muted" style="text-align:center;padding:2rem;">' + esc(e.message) + '</td></tr>'; }
  }

  function fillForm(d) {
    el('fCompany').value = d.company_name || ''; el('fCurrency').value = d.currency || 'TRY';
    el('fContact').value = d.contact_name || ''; el('fPhone').value = d.phone || '';
    el('fEmail').value = d.email || ''; el('fCountry').value = d.country || '';
    el('fVat').value = d.vat || ''; el('fAddress').value = d.address || '';
    el('fActive').value = (d.is_active === false) ? 'false' : 'true';
  }

  window.openCreate = function () {
    el('distModalTitle').textContent = 'Yeni Distribütör'; el('distId').value = '';
    fillForm({}); el('fUsername').value = ''; el('fPassword').value = '';
    el('wUser').style.display = ''; el('wPass').style.display = '';
    el('btnResetPw').style.display = 'none';
    el('distModal').style.display = 'flex';
  };

  window.openEdit = function (id) {
    var d = DISTS[id]; if (!d) return;
    el('distModalTitle').textContent = 'Distribütör Düzenle'; el('distId').value = id;
    fillForm(d);
    el('wUser').style.display = 'none'; el('wPass').style.display = 'none';
    el('btnResetPw').style.display = window.can('distributors', 'edit') ? '' : 'none';
    el('distModal').style.display = 'flex';
  };

  window.saveDist = async function () {
    var id = el('distId').value;
    var payload = {
      company_name: el('fCompany').value.trim(), currency: el('fCurrency').value,
      contact_name: el('fContact').value.trim() || null, phone: el('fPhone').value.trim() || null,
      email: el('fEmail').value.trim() || null, country: el('fCountry').value.trim() || null,
      vat: el('fVat').value.trim() || null, address: el('fAddress').value.trim() || null,
    };
    if (!payload.company_name) { notify('Firma adı zorunludur.', 'error'); return; }
    try {
      if (id) {
        payload.is_active = (el('fActive').value === 'true');
        await api('/api/distributors/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        notify('Distribütör güncellendi.', 'success');
      } else {
        payload.username = el('fUsername').value.trim(); payload.password = el('fPassword').value;
        if (!payload.username || !payload.password) { notify('Kullanıcı adı ve şifre zorunludur.', 'error'); return; }
        await api('/api/distributors', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        notify('Distribütör oluşturuldu.', 'success');
      }
      closeModal('distModal'); loadDistributors();
    } catch (e) { notify(e.message, 'error'); }
  };

  window.resetPw = async function () {
    var id = el('distId').value; if (!id) return;
    var pw = prompt('Yeni şifre (min 10 karakter):');
    if (!pw) return;
    try {
      await api('/api/distributors/' + id + '/reset-password', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_password: pw }) });
      notify('Şifre güncellendi.', 'success');
    } catch (e) { notify(e.message, 'error'); }
  };

  window.openPrices = async function (id) {
    var d = DISTS[id]; if (!d) return;
    PRICE_DIST = id;
    el('priceCompany').textContent = d.company_name;
    el('priceCur').textContent = d.currency;
    el('priceBody').innerHTML = '<tr><td colspan="3" class="muted" style="text-align:center;padding:1.5rem;">Yükleniyor…</td></tr>';
    el('priceModal').style.display = 'flex';
    try {
      var res = await api('/api/distributors/' + id + '/prices');
      if (!res.items.length) { el('priceBody').innerHTML = '<tr><td colspan="3" class="muted" style="text-align:center;padding:1.5rem;">Bu panelde satılabilir ürün yok.</td></tr>'; return; }
      el('priceBody').innerHTML = res.items.map(function (it) {
        return '<tr><td>' + esc(it.name) + (it.name_tr ? '<div class="muted">' + esc(it.name_tr) + '</div>' : '') + '</td>' +
          '<td class="muted">' + esc(it.sku || '') + '</td>' +
          '<td><input class="form-control price-in" data-item="' + it.item_id + '" type="number" min="0" step="0.01" value="' + (it.unit_price != null ? it.unit_price : '') + '" placeholder="—" /></td></tr>';
      }).join('');
    } catch (e) { el('priceBody').innerHTML = '<tr><td colspan="3" class="muted" style="text-align:center;padding:1.5rem;">' + esc(e.message) + '</td></tr>'; }
  };

  window.savePrices = async function () {
    if (!PRICE_DIST) return;
    var prices = [];
    document.querySelectorAll('#priceBody .price-in').forEach(function (inp) {
      var v = inp.value.trim();
      prices.push({ item_id: parseInt(inp.getAttribute('data-item'), 10), unit_price: v === '' ? null : parseFloat(v) });
    });
    try {
      var res = await api('/api/distributors/' + PRICE_DIST + '/prices', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prices: prices }) });
      notify(res.message || 'Fiyatlar kaydedildi.', 'success');
      closeModal('priceModal');
    } catch (e) { notify(e.message, 'error'); }
  };

  document.addEventListener('DOMContentLoaded', loadDistributors);
})();
