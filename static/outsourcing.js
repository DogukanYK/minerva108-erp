/* Minerva 108 — internal coded outsourcing workflow. Public previews use only external_packet. */
(function () {
  'use strict';

  const API = '/api/outsourcing';
  const state = { bootstrap: null, jobs: [], detail: null, selectedId: null, detailEpoch: 0,
    previewEpoch: 0, preview: null, previewBody: null, previewTimer: null, busy: false, action: null, approval: null };
  const STATUS = { DRAFT: 'Hazırlanıyor', PREPARED: 'Onay bekliyor', PENDING: 'Onay bekliyor', TECH_APPROVED: 'Teknik onay tamamlandı',
    APPROVED: 'Gönderime hazır', DISPATCHED: 'Fason üretimde', IN_PROGRESS: 'Fason üretimde',
    PARTIALLY_DISPATCHED: 'Etaplı gönderiliyor', RECONCILING: 'Tüketim / geri kabul', RECEIVED: 'Kalite kontrol bekliyor',
    QC_PENDING: 'Kalite kontrol bekliyor', CLOSED: 'Tamamlandı', CANCELLED: 'İptal edildi' };
  const ROLE_LABEL = { technical: 'Teknik onay', owner: 'İş sahibi onayı', manager: 'Yönetici onayı' };
  const $ = id => document.getElementById(id);
  const can = action => !!window.can('outsourcing', action);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = value => Number(value || 0).toLocaleString('tr-TR', { maximumFractionDigits: 6 });
  const positiveId = value => Number.isSafeInteger(Number(value)) && Number(value) > 0;
  const reference = job => job.reference || `FS-${Number(job.id)}`;
  const statusText = value => STATUS[String(value || '').toUpperCase()] || 'İşlem sürüyor';
  const allowed = (action, permission) => can(permission) && !!(state.detail && state.detail.actions && state.detail.actions[action]);
  const uid = () => window.crypto && typeof window.crypto.randomUUID === 'function'
    ? window.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;

  function errorText(value) {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map(v => typeof v === 'string' ? v : (v.msg || 'Alanları kontrol edin.')).join(' ');
    return 'İşlem tamamlanamadı. Bilgileri kontrol edip yeniden deneyin.';
  }
  function notify(message, bad = false) {
    const node = document.createElement('div');
    node.className = 'os-notice' + (bad ? ' os-notice-danger' : '');
    node.setAttribute('role', bad ? 'alert' : 'status');
    node.textContent = message;
    $('toast-container').appendChild(node);
    window.setTimeout(() => node.remove(), 6000);
  }
  function pageError(message) {
    $('osAlert').replaceChildren();
    if (!message) return;
    const node = document.createElement('div');
    node.className = 'os-notice os-notice-danger'; node.textContent = message;
    $('osAlert').appendChild(node);
  }
  async function request(path, body) {
    const response = await fetch(API + path, { method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin', cache: 'no-store', headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorText(data.detail));
    return data;
  }
  function options(select, rows, label, empty = 'Seçin', selected) {
    if (!select) return;
    const value = selected == null ? select.value : String(selected);
    select.innerHTML = `<option value="">${esc(empty)}</option>` + rows.map(row =>
      `<option value="${Number(row.id)}">${esc(label(row))}</option>`).join('');
    if (rows.some(row => String(row.id) === value)) select.value = value;
  }
  function badge(status) {
    const s = String(status || '').toUpperCase();
    const kind = s === 'CLOSED' || s === 'APPROVED' ? 'good' : s === 'CANCELLED' ? 'bad' : 'warn';
    return `<span class="os-badge os-badge-${kind}">${esc(statusText(s))}</span>`;
  }
  function tab(name) {
    if (name === 'partners' && !can('manage') || name === 'mappings' && !can('mapping')) return;
    document.querySelectorAll('[data-os-tab]').forEach(button => {
      button.setAttribute('aria-selected', String(button.dataset.osTab === name));
    });
    ['jobs', 'partners', 'mappings'].forEach(key => {
      const node = $('os' + key[0].toUpperCase() + key.slice(1) + 'Panel');
      if (node) node.hidden = key !== name;
    });
  }
  function applyPermissions(permissions) {
    if (permissions && typeof permissions === 'object') {
      const scoped = permissions.outsourcing || permissions;
      if (typeof scoped.view === 'boolean') window.PERMS.outsourcing = scoped;
    }
    const fresh = can('view');
    if ($('osNewJob')) $('osNewJob').hidden = !fresh || !can('manage');
    document.querySelector('[data-os-tab="partners"]')?.toggleAttribute('hidden', !can('manage'));
    document.querySelector('[data-os-tab="mappings"]')?.toggleAttribute('hidden', !can('mapping'));
    if (!fresh) throw new Error('Fason üretim erişiminiz değişti. Sayfayı yenileyin.');
  }

  function renderSetup() {
    const b = state.bootstrap;
    if (!b) return;
    const partners = b.partners || [];
    options($('osJobPartner'), partners, row => row.name, 'Üretici seçin');
    options($('osMappingPartner'), partners, row => row.name, 'Üretici seçin');
    options($('osJobRecipe'), b.recipes || [], row => row.name, 'Reçete seçin');
    ['osTechnicalApprover', 'osOwnerApprover', 'osManagerApprover'].forEach(id =>
      options($(id), b.approvers || [], row => row.full_name || row.username, 'Onaylayacak kişiyi seçin'));
    if (can('mapping')) {
      options($('osMappingItem'), b.items || [], row => `${row.name} · ${row.unit || ''}`, 'Malzeme seçin');
      renderMappings();
    } else if ($('osMappingsHost')) $('osMappingsHost').replaceChildren();
    if ($('osPartnersHost') && can('manage')) {
      $('osPartnersHost').innerHTML = partners.length ? `<table class="os-table"><thead><tr><th>Firma</th><th>İlgili kişi</th></tr></thead><tbody>${partners.map(p => `<tr><td><strong>${esc(p.name)}</strong></td><td>${esc(p.contact || '—')}</td></tr>`).join('')}</tbody></table>`
        : '<div class="os-empty">Henüz fason üretici yok. İlk firmanızı yukarıdan ekleyin.</div>';
    }
  }
  function renderMappings() {
    if (!$('osMappingsHost') || !can('mapping')) return;
    const selectedPartner = Number($('osMappingPartner').value);
    const rows = (state.bootstrap?.material_codes || []).filter(m => !selectedPartner || Number(m.partner_id) === selectedPartner);
    $('osMappingsHost').innerHTML = rows.length ? `<table class="os-table"><thead><tr><th>Üretici</th><th>Kod</th><th>İç malzeme kartı</th><th>Doğrulanan tanım</th></tr></thead><tbody>${rows.map(m => {
      const item = (state.bootstrap.items || []).find(i => Number(i.id) === Number(m.item_id));
      const partner = (state.bootstrap.partners || []).find(p => Number(p.id) === Number(m.partner_id));
      return `<tr><td>${esc(partner?.name || m.partner_name || '—')}</td><td class="os-code">${esc(m.code)}</td><td>${esc(item?.name || m.item_name || '—')}</td><td>${esc(m.specification)}</td></tr>`;
    }).join('')}</tbody></table>` : '<div class="os-empty">Bu üretici için doğrulanmış malzeme kodu yok.</div>';
  }
  function renderJobs() {
    const query = $('osSearch').value.trim().toLocaleLowerCase('tr-TR');
    const jobs = state.jobs.filter(j => [reference(j), j.partner_name, j.external_product_name].join(' ').toLocaleLowerCase('tr-TR').includes(query));
    const open = state.jobs.filter(j => !['CLOSED', 'CANCELLED'].includes(String(j.status).toUpperCase()));
    const counts = [open.length, open.filter(j => ['DRAFT', 'PREPARED', 'PENDING', 'TECH_APPROVED', 'APPROVED'].includes(String(j.status).toUpperCase())).length,
      open.filter(j => ['DISPATCHED', 'PARTIALLY_DISPATCHED', 'IN_PROGRESS', 'RECONCILING'].includes(String(j.status).toUpperCase())).length,
      state.jobs.filter(j => String(j.status).toUpperCase() === 'CLOSED').length];
    $('osStats').innerHTML = ['Açık iş', 'Hazırlık / onay', 'Fason üretimde', 'Tamamlanan iş'].map((label, i) =>
      `<div class="os-stat"><strong>${counts[i]}</strong><span>${label}</span></div>`).join('');
    $('osJobsHost').innerHTML = jobs.length ? `<table class="os-table"><thead><tr><th>İş</th><th>Ürün / üretici</th><th class="num">Miktar</th><th>Sürüm</th><th>Durum</th><th></th></tr></thead><tbody>${jobs.map(j => `<tr><td class="os-code">${esc(reference(j))}</td><td><strong>${esc(j.external_product_name || 'Kodlu ürün')}</strong><div class="os-muted">${esc(j.partner_name || '—')}</div></td><td class="num">${fmt(j.quantity)} ${esc(j.unit || 'adet')}</td><td>${Number(j.revision || 1)}</td><td>${badge(j.status)}</td><td><button type="button" class="os-b os-b-sm" data-os-open="${Number(j.id)}">İşi aç <i class="bi bi-arrow-right"></i></button></td></tr>`).join('')}</tbody></table>`
      : `<div class="os-empty"><i class="bi bi-box-arrow-up-right"></i>${query ? 'Aramanızla eşleşen iş yok.' : 'Henüz fason işi yok. Firma ve malzeme kodlarını hazırlayıp ilk işinizi oluşturun.'}</div>`;
  }
  async function reload() {
    const results = await Promise.allSettled([request('/bootstrap'), request('/jobs')]);
    if (results[0].status === 'fulfilled') {
      state.bootstrap = results[0].value; applyPermissions(state.bootstrap.permissions); renderSetup();
    }
    if (results[1].status === 'fulfilled') { state.jobs = results[1].value.jobs || []; renderJobs(); }
    const failure = results.find(r => r.status === 'rejected');
    if (failure) throw failure.reason;
    pageError('');
  }
  function acceptDetail(detail) {
    if (!detail || !positiveId(detail.job?.id)) throw new Error('İş bilgileri alınamadı. Sayfayı yenileyin.');
    state.detail = detail; state.selectedId = Number(detail.job.id);
    const index = state.jobs.findIndex(j => Number(j.id) === state.selectedId);
    if (index >= 0) state.jobs[index] = detail.job; else state.jobs.unshift(detail.job);
    renderJobs(); renderDetail();
  }
  async function openJob(id) {
    if (!positiveId(id) || state.busy) return;
    const epoch = ++state.detailEpoch; state.selectedId = Number(id); state.detail = null;
    $('osJobDetail').hidden = false; $('osJobDetail').innerHTML = '<div class="os-card"><div class="os-empty">İş yükleniyor…</div></div>';
    try {
      const detail = await request(`/jobs/${Number(id)}`);
      if (epoch !== state.detailEpoch || state.selectedId !== Number(id)) return;
      acceptDetail(detail);
      $('osJobDetail').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) { if (epoch === state.detailEpoch) { $('osJobDetail').innerHTML = ''; notify(error.message, true); } }
  }

  // Construct this preview from the public DTO only. Never fall back to internal materials or notes.
  function codedPreview(packet) {
    if (!packet) return '<div class="os-empty">Kodlu paket henüz hazırlanmadı.</div>';
    const rows = (packet.materials || []).map(m => `<tr><td class="os-code">${esc(m.code)}</td><td>${esc(m.phase || (m.parts || []).map(p => p.phase).filter(Boolean).join(', ') || '—')}</td><td class="num">${fmt(m.quantity)} ${esc(m.unit)}</td></tr>`).join('');
    const containers = (packet.containers || []).map(c => `<tr><td class="os-code">${esc(c.container_uid)}</td><td class="os-code">${esc(c.code)}</td><td class="num">${fmt(c.quantity)} ${esc(c.unit)}</td><td>${esc(c.expiry_date || '—')}</td></tr>`).join('');
    return `<div class="os-preview"><h3>${esc(packet.product_name || 'Kodlu ürün')} · ${esc(packet.job_reference || '')}</h3><p class="os-muted">Sürüm ${Number(packet.revision || 1)} · ${fmt(packet.quantity)} ${esc(packet.unit || 'adet')} · ${esc(packet.label_language || '')}</p><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Malzeme kodu</th><th>Faz</th><th class="num">Tartım hedefi</th></tr></thead><tbody>${rows || '<tr><td colspan="3">Malzeme yok.</td></tr>'}</tbody></table></div><h4 class="os-section-title">Kodlu yapılış ve güvenlik talimatı</h4><p class="os-pre">${esc(packet.instructions || '—')}</p><h4 class="os-section-title">Kap etiketleri</h4><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Kap</th><th>Kod</th><th class="num">Miktar</th><th>Son kullanma</th></tr></thead><tbody>${containers || '<tr><td colspan="4">Henüz kap hazırlanmadı.</td></tr>'}</tbody></table></div></div>`;
  }
  function renderApprovals(detail) {
    const ids = detail.job.approver_ids || {};
    return Object.entries(ROLE_LABEL).map(([role, label]) => {
      const person = (state.bootstrap?.approvers || []).find(p => Number(p.id) === Number(ids[role]));
      const approval = (detail.approvals || []).find(a => a.role === role);
      return `<div class="os-approval"><div><strong>${label}</strong><span class="os-muted">${esc(person?.full_name || person?.username || 'Atanmadı')}</span>${approval ? `<div class="os-muted">${esc(approval.approved_at || approval.created_at || '')}</div>` : ''}</div><span class="os-badge ${approval ? 'os-badge-good' : 'os-badge-warn'}">${approval ? 'Onaylandı' : 'Bekliyor'}</span></div>`;
    }).join('');
  }
  function renderDetail() {
    const d = state.detail; if (!d) return;
    const j = d.job; const packet = d.external_packet;
    const materialRows = (d.materials || []).map(m => `<tr><td class="os-code">${esc(m.code)}</td>${can('mapping') ? `<td>${esc(m.item_name || '—')}<div class="os-muted">${esc(m.specification || '')}</div></td>` : ''}<td>${esc(m.phase || (m.parts || []).map(p => p.phase).filter(Boolean).join(', ') || '—')}</td><td class="num">${fmt(m.quantity)} ${esc(m.unit)}</td></tr>`).join('');
    const balanceRows = (d.balance || []).map(b => `<tr><td class="os-code">${esc(b.container_uid)}</td><td class="os-code">${esc(b.code)}</td><td class="num">${fmt(b.dispatched)}</td><td class="num">${fmt(b.consumed)}</td><td class="num">${fmt(b.waste)}</td><td class="num">${fmt(b.returned)}</td><td class="num"><strong>${fmt(b.outstanding)} ${esc(b.unit)}</strong></td></tr>`).join('');
    const receiptRows = (d.receipts || []).filter(r => r.kind !== 'return').map(r => `<tr><td>${esc(r.external_lot || '—')}</td><td class="num">${fmt(r.quantity)} ${esc(r.unit || packet?.unit || 'adet')}</td><td class="num">${fmt(r.sample_quantity)}</td><td class="num">${fmt(r.saleable_quantity)}</td><td>${esc(r.status_label || (r.status === 'APPROVED' ? 'Onaylandı' : r.status === 'REJECTED' ? 'Reddedildi' : 'Kalite kontrol bekliyor'))}</td></tr>`).join('');
    const documents = allowed('documents', 'view') ? `<div class="os-downloads">${[['sheet', 'Kodlu üretim föyü'], ['labels', 'Kap etiketleri'], ['manifest', 'Sevk manifestosu'], ['report', 'Tüketim / fire / iade formu']].map(([kind, label]) => `<a class="os-b" href="${API}/jobs/${Number(j.id)}/documents/${kind}" target="_blank" rel="noopener"><i class="bi bi-file-earmark-pdf"></i>${label}</a>`).join('')}</div>` : '<p class="os-muted">Belgeler paket hazır olduğunda indirilebilir.</p>';
    const canPrepareContainers = allowed('edit', 'manage') && can('mapping');
    const stage = ['DRAFT', 'PREPARED', 'PENDING'].includes(String(j.status).toUpperCase()) ? 0 : ['TECH_APPROVED', 'APPROVED'].includes(j.status) ? 1 : j.status === 'CLOSED' ? 4 : (d.receipts || []).length ? 3 : 2;
    $('osJobDetail').hidden = false;
    $('osJobDetail').innerHTML = `<div class="os-detail-head"><div><h2>${esc(reference(j))} · ${esc(j.external_product_name || 'Kodlu ürün')}</h2><span class="os-muted">${esc(j.partner_name || '—')} · Sürüm ${Number(j.revision)}</span></div><div class="os-tools">${badge(j.status)}<button type="button" class="os-b" data-os-action="refresh-detail"><i class="bi bi-arrow-clockwise"></i>Yenile</button></div></div><ol class="os-steps">${['Hazırlık', 'Üç hesap onayı', 'Etaplı gönderim', 'Üretim ve geri kabul', 'Kapanış'].map((label, i) => `<li class="os-step ${i < stage ? 'is-done' : i === stage ? 'is-current' : ''}"><b>${i + 1}</b>${label}</li>`).join('')}</ol><div class="os-layout"><div><div class="os-card"><div class="os-card-head"><i class="bi bi-box-seam"></i><h2>Malzeme ve kap hazırlığı</h2><div class="os-tools">${canPrepareContainers ? '<button type="button" class="os-b" data-os-action="container"><i class="bi bi-plus-lg"></i>Kap hazırla</button>' : ''}</div></div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Kod</th>${can('mapping') ? '<th>İç malzeme / doğrulanan tanım</th>' : ''}<th>Faz</th><th class="num">Tartım hedefi</th></tr></thead><tbody>${materialRows}</tbody></table></div><div class="os-card-body"><div class="os-muted">Tartım hedefi reçeteye göre hesaplanır. Gönderilecek kap miktarları ayrıca takip edilir.</div>${!can('mapping') && allowed('edit', 'manage') ? '<div class="os-notice" style="margin-top:.7rem">Kaynak lot ve kap hazırlığı için malzeme eşleme yetkisi olan bir kullanıcı gerekir.</div>' : ''}${renderContainers(d)}</div></div><div class="os-card"><div class="os-card-head"><i class="bi bi-file-earmark-lock"></i><h2>Üreticiye verilecek kodlu paket</h2></div><div class="os-card-body">${codedPreview(packet)}</div></div><div class="os-card"><div class="os-card-head"><i class="bi bi-arrow-left-right"></i><h2>Tüketim, fire ve fiziksel iade</h2><div class="os-tools">${allowed('record', 'record') ? '<button class="os-b" type="button" data-os-action="consumption">Tüketim / fire kaydet</button><button class="os-b" type="button" data-os-action="returns">Fiziksel iade al</button>' : ''}</div></div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Kap</th><th>Kod</th><th class="num">Gönderilen</th><th class="num">Tüketilen</th><th class="num">Fire</th><th class="num">İade</th><th class="num">Dışarıda kalan</th></tr></thead><tbody>${balanceRows || '<tr><td colspan="7" class="os-muted">Henüz gönderim yapılmadı.</td></tr>'}</tbody></table></div><div class="os-card-body os-muted">Fiziksel iadeler kalite kontrol bekleyen ayrı lotlara alınır. Tüketim kaydı yerel stoktan tekrar düşmez.</div></div><div class="os-card"><div class="os-card-head"><i class="bi bi-patch-check"></i><h2>Bitmiş ürün kabulü</h2><div class="os-tools">${allowed('record', 'record') ? '<button class="os-b" type="button" data-os-action="receipts">Bitmiş ürün al</button>' : ''}</div></div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Üretici lotu</th><th class="num">Üretilen toplam</th><th class="num">Şahit numune</th><th class="num">Müşteriye ayrılabilecek</th><th>Durum</th></tr></thead><tbody>${receiptRows || '<tr><td colspan="5" class="os-muted">Henüz bitmiş ürün kabulü yok.</td></tr>'}</tbody></table></div><div class="os-card-body os-muted">Ürünler kalite kontrol onayına kadar kullanılabilir stoğa eklenmez. Şahit numuneler ayrı tutulur.</div></div></div><aside><div class="os-card"><div class="os-card-head"><i class="bi bi-person-check"></i><h2>Paket onayları</h2></div><div class="os-card-body">${renderApprovals(d)}${allowed('approve', 'approve') ? '<button type="button" class="os-b os-b-main" style="width:100%;margin-top:1rem" data-os-action="approve">Kendi hesabımla onayla</button>' : ''}<p class="os-muted" style="margin:.8rem 0 0">Önce teknik onay, ardından iş sahibi ve yönetici onayı. Üç ayrı hesap gerekir.</p></div></div><div class="os-card"><div class="os-card-head"><i class="bi bi-download"></i><h2>Kodlu belgeler</h2></div><div class="os-card-body">${documents}</div></div><div class="os-card"><div class="os-card-head"><i class="bi bi-truck"></i><h2>Gönderim ve kapanış</h2></div><div class="os-card-body">${allowed('dispatch', 'dispatch') ? '<button class="os-b os-b-main" style="width:100%;margin-bottom:.6rem" type="button" data-os-action="dispatch">Kapları gönder</button>' : ''}${renderShipments(d)}${allowed('close', 'record') ? '<button class="os-b os-b-main" style="width:100%;margin-top:.7rem" type="button" data-os-action="close">İşi tamamla</button>' : ''}${allowed('cancel', 'manage') ? '<button class="os-b os-b-danger" style="width:100%;margin-top:.6rem" type="button" data-os-action="cancel">İşi iptal et</button>' : ''}<p class="os-muted" style="margin:.8rem 0 0">Kapanış için dışarıdaki miktarlar sıfırlanmalı ve bütün kabuller sonuçlanmalıdır.</p></div></div></aside></div>`;
  }
  function renderContainers(d) {
    const rows = d.containers || [];
    if (!rows.length) return '<div class="os-empty">Henüz kap hazırlanmadı.</div>';
    // Gönderilmemiş kap taslaktan kaldırılabilir (paket sürümü artar, onaylar yenilenir).
    const canVoid = allowed('edit', 'manage') && can('mapping');
    return `<div class="os-card-head" style="padding:.8rem 0;border:0"><h3 class="os-section-title" style="margin:0">Hazırlanan kaplar</h3><button class="os-b os-b-sm" type="button" data-os-action="scan"><i class="bi bi-qr-code-scan"></i>Kap tara</button></div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Kap</th><th>Kod</th><th class="num">Hazırlanan</th><th>Durum</th>${canVoid ? '<th></th>' : ''}</tr></thead><tbody>${rows.map(c => `<tr><td class="os-code">${esc(c.container_uid)}</td><td class="os-code">${esc(c.code)}</td><td class="num">${fmt(c.quantity)} ${esc(c.unit)}</td><td>${c.dispatched ? 'Gönderildi' : 'Hazır'}</td>${canVoid ? `<td>${c.dispatched ? '' : `<button type="button" class="os-b os-b-sm" data-os-void="${Number(c.id)}" title="Kabı taslaktan kaldır"><i class="bi bi-x-lg"></i>Kaldır</button>`}</td>` : ''}</tr>`).join('')}</tbody></table></div>`;
  }
  function renderShipments(d) {
    return (d.shipments || []).map((s, i) => `<div class="os-kv"><span>${i + 1}. gönderim</span><span>${esc(s.dispatched_at || s.created_at || '')}</span></div>`).join('') || '<p class="os-muted">Henüz gönderim yok.</p>';
  }

  function previewRequestBody() {
    return { partner_id: Number($('osJobPartner').value), recipe_id: Number($('osJobRecipe').value),
      quantity: Number($('osJobQuantity').value), label_language: $('osJobLanguage').value };
  }
  function schedulePreview() {
    clearTimeout(state.previewTimer); ++state.previewEpoch; state.preview = null; state.previewBody = null;
    $('osPrepareSubmit').disabled = true;
    const body = previewRequestBody();
    if (!positiveId(body.partner_id) || !positiveId(body.recipe_id) || !(body.quantity > 0)) {
      $('osPrepareMaterials').innerHTML = ''; $('osPrepareNotice').hidden = true; return;
    }
    $('osPrepareMaterials').innerHTML = '<span class="os-muted">Malzeme ihtiyacı hesaplanıyor…</span>';
    const epoch = state.previewEpoch;
    state.previewTimer = setTimeout(() => loadPreview(body, epoch), 300);
  }
  async function loadPreview(body, epoch) {
    try {
      const result = await request('/preview', body);
      if (epoch !== state.previewEpoch || JSON.stringify(body) !== JSON.stringify(previewRequestBody())) return;
      state.preview = result; state.previewBody = JSON.parse(JSON.stringify(body));
      const materials = result.materials || [];
      const missing = materials.filter(m => !(m.material_codes || []).length);
      $('osPrepareMaterials').innerHTML = `<h3 class="os-section-title">Üreticiye özel malzeme kodları</h3><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Malzeme</th><th>Faz</th><th class="num">Tartım hedefi</th><th>Onaylanacak gönderim</th><th>Doğrulanan kod</th></tr></thead><tbody>${materials.map(m => `<tr><td>${esc(can('mapping') ? m.item_name : 'Kodlu malzeme')}</td><td>${esc(m.phase || (m.parts || []).map(p => p.phase).filter(Boolean).join(', ') || '—')}</td><td class="num">${fmt(m.quantity)} ${esc(m.unit)}</td><td><input class="os-input" type="number" data-os-dispatch-quantity="${Number(m.item_id)}" min="${Number(m.quantity)}" step="any" value="${Number(m.dispatch_quantity ?? m.quantity)}" required aria-label="Onaylanacak gönderim miktarı" /><span class="os-muted">${esc(m.unit)}</span></td><td><select class="os-select" data-os-material="${Number(m.item_id)}" aria-label="Malzeme kodu" required>${(m.material_codes || []).map(c => `<option value="${Number(c.id)}">${esc(c.code)}${can('mapping') ? ' · ' + esc(c.specification) : ''}</option>`).join('') || '<option value="">Önce kod doğrulanmalı</option>'}</select></td></tr>`).join('')}</tbody></table></div><p class="os-muted" style="margin-top:.5rem">Gönderim fazlası tartım hedefini değiştirmez. Kullanılmayan miktar iş sonunda fiziksel olarak geri alınır.</p>`;
      $('osPrepareNotice').hidden = !missing.length;
      $('osPrepareNotice').textContent = missing.length ? 'Bu üretici için bazı malzeme kodları doğrulanmamış. Malzeme kodları bölümünden tamamlayıp hazırlığı yeniden deneyin.' : '';
      $('osPrepareSubmit').disabled = state.busy || !materials.length || !!missing.length || !can('manage');
    } catch (error) {
      if (epoch !== state.previewEpoch) return;
      $('osPrepareMaterials').innerHTML = ''; $('osPrepareNotice').hidden = false; $('osPrepareNotice').textContent = error.message;
    }
  }
  function newJob() {
    if (!can('manage') || state.busy) return;
    $('osPrepareForm').reset(); renderSetup(); state.preview = null; state.previewBody = null;
    ++state.previewEpoch; clearTimeout(state.previewTimer);
    $('osPrepareMaterials').innerHTML = ''; $('osPrepareNotice').hidden = true; $('osPrepareSubmit').disabled = true;
    bootstrap.Modal.getOrCreateInstance($('osPrepareModal')).show();
  }
  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll('.os-dialog button,.os-dialog input,.os-dialog select,.os-dialog textarea,[data-os-action],[data-os-open],#osNewJob,#osRefresh,#osPartnerForm button,#osMappingForm button').forEach(node => {
      if (busy) { node.dataset.osWasDisabled = node.disabled ? '1' : '0'; node.disabled = true; }
      else if (node.dataset.osWasDisabled != null) { node.disabled = node.dataset.osWasDisabled === '1'; delete node.dataset.osWasDisabled; }
    });
  }
  async function mutate(path, payload, modalId) {
    if (state.busy) return;
    setBusy(true); pageError('');
    try {
      const detail = await request(path, payload);
      if (modalId) bootstrap.Modal.getInstance($(modalId))?.hide();
      if (detail.job) acceptDetail(detail); else await reload();
      notify('İşlem kaydedildi.');
    } catch (error) { notify(error.message, true); }
    finally { setBusy(false); }
  }
  async function prepareJob(event) {
    event.preventDefault();
    if (!can('manage') || state.busy) return;
    const current = previewRequestBody();
    if (!state.preview || JSON.stringify(current) !== JSON.stringify(state.previewBody)) {
      notify('Hazırlık hesabı değişti. Malzeme listesinin yenilenmesini bekleyin.', true); schedulePreview(); return;
    }
    const ids = ['osTechnicalApprover', 'osOwnerApprover', 'osManagerApprover'].map(id => Number($(id).value));
    if (ids.some(id => !positiveId(id)) || new Set(ids).size !== 3) { notify('Üç farklı onaylayıcı seçin.', true); return; }
    const materialCodes = {};
    const dispatchQuantities = {};
    $('osPrepareMaterials').querySelectorAll('[data-os-material]').forEach(select => { materialCodes[select.dataset.osMaterial] = Number(select.value); });
    $('osPrepareMaterials').querySelectorAll('[data-os-dispatch-quantity]').forEach(input => { dispatchQuantities[input.dataset.osDispatchQuantity] = Number(input.value); });
    if ((state.preview.materials || []).some(m => !positiveId(materialCodes[String(m.item_id)]))) { notify('Her malzeme için doğrulanmış bir kod seçin.', true); return; }
    const payload = { ...JSON.parse(JSON.stringify(state.previewBody)), external_product_name: $('osExternalProduct').value.trim(),
      external_notes: 'PROSES:\n' + $('osProcessText').value.trim() + '\n\nGÜVENLİK:\n' + $('osSafetyText').value.trim(),
      approver_ids: { technical: ids[0], owner: ids[1], manager: ids[2] }, material_code_ids: materialCodes, dispatch_quantities: dispatchQuantities };
    await mutate('/jobs', payload, 'osPrepareModal');
  }

  function field(id, label, type = 'text', extra = '') {
    return `<div><label class="os-label" for="${id}">${label}</label><input class="os-input" id="${id}" type="${type}" ${extra} /></div>`;
  }
  function unitOptions(unit) {
    const key = String(unit || '').toLowerCase();
    const choices = ['g', 'gr', 'kg'].includes(key) ? ['g', 'kg'] : ['ml', 'l', 'lt'].includes(key) ? ['ml', 'l'] : [unit || 'adet'];
    return choices.map(u => `<option value="${esc(u)}" ${u === unit ? 'selected' : ''}>${esc(u)}</option>`).join('');
  }
  function openAction(kind) {
    const d = state.detail; if (!d || state.busy) return;
    if (kind === 'refresh-detail') { openJob(d.job.id); return; }
    if (kind === 'scan') { scanContainer(); return; }
    if (kind === 'approve') { openApproval(); return; }
    const permission = { container: 'manage', dispatch: 'dispatch', consumption: 'record', returns: 'record', receipts: 'record', close: 'record', cancel: 'manage' }[kind];
    const action = { container: 'edit', consumption: 'record', returns: 'record', receipts: 'record' }[kind] || kind;
    if (!permission || !allowed(action, permission) || kind === 'container' && !can('mapping')) return;
    state.action = { kind, jobId: Number(d.job.id), revision: d.job.revision, packetHash: d.job.packet_hash, idempotencyKey: uid() };
    const title = { container: 'Kaynak lottan kap hazırla', dispatch: 'Hazırlanan kapları gönder', consumption: 'Fiilî tüketim ve fire kaydı',
      returns: 'Kullanılmayan malzemeyi fiziksel teslim al', receipts: 'Bitmiş ürün kabulü', close: 'İşi tamamla', cancel: 'Fason işini iptal et' }[kind];
    $('osActionTitle').textContent = title;
    let html = '';
    if (kind === 'container') {
      html = `<div class="os-grid"><div class="os-span"><label class="os-label" for="osContainerMaterial">Malzeme kodu</label><select class="os-select" id="osContainerMaterial" required><option value="">Kod seçin</option>${(d.materials || []).map(m => `<option value="${Number(m.code_id)}">${esc(m.code)} · ${esc(m.item_name || '')}</option>`).join('')}</select></div><div class="os-span"><label class="os-label" for="osContainerLot">Kaynak lot</label><select class="os-select" id="osContainerLot"></select><p class="os-muted" id="osContainerHint" style="margin-top:.4rem"></p></div>${field('osContainerQuantity', 'Kaba konulan miktar', 'number', 'min="0.000001" step="any" required')}<div><label class="os-label" for="osContainerUnit">Birim</label><select class="os-select" id="osContainerUnit" required></select></div></div><div class="os-notice" style="margin-top:1rem">Her kap tek kaynak lotuna bağlıdır. Yeni kap hazırlamak paket sürümünü değiştirir; önceki onaylar yenilenir.</div>`;
    } else if (kind === 'dispatch') {
      const sent = new Set((d.balance || []).filter(b => Number(b.dispatched) > 0).map(b => Number(b.container_id)));
      const ready = (d.containers || []).filter(c => !c.dispatched && !sent.has(Number(c.id)));
      html = `<div class="os-notice">Seçtiğiniz kaplar bu etapta gönderilir. Stok ve lotlar gönderim sırasında yeniden kontrol edilir.</div>${ready.map(c => `<label class="os-check" style="padding:.7rem 0;border-bottom:1px solid var(--os-line)"><input type="checkbox" name="dispatch-container" value="${Number(c.id)}" /><span><strong class="os-code">${esc(c.container_uid)} · ${esc(c.code)}</strong><br>${fmt(c.quantity)} ${esc(c.unit)}</span></label>`).join('') || '<div class="os-empty">Gönderilmeyi bekleyen kap yok.</div>'}`;
    } else if (kind === 'consumption' || kind === 'returns') {
      const balances = (d.balance || []).filter(b => Number(b.outstanding) > 0);
      html = `<div class="os-notice">${kind === 'returns' ? 'Yalnız fiziksel olarak geri gelen miktarı kaydedin. Malzeme kalite kontrol bekleyen ayrı bir lota alınır.' : 'Bu kayda ait yeni tüketim ve fire miktarlarını girin. Önceki kayıtları tekrar eklemeyin.'}</div><div class="os-table-wrap"><table class="os-table"><thead><tr><th>Kap / kod</th><th class="num">Kalan</th><th>${kind === 'returns' ? 'Fiziksel iade' : 'Yeni tüketim'}</th>${kind === 'consumption' ? '<th>Yeni fire</th><th>Fire / sapma gerekçesi</th>' : ''}</tr></thead><tbody>${balances.map(b => `<tr data-os-balance="${Number(b.container_id)}" data-os-unit="${esc(b.unit)}"><td><strong class="os-code">${esc(b.container_uid)}</strong><div class="os-muted">${esc(b.code)} · ${esc(b.unit)}</div></td><td class="num">${fmt(b.outstanding)}</td><td><input type="number" class="os-input" data-os-qty min="0" max="${Number(b.outstanding)}" step="any" value="0" aria-label="${kind === 'returns' ? 'İade' : 'Tüketim'} miktarı" /></td>${kind === 'consumption' ? `<td><input type="number" class="os-input" data-os-waste min="0" max="${Number(b.outstanding)}" step="any" value="0" aria-label="Fire miktarı" /></td><td><input class="os-input" data-os-reason maxlength="2000" placeholder="Gerekçeyi yazın" aria-label="Fire ve sapma gerekçesi" /></td>` : ''}</tr>`).join('') || '<tr><td colspan="5">Dışarıda kalan miktar yok.</td></tr>'}</tbody></table></div>`;
    } else if (kind === 'receipts') {
      html = `<div class="os-grid">${field('osReceiptQuantity', 'Üretilen toplam adet / miktar', 'number', 'min="0.000001" step="any" required')}${field('osReceiptSamples', 'Toplamın içindeki şahit numune adedi', 'number', 'min="0" step="1" value="0" required')}${field('osReceiptLot', 'Üreticinin lot / parti numarası', 'text', 'maxlength="100" required')}${field('osReceiptExpiry', 'Son kullanma tarihi', 'date', 'required')}</div><div id="osReceiptAvailable" class="os-notice" style="margin-top:1rem"></div><p class="os-muted">Kabul kalite kontrol bekleyen lot oluşturur. Müşteriye ayrılabilecek miktar, toplamdan şahit numuneler çıkarılarak gösterilir.</p>`;
    } else if (kind === 'close') {
      html = '<div class="os-notice">Bu işin bütün dış bakiyeleri ve kalite kontrol durumları sunucuda doğrulanır.</div><label class="os-check"><input id="osCloseConfirmed" type="checkbox" required /><span>Tüm malzemelerin hesabını ve fiziksel geri kabulleri kontrol ettim; işi tamamlamak istiyorum.</span></label>';
    } else if (kind === 'cancel') {
      html = '<div class="os-notice">İptal, gönderilmiş malzemeleri otomatik olarak yerel stoğa eklemez. Fiziksel iadeler ayrıca kaydedilir.</div><label class="os-label" for="osCancelReason">İptal gerekçesi</label><textarea class="os-textarea" id="osCancelReason" minlength="5" maxlength="2000" required></textarea>';
    }
    $('osActionFields').innerHTML = html;
    $('osActionSubmit').textContent = kind === 'dispatch' ? 'Seçilen kapları gönder' : kind === 'close' ? 'İşi tamamla' : kind === 'cancel' ? 'İşi iptal et' : 'Kaydet';
    $('osActionSubmit').disabled = false;
    if (kind === 'container') { $('osContainerMaterial').addEventListener('change', containerMaterialChanged); containerMaterialChanged(); }
    if (kind === 'receipts') {
      const renderAvailable = () => {
        const total = Number($('osReceiptQuantity').value || 0); const samples = Number($('osReceiptSamples').value || 0);
        $('osReceiptAvailable').textContent = samples > total ? 'Şahit numune miktarı üretilen toplamı aşamaz.' : `Müşteriye ayrılabilecek miktar: ${fmt(total - samples)} ${d.external_packet?.unit || 'adet'} · Kalite kontrol bekleyecek.`;
      };
      $('osReceiptQuantity').addEventListener('input', renderAvailable); $('osReceiptSamples').addEventListener('input', renderAvailable); renderAvailable();
    }
    bootstrap.Modal.getOrCreateInstance($('osActionModal')).show();
  }
  function scanContainer() {
    if (!state.detail || typeof window.openBarcodeScanner !== 'function') { notify('Tarayıcı henüz hazır değil. Yeniden deneyin.', true); return; }
    const jobId = Number(state.detail.job.id);
    window.openBarcodeScanner(scanned => {
      if (!state.detail || Number(state.detail.job.id) !== jobId) return;
      const code = String(scanned || '').trim();
      const container = (state.detail.external_packet?.containers || []).find(c => c.container_uid === code);
      if (!container) { notify('Bu kap, açık işin kodlu paketinde bulunmuyor. İş ve etiketi kontrol edin.', true); return; }
      notify(`${container.container_uid} · ${container.code} · ${fmt(container.quantity)} ${container.unit} — Bu işin paketine ait.`);
    }, { title: 'Kodlu kapı kontrol et', hint: 'Kap etiketindeki QR kodu okutun.' });
  }
  function containerMaterialChanged() {
    const m = (state.detail?.materials || []).find(row => Number(row.code_id) === Number($('osContainerMaterial').value));
    const lotSelect = $('osContainerLot');
    if (!m) { lotSelect.innerHTML = '<option value="">Önce malzeme kodu seçin</option>'; $('osContainerUnit').innerHTML = ''; return; }
    $('osContainerUnit').innerHTML = unitOptions(m.unit);
    const lots = (state.bootstrap?.lots || []).filter(l => Number(l.item_id) === Number(m.item_id));
    options(lotSelect, lots, l => `${l.lot_number || 'Lotsuz'} · ${fmt(l.quantity)} ${l.unit || m.unit} · SKT ${l.expiry_date || 'yok'}`, m.kind === 'raw' ? 'Kaynak lot seçin' : 'Toplam stoktan (lot yok)');
    lotSelect.required = m.kind === 'raw';
    $('osContainerHint').textContent = `Tartım hedefi ${fmt(m.quantity)} ${m.unit}; onaylanacak gönderim ${fmt(m.dispatch_quantity ?? m.quantity)} ${m.unit}. ${m.kind === 'raw' ? 'Gönderim için uygunluğu lot durumu, numune ve son kullanma bilgileriyle kontrol edilir.' : 'Ambalaj ve etiket fireden muaftır.'}`;
  }
  async function submitAction(event) {
    event.preventDefault();
    const context = state.action; const d = state.detail;
    if (!context || !d || state.busy || Number(d.job.id) !== context.jobId) return;
    let payload; const kind = context.kind;
    const base = { idempotency_key: context.idempotencyKey };
    if (kind === 'container') {
      payload = { material_code_id: Number($('osContainerMaterial').value), quantity: Number($('osContainerQuantity').value), unit: $('osContainerUnit').value };
      if ($('osContainerLot').value) payload.inventory_id = Number($('osContainerLot').value);
    } else if (kind === 'dispatch') {
      const ids = [...$('osActionFields').querySelectorAll('[name="dispatch-container"]:checked')].map(input => Number(input.value));
      if (!ids.length) { notify('Göndermek istediğiniz kapları seçin.', true); return; }
      payload = { ...base, revision: context.revision, packet_hash: context.packetHash, container_ids: ids };
    } else if (kind === 'consumption' || kind === 'returns') {
      const entries = [];
      for (const row of $('osActionFields').querySelectorAll('[data-os-balance]')) {
        const quantity = Number(row.querySelector('[data-os-qty]').value || 0);
        const waste = kind === 'consumption' ? Number(row.querySelector('[data-os-waste]').value || 0) : 0;
        if (!(quantity > 0 || waste > 0)) continue;
        const entry = { container_id: Number(row.dataset.osBalance), unit: row.dataset.osUnit };
        if (kind === 'consumption') {
          entry.consumed_quantity = quantity; entry.waste_quantity = waste; entry.reason = row.querySelector('[data-os-reason]').value.trim();
          if (waste > 0 && entry.reason.length < 5) { notify('Fire için en az 5 karakterlik gerekçe yazın.', true); return; }
        } else entry.quantity = quantity;
        entries.push(entry);
      }
      if (!entries.length) { notify('En az bir kap için miktar girin.', true); return; }
      payload = { ...base, entries };
    } else if (kind === 'receipts') {
      const total = Number($('osReceiptQuantity').value); const samples = Number($('osReceiptSamples').value);
      if (samples > total || samples < 0 || !Number.isInteger(samples)) { notify('Şahit numune adedini kontrol edin.', true); return; }
      payload = { ...base, quantity: total, unit: d.external_packet?.unit || 'adet', sample_quantity: samples,
        external_lot: $('osReceiptLot').value.trim(), expiry_date: $('osReceiptExpiry').value || null };
    } else if (kind === 'close') payload = base;
    else if (kind === 'cancel') payload = { ...base, reason: $('osCancelReason').value.trim() };
    if (!payload) return;
    const route = kind === 'container' ? 'containers' : kind;
    await mutate(`/jobs/${context.jobId}/${route}`, payload, 'osActionModal');
  }
  async function voidContainer(containerId) {
    const d = state.detail;
    if (!d || state.busy || !positiveId(containerId) || !allowed('edit', 'manage') || !can('mapping')) return;
    if (!window.confirm('Bu kap taslaktan kaldırılsın mı? Paket sürümü değişir; verilen onaylar yenilenmelidir.')) return;
    await mutate(`/jobs/${Number(d.job.id)}/containers/${containerId}/void`, {});
  }
  function openApproval() {
    if (!allowed('approve', 'approve') || state.busy) return;
    const job = state.detail.job;
    state.approval = { jobId: Number(job.id), revision: job.revision, packetHash: job.packet_hash };
    $('osApprovalForm').reset();
    $('osApprovalSummary').textContent = `${reference(job)} · ${job.external_product_name || 'Kodlu ürün'} · Sürüm ${job.revision}`;
    bootstrap.Modal.getOrCreateInstance($('osApprovalModal')).show();
  }
  async function submitApproval(event) {
    event.preventDefault();
    const context = state.approval;
    if (!context || state.busy || !allowed('approve', 'approve') || Number(state.detail.job.id) !== context.jobId) return;
    await mutate(`/jobs/${context.jobId}/approve`, { revision: context.revision, packet_hash: context.packetHash }, 'osApprovalModal');
  }

  document.querySelectorAll('[data-os-tab]').forEach(button => button.addEventListener('click', () => tab(button.dataset.osTab)));
  $('osSearch').addEventListener('input', renderJobs);
  $('osRefresh').addEventListener('click', () => { if (!state.busy) reload().catch(error => pageError(error.message)); });
  $('osNewJob')?.addEventListener('click', newJob);
  $('osJobsHost').addEventListener('click', event => { const button = event.target.closest('[data-os-open]'); if (button) openJob(Number(button.dataset.osOpen)); });
  $('osJobDetail').addEventListener('click', event => {
    const voidButton = event.target.closest('[data-os-void]');
    if (voidButton) { voidContainer(Number(voidButton.dataset.osVoid)); return; }
    const button = event.target.closest('[data-os-action]'); if (button) openAction(button.dataset.osAction);
  });
  ['osJobPartner', 'osJobRecipe', 'osJobQuantity', 'osJobLanguage'].forEach(id => $(id).addEventListener('input', schedulePreview));
  $('osPrepareForm').addEventListener('submit', prepareJob);
  $('osActionForm').addEventListener('submit', submitAction);
  $('osApprovalForm').addEventListener('submit', submitApproval);
  $('osMappingPartner')?.addEventListener('change', renderMappings);
  $('osPartnerForm')?.addEventListener('submit', event => {
    event.preventDefault(); if (!can('manage')) return;
    mutate('/partners', { name: $('osPartnerName').value.trim(), contact: $('osPartnerContact').value.trim() || null }).then(() => {});
  });
  $('osMappingForm')?.addEventListener('submit', event => {
    event.preventDefault(); if (!can('mapping') || !$('osMappingConfirmed').checked) return;
    const body = { partner_id: Number($('osMappingPartner').value), item_id: Number($('osMappingItem').value), specification: $('osMappingIdentity').value.trim(), safety_instructions: $('osMappingSafety').value.trim() };
    if ($('osMappingCode').value.trim()) body.code = $('osMappingCode').value.trim();
    mutate('/material-codes', body);
  });
  $('osPrepareModal').addEventListener('hidden.bs.modal', () => { ++state.previewEpoch; clearTimeout(state.previewTimer); state.preview = null; state.previewBody = null; });
  $('osActionModal').addEventListener('hidden.bs.modal', () => { state.action = null; });
  $('osApprovalModal').addEventListener('hidden.bs.modal', () => { state.approval = null; $('osApprovalForm').reset(); });
  $('osLogout').addEventListener('click', async () => {
    if (state.busy) return;
    try { const response = await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }); if (response.ok) window.location.href = '/login'; }
    catch { notify('Çıkış yapılamadı. Yeniden deneyin.', true); }
  });
  reload().catch(error => pageError(error.message));
})();
