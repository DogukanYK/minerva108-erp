/* Minerva 108 — Trendyol (yalnız okuma + Liste Doktoru / Sorular).
   Ürün adları, açıklamalar, müşteri soruları ve yapay zekâ metinleri dış
   kaynaklı olduğu için DOM createElement + textContent ile kurulur; innerHTML
   ataması YOK.  Yetki kararı sunucuda: düğmeler yalnız TY_CFG izin verdiğinde
   çizilir, uçlar ayrıca denetler.  Yapay zekâ işleri arka planda koşar
   (uç 202 döner); sayfa `job` alanını yoklar. */
(function () {
  'use strict';

  const API = '/api/trendyol';
  const CFG = window.TY_CFG || {};
  const $ = id => document.getElementById(id);
  const TABS = ['listing', 'questions', 'orders', 'catalog', 'connection'];
  const state = { tab: 'listing', page: 0, size: 50, pollTimer: null, statusLabels: {},
                  map: { barcode: null, comps: [], manual: false, title: '' }, searchTimer: null,
                  listing: { rows: [], timer: null, open: null, detailTimer: null, estimate: null },
                  questions: { timer: null } };

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
  const usd = v => v == null ? '—' : '$' + Number(v).toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fold = t => String(t || '').toLocaleLowerCase('tr-TR');
  // Dış bağlantı yalnız Trendyol alan adına (ürün / soru sayfası).
  const safeTyUrl = u => typeof u === 'string' && /^https:\/\/([a-z0-9-]+\.)*trendyol\.com(\/|$)/i.test(u) ? u : null;
  function extLink(url, text) {
    const u = safeTyUrl(url);
    return u ? h('a', { href: u, target: '_blank', rel: 'noopener noreferrer', class: 'os-b os-b-sm' }, h('i', { class: 'bi bi-box-arrow-up-right' }), text) : null;
  }
  async function copyText(text, flagged) {
    if (!text) return;
    if (flagged && !window.confirm('Bu metin otomatik denetimden geçmedi (bulgular öneri kutusunda). Kontrol ederek kullanın — yine de kopyalansın mı?')) return;
    try {
      await navigator.clipboard.writeText(text);
      toast('Kopyalandı.');
    } catch (e) {
      const ta = h('textarea', { style: 'position:fixed;top:-1000px' });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); toast('Kopyalandı.'); } catch (e2) { toast('Kopyalanamadı — metni elle seçin.', 'error'); }
      ta.remove();
    }
  }
  function copyBtn(getText, flagged, label) {
    return h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => copyText(typeof getText === 'function' ? getText() : getText, flagged) },
      h('i', { class: 'bi bi-clipboard' }), label || 'Kopyala');
  }
  function spinner(text) {
    return h('span', { class: 'ty-progress' }, h('span', { class: 'spinner-border spinner-border-sm', role: 'status', 'aria-hidden': 'true' }), h('span', null, text));
  }
  const BUYBOX_KIND = { ours: 'good', shared: 'warn', lost: 'bad' };
  function buyboxBadge(bb) {
    if (!bb) return '—';
    const tip = [bb.buybox_price != null ? 'Buybox fiyatı ' + money(bb.buybox_price) : null, bb.our_price != null ? 'bizim ' + money(bb.our_price) : null,
      bb.second_price != null ? '2. ' + money(bb.second_price) : null, bb.third_price != null ? '3. ' + money(bb.third_price) : null,
      bb.checked_at ? 'kontrol ' + bb.checked_at : null].filter(Boolean).join(' · ');
    return badge(bb.label, BUYBOX_KIND[bb.state], tip || null);
  }

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
    $('tyListingPanel').hidden = name !== 'listing';
    $('tyQuestionsPanel').hidden = name !== 'questions';
    $('tyOrdersPanel').hidden = name !== 'orders';
    $('tyCatalogPanel').hidden = name !== 'catalog';
    $('tyConnPanel').hidden = name !== 'connection';
    try { localStorage.setItem('ty.tab', name); } catch (e) { /* yoksay */ }
    if (name === 'listing') loadListing();
    else if (name === 'questions') loadQuestions();
    else if (name === 'orders') loadOrders();
    else if (name === 'catalog') loadCatalog();
    else loadStatus();
  }

  // ─── Liste Doktoru ────────────────────────────────────────────────────────
  function scoreColor(v) { return v >= 80 ? 'var(--os-good)' : (v >= 60 ? 'var(--os-warn)' : 'var(--os-bad)'); }
  function scoreCell(v) {
    return h('div', { class: 'ty-score', title: v + ' / 100' }, h('strong', { style: 'color:' + scoreColor(v) }, String(v)),
      h('div', { class: 'ty-score-bar' }, h('span', { style: 'width:' + Math.max(0, Math.min(100, v)) + '%;background:' + scoreColor(v) })));
  }
  async function loadListing() {
    clearTimeout(state.listing.timer); state.listing.timer = null;
    try {
      const d = await request('/listing');
      state.listing.data = d;
      renderListing(d);
      const busy = (d.batch && d.batch.running) || (d.rows || []).some(r => r.job && r.job.state === 'running');
      if (busy && state.tab === 'listing') state.listing.timer = setTimeout(loadListing, 4000);
    } catch (err) { fill($('tyListingHost'), empty(err.message)); }
  }
  function suggestionCell(r) {
    if (r.job && r.job.state === 'running') return spinner('Hazırlanıyor…');
    const out = [];
    if (r.job && r.job.state === 'error') out.push(badge('Hata', 'bad', r.job.detail));
    if (r.suggestion_at) {
      out.push(h('div', null, r.suggestion_at));
      if (r.suggestion_flagged) out.push(badge('Denetimden geçmedi', 'bad'));
      if (r.suggestion_stale) out.push(badge('İçerik/not değişti', 'warn'));
    } else if (!out.length) out.push(h('span', { class: 'os-muted' }, 'yok'));
    return out;
  }
  function renderListing(d) {
    const s = d.summary || {}, ai = d.ai || {};
    fill($('tyListingStats'), [
      [num(s.products), 'Trendyol ürünü'], [s.avg_score == null ? '—' : num(s.avg_score), 'Ortalama puan'],
      [num(s.low), (s.low_threshold || 70) + ' altı puan'], [num(s.buybox_lost), 'Buybox kaybı'],
      [num(s.notes_missing), 'Bilgi notu eksik'],
      [usd(ai.trendyol_month_usd), 'Bu ay Trendyol yapay zekâ'],
      [ai.month_remaining_usd == null ? '—' : usd(ai.month_remaining_usd), 'Kalan aylık tavan (asistanla ortak)'],
    ].map(x => h('div', { class: 'os-stat' }, h('strong', null, x[0]), h('span', null, x[1]))));
    const manage = !!d.can_manage;
    fill($('tyAiNotice'), manage && ai.configured === false ? h('div', { class: 'os-notice os-notice-danger' },
      h('strong', null, 'Yapay zekâ önerileri kapalı: '), 'sunucunun .env dosyasında ANTHROPIC_API_KEY yok. Kural puanı ve buybox yine çalışır.') : null);
    $('tyBatchToggle').hidden = !(manage && ai.configured);
    renderBatch(d.batch || {});
    const q = fold($('tyListQ').value.trim());
    const rows = (d.rows || []).filter(r => !q || [r.title, r.barcode, r.brand, r.category].some(t => fold(t).includes(q)));
    const trs = rows.map(r => h('tr', null,
      td([h('div', { style: 'font-weight:600' }, r.title || '—'), h('div', { class: 'os-muted' }, [r.brand, r.barcode, r.match].filter(Boolean).join(' · '))], 'ty-wide'),
      td(scoreCell(r.score)),
      td(r.fixes && r.fixes.length ? h('ol', { class: 'ty-fixes' }, r.fixes.map(f => h('li', null, f.text + ' (+' + num(f.gain, 0) + ')'))) : badge('Tamam', 'good'), 'ty-wide'),
      td(buyboxBadge(r.buybox)),
      td(r.has_note ? badge('Var', 'good') : badge('Yok', 'warn')),
      td(suggestionCell(r)),
      td(h('div', { class: 'ty-actions' },
        h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => openListing(r.barcode) }, 'Detay ', h('i', { class: 'bi bi-arrow-right' })),
        manage && ai.configured && !(r.job && r.job.state === 'running') && !(r.suggestion_at && !r.suggestion_stale)
          ? h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => suggest(r.barcode, false) }, h('i', { class: 'bi bi-stars' }), 'Öneri üret') : null))));
    fill($('tyListingHost'), trs.length ? table(['Ürün', 'Puan', 'İlk düzeltmeler', 'Buybox', 'Bilgi notu', 'Son öneri', ''], trs)
      : empty((d.rows || []).length ? 'Aramayla eşleşen ürün yok.' : 'Katalog henüz çekilmedi ya da içerik gelmedi. "Bağlantı / Durum" sekmesinden senkronu başlatın.'));
  }
  function renderBatch(b) {
    const box = $('tyBatchProgress');
    if (b.running) {
      box.hidden = false;
      fill(box, [spinner('Toplu öneri üretiliyor: ' + num(b.done) + ' / ' + num(b.total)), h('div', { class: 'os-muted', style: 'margin-top:.3rem' },
        'Üretilen ' + num(b.generated) + ' · önbellekten ' + num(b.cached) + ' · hata ' + num(b.failed) + (b.actor ? ' · başlatan ' + b.actor : ''))]);
      $('tyBatchStart').disabled = true;
      return;
    }
    $('tyBatchStart').disabled = false;
    if (b.finished_at) {
      box.hidden = false;
      fill(box, [h('strong', null, 'Son toplu üretim (' + b.finished_at + '): '),
        num(b.done) + ' / ' + num(b.total) + ' ürün · üretilen ' + num(b.generated) + ' · önbellekten ' + num(b.cached) + ' · hata ' + num(b.failed),
        b.stopped ? h('div', { class: 'os-badge os-badge-warn', style: 'margin-top:.4rem;white-space:normal' }, 'Durdu: ' + b.stopped) : null,
        (b.errors || []).length ? h('ul', { class: 'ty-fixes', style: 'margin-top:.4rem' }, b.errors.map(e => h('li', null, e.barcode + ': ' + e.detail))) : null]);
    } else box.hidden = true;
  }
  async function batchEstimate() {
    const limit = Math.max(1, Math.min(500, parseInt($('tyBatchLimit').value, 10) || 10));
    try {
      const e = await request('/listing/estimate?limit=' + limit);
      state.listing.estimate = e;
      let t;
      if (!e.count) t = 'Güncel önerisi olmayan ürün kalmadı.';
      else {
        t = e.count + ' ürün · ürün başına ~' + usd(e.per_item_usd) + ' · toplam ~' + usd(e.total_usd) + ' (' + (e.model_label || 'model') + ').';
        if (e.room_usd != null) t += ' Kalan limitiniz ' + usd(e.room_usd) + (e.fits ? '.' : ' — tamamı sığmayabilir; limit dolunca üretim kaldığı yerde durur.');
      }
      $('tyBatchEstimateText').textContent = t;
      $('tyBatchStart').hidden = !e.count || e.running;
    } catch (err) { $('tyBatchEstimateText').textContent = err.message; $('tyBatchStart').hidden = true; }
  }
  async function batchStart() {
    const e = state.listing.estimate;
    if (!e || !e.count) return;
    if (!window.confirm(e.count + ' ürün için yapay zekâ önerisi üretilecek (tahmini ~' + usd(e.total_usd) + '). Devam edilsin mi?')) return;
    $('tyBatchStart').disabled = true;
    try {
      const d = await request('/listing/suggest-batch', { method: 'POST', body: { limit: e.count } });
      toast(d.message || 'Toplu üretim başladı.', 'info');
      $('tyBatchBox').hidden = true;
      $('tyBatchStart').hidden = true;
      $('tyBatchEstimateText').textContent = '';
      loadListing();
    } catch (err) { toast(err.message, 'error'); $('tyBatchStart').disabled = false; }
  }
  async function suggest(barcode, force) {
    try {
      await request('/listing/' + encodeURIComponent(barcode) + '/suggest', { method: 'POST', body: { force: !!force } });
      toast('Öneri hazırlanıyor — 1-2 dakika sürebilir; bu sayfada kendiliğinden görünür.', 'info');
    } catch (err) { toast(err.message, 'error'); }
    if (state.listing.open === barcode) loadListingDetail(barcode);
    if (state.tab === 'listing') loadListing();
  }
  function openListing(barcode) {
    state.listing.open = barcode;
    $('tyListTitle').textContent = 'Liste Doktoru';
    fill($('tyListBody'), empty('Yükleniyor…'));
    const m = modal('tyListModal'); if (m) m.show();
    loadListingDetail(barcode);
  }
  async function loadListingDetail(barcode) {
    clearTimeout(state.listing.detailTimer); state.listing.detailTimer = null;
    try {
      const d = await request('/listing/' + encodeURIComponent(barcode));
      if (state.listing.open !== barcode) return;
      const wasRunning = state.listing.lastJob === 'running' && state.listing.lastJobBc === barcode;
      state.listing.lastJob = d.job ? d.job.state : null;
      state.listing.lastJobBc = barcode;
      $('tyListTitle').textContent = d.title || barcode;
      fill($('tyListBody'), renderListingDetail(d));
      if (d.job && d.job.state === 'running') state.listing.detailTimer = setTimeout(() => loadListingDetail(barcode), 3000);
      else if (wasRunning) {
        if (d.job && d.job.state === 'error') toast(d.job.detail || 'Öneri üretilemedi.', 'error');
        else toast('Öneri hazır.');
        if (state.tab === 'listing') loadListing();
      }
    } catch (err) { if (state.listing.open === barcode) fill($('tyListBody'), empty(err.message)); }
  }
  function sparkline(history) {
    if (!history || history.length < 2) return h('p', { class: 'os-muted' }, history && history.length ? 'Puan: ' + history[0].score + ' (' + history[0].day + ')' : 'Henüz puan geçmişi yok — her katalog senkronunda günlük puan yazılır.');
    const W = 600, H = 60, n = history.length;
    const pts = history.map((x, i) => (i * (W - 8) / (n - 1) + 4).toFixed(1) + ',' + (H - 4 - (x.score / 100) * (H - 8)).toFixed(1)).join(' ');
    const NS = 'http://www.w3.org/2000/svg';
    const svgEl = document.createElementNS(NS, 'svg');
    svgEl.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svgEl.setAttribute('preserveAspectRatio', 'none');
    svgEl.setAttribute('class', 'ty-spark');
    svgEl.setAttribute('role', 'img');
    svgEl.setAttribute('aria-label', 'Puan geçmişi');
    const line = document.createElementNS(NS, 'polyline');
    line.setAttribute('points', pts);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', 'currentColor');
    line.setAttribute('stroke-width', '2');
    svgEl.appendChild(line);
    const first = history[0], last = history[n - 1];
    return h('div', { style: 'color:var(--os-head)' }, svgEl,
      h('div', { class: 'os-muted' }, first.day + ': ' + first.score + ' → ' + last.day + ': ' + last.score));
  }
  function field(label, value, extra) {
    return h('div', { class: 'ty-field' }, h('div', { class: 'ty-field-head' }, h('span', null, label), extra || null), value);
  }
  function renderSuggestion(d, manage, aiOn) {
    const sug = d.suggestion, job = d.job;
    const box = h('div', { class: 'ty-box' });
    const head = h('h3', null, h('i', { class: 'bi bi-stars', style: 'color:var(--gold)' }), 'Yapay zekâ önerisi');
    box.appendChild(head);
    const running = job && job.state === 'running';
    if (manage && aiOn) {
      head.appendChild(h('span', { style: 'margin-left:auto' }, running ? spinner('Hazırlanıyor…')
        : h('button', { type: 'button', class: 'os-b os-b-sm os-b-main', onclick: () => suggest(d.barcode, !!sug) }, h('i', { class: 'bi bi-stars' }), sug ? 'Yeniden üret' : 'Öneri üret')));
    }
    if (job && job.state === 'error') box.appendChild(h('div', { class: 'os-notice os-notice-danger' }, job.detail || 'Öneri üretilemedi.'));
    if (!sug) {
      box.appendChild(h('p', { class: 'os-muted' }, running ? 'Öneri hazırlanıyor; bu pencere kendiliğinden yenilenir.'
        : 'Henüz öneri yok. Önce bilgi notunu doldurmak öneriyi belirgin biçimde iyileştirir.'));
      return box;
    }
    const pl = sug.payload || {}, flagged = !!sug.flagged;
    box.appendChild(h('div', { class: 'os-muted', style: 'margin-bottom:.6rem' },
      [sug.created_at, sug.created_by, sug.model, sug.cost_usd != null ? usd(sug.cost_usd) : null].filter(Boolean).join(' · '), ' ',
      flagged ? badge('Denetimden geçmedi', 'bad') : badge('Denetimden geçti', 'good'), ' ',
      sug.current === false ? badge('Eski içerik/not için üretildi', 'warn') : null));
    if ((sug.flags || []).length) box.appendChild(h('div', { class: flagged ? 'os-notice os-notice-danger' : 'os-notice' },
      h('strong', null, flagged ? 'Denetim bulguları — kopyalamadan önce düzeltin:' : 'Denetim notları:'),
      h('ul', { class: 'ty-fixes', style: 'margin-top:.3rem' }, sug.flags.map(f => h('li', null, f)))));
    const kw = pl.keywords || {};
    const KW = [['primary', 'Birincil anahtar kelimeler'], ['secondary', 'İkincil'], ['long_tail', 'Uzun kuyruk']];
    const USE = { baslik: 'başlık', aciklama: 'açıklama', ozellik: 'özellik' };
    KW.forEach(([k, label]) => {
      const items = kw[k] || [];
      if (!items.length) return;
      box.appendChild(field(label, h('div', { class: 'ty-chips' }, items.map(x => h('span', { class: 'ty-chip' }, x.term, h('small', null, (x.use_in || []).map(u => USE[u] || u).join(', '))))),
        copyBtn(items.map(x => x.term).join(', '), flagged)));
    });
    if (pl.title) box.appendChild(field('Önerilen ürün adı (' + pl.title.length + ' karakter)',
      [h('div', { class: 'ty-text', style: 'max-height:none' }, pl.title), pl.title_reason ? h('div', { class: 'os-muted', style: 'margin-top:.3rem' }, pl.title_reason) : null],
      copyBtn(pl.title, flagged)));
    if (pl.description) box.appendChild(field('Önerilen açıklama (' + pl.description.length + ' karakter)', h('div', { class: 'ty-text' }, pl.description), copyBtn(pl.description, flagged)));
    const attrs = pl.attributes || [];
    if (attrs.length) box.appendChild(field('Eksik özellikler için öneri', h('div', { class: 'os-table-wrap' }, table(['Özellik', 'Değer', 'Durum', 'Gerekçe', ''],
      attrs.map(a => h('tr', null, td(a.name), td(a.value || '—'), td(a.manual_check ? badge('Elle kontrol', 'warn') : badge('Listeden', 'good')),
        td(h('span', { class: 'os-muted' }, a.reason || '—')), td(a.value ? copyBtn(a.value, flagged, '') : null)))))));
    if ((pl.warnings || []).length) box.appendChild(field('Uyarılar', h('ul', { class: 'ty-fixes' }, pl.warnings.map(w => h('li', null, w)))));
    return box;
  }
  function renderListingDetail(d) {
    const manage = !!CFG.can_manage, aiOn = !(state.listing.data && state.listing.data.ai && state.listing.data.ai.configured === false);
    const parts = [];
    parts.push(h('div', { class: 'ty-kv' },
      kv('Puan', h('div', null, scoreCell(d.score))), kv('Barkod', h('span', { class: 'os-code' }, d.barcode)), kv('Buybox', buyboxBadge(d.buybox)),
      kv('IMS kartı', d.match ? d.match.label : '—'), kv('Görsel', d.image_count == null ? '—' : num(d.image_count)),
      kv('Teslimat süresi', d.delivery_duration == null ? '—' : d.delivery_duration + ' gün'),
      kv('Satış fiyatı', money(d.sale_price)), kv('Kategori', d.category || '—')));
    const link = extLink(d.product_url, "Trendyol'da aç");
    if (link) parts.push(h('div', { style: 'margin:-.4rem 0 1rem' }, link));
    // Sol: mevcut liste · Sağ: öneri
    const cur = h('div', { class: 'ty-box' }, h('h3', null, h('i', { class: 'bi bi-card-text' }), 'Mevcut liste'),
      field('Ürün adı (' + (d.title || '').length + ' karakter)', h('div', { class: 'ty-text', style: 'max-height:none' }, d.title || '—')),
      field('Açıklama (' + (d.description_text || '').length + ' karakter)', h('div', { class: 'ty-text' }, d.description_text || '— (boş)')),
      field('Özellikler', (d.attributes || []).length ? h('ul', { class: 'ty-fixes' }, d.attributes.map(a => h('li', null, (a.name || a.id) + ': ' + ((a.values || []).join(', ') || '—')))) : h('p', { class: 'os-muted' }, '—')),
      field('Boş kategori özellikleri', (d.missing_attributes || []).length ? h('div', { class: 'ty-chips' }, d.missing_attributes.map(a => h('span', { class: 'ty-chip' }, a.name || a.attribute_id,
        a.required ? h('small', null, 'zorunlu') : null, a.slicer ? h('small', null, 'süzgeç') : null))) : h('p', { class: 'os-muted' }, 'Yok')));
    parts.push(h('div', { class: 'ty-cols' }, cur, renderSuggestion(d, manage, aiOn)));
    // Kural dökümü
    const rows = [];
    (d.breakdown || []).forEach(sec => {
      rows.push(h('tr', null, h('td', { colspan: '4', style: 'background:var(--os-soft);font-weight:600' }, sec.label + ' — ' + num(sec.points, 1) + ' / ' + sec.max)));
      sec.checks.forEach(c => rows.push(h('tr', null,
        td(h('i', { class: 'bi ' + (c.ok ? 'bi-check-circle-fill ty-check-ok' : 'bi-x-circle-fill ty-check-no'), 'aria-label': c.ok ? 'tamam' : 'eksik' })),
        td(c.label), td(num(c.points, 1) + ' / ' + c.max, 'num'),
        td([c.detail ? h('div', null, c.detail) : null, !c.ok && c.fix ? h('div', { class: 'os-muted' }, '→ ' + c.fix) : null]))));
    });
    parts.push(h('div', { class: 'ty-box' }, h('h3', null, h('i', { class: 'bi bi-list-check' }), 'Kural dökümü'), h('div', { class: 'os-table-wrap' }, table(['', 'Kontrol', { t: 'Puan', num: true }, 'Açıklama'], rows))));
    parts.push(h('div', { class: 'ty-box' }, h('h3', null, h('i', { class: 'bi bi-graph-up' }), 'Puan geçmişi (60 gün)'), sparkline(d.history)));
    // Bilgi notu
    const note = d.note || {};
    const noteBox = h('div', { class: 'ty-box' }, h('h3', null, h('i', { class: 'bi bi-journal-text' }), 'Bilgi notu'),
      h('p', { class: 'os-muted' }, 'Yapay zekâya giden tek ekip girdisi: öne çıkan içerikler, cilt/saç tipi, kullanım şekli, hacim, belgesi olan test/sertifika. Reçete, oran ve maliyet YAZMAYIN.'));
    if (manage) {
      const ta = h('textarea', { class: 'os-input ty-note', maxlength: '2000', 'aria-label': 'Bilgi notu' });
      ta.value = note.text || '';
      const counter = h('span', { class: 'os-muted' }, (ta.value.length) + ' / 2000');
      ta.addEventListener('input', () => { counter.textContent = ta.value.length + ' / 2000'; });
      noteBox.appendChild(ta);
      noteBox.appendChild(h('div', { class: 'ty-actions', style: 'margin-top:.5rem' },
        h('button', { type: 'button', class: 'os-b os-b-main os-b-sm', onclick: () => saveNote(d.barcode, ta.value) }, h('i', { class: 'bi bi-check2' }), 'Notu kaydet'),
        counter, note.updated_at ? h('span', { class: 'os-muted' }, 'Son güncelleme: ' + note.updated_at + (note.updated_by ? ' · ' + note.updated_by : '')) : null));
    } else noteBox.appendChild(h('div', { class: 'ty-text' }, note.text || '— (girilmemiş)'));
    parts.push(noteBox);
    return parts;
  }
  async function saveNote(barcode, text) {
    try {
      await request('/listing/' + encodeURIComponent(barcode) + '/note', { method: 'PUT', body: { note: text } });
      toast('Bilgi notu kaydedildi. Yeni not bir sonraki öneride kullanılır.');
      loadListingDetail(barcode);
      if (state.tab === 'listing') loadListing();
    } catch (err) { toast(err.message, 'error'); }
  }

  // ─── Müşteri soruları ─────────────────────────────────────────────────────
  async function loadQuestions() {
    clearTimeout(state.questions.timer); state.questions.timer = null;
    const st = $('tyQStatus').value;
    try {
      const d = await request('/questions' + (st ? '?status=' + encodeURIComponent(st) : ''));
      renderQuestions(d);
      if ((d.questions || []).some(q => q.job && q.job.state === 'running') && state.tab === 'questions') {
        state.questions.timer = setTimeout(loadQuestions, 3000);
      }
    } catch (err) { fill($('tyQuestionsHost'), empty(err.message)); }
  }
  function renderQuestions(d) {
    const c = d.counts || {}, labels = d.status_labels || {};
    fill($('tyQuestionStats'), Object.keys(labels).filter(k => c[k]).map(k => h('div', { class: 'os-stat' }, h('strong', null, num(c[k])), h('span', null, labels[k]))));
    const manage = !!d.can_manage;
    const qs = d.questions || [];
    fill($('tyQuestionsHost'), qs.length ? qs.map(q => {
      const waiting = q.status === 'WAITING_FOR_ANSWER';
      const running = q.job && q.job.state === 'running';
      const head = h('div', { class: 'ty-q-head' },
        badge(q.status_label || q.status || '—', waiting ? 'warn' : (q.status === 'ANSWERED' ? 'good' : null)),
        waiting && q.waiting_hours != null ? badge(num(q.waiting_hours, 1) + ' saattir bekliyor', q.waiting_hours >= 24 ? 'bad' : 'warn') : null,
        h('span', null, q.asked_at || ''), h('strong', { style: 'color:var(--os-ink)' }, q.product_name || q.barcode || '—'));
      const body = [head, h('p', { class: 'ty-q-text' }, q.text || '—')];
      if (q.answer_text) body.push(field('Verilen cevap' + (q.answered_at ? ' (' + q.answered_at + ')' : ''), h('div', { class: 'ty-text' }, q.answer_text)));
      if (q.rejected_reason) body.push(h('div', { class: 'os-notice os-notice-danger' }, 'Red nedeni: ' + q.rejected_reason));
      if (q.job && q.job.state === 'error') body.push(h('div', { class: 'os-notice os-notice-danger' }, q.job.detail || 'Taslak üretilemedi.'));
      if (q.draft_text) {
        const flagged = !!q.draft_flagged;
        body.push(field('Taslak (' + q.draft_text.length + ' karakter' + (q.draft_at ? ' · ' + q.draft_at : '') + (q.draft_by ? ' · ' + q.draft_by : '') + ')',
          [h('div', { class: 'ty-text' }, q.draft_text),
            flagged ? h('div', { class: 'os-notice os-notice-danger', style: 'margin-top:.4rem' }, h('strong', null, 'Denetimden geçmedi: '), (q.draft_flags || []).join(' ')) : null,
            (q.draft_warnings || []).length ? h('ul', { class: 'ty-fixes', style: 'margin-top:.4rem' }, q.draft_warnings.map(w => h('li', null, w))) : null],
          copyBtn(q.draft_text, flagged)));
      }
      const acts = [];
      if (manage && waiting) acts.push(running ? spinner('Taslak hazırlanıyor…')
        : h('button', { type: 'button', class: 'os-b os-b-sm os-b-main', onclick: () => draftAnswer(q.question_id) }, h('i', { class: 'bi bi-stars' }), q.draft_text ? 'Yeniden yaz' : 'Taslak yaz'));
      const link = extLink(q.web_url, "Trendyol'da aç");
      if (link) acts.push(link);
      if (acts.length) body.push(h('div', { class: 'ty-actions' }, acts));
      return h('div', { class: 'ty-q' }, body);
    }) : empty('Bu süzgeçte soru yok.'));
  }
  async function draftAnswer(id) {
    try {
      await request('/questions/' + encodeURIComponent(id) + '/draft', { method: 'POST' });
      toast('Taslak hazırlanıyor…', 'info');
    } catch (err) { toast(err.message, 'error'); }
    loadQuestions();
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
      [s.ambiguous || 0, 'Belirsiz barkod'], [s.invalid_map || 0, 'Geçersiz eşleme'], [s.over || 0, 'Trendyol stoğu IMS\'ten fazla'], [s.buybox_lost || 0, 'Buybox kaybı']]
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
        td(buyboxBadge(r.buybox)),
        td(flags.length ? flags : '—'),
        manage ? td(h('button', { type: 'button', class: 'os-b os-b-sm', onclick: () => openMap(r.barcode, r.title, r.resolution) }, h('i', { class: 'bi bi-link-45deg' }), 'Eşle')) : null);
    });
    const headers = ['Trendyol ürünü', 'Barkod', { t: 'Satış fiyatı', num: true }, { t: 'Trendyol stok', num: true }, 'IMS kartı', 'Panel',
      { t: 'IMS stok', num: true }, { t: 'Fark', num: true }, 'Buybox', 'Durum'].concat(manage ? [''] : []);
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
      kv('Kargo faturası kalemi', num(c.cargo_items)), kv('Katalog ürünü', num(c.products)), kv('Elle eşlenen barkod', num(c.mappings)),
      kv('Cevap bekleyen soru', num(c.questions_waiting))));
    setQuestionBadge(c.questions_waiting);
    renderProgress(d.progress || {}, d.running);
  }
  function setQuestionBadge(n) {
    $('tyTabQuestions').textContent = n ? 'Sorular (' + n + ')' : 'Sorular';
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
    $('tyListRefresh').addEventListener('click', loadListing);
    $('tyListQ').addEventListener('input', () => { if (state.listing.data) renderListing(state.listing.data); });
    $('tyBatchToggle').addEventListener('click', () => { $('tyBatchBox').hidden = !$('tyBatchBox').hidden; $('tyBatchStart').hidden = true; $('tyBatchEstimateText').textContent = ''; });
    $('tyBatchEstimate').addEventListener('click', batchEstimate);
    $('tyBatchStart').addEventListener('click', batchStart);
    $('tyQRefresh').addEventListener('click', loadQuestions);
    $('tyQStatus').addEventListener('change', loadQuestions);
    $('tyListModal').addEventListener('hidden.bs.modal', () => { state.listing.open = null; clearTimeout(state.listing.detailTimer); state.listing.detailTimer = null; });
    $('tyLogout').addEventListener('click', async () => {
      try { await fetch('/api/logout', { method: 'POST', headers: { 'X-Requested-With': '1' } }); } catch (e) { /* yoksay */ }
      window.location.href = '/login';
    });

    let tab = 'listing';
    try { tab = localStorage.getItem('ty.tab') || 'listing'; } catch (e) { /* yoksay */ }
    if (!TABS.includes(tab)) tab = 'listing';
    if (window.location.hash === '#sorular') tab = 'questions';      // soru bildirimi bağlantısı
    showTab(tab);
    // Bağlantı kurulmamışsa ya da henüz hiç veri çekilmemişse kullanıcıyı
    // yapılacak işin olduğu sekmeye götür.
    request('/status').then(d => {
      const c = d.counts || {};
      const noData = !c.packages && !c.products;
      if ((!d.configured || noData) && state.tab !== 'connection') showTab('connection');
      if (d.running) schedulePoll();
      setQuestionBadge(c.questions_waiting);
    }).catch(() => {});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
