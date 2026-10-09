/* Minerva 108 — B2B sipariş akışı (onay → proforma → parti üretimi → tek sevkiyat).
   Yetki/atama kararı SUNUCUDADIR: düğmeler yalnız detail.actions izin verdiğinde çizilir. */
(function () {
  'use strict';

  const API = '/api/b2b-orders';
  const state = { boot: null, orders: [], detail: null, selectedId: null, busy: false, action: null, epoch: 0 };
  const $ = id => document.getElementById(id);
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = v => (v == null || v === '') ? '—' : Number(v).toLocaleString('tr-TR', { maximumFractionDigits: 4 });
  const money = (v, cur) => v == null ? '—' : `${Number(v).toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${esc(cur || '')}`;
  const posId = v => Number.isSafeInteger(Number(v)) && Number(v) > 0;
  const STEPS = [['SUBMITTED', 'Teknik değerlendirme'], ['TECH_REVIEWED', 'Yönetim onayı'], ['APPROVED', 'Ödeme · hazırlık · üretim'], ['SHIPPED', 'Sevk']];
  const PL_STATUS = { open: 'Açık', ordered: 'Sipariş verildi', received: 'Teslim alındı', cancelled: 'İptal' };
  const BATCH = { STARTED: 'Başladı (tüketim yazıldı)', COMPLETED: 'Tamamlandı', CANCELLED: 'İptal edildi' };

  function errorText(d) {
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map(x => (x && x.msg) || 'Alanları kontrol edin.').join(' ');
    return 'İşlem tamamlanamadı. Bilgileri kontrol edip yeniden deneyin.';
  }
  async function request(path, body, method) {
    const m = method || (body === undefined ? 'GET' : 'POST');
    const res = await fetch(API + path, { method: m, credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(errorText(data.detail));
      err.data = data; throw err;
    }
    return data;
  }
  function notify(msg, bad = false) {
    if (typeof window.showToast === 'function') { window.showToast(msg, bad ? 'error' : 'success'); return; }
    const n = document.createElement('div'); n.className = 'os-notice' + (bad ? ' os-notice-danger' : ''); n.textContent = msg;
    $('toast-container').appendChild(n); setTimeout(() => n.remove(), 6000);
  }
  function pageError(msg) {
    $('b2Alert').replaceChildren();
    if (!msg) return;
    const n = document.createElement('div'); n.className = 'os-notice os-notice-danger'; n.textContent = msg;
    $('b2Alert').appendChild(n);
  }
  function badge(status, label) {
    const kind = status === 'SHIPPED' || status === 'APPROVED' ? 'good' : status === 'CANCELLED' ? 'bad' : 'warn';
    return `<span class="os-badge os-badge-${kind}">${esc(label || status)}</span>`;
  }
  const allowed = a => !!(state.detail && state.detail.actions && state.detail.actions[a]);
  const IBANS = [['USD', 'iban_usd'], ['EUR', 'iban_eur'], ['TRY', 'iban_try'], ['RUB', 'iban_rub']];
  const ibanCcys = b => IBANS.filter(([, f]) => b[f]).map(([c]) => c).join(' · ');
  const bankNames = banks => (banks || []).map(b => b.label || b.bank_name).join(', ');
  function termsText(t) {
    if (!t) return '';
    const days = t.loading_days == null || t.loading_days === '' ? '—' : `${fmt(t.loading_days)} gün`;
    return `${esc(t.transportation)} · ${esc(t.payment || '')} · ${esc(t.shipment)} · ${esc(t.delivery_type)} · yükleme ${days}`;
  }

  // ─── Liste ────────────────────────────────────────────────────────────────
  function renderStats() {
    const o = state.orders;
    const cnt = s => o.filter(x => x.status === s).length;
    $('b2Stats').innerHTML = [[o.filter(x => ['SUBMITTED', 'TECH_REVIEWED', 'APPROVED'].includes(x.status)).length, 'Açık sipariş'],
      [cnt('SUBMITTED'), 'Teknik değerlendirme'], [cnt('TECH_REVIEWED'), 'İmza bekliyor'], [cnt('APPROVED'), 'Onaylı / üretimde']]
      .map(([n, l]) => `<div class="os-stat"><strong>${n}</strong><span>${l}</span></div>`).join('');
  }
  function renderList() {
    const show = state.boot && state.boot.show_prices;
    $('b2ListHost').innerHTML = state.orders.length ? `<table class="os-table"><thead><tr><th>Sipariş</th><th>Müşteri</th><th>Durum</th><th>Sıradaki adım</th>${show ? '<th class="num">Tutar</th>' : ''}<th>Hedef</th><th></th></tr></thead><tbody>${state.orders.map(o => `<tr><td class="os-code">${esc(o.reference)}</td><td><strong>${esc(o.customer_name)}</strong><div class="os-muted">${esc(o.customer_country)}</div></td><td>${badge(o.status, o.status_label)}</td><td class="b2-next">${o.next_step ? `<strong>${esc(o.next_step.text)}</strong><br>${esc(o.next_step.who)}` : '—'}</td>${show ? `<td class="num">${money(o.total, o.currency)}</td>` : ''}<td>${esc(o.target_date || '—')}</td><td><button type="button" class="os-b os-b-sm" data-b2-open="${Number(o.id)}">Aç <i class="bi bi-arrow-right"></i></button></td></tr>`).join('')}</tbody></table>`
      : '<div class="os-empty">Bu süzgeçte sipariş yok. Teklifler sayfasında taslak bir teklifi "Siparişe dönüştür" ile başlatın.</div>';
  }
  async function reload() {
    const filter = $('b2Filter').value;
    const [boot, list] = await Promise.all([state.boot ? Promise.resolve(state.boot) : request('/bootstrap'),
      request(filter ? `?status=${encodeURIComponent(filter)}` : '')]);
    state.boot = boot; state.orders = list.orders || [];
    renderStats(); renderList(); renderBanks(); pageError('');
  }

  // ─── Detay ────────────────────────────────────────────────────────────────
  async function openOrder(id) {
    if (!posId(id)) return;
    const epoch = ++state.epoch; state.selectedId = Number(id);
    $('b2Detail').hidden = false; $('b2Detail').innerHTML = '<div class="os-card"><div class="os-empty">Sipariş yükleniyor…</div></div>';
    try {
      const d = await request(`/${Number(id)}`);
      if (epoch !== state.epoch) return;
      state.detail = d; renderDetail();
      if (location.hash !== `#${id}`) history.replaceState(null, '', `#${id}`);
      $('b2Detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) { $('b2Detail').innerHTML = ''; notify(e.message, true); }
  }
  function card(icon, title, body, tools = '') {
    return `<div class="os-card"><div class="os-card-head"><i class="bi ${icon}"></i><h2>${title}</h2><div class="os-tools">${tools}</div></div><div class="os-card-body">${body}</div></div>`;
  }
  function btn(action, label, main = false, data = '') {
    return `<button type="button" class="os-b${main ? ' os-b-main' : ''}" data-b2-action="${action}" ${data}>${label}</button>`;
  }
  function renderDetail() {
    const d = state.detail; if (!d) return;
    const o = d.order; const show = !!d.commercial;
    const idx = STEPS.findIndex(s => s[0] === o.status);
    const steps = `<div class="b2-steps">${STEPS.map((s, i) => `<span class="b2-step ${o.status === 'CANCELLED' ? '' : i < idx ? 'is-done' : i === idx ? 'is-current' : ''}">${i + 1}. ${s[1]}</span>`).join('')}</div>`;
    const next = o.next_step ? `<div class="os-notice">Sıradaki adım: <strong>${esc(o.next_step.text)}</strong> — ${esc(o.next_step.who)}</div>` : '';
    const cust = d.customer || {};
    const lineHead = `<tr><th>Ürün</th><th class="num">Sipariş</th>${show ? '<th class="num">Birim fiyat</th><th class="num">Tutar</th>' : ''}<th class="num">Stoktan</th><th class="num">Üretilecek</th><th>Reçete</th><th class="num">Üretilen (müşteri)</th><th class="num">Şahit</th><th class="num">Açık parti</th><th class="num">Sevke uygun stok</th></tr>`;
    const lineRows = d.lines.map(l => `<tr><td><strong>${esc(l.name)}</strong></td><td class="num">${fmt(l.quantity)} ${esc(l.unit)}</td>${show ? `<td class="num">${money(l.unit_price, d.commercial.currency)}</td><td class="num">${money(l.line_total, d.commercial.currency)}</td>` : ''}<td class="num">${fmt(l.use_stock)}</td><td class="num">${fmt(l.produce)}</td><td>${esc(l.recipe_name || '—')}${l.recipe_changed ? ' <span class="b2-bad">(reçete değişti — yeniden değerlendirin)</span>' : ''}</td><td class="num">${fmt(l.customer_quantity)}</td><td class="num">${fmt(l.witness)}</td><td class="num">${fmt(l.started)}</td><td class="num">${fmt(l.released_stock)}</td></tr>`).join('');
    const commercial = show ? `<div class="os-meta"><div><span class="os-muted">Ara toplam</span><br>${money(d.commercial.subtotal, d.commercial.currency)}</div><div><span class="os-muted">Kargo</span><br>${money(d.commercial.shipping, d.commercial.currency)}</div><div><span class="os-muted">KDV %${fmt(d.commercial.tax_percentage)}</span><br>${money(d.commercial.tax_amount, d.commercial.currency)}</div><div><span class="os-muted">Genel toplam</span><br><strong>${money(d.commercial.total, d.commercial.currency)}</strong></div></div><p class="os-muted" style="margin-top:.6rem">Ödeme: ${esc(o.payment_terms_label)}${o.payment_terms === 'advance' ? ` (%${fmt(o.advance_percent)})` : ''} · Bankalar: ${(d.commercial.banks || []).length ? esc(bankNames(d.commercial.banks)) : '<span class="b2-bad">seçilmedi — imzadan önce ticari revizyonla seçin</span>'}</p><p class="os-muted">Proforma şartları: ${termsText(d.commercial.terms)}</p>` : '<p class="os-muted">Teknik görünüm: satış fiyatları ve banka bilgisi gösterilmez.</p>';
    const tech = d.technical;
    const techBody = tech ? `${tech.stale ? '<div class="os-notice os-notice-danger">Ticari sürüm değişti — teknik değerlendirme yenilenmeli.</div>' : ''}<p class="os-muted">Sürüm ${tech.revision} · ${esc(tech.created_by)} · ${esc(tech.created_at)}</p>${tech.note ? `<p>${esc(tech.note)}</p>` : ''}${(tech.warnings || []).map(w => `<div class="os-notice">${esc(w)}</div>`).join('')}<h3 class="os-section-title">Malzeme ihtiyacı ve eksikler</h3><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Malzeme</th><th class="num">İhtiyaç</th><th class="num">Stok</th><th class="num">Alınacak</th><th>Önerilen tedarikçi</th>${show ? '<th class="num">Tutar</th>' : ''}</tr></thead><tbody>${(tech.materials || []).map(m => `<tr><td>${esc(m.name)}</td><td class="num">${fmt(m.need)} ${esc(m.unit)}</td><td class="num">${fmt(m.stock)}</td><td class="num ${m.buy > 0 ? 'b2-bad' : 'b2-ok'}">${fmt(m.buy)}</td><td>${esc(m.supplier || '—')}</td>${show ? `<td class="num">${m.amount == null ? '—' : fmt(m.amount)}</td>` : ''}</tr>`).join('') || '<tr><td colspan="6" class="os-muted">Üretilecek satır yok.</td></tr>'}</tbody></table></div>` : '<p class="os-muted">Teknik değerlendirme henüz yapılmadı.</p>';
    const sig = d.signature;
    const sigBody = sig ? `<p><span class="b2-ok"><i class="bi bi-check-circle"></i></span> <strong>${esc(sig.signer_name)}</strong> · ${esc(sig.signed_at)} · ticari r${sig.commercial_revision} / teknik r${sig.technical_revision}</p>${sig.image ? `<img class="b2-sigimg" alt="İmza" src="${esc(sig.image)}" />` : ''}<p class="os-muted">Belge özeti ${esc((sig.document_hash || '').slice(0, 16))}…</p>` : `<p class="os-muted">Güncel sürüm için yönetim onayı yok. İmzacı: ${esc(o.signer)}</p>`;
    const pay = d.payment;
    const payBody = show ? `<p>Ödenen <strong>${money(pay.paid, pay.currency)}</strong> / toplam ${money(pay.total, pay.currency)} · Üretim için gereken ${money(pay.required_for_production, pay.currency)} <span class="${pay.production_ok ? 'b2-ok' : 'b2-bad'}">${pay.production_ok ? '✓' : '✗'}</span> · Sevkiyat için ${money(pay.required_for_shipment, pay.currency)} <span class="${pay.shipment_ok ? 'b2-ok' : 'b2-bad'}">${pay.shipment_ok ? '✓' : '✗'}</span></p><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Tarih</th><th class="num">Tutar</th><th>Referans</th><th>Doğrulayan</th></tr></thead><tbody>${(d.payments || []).map(p => `<tr><td>${esc(p.received_on || p.created_at)}</td><td class="num">${money(p.amount, p.currency)}</td><td>${esc(p.reference || '—')}</td><td>${esc(p.verified_by)}</td></tr>`).join('') || '<tr><td colspan="4" class="os-muted">Ödeme kaydı yok.</td></tr>'}</tbody></table></div>` : `<p>Üretim için ödeme koşulu: <span class="${pay.production_ok ? 'b2-ok' : 'b2-bad'}">${pay.production_ok ? 'sağlandı' : 'bekleniyor'}</span> · Sevkiyat için: <span class="${pay.shipment_ok ? 'b2-ok' : 'b2-bad'}">${pay.shipment_ok ? 'sağlandı' : 'bekleniyor'}</span></p>`;
    const purchase = d.purchase_lines.filter(p => p.status !== 'cancelled');
    const purchaseBody = purchase.length ? `<div class="os-table-wrap"><table class="os-table"><thead><tr><th>Malzeme</th><th class="num">İhtiyaç</th><th>Tedarikçi</th><th>Durum</th><th class="num">Sipariş</th><th class="num">Teslim</th><th></th></tr></thead><tbody>${purchase.map(p => `<tr><td>${esc(p.item_name)}</td><td class="num">${fmt(p.need_quantity)} ${esc(p.unit)}</td><td>${esc(p.supplier_name || '—')}</td><td><span class="b2-pill">${esc(PL_STATUS[p.status] || p.status)}</span></td><td class="num">${fmt(p.ordered_quantity)}</td><td class="num">${fmt(p.received_quantity)}</td><td>${allowed('purchase') ? btn('purchase', 'Güncelle', false, `data-b2-id="${Number(p.id)}"`) : ''}</td></tr>`).join('')}</tbody></table></div>` : '<p class="os-muted">Alım ihtiyacı yok.</p>';
    const batches = d.batches.map(b => `<tr><td class="os-code">${esc(b.lot_number)}</td><td>${esc((d.lines.find(l => l.item_id === b.item_id) || {}).name || '')}</td><td>${esc(BATCH[b.status] || b.status)}</td><td class="num">${fmt(b.planned_quantity)}</td><td class="num">${fmt(b.produced_quantity)}</td><td class="num">${fmt(b.witness_quantity)}</td><td class="os-muted">${esc(b.started_by || '')} ${esc(b.started_at)}</td><td>${allowed('produce') && b.status === 'STARTED' ? btn('complete', 'Tamamla', true, `data-b2-id="${Number(b.id)}"`) + ' ' + btn('cancel-batch', 'İptal', false, `data-b2-id="${Number(b.id)}"`) : ''}${allowed('produce') && b.status === 'CANCELLED' ? btn('return', 'Fiziksel iade', false, `data-b2-id="${Number(b.id)}"`) : ''}</td></tr>`).join('');
    const ship = d.shipment;
    const shipBody = o.status === 'SHIPPED' ? `<p class="b2-ok"><i class="bi bi-truck"></i> ${esc(o.shipped_by)} · ${esc(o.shipped_at)} — teslimat kaydı #${Number(o.delivery_id)}</p>` : ship ? `${ship.problems.length ? `<ul class="b2-bad">${ship.problems.map(p => `<li>${esc(p)}</li>`).join('')}</ul>` : '<p class="b2-ok">Sevkiyata hazır.</p>'}` : '<p class="os-muted">Sevkiyat yönetim onayından sonra.</p>';
    const tools = [allowed('proforma') ? `<a class="os-b" href="${API}/${Number(o.id)}/proforma" target="_blank" rel="noopener"><i class="bi bi-file-earmark-pdf"></i>Proforma</a>` : '',
      `<a class="os-b" href="${API}/${Number(o.id)}/technical-sheet" target="_blank" rel="noopener" title="İç belge — müşteriye gönderilmez"><i class="bi bi-file-earmark-medical"></i>İç teknik föy</a>`,
      allowed('reassign') ? btn('reassign', 'Sorumlular') : '', allowed('cancel') ? `<button type="button" class="os-b os-b-danger" data-b2-action="cancel">Siparişi iptal et</button>` : '',
      `<button type="button" class="os-b" data-b2-action="refresh"><i class="bi bi-arrow-clockwise"></i>Yenile</button>`].join('');
    $('b2Detail').innerHTML = `<div class="os-detail-head"><div><h2>${esc(o.reference)} · ${esc(cust.name)}</h2><span class="os-muted">${esc(cust.country)} · Ticari r${o.commercial_revision} · Teknik r${o.technical_revision} · Etiket ${esc(o.label_language)}${o.target_date ? ` · Hedef ${esc(o.target_date)}` : ''}</span></div><div class="os-tools">${badge(o.status, o.status_label)}${tools}</div></div>${steps}${next}${o.cancel_reason ? `<div class="os-notice os-notice-danger">İptal: ${esc(o.cancel_reason)}</div>` : ''}
      ${card('bi-receipt', 'Sipariş ve ticari bilgi', `<p>${esc(cust.name)} · ${esc(cust.contact || '')} · ${esc(cust.email || '')} · ${esc(cust.phone || '')}</p><p class="os-muted">${esc(cust.address || '')}</p><div class="os-table-wrap"><table class="os-table"><thead>${lineHead}</thead><tbody>${lineRows}</tbody></table></div>${commercial}<p class="os-muted">Sorumlular: sipariş ${esc(o.owner)} · teknik ${esc(o.technical_user)} · imza ${esc(o.signer)}</p>`, allowed('revise') ? btn('revise', 'Ticari revizyon') : '')}
      ${card('bi-clipboard-data', 'Teknik değerlendirme', techBody, allowed('tech_review') ? btn('review', tech ? 'Yeniden değerlendir' : 'Değerlendir', true) : '')}
      ${card('bi-pen', 'Yönetim onayı (şifre + imza)', sigBody, allowed('sign') ? btn('sign', 'İmzala ve onayla', true) : '')}
      ${card('bi-cash-coin', 'Ödeme', payBody, allowed('payment') ? btn('payment', 'Ödeme kaydet') : '')}
      ${card('bi-cart-check', 'Alım satırları', purchaseBody)}
      ${card('bi-check2-square', 'Son hazırlık kontrolü', o.prep_confirmed_at ? `<p class="b2-ok">✓ ${esc(o.prep_confirmed_by)} · ${esc(o.prep_confirmed_at)}</p>` : '<p class="os-muted">Eksik malzemeler teslim alınınca teknik sorumlu onaylar.</p>', allowed('prepare') ? btn('prepare', 'Hazırlık tamam', true) : '')}
      ${card('bi-gear', 'Üretim partileri', batches ? `<div class="os-table-wrap"><table class="os-table"><thead><tr><th>Lot</th><th>Ürün</th><th>Durum</th><th class="num">Plan</th><th class="num">Üretilen</th><th class="num">Şahit</th><th>Başlatan</th><th></th></tr></thead><tbody>${batches}</tbody></table></div>` : '<p class="os-muted">Henüz parti yok. Başlatınca malzemeler o an düşer; bitmiş ürün tamamlamada stoğa girer.</p>', allowed('produce') ? btn('start', 'Parti başlat', true) : '')}
      ${card('bi-truck', 'Tek sevkiyat', shipBody, allowed('ship') ? btn('ship', 'Siparişi sevk et', true) : '')}
      ${card('bi-clock-history', 'Zaman çizelgesi', `<ul class="b2-timeline">${(d.timeline || []).map(t => `<li><span class="os-muted">${esc(t.at)}</span> · <strong>${esc(t.action)}</strong> — ${esc(t.actor)}</li>`).join('')}</ul>`)}`;
  }

  // ─── Eylem modali ─────────────────────────────────────────────────────────
  function field(id, label, type = 'text', extra = '', value = '') {
    return `<div><label class="os-label" for="${id}">${label}</label><input class="os-input" id="${id}" type="${type}" value="${esc(value)}" ${extra} /></div>`;
  }
  function select(id, label, options, value) {
    return `<div><label class="os-label" for="${id}">${label}</label><select class="os-select" id="${id}">${options.map(([v, t]) => `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>`;
  }
  function openAction(kind, id) {
    const d = state.detail; if (!d || state.busy) return;
    if (kind === 'refresh') { openOrder(d.order.id); return; }
    const o = d.order; let html = ''; let title = ''; let submit = 'Kaydet';
    state.action = { kind, id: id ? Number(id) : null, orderId: o.id };
    if (kind === 'review') {
      title = 'Teknik değerlendirme';
      html = `<p class="os-muted">Her ürün için mevcut bitmiş stoktan kullanılacak adedi girin; kalanı üretilecek. Eksik malzeme Satın Alma Planı motoruyla hesaplanır.</p><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Ürün</th><th class="num">Sipariş</th><th class="num">Sevke uygun stok</th><th>Stoktan kullan</th><th>Reçete</th></tr></thead><tbody>${d.lines.map(l => `<tr data-b2-line="${Number(l.item_id)}"><td>${esc(l.name)}</td><td class="num">${fmt(l.quantity)}</td><td class="num">${fmt(l.released_stock)}</td><td><input class="os-input" type="number" min="0" max="${Number(l.quantity)}" step="any" data-b2-use value="${Number(l.use_stock || 0)}" /></td><td><select class="os-select" data-b2-recipe>${(l.recipe_options || []).map(r => `<option value="${Number(r.id)}" ${r.id === l.recipe_id ? 'selected' : ''}>${esc(r.name)}</option>`).join('') || '<option value="">Reçete yok — tamamı stoktan</option>'}</select></td></tr>`).join('')}</tbody></table></div><label class="os-label" for="b2Note">Not</label><textarea id="b2Note" class="os-textarea" maxlength="4000">${esc(d.technical ? d.technical.note : '')}</textarea>`;
    } else if (kind === 'sign') {
      title = 'Yönetim onayı — şifre ve imza'; submit = 'İmzala ve onayla';
      const c = d.commercial || {};
      html = `<div class="os-notice">${esc(o.reference)} · ${esc(d.customer.name)} · Genel toplam ${money(c.total, c.currency)} · Ticari r${o.commercial_revision} / Teknik r${o.technical_revision} · Bankalar ${esc(bankNames(c.banks) || '—')}<br><span class="os-muted">Belge özeti ${esc((o.document_hash || '').slice(0, 16))}…</span></div><p class="os-muted">Onay; siparişi, eksik alım listesini ve üretim planını kapsar. Şifreniz saklanmaz; yanlış denemeler giriş kilidine sayılır.</p><label class="os-label">İmzanızı çizin</label><canvas id="b2Pad" class="b2-sign-pad" width="900" height="240"></canvas><button type="button" class="os-b os-b-sm" id="b2PadClear" style="margin:.4rem 0 1rem">Temizle</button>${field('b2Password', 'Şifreniz', 'password', 'autocomplete="current-password" required')}<label class="os-check" style="margin-top:1rem"><input type="checkbox" id="b2SignConfirm" required /><span>Gösterilen sürümü inceledim ve kendi hesabımla onaylıyorum.</span></label>`;
    } else if (kind === 'payment') {
      title = 'Ödeme kaydet';
      html = `<div class="os-grid">${field('b2PayAmount', `Tutar (${esc(d.commercial.currency)})`, 'number', 'min="0.01" step="0.01" required')}${field('b2PayDate', 'Ödeme tarihi', 'date')}${field('b2PayRef', 'Referans / dekont no', 'text', 'maxlength="150"')}</div>`;
    } else if (kind === 'prepare') {
      title = 'Son hazırlık kontrolü'; submit = 'Hazırlık tamam';
      const open = d.purchase_lines.filter(p => ['open', 'ordered'].includes(p.status)).length;
      html = `${open ? `<div class="os-notice os-notice-danger">${open} alım satırı henüz teslim alınmadı. Parti başlangıcında stok yeniden kontrol edilir.</div>` : ''}<label class="os-label" for="b2Note">Not</label><textarea id="b2Note" class="os-textarea" maxlength="2000"></textarea>`;
    } else if (kind === 'purchase') {
      const p = d.purchase_lines.find(x => x.id === Number(id)); if (!p) return;
      title = `Alım satırı — ${p.item_name}`;
      html = `<div class="os-grid">${select('b2PlStatus', 'Durum', Object.entries(PL_STATUS), p.status)}${field('b2PlSupplier', 'Tedarikçi', 'text', 'maxlength="150"', p.supplier_name)}${field('b2PlOrdered', `Sipariş verilen (${esc(p.unit)})`, 'number', 'min="0" step="any"', p.ordered_quantity)}${field('b2PlReceived', `Teslim alınan (${esc(p.unit)})`, 'number', 'min="0" step="any"', p.received_quantity)}</div><label class="os-label" for="b2Note">Not</label><textarea id="b2Note" class="os-textarea" maxlength="2000">${esc(p.note)}</textarea>`;
    } else if (kind === 'start') {
      title = 'Sipariş partisi başlat'; submit = 'Partiyi başlat';
      const opts = d.lines.filter(l => (l.remaining_to_start || 0) > 0).map(l => [l.item_id, `${l.name} — kalan ${fmt(l.remaining_to_start)}`]);
      if (!opts.length) { notify('Başlatılacak üretim kalmadı.', true); return; }
      html = `<div class="os-notice">Başlatınca hammadde, ambalaj ve etiket o an stoktan düşer; bitmiş ürün tamamlamada girer. Hammadde lotlarının SKT'si dolu ve geçerli olmalıdır.</div><div class="os-grid">${select('b2BatchItem', 'Ürün', opts, opts[0][0])}${field('b2BatchQty', 'Parti miktarı', 'number', 'min="0.000001" step="any" required')}</div>`;
    } else if (kind === 'complete') {
      const b = d.batches.find(x => x.id === Number(id)); if (!b) return;
      title = `Partiyi tamamla — lot ${b.lot_number}`; submit = 'Tamamla';
      html = `<div class="os-notice">Malzemeler başlangıçta düşüldü; burada yalnız bitmiş ürün stoğa girer. Şahit numune müşteri adedinden ayrı tutulur.</div><div class="os-grid">${field('b2Produced', 'Gerçek üretilen miktar', 'number', 'min="0.000001" step="any" required', b.planned_quantity)}${field('b2Witness', 'Şahit numune (toplamın içinde)', 'number', 'min="0" step="any"', 0)}</div>`;
    } else if (kind === 'cancel-batch' || kind === 'cancel') {
      title = kind === 'cancel' ? 'Siparişi iptal et' : 'Partiyi iptal et'; submit = 'İptal et';
      html = `${kind === 'cancel-batch' ? '<div class="os-notice os-notice-danger">İptal, tüketilen malzemeleri otomatik geri VERMEZ. Kullanılmayan malzeme fiziksel iade ile ayrıca kaydedilir.</div>' : ''}<label class="os-label" for="b2Reason">Gerekçe</label><textarea id="b2Reason" class="os-textarea" minlength="5" maxlength="2000" required></textarea>`;
    } else if (kind === 'return') {
      const b = d.batches.find(x => x.id === Number(id)); if (!b) return;
      title = `Fiziksel iade — lot ${b.lot_number}`;
      html = `<div class="os-grid">${select('b2RetItem', 'Malzeme', b.consumed.map(c => [c.item_id, `${c.name} — tüketilen ${fmt(c.quantity)}, iade ${fmt(c.returned)}`]), b.consumed[0] && b.consumed[0].item_id)}${field('b2RetQty', 'İade miktarı', 'number', 'min="0.000001" step="any" required')}</div><label class="os-label" for="b2Reason">Gerekçe</label><textarea id="b2Reason" class="os-textarea" minlength="5" maxlength="2000" required></textarea>`;
    } else if (kind === 'ship') {
      title = 'Siparişi tek seferde sevk et'; submit = 'Sevk et';
      html = `<div class="os-notice">Stok ve lotlar kilit altında yeniden doğrulanır; QC bekleyen ve şahit lotlar kullanılmaz; sipariş bir kez düşer.</div><div class="os-grid">${field('b2Tracking', 'Kargo takip no', 'text', 'maxlength="100" required')}${field('b2Carrier', 'Kargo firması', 'text', 'maxlength="80"')}</div>`;
    } else if (kind === 'reassign') {
      title = 'Sorumluları değiştir';
      html = `<div class="os-grid">${select('b2Tech', 'Teknik değerlendirme', state.boot.technical_users.map(u => [u.id, u.name]), o.technical_user_id)}${select('b2Signer', 'Yönetim onayı', state.boot.signers.map(u => [u.id, u.name]), o.signer_user_id)}</div>`;
    } else if (kind === 'revise') {
      title = 'Ticari revizyon'; submit = 'Yeni ticari sürüm kaydet';
      const c = d.commercial;
      html = `<div class="os-notice">Ürün, adet veya etiket dili değişirse teknik ve yönetim onayı; yalnız fiyat/şart/banka değişirse yönetim onayı yenilenir.</div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Ürün</th><th>Adet</th><th>Birim fiyat (${esc(c.currency)})</th></tr></thead><tbody>${d.lines.map(l => `<tr data-b2-rev="${Number(l.item_id)}"><td>${esc(l.name)}</td><td><input class="os-input" type="number" min="0.000001" step="any" data-b2-qty value="${Number(l.quantity)}" /></td><td><input class="os-input" type="number" min="0" step="any" data-b2-price value="${Number(l.unit_price)}" /></td></tr>`).join('')}</tbody></table></div><div class="os-grid">${select('b2Terms', 'Ödeme koşulu', Object.entries(state.boot.payment_terms), o.payment_terms)}${field('b2Advance', 'Avans %', 'number', 'min="1" max="99" step="any"', o.advance_percent || '')}${select('b2Lang', 'Etiket dili', [['EN', 'İngilizce'], ['TR', 'Türkçe']], o.label_language)}${field('b2Target', 'Hedef tarih', 'date', '', o.target_date || '')}${field('b2Shipping', 'Kargo tutarı', 'number', 'min="0" step="any"', c.shipping)}${field('b2Tax', 'KDV %', 'number', 'min="0" max="100" step="any"', c.tax_percentage)}</div>${bankPicker(o.bank_profile_ids || [])}${termsFields(o.proforma_terms || {})}`;
    }
    $('b2ActionTitle').textContent = title; $('b2ActionFields').innerHTML = html; $('b2ActionSubmit').textContent = submit;
    bootstrap.Modal.getOrCreateInstance($('b2ActionModal')).show();
    if (kind === 'sign') setupPad();
  }

  // Proforma bankaları (1–3, sıra = sütun sırası) + şartlar — revizyon modali
  function bankPicker(selected) {
    const rows = (state.boot.banks || []).filter(b => b.is_active && ibanCcys(b));
    if (!rows.length) return '<p class="os-muted" style="margin-top:1rem">Tanımlı aktif banka hesabı yok — Banka hesapları sekmesinden ekleyin.</p>';
    const max = Number(state.boot.max_banks || 3);
    return `<label class="os-label" style="margin-top:1rem">Proformadaki banka hesapları (en fazla ${max})</label><div style="display:flex;flex-wrap:wrap;gap:.4rem 1.2rem">${rows.map(b => `<label class="os-check"><input type="checkbox" data-b2-bank="${Number(b.id)}" ${selected.includes(b.id) ? 'checked' : ''} /><span>${esc(b.label)} <span class="os-muted">(${esc(ibanCcys(b))})</span></span></label>`).join('')}</div>`;
  }
  function termsFields(t) {
    const v = k => (t[k] == null ? '' : t[k]);
    return `<div class="os-grid" style="margin-top:1rem">${field('b2TTransport', 'Transportation', 'text', 'maxlength="120"', v('transportation'))}${field('b2TShipment', 'Shipment', 'text', 'maxlength="120"', v('shipment'))}${field('b2TDelivery', 'Type of delivery', 'text', 'maxlength="120"', v('delivery_type'))}${field('b2TDays', 'Yükleme (gün)', 'number', 'min="0" max="365" step="1" placeholder="____"', v('loading_days'))}</div><p class="os-muted">Payment terms satırı ödeme koşulundan yazılır.</p>`;
  }

  // ─── İmza tuvali ──────────────────────────────────────────────────────────
  function setupPad() {
    const pad = $('b2Pad'); const ctx = pad.getContext('2d'); let drawing = false; state.action.strokes = 0;
    ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.strokeStyle = '#1f2d52';
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, pad.width, pad.height);
    const pos = e => { const r = pad.getBoundingClientRect(); return [(e.clientX - r.left) * pad.width / r.width, (e.clientY - r.top) * pad.height / r.height]; };
    pad.addEventListener('pointerdown', e => { drawing = true; pad.setPointerCapture(e.pointerId); const [x, y] = pos(e); ctx.beginPath(); ctx.moveTo(x, y); });
    pad.addEventListener('pointermove', e => { if (!drawing) return; const [x, y] = pos(e); ctx.lineTo(x, y); ctx.stroke(); state.action.strokes++; });
    const end = () => { drawing = false; };
    pad.addEventListener('pointerup', end); pad.addEventListener('pointercancel', end);
    $('b2PadClear').addEventListener('click', () => { ctx.fillRect(0, 0, pad.width, pad.height); state.action.strokes = 0; });
  }

  async function submitAction(ev) {
    ev.preventDefault();
    const a = state.action; const d = state.detail;
    if (a && a.kind === 'bank') { if (!state.busy) await submitBank(); return; }
    if (!a || !d || state.busy || d.order.id !== a.orderId) return;
    const o = d.order; const base = `/${Number(o.id)}`;
    let path = ''; let body = {};
    const val = id => ($(id) ? $(id).value.trim() : '');
    const num = id => Number(val(id));
    if (a.kind === 'review') {
      path = '/technical-review';
      body = { commercial_revision: o.commercial_revision, note: val('b2Note'),
        lines: [...document.querySelectorAll('[data-b2-line]')].map(r => ({ item_id: Number(r.dataset.b2Line),
          use_stock_quantity: Number(r.querySelector('[data-b2-use]').value || 0),
          recipe_id: Number(r.querySelector('[data-b2-recipe]').value) || null })) };
    } else if (a.kind === 'sign') {
      if ((a.strokes || 0) < 15) { notify('Lütfen imzanızı çizin.', true); return; }
      if (!$('b2SignConfirm').checked || !val('b2Password')) { notify('Şifre ve onay kutusu gerekli.', true); return; }
      path = '/sign';
      body = { commercial_revision: o.commercial_revision, technical_revision: o.technical_revision,
        document_hash: o.document_hash, password: $('b2Password').value, signature: $('b2Pad').toDataURL('image/png') };
    } else if (a.kind === 'payment') {
      path = '/payments'; body = { amount: num('b2PayAmount'), received_on: val('b2PayDate') || null, reference: val('b2PayRef') || null };
    } else if (a.kind === 'prepare') {
      path = '/preparation'; body = { note: val('b2Note') };
    } else if (a.kind === 'purchase') {
      path = `/purchase-lines/${a.id}`;
      body = { status: val('b2PlStatus'), supplier_name: val('b2PlSupplier'), ordered_quantity: num('b2PlOrdered'), received_quantity: num('b2PlReceived'), note: val('b2Note') };
    } else if (a.kind === 'start') {
      path = '/batches'; body = { item_id: num('b2BatchItem'), quantity: num('b2BatchQty') };
    } else if (a.kind === 'complete') {
      path = `/batches/${a.id}/complete`; body = { produced_quantity: num('b2Produced'), witness_quantity: num('b2Witness') || 0 };
    } else if (a.kind === 'cancel-batch') {
      path = `/batches/${a.id}/cancel`; body = { reason: val('b2Reason') };
    } else if (a.kind === 'cancel') {
      path = '/cancel'; body = { reason: val('b2Reason') };
    } else if (a.kind === 'return') {
      path = `/batches/${a.id}/returns`; body = { item_id: num('b2RetItem'), quantity: num('b2RetQty'), reason: val('b2Reason') };
    } else if (a.kind === 'ship') {
      path = '/ship'; body = { tracking_no: val('b2Tracking'), carrier: val('b2Carrier') || null };
    } else if (a.kind === 'reassign') {
      path = '/reassign'; body = { technical_user_id: num('b2Tech'), signer_user_id: num('b2Signer') };
    } else if (a.kind === 'revise') {
      path = '/revise';
      const bankIds = [...document.querySelectorAll('[data-b2-bank]:checked')].map(x => Number(x.dataset.b2Bank));
      if (bankIds.length > Number(state.boot.max_banks || 3)) { notify(`Proformaya en fazla ${Number(state.boot.max_banks || 3)} banka basılabilir.`, true); return; }
      const keepOrder = (o.bank_profile_ids || []).filter(id => bankIds.includes(id));
      body = { commercial_revision: o.commercial_revision, payment_terms: val('b2Terms'),
        advance_percent: val('b2Advance') ? num('b2Advance') : null,
        bank_profile_ids: keepOrder.concat(bankIds.filter(id => !keepOrder.includes(id))),
        terms: { transportation: val('b2TTransport') || null, shipment: val('b2TShipment') || null,
          delivery_type: val('b2TDelivery') || null, loading_days: val('b2TDays') === '' ? null : num('b2TDays') },
        label_language: val('b2Lang'), target_date: val('b2Target') || null, shipping_amount: num('b2Shipping'),
        tax_percentage: num('b2Tax'),
        lines: [...document.querySelectorAll('[data-b2-rev]')].map(r => ({ item_id: Number(r.dataset.b2Rev),
          quantity: Number(r.querySelector('[data-b2-qty]').value), unit_price: Number(r.querySelector('[data-b2-price]').value) })) };
    }
    state.busy = true; $('b2ActionSubmit').disabled = true;
    try {
      const detail = await request(base + path, body);
      bootstrap.Modal.getInstance($('b2ActionModal'))?.hide();
      state.detail = detail; renderDetail(); notify('Kaydedildi.');
      reload().catch(() => {});
    } catch (e) {
      const extra = e.data && (e.data.errors || e.data.problems);
      const refreshed = !!(e.data && e.data.shortages_refreshed);
      notify(e.message + (Array.isArray(extra) && extra.length > 1 ? ` (+${extra.length - 1} sorun)` : '')
        + (refreshed ? ' Eksikler alım satırlarına işlendi.' : ''), true);
      if (refreshed) { bootstrap.Modal.getInstance($('b2ActionModal'))?.hide(); openOrder(o.id); }
    } finally { state.busy = false; $('b2ActionSubmit').disabled = false; }
  }

  // ─── Banka hesapları ──────────────────────────────────────────────────────
  function renderBanks() {
    if (!$('b2BanksHost') || !state.boot || !state.boot.banks) return;
    const banks = state.boot.banks;
    $('b2BanksHost').innerHTML = `<div class="os-tools" style="margin-bottom:.6rem"><button type="button" class="os-b os-b-main" data-b2-bank-edit="new"><i class="bi bi-plus-lg"></i>Banka hesabı ekle</button></div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Hesap</th><th>Banka / şube</th><th>SWIFT</th><th>USD IBAN</th><th>EUR IBAN</th><th>TRY IBAN</th><th>RUB IBAN</th><th>Durum</th><th></th></tr></thead><tbody>${banks.map(b => `<tr><td><strong>${esc(b.label)}</strong></td><td>${esc(b.bank_name)}<div class="os-muted">${esc(b.branch)}</div></td><td class="os-code">${esc(b.swift)}</td><td class="os-code">${esc(b.iban_usd || '—')}</td><td class="os-code">${esc(b.iban_eur || '—')}</td><td class="os-code">${esc(b.iban_try || '—')}</td><td class="os-code">${esc(b.iban_rub || '—')}</td><td>${b.is_active ? 'Aktif' : 'Pasif'}${b.is_default ? '<div class="os-muted">varsayılan</div>' : ''}</td><td><button type="button" class="os-b os-b-sm" data-b2-bank-edit="${Number(b.id)}">Düzenle</button></td></tr>`).join('')}</tbody></table></div>`;
    const sel = $('b2RuleBank');
    if (sel) sel.innerHTML = banks.filter(b => b.is_active).map(b => `<option value="${Number(b.id)}">${esc(b.label)}</option>`).join('');
    const name = id => (banks.find(b => b.id === id) || {}).label || '—';
    $('b2RulesHost').innerHTML = (state.boot.bank_rules || []).length ? `<table class="os-table"><thead><tr><th>Ülke</th><th>Para birimi</th><th>Banka</th><th></th></tr></thead><tbody>${state.boot.bank_rules.map(r => `<tr><td>${esc(r.country)}</td><td>${esc(r.currency)}</td><td>${esc(name(r.bank_profile_id))}</td><td><button type="button" class="os-b os-b-sm" data-b2-rule-del="${Number(r.id)}">Sil</button></td></tr>`).join('')}</tbody></table>` : '<div class="os-empty">Kural yok — siparişe dönüştürürken banka elle seçilir.</div>';
  }
  async function refreshBoot() { state.boot = await request('/bootstrap'); renderBanks(); }

  function openBankForm(id) {
    const b = id === 'new' ? null : (state.boot.banks || []).find(x => x.id === Number(id));
    if (id !== 'new' && !b) return;
    const v = k => (b ? b[k] : '');
    state.action = { kind: 'bank', id: b ? b.id : null, orderId: null };
    $('b2ActionTitle').textContent = b ? `Banka hesabı — ${b.label}` : 'Yeni banka hesabı';
    $('b2ActionSubmit').textContent = 'Kaydet';
    $('b2ActionFields').innerHTML = `<div class="os-notice">IBAN değişiklikleri eski → yeni değeriyle denetim kaydına yazılır. Proformaya en az bir IBAN'ı olan aktif hesap basılabilir; RUB IBAN satırı yalnız seçilen bankada varsa çıkar.</div><div class="os-grid">${field('b2BkLabel', 'Kısa ad (seçim listesinde)', 'text', 'maxlength="80" required', v('label'))}${field('b2BkName', 'Banka adı (proformada)', 'text', 'maxlength="150" required', v('bank_name'))}${field('b2BkBranch', 'Şube', 'text', 'maxlength="150"', v('branch'))}${field('b2BkSwift', 'SWIFT', 'text', 'maxlength="20"', v('swift'))}${field('b2BkHolder', 'Hesap sahibi', 'text', 'maxlength="200" required', b ? b.account_holder : (state.boot.account_holder || ''))}${field('b2BkOrder', 'Sıra', 'number', 'step="1"', b ? b.sort_order : '')}${field('b2BkUsd', 'USD IBAN', 'text', 'maxlength="40"', v('iban_usd'))}${field('b2BkEur', 'EUR IBAN', 'text', 'maxlength="40"', v('iban_eur'))}${field('b2BkTry', 'TRY IBAN', 'text', 'maxlength="40"', v('iban_try'))}${field('b2BkRub', 'RUB IBAN', 'text', 'maxlength="40"', v('iban_rub'))}</div><label class="os-check" style="margin-top:1rem"><input type="checkbox" id="b2BkActive" ${!b || b.is_active ? 'checked' : ''} /><span>Aktif</span></label><label class="os-check"><input type="checkbox" id="b2BkDefault" ${b && b.is_default ? 'checked' : ''} /><span>Varsayılan — ülke kuralı yoksa proformada önceden seçili gelir</span></label>`;
    bootstrap.Modal.getOrCreateInstance($('b2ActionModal')).show();
  }
  async function submitBank() {
    const a = state.action;
    const val = id => ($(id) ? $(id).value.trim() : '');
    const body = { label: val('b2BkLabel'), bank_name: val('b2BkName'), branch: val('b2BkBranch') || null,
      swift: val('b2BkSwift') || null, account_holder: val('b2BkHolder'), iban_usd: val('b2BkUsd') || null,
      iban_eur: val('b2BkEur') || null, iban_try: val('b2BkTry') || null, iban_rub: val('b2BkRub') || null,
      is_active: $('b2BkActive').checked, is_default: $('b2BkDefault').checked,
      sort_order: val('b2BkOrder') === '' ? null : Number(val('b2BkOrder')) };
    if (!body.label || !body.bank_name || !body.account_holder) { notify('Kısa ad, banka adı ve hesap sahibi gerekli.', true); return; }
    if (!(body.iban_usd || body.iban_eur || body.iban_try || body.iban_rub)) { notify('En az bir IBAN girin.', true); return; }
    state.busy = true; $('b2ActionSubmit').disabled = true;
    try {
      await request(a.id ? `/banks/${Number(a.id)}` : '/banks', body, a.id ? 'PUT' : 'POST');
      bootstrap.Modal.getInstance($('b2ActionModal'))?.hide();
      await refreshBoot(); notify('Banka hesabı kaydedildi.');
    } catch (e) { notify(e.message, true); }
    finally { state.busy = false; $('b2ActionSubmit').disabled = false; }
  }

  // ─── Olaylar ──────────────────────────────────────────────────────────────
  document.querySelectorAll('[data-b2-tab]').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('[data-b2-tab]').forEach(x => x.setAttribute('aria-selected', String(x === b)));
    $('b2OrdersPanel').hidden = b.dataset.b2Tab !== 'orders';
    if ($('b2BanksPanel')) $('b2BanksPanel').hidden = b.dataset.b2Tab !== 'banks';
  }));
  $('b2Filter').addEventListener('change', () => reload().catch(e => pageError(e.message)));
  $('b2Refresh').addEventListener('click', () => reload().catch(e => pageError(e.message)));
  $('b2ListHost').addEventListener('click', e => { const b = e.target.closest('[data-b2-open]'); if (b) openOrder(Number(b.dataset.b2Open)); });
  $('b2Detail').addEventListener('click', e => { const b = e.target.closest('[data-b2-action]'); if (b) openAction(b.dataset.b2Action, b.dataset.b2Id); });
  $('b2ActionForm').addEventListener('submit', submitAction);
  $('b2ActionModal').addEventListener('hidden.bs.modal', () => { state.action = null; $('b2ActionFields').replaceChildren(); });
  $('b2RuleForm')?.addEventListener('submit', async e => {
    e.preventDefault();
    try { await request('/bank-rules', { country: $('b2RuleCountry').value.trim(), currency: $('b2RuleCurrency').value, bank_profile_id: Number($('b2RuleBank').value) }); await refreshBoot(); notify('Kural kaydedildi.'); }
    catch (err) { notify(err.message, true); }
  });
  $('b2BanksHost')?.addEventListener('click', e => {
    const b = e.target.closest('[data-b2-bank-edit]'); if (b && !state.busy) openBankForm(b.dataset.b2BankEdit);
  });
  $('b2RulesHost')?.addEventListener('click', async e => {
    const b = e.target.closest('[data-b2-rule-del]'); if (!b) return;
    try { await request(`/bank-rules/${Number(b.dataset.b2RuleDel)}`, undefined, 'DELETE'); await refreshBoot(); notify('Kural silindi.'); }
    catch (err) { notify(err.message, true); }
  });
  $('b2Logout').addEventListener('click', async () => {
    try { const r = await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); if (r.ok) location.href = '/login'; }
    catch { notify('Çıkış yapılamadı.', true); }
  });
  window.addEventListener('hashchange', () => { const id = Number(location.hash.slice(1)); if (posId(id) && id !== state.selectedId) openOrder(id); });
  reload().then(() => { const id = Number(location.hash.slice(1)); if (posId(id)) openOrder(id); }).catch(e => pageError(e.message));
})();
