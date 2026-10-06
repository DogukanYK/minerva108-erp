/* ─────────────────────────────────────────────────────────────────────────────
 * Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
 * Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Satın Alma Planı — /satin-alma sayfasının istemcisi (templates/satin_alma.html).
 *
 * Akış: ürün × adet seç (1) → seçenekler (2) → önizle / PDF / Excel (3).
 * API: /api/purchase-plan/* (routers/purchase_plan.py).  İstek gövdesi
 * `PlanRequest` (core/purchase_plan_models.py) — aynı sözleşme senaryo
 * `config`'i olarak da saklanır.
 *
 * Kurallar:
 *  • Satır = ürün listesindeki bir kayıt (`r:<reçete>` ya da reçetesiz
 *    `i:<ürün>`).  Plan satırı = işaretli VE adedi > 0 olan kayıt.
 *  • Senaryo/taslak açılırken ürün listesine eşlenemeyen satır (reçete
 *    silinmiş/pasif, ürün başka panelde) SESSİZCE DÜŞÜRÜLMEZ: "eksik kayıt"
 *    kutusunda gösterilir, önizlemeye gönderilmez ama kaydederken korunur;
 *    kullanıcı "Kaldır" ile açıkça çıkarır.  Seçeneklerdeki silinmiş kart
 *    (hariç/bekletilen/ek ambalaj) da kırmızı çiple işaretlenir, aynı kural.
 *  • Taslak panel (domain) başına localStorage'da otomatik saklanır.
 *  • Sınıf öneki `sap-`; `btn-*` adı YASAK (Bootstrap gölgeleme tuzağı).
 *  • TR sayı/para biçimi tedarikçi hücresinde sunucudan gelir (`m.ui`);
 *    burada yalnız basit miktarlar biçimlenir.
 */
(function () {
  'use strict';

  var API = '/api/purchase-plan';
  var DOMAIN = window.DOMAIN || 'cosmetics';
  var DRAFT_KEY = 'sap.draft.v1.' + DOMAIN;
  var SYM = { USD: '$', EUR: '€', TRY: '₺' };
  var UNIT_TEXT = { kg: 'kg', l: 'litre', adet: 'adet' };
  var MAT_SECTIONS = ['raw_priced', 'raw_unpriced', 'pkg_priced', 'pkg_unpriced', 'labels'];
  var DEFAULT_OPTIONS = {
    stock_mode: 'net', subtract_open_orders: false, subtract_finished_stock: false,
    extra_waste_pct: { raw: 0, packaging: 0, label: 0 },
    label_mode: 'TR', new_label_title: 'Yeni etiket',
    merge_duplicates: true, safe_rounding: true, round_to_package: false,
    currency: 'USD', lookalike_check: true, checklist_owner: ''
  };

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
  function fmt(n, d) {
    return Number(n || 0).toLocaleString('tr-TR', { maximumFractionDigits: d == null ? 2 : d });
  }
  function priceText(v) {
    return Number(v).toLocaleString('tr-TR', { minimumFractionDigits: v < 1 ? 4 : 2, maximumFractionDigits: v < 1 ? 4 : 2 });
  }
  function toast(msg, type) {
    if (window.showToast) window.showToast(msg, type || 'error', type === 'success' ? 3000 : 5000);
    else window.alert(msg);
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function num(v) {
    if (v === '' || v == null) return null;
    var n = parseFloat(String(v).replace(',', '.'));
    return isFinite(n) ? n : null;
  }
  function debounce(fn, ms) {
    var t = null;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }
  function trSorted(arr, keyFn) {
    if (window.trSort) return window.trSort(arr.slice(), keyFn);
    return arr.slice().sort(function (a, b) { return String(keyFn(a)).localeCompare(String(keyFn(b)), 'tr'); });
  }
  function isoDate(d) {
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }
  async function errText(r, fallback) {
    try {
      var j = await r.json();
      if (typeof j.detail === 'string') return j.detail;
      if (Array.isArray(j.detail) && j.detail.length) {
        return 'Geçersiz giriş: ' + j.detail.slice(0, 3).map(function (d) {
          var loc = (d.loc || []).filter(function (x) { return x !== 'body'; });
          var where = loc.map(function (x) { return typeof x === 'number' ? (x + 1) + '.' : x; }).join(' ');
          return (where ? where + ' — ' : '') + (d.msg || '');
        }).join(' · ');
      }
    } catch (e) { /* JSON değil */ }
    return fallback;
  }
  async function api(method, url, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    return fetch(url, opt);
  }

  // ─── Durum ─────────────────────────────────────────────────────────────────
  var S = {
    products: [], byKey: {}, brands: [], defaults: { excluded_items: [] }, recipeList: [],
    rows: {},              // key → {on, qty, o:{recipe_id, scale, use_pkg, extra[], faces, display_name, note}}
    rowWarn: {},           // key → senaryodan gelen uyarı metni (pasif kart vb.)
    filterQ: '', onlySel: false,
    options: clone(DEFAULT_OPTIONS),
    excluded: null,        // null = varsayılan (AppSetting); dizi = açık liste [{id,name,unit,missing}]
    held: [],              // [{id,name,reason,missing}]
    manual: [],            // [{section,name,qty,qty_text,unit,note}]
    passthrough: { manual_merges: [], item_notes: {} },
    missingItems: {},      // id → true (sunucu: not_found) — önizlemeye gönderilmez
    title: '', notes: '',
    orphans: [],           // [{line, no, text, computable}]
    scenario: null,        // {id, name, can_edit, created_by, updated_at_tr, last_run_at_tr}
    scenarios: [],
    dirty: false,
    report: null,
    items: null, itemsById: {}
  };

  function defRowOpts() {
    return { recipe_id: null, scale: 1, use_pkg: true, extra: [], faces: null, display_name: '', note: '' };
  }
  function hasOpts(o) {
    return !!(o && (o.recipe_id || (o.scale && o.scale !== 1) || o.use_pkg === false ||
      (o.extra && o.extra.length) || o.faces || o.display_name || o.note));
  }
  function rowOf(key) {
    if (!S.rows[key]) S.rows[key] = { on: false, qty: null, o: defRowOpts() };
    if (!S.rows[key].o) S.rows[key].o = defRowOpts();
    return S.rows[key];
  }
  function recipeLabel(id) {
    var r = S.recipeList.find(function (x) { return x.id === id; });
    return r ? r.label : ('Reçete #' + id);
  }
  function itemName(id) {
    var it = S.itemsById[id];
    return it ? it.name : null;
  }

  // ─── Değişiklik / taslak ───────────────────────────────────────────────────
  function saveDraft() {
    try {
      var checked = Object.keys(S.rows).filter(function (k) { return S.rows[k].on && !(S.rows[k].qty > 0); });
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        v: 1, at: Date.now(), dirty: S.dirty, req: buildRequest(true), checked: checked,
        scenario: S.scenario ? { id: S.scenario.id, name: S.scenario.name } : null
      }));
    } catch (e) { /* özel pencere / kota — taslak opsiyonel */ }
  }
  var saveDraftSoon = debounce(saveDraft, 400);
  function loadDraft() {
    try {
      var d = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
      return (d && d.v === 1 && d.req) ? d : null;
    } catch (e) { return null; }
  }
  function touch() {
    S.dirty = true;
    if (S.report) $('sapStale').classList.add('on');
    renderScenarioInfo();
    saveDraftSoon();
  }

  // ─── İstek (PlanRequest) ───────────────────────────────────────────────────
  /** forSave=true: senaryo/taslak — eksik (eşlenemeyen) satır ve kartlar KORUNUR.
   *  forSave=false: önizleme/dışa aktarma — yalnız hesaplanabilenler gider. */
  function buildRequest(forSave) {
    var lines = [];
    S.products.forEach(function (p) {
      var r = S.rows[p.key];
      if (!r || !r.on || !(r.qty > 0)) return;
      var o = r.o || defRowOpts();
      var line = { qty: r.qty };
      var rid = o.recipe_id || p.recipe_id;
      if (rid) line.recipe_id = rid;
      if (p.target_item_id) line.target_item_id = p.target_item_id;
      if (o.scale && o.scale !== 1) line.scale = o.scale;
      if (o.use_pkg === false) line.use_recipe_packaging = false;
      var extra = (o.extra || []).filter(function (e) { return forSave || !e.missing; });
      if (extra.length) {
        line.extra_packaging = extra.map(function (e) {
          var x = { per_unit: e.per_unit || 1, note: e.note || null };
          if (e.item_id) x.item_id = e.item_id; else x.new_name = e.new_name;
          return x;
        });
      }
      if (o.faces) line.label_faces = o.faces;
      if (o.display_name) line.display_name = o.display_name;
      if (o.note) line.note = o.note;
      lines.push(line);
    });
    S.orphans.forEach(function (orph) {
      if (forSave || orph.computable) lines.push(orph.line);
    });
    var keep = function (id) { return forSave || !S.missingItems[id]; };
    var o = S.options;
    var notes = String(S.notes || '').split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
    var itemNotes = {};
    Object.keys(S.passthrough.item_notes || {}).forEach(function (k) {
      if (keep(parseInt(k, 10))) itemNotes[k] = S.passthrough.item_notes[k];
    });
    return {
      title: (S.title || '').trim() || null,
      lines: lines,
      manual_lines: S.manual.filter(function (m) { return (m.name || '').trim(); }).map(function (m) {
        return { section: m.section || 'shipping', name: m.name.trim(), qty: m.qty == null ? null : m.qty,
          qty_text: (m.qty_text || '').trim() || null, unit: (m.unit || '').trim() || 'adet',
          note: (m.note || '').trim() || null };
      }),
      options: {
        stock_mode: o.stock_mode, subtract_open_orders: !!o.subtract_open_orders,
        subtract_finished_stock: !!o.subtract_finished_stock,
        extra_waste_pct: { raw: o.extra_waste_pct.raw || 0, packaging: o.extra_waste_pct.packaging || 0,
          label: o.extra_waste_pct.label || 0 },
        label_mode: o.label_mode, new_label_title: (o.new_label_title || '').trim() || 'Yeni etiket',
        merge_duplicates: !!o.merge_duplicates,
        manual_merges: (S.passthrough.manual_merges || []).filter(function (pr) { return keep(pr[0]) && keep(pr[1]); }),
        excluded_item_ids: S.excluded === null ? null
          : S.excluded.filter(function (x) { return keep(x.id); }).map(function (x) { return x.id; }),
        held_items: S.held.filter(function (h) { return keep(h.id); })
          .map(function (h) { return { item_id: h.id, reason: (h.reason || '').trim() }; }),
        safe_rounding: !!o.safe_rounding, round_to_package: !!o.round_to_package,
        currency: o.currency, lookalike_check: !!o.lookalike_check,
        item_notes: itemNotes, notes: notes,
        checklist_owner: (o.checklist_owner || '').trim() || null
      }
    };
  }

  // ─── Senaryo / taslak → durum ──────────────────────────────────────────────
  function mapLine(ln) {
    var rid = ln.recipe_id == null ? null : ln.recipe_id;
    var tid = ln.target_item_id == null ? null : ln.target_item_id;
    var p = null;
    if (tid != null) {
      var cands = S.products.filter(function (x) { return x.target_item_id === tid; });
      p = cands.find(function (x) { return rid != null && x.recipe_id === rid; })
        || cands.find(function (x) { return x.primary; }) || cands[0] || null;
    } else if (rid != null) {
      p = S.byKey['r:' + rid] || null;
    }
    if (!p) return null;
    var override = null;
    if (rid != null && rid !== p.recipe_id) {
      if (!S.recipeList.some(function (x) { return x.id === rid; })) return null;
      override = rid;
    }
    return {
      key: p.key,
      o: {
        recipe_id: override, scale: ln.scale || 1, use_pkg: ln.use_recipe_packaging !== false,
        extra: (ln.extra_packaging || []).map(function (e) {
          return { item_id: e.item_id || null, new_name: e.item_id ? null : (e.new_name || ''),
            name: e.item_id ? (itemName(e.item_id) || ('Kalem #' + e.item_id)) : (e.new_name || ''),
            per_unit: e.per_unit || 1, note: e.note || '', missing: !!(e.item_id && S.missingItems[e.item_id]) };
        }),
        faces: ln.label_faces || null, display_name: ln.display_name || '', note: ln.note || ''
      }
    };
  }

  /** `missing` = sunucunun eksik kayıt listesi: senaryoda `GET /scenarios/{id}`
   *  → missing[], taslakta `GET /item-refs` (bkz. draftMissing).  Yalnız
   *  'not_found' kart önizlemeden düşer; pasif kart hesaba girer (sunucu uyarır). */
  function applyConfig(cfg, missing) {
    cfg = cfg || {};
    missing = missing || [];
    S.rows = {}; S.rowWarn = {}; S.orphans = []; S.missingItems = {};
    var missName = {};
    missing.forEach(function (m) {
      if (m.kind === 'item') {
        if (m.status === 'not_found') S.missingItems[m.id] = true;
        if (m.name) missName[m.id] = m.name;
      }
    });
    var o = cfg.options || {};
    var nameOf = function (id) { return itemName(id) || missName[id] || ('Kalem #' + id); };
    S.options = clone(DEFAULT_OPTIONS);
    ['stock_mode', 'subtract_open_orders', 'subtract_finished_stock', 'label_mode', 'new_label_title',
      'merge_duplicates', 'safe_rounding', 'round_to_package', 'currency', 'lookalike_check'].forEach(function (k) {
      if (o[k] !== undefined && o[k] !== null) S.options[k] = o[k];
    });
    S.options.checklist_owner = o.checklist_owner || '';
    var w = o.extra_waste_pct || {};
    S.options.extra_waste_pct = { raw: w.raw || 0, packaging: w.packaging || 0, label: w.label || 0 };
    S.excluded = (o.excluded_item_ids == null) ? null : o.excluded_item_ids.map(function (id) {
      var it = S.itemsById[id];
      return { id: id, name: nameOf(id), unit: it ? it.unit : '', missing: !!S.missingItems[id] };
    });
    S.held = (o.held_items || []).map(function (h) {
      return { id: h.item_id, name: nameOf(h.item_id), reason: h.reason || '', missing: !!S.missingItems[h.item_id] };
    });
    S.passthrough = { manual_merges: o.manual_merges || [], item_notes: o.item_notes || {} };
    S.notes = (o.notes || []).join('\n');
    S.title = cfg.title || '';
    S.manual = (cfg.manual_lines || []).map(function (m) {
      return { section: m.section || 'shipping', name: m.name || '', qty: m.qty == null ? null : m.qty,
        qty_text: m.qty_text || '', unit: m.unit || 'adet', note: m.note || '' };
    });
    (cfg.lines || []).forEach(function (ln, i) {
      var no = i + 1;
      var reasons = missing.filter(function (m) { return m.line === no; }).map(function (m) { return m.text; });
      var mp = mapLine(ln);
      if (!mp) {
        S.orphans.push({ line: ln, no: no, computable: false,
          text: reasons.join(' ') || 'Ürün listesinde bulunamadı (reçete/ürün silinmiş, pasif ya da bu panelde değil).' });
        return;
      }
      if (S.rows[mp.key] && S.rows[mp.key].on) {
        S.orphans.push({ line: ln, no: no, computable: true,
          text: 'Aynı ürün planda ikinci kez geçiyor; ayrı satır olarak hesaba katılıyor.' });
        return;
      }
      S.rows[mp.key] = { on: true, qty: ln.qty, o: mp.o };
      if (reasons.length) S.rowWarn[mp.key] = reasons.join(' ');
    });
  }

  /** Yapılandırmanın baktığı bütün kart id'leri (seçenekler + ek ambalaj). */
  function configItemIds(cfg) {
    var o = (cfg && cfg.options) || {};
    var ids = [].concat(o.excluded_item_ids || [], (o.held_items || []).map(function (h) { return h.item_id; }),
      Object.keys(o.item_notes || {}).map(function (k) { return parseInt(k, 10); }),
      [].concat.apply([], o.manual_merges || []));
    ((cfg && cfg.lines) || []).forEach(function (ln) {
      (ln.extra_packaging || []).forEach(function (e) { if (e.item_id) ids.push(e.item_id); });
    });
    return ids.filter(function (id, i) { return id != null && !isNaN(id) && ids.indexOf(id) === i; });
  }

  /** Taslağın eksik kartları SUNUCUDAN (`/item-refs`).  Eskiden istemcide
   *  `/api/items`'a bakılıyordu: o liste pasif kartları içermez (bekletilen
   *  pasif kart "silinmiş" sanılıp alınacaklara karışıyordu) ve önbelleği
   *  panele göre ayrılmaz.  İstek düşerse hiçbir kart işaretlenmez — gerçekten
   *  yoksa önizleme "Bu panelde değil" der, sessizce yanlış hesap çıkmaz. */
  async function draftMissing(cfg) {
    var ids = configItemIds(cfg);
    if (!ids.length) return [];
    try {
      var r = await fetch(API + '/item-refs?ids=' + encodeURIComponent(ids.join(',')));
      if (!r.ok) return [];
      return (await r.json()).missing || [];
    } catch (e) {
      return [];
    }
  }

  function resetPlan() {
    S.rows = {}; S.rowWarn = {}; S.orphans = []; S.missingItems = {};
    S.options = clone(DEFAULT_OPTIONS);
    S.excluded = null; S.held = []; S.manual = [];
    S.passthrough = { manual_merges: [], item_notes: {} };
    S.title = ''; S.notes = '';
    S.report = null;
  }

  // ─── Yükleme ───────────────────────────────────────────────────────────────
  async function loadProducts() {
    var r = await fetch(API + '/products');
    if (!r.ok) throw new Error(await errText(r, 'Ürün listesi alınamadı.'));
    var d = await r.json();
    S.products = d.products || [];
    S.byKey = {};
    S.products.forEach(function (p) {
      p._fold = fold([p.brand, p.name, p.recipe_name || ''].join(' '));
      S.byKey[p.key] = p;
    });
    S.brands = d.brands || [];
    S.defaults = d.defaults || { excluded_items: [] };
    S.recipeList = trSorted(S.products.filter(function (p) { return p.recipe_id; }).map(function (p) {
      var extra = (p.recipe_name && fold(p.recipe_name) !== fold(p.name)) ? ' (reçete: ' + p.recipe_name + ')' : '';
      return { id: p.recipe_id, label: p.brand + ' · ' + p.name + extra, size: p.size_ml };
    }), function (x) { return x.label; });
    $('sapStockAsOf').textContent = d.stock_as_of ? 'Stoklar ' + d.stock_as_of + ' itibarıyla' : '';
  }

  async function loadItems() {
    if (S.items) return S.items;
    try {
      // fetchItems(true): items-cache'in sessionStorage anahtarı panele göre
      // ayrılmaz — Kozmetik/Supplement geçişinden sonraki 30 sn içinde öteki
      // panelin kartlarını verirdi (seçiciler yanlış kart, önizleme 400).
      var list = window.fetchItems ? await window.fetchItems(true)
        : await fetch('/api/items').then(function (r) { if (!r.ok) throw new Error(); return r.json(); });
      S.items = (list || []).map(function (it) {
        it._fold = fold((it.name || '') + ' ' + (it.name_tr || ''));
        return it;
      });
    } catch (e) {
      S.items = [];
    }
    S.itemsById = {};
    S.items.forEach(function (it) { S.itemsById[it.id] = it; });
    return S.items;
  }

  async function loadScenarios() {
    try {
      var r = await fetch(API + '/scenarios');
      if (!r.ok) throw new Error();
      S.scenarios = (await r.json()).scenarios || [];
    } catch (e) {
      S.scenarios = [];
    }
    renderScenarioSelect();
  }

  // ─── Senaryo çubuğu ────────────────────────────────────────────────────────
  function renderScenarioSelect() {
    var sel = $('sapScenario');
    var cur = S.scenario ? String(S.scenario.id) : '';
    sel.innerHTML = '<option value="">— Kaydedilmemiş taslak —</option>' + S.scenarios.map(function (s) {
      return '<option value="' + s.id + '">' + esc(s.name) + (s.created_by ? ' — ' + esc(s.created_by) : '') + '</option>';
    }).join('');
    sel.value = cur;
    if (sel.value !== cur) { S.scenario = null; sel.value = ''; }
    renderScenarioInfo();
  }

  function renderScenarioInfo() {
    var sc = S.scenario;
    var info = $('sapScInfo');
    var parts = [];
    if (sc) {
      var meta = S.scenarios.find(function (x) { return x.id === sc.id; }) || sc;
      if (meta.created_by) parts.push('Kaydeden: ' + meta.created_by);
      if (meta.updated_at_tr) parts.push('son güncelleme ' + meta.updated_at_tr + (meta.updated_by ? ' (' + meta.updated_by + ')' : ''));
      if (meta.last_run_at_tr) parts.push('son çalıştırma ' + meta.last_run_at_tr);
      if (!sc.can_edit) parts.push('yalnız okuma — sahibi, SuperAdmin ya da Manager değiştirebilir; "Farklı kaydet" ile kopyalayın');
    } else {
      parts.push('Taslak bu tarayıcıda otomatik saklanır; kalıcı olması için kaydedin.');
    }
    if (S.dirty && sc) parts.push('● kaydedilmemiş değişiklik var');
    info.textContent = parts.join(' · ');
    $('sapScSave').disabled = !!(sc && !sc.can_edit);
    $('sapScSave').title = (sc && !sc.can_edit) ? 'Bu senaryoyu değiştirme yetkiniz yok' : '';
    $('sapScDelete').disabled = !(sc && sc.can_edit);
  }

  async function openScenario(id) {
    var r = await fetch(API + '/scenarios/' + id);
    if (!r.ok) { toast(await errText(r, 'Senaryo açılamadı.')); renderScenarioSelect(); return; }
    var d = await r.json();
    await loadItems();
    resetPlan();
    if (d.config_error) toast(d.config_error);
    applyConfig(d.config, d.missing);
    S.scenario = { id: d.id, name: d.name, can_edit: d.can_edit };
    S.dirty = false;
    renderAll();
    clearPreview();
    saveDraft();
    if ((d.missing || []).length) toast('Senaryoda silinmiş/pasif kayıtlar var — üstteki uyarı kutusuna bakın.');
    else toast('“' + d.name + '” açıldı.', 'success');
  }

  function askName(title, def) {
    return new Promise(function (resolve) {
      openModal({
        title: title,
        body: '<div><label class="sap-lbl" for="sapNameIn">Senaryo adı</label>' +
          '<input type="text" id="sapNameIn" class="sap-in sap-full" maxlength="150" value="' + esc(def || '') + '"></div>' +
          '<div class="sap-faint">Ad bu panelde tekil olmalı. Senaryo ürünleri, adetleri ve bütün seçenekleri saklar; stok ve fiyatlar her açılışta güncel okunur.</div>',
        okText: 'Kaydet',
        onOk: function () {
          var v = ($('sapNameIn').value || '').trim().replace(/\s+/g, ' ');
          if (!v) { toast('Senaryo adı boş olamaz.'); return false; }
          resolve(v);
          return true;
        },
        onCancel: function () { resolve(null); },
        focus: 'sapNameIn'
      });
    });
  }

  async function saveScenario(asNew) {
    var cfg = buildRequest(true);
    if (!cfg.lines.length) { toast('Kaydetmek için en az bir ürüne adet girin.'); return; }
    if (!asNew && S.scenario && S.scenario.can_edit) {
      var r = await api('PUT', API + '/scenarios/' + S.scenario.id, { config: cfg });
      if (!r.ok) { toast(await errText(r, 'Senaryo kaydedilemedi.')); return; }
      S.dirty = false;
      await loadScenarios();
      saveDraft();
      toast('“' + S.scenario.name + '” kaydedildi.', 'success');
      return;
    }
    var def = S.scenario ? S.scenario.name + ' (kopya)' : (S.title || '');
    var name = await askName(S.scenario ? 'Farklı kaydet' : 'Senaryoyu kaydet', def);
    if (!name) return;
    var r2 = await api('POST', API + '/scenarios', { name: name, config: cfg });
    if (!r2.ok) { toast(await errText(r2, 'Senaryo kaydedilemedi.')); return; }
    var d = await r2.json();
    S.scenario = { id: d.id, name: d.name, can_edit: true };
    S.dirty = false;
    await loadScenarios();
    saveDraft();
    toast('“' + d.name + '” kaydedildi.', 'success');
  }

  async function deleteScenario() {
    var sc = S.scenario;
    if (!sc || !sc.can_edit) return;
    if (!window.confirm('“' + sc.name + '” senaryosu silinsin mi? (Ekrandaki plan taslak olarak kalır.)')) return;
    var r = await api('DELETE', API + '/scenarios/' + sc.id);
    if (!r.ok) { toast(await errText(r, 'Senaryo silinemedi.')); return; }
    S.scenario = null;
    S.dirty = true;
    await loadScenarios();
    saveDraft();
    toast('Senaryo silindi.', 'success');
  }

  // ─── Eksik kayıt kutusu ────────────────────────────────────────────────────
  function renderMissing() {
    var box = $('sapMissing');
    var items = [];
    S.orphans.forEach(function (o, i) {
      var ln = o.line || {};
      items.push('<li><b>' + o.no + '. satır</b> (' + fmt(ln.qty, 2) + ' adet' +
        (ln.display_name ? ', ' + esc(ln.display_name) : '') + '): ' + esc(o.text) +
        ' <button type="button" class="sap-b sap-b-sm" data-orphan="' + i + '">Kaldır</button></li>');
    });
    var opt = [];
    (S.excluded || []).forEach(function (x) { if (x.missing) opt.push('hariç: ' + x.name); });
    S.held.forEach(function (x) { if (x.missing) opt.push('bekletilen: ' + x.name); });
    S.products.forEach(function (p) {
      var r = S.rows[p.key];
      if (r && r.on && r.o && (r.o.extra || []).some(function (e) { return e.missing; })) opt.push('ek ambalaj (' + p.name + ')');
    });
    if (!items.length && !opt.length) { box.innerHTML = ''; return; }
    var nonComp = S.orphans.filter(function (o) { return !o.computable; }).length;
    box.innerHTML = '<div class="sap-box sap-box-warn"><i class="bi bi-exclamation-triangle-fill"></i> ' +
      '<b>Senaryoda artık bulunmayan ya da tekrar eden kayıtlar var.</b> ' +
      (nonComp ? 'Eşlenemeyen satırlar önizlemeye ve çıktılara <b>katılmıyor</b>, ama kaydederken korunuyor; istemiyorsanız “Kaldır”a basın.' : '') +
      (items.length ? '<ul>' + items.join('') + '</ul>' : '') +
      (opt.length ? '<div style="margin-top:0.35rem;">Silinmiş kartlar (hesaba katılmıyor): ' + esc(opt.join(' · ')) + '</div>' : '') +
      '</div>';
  }

  // ─── Ürün tablosu ──────────────────────────────────────────────────────────
  function visibleProducts() {
    var q = fold(S.filterQ.trim());
    return S.products.filter(function (p) {
      if (S.onlySel && !(S.rows[p.key] && S.rows[p.key].on)) return false;
      return !q || p._fold.indexOf(q) !== -1;
    });
  }

  function rowHtml(p) {
    var r = S.rows[p.key] || { on: false, qty: null, o: defRowOpts() };
    var o = r.o || defRowOpts();
    var subs = [];
    if (p.recipe_name && fold(p.recipe_name) !== fold(p.name)) subs.push('reçete: ' + esc(p.recipe_name));
    if (!p.recipeless && !p.primary) subs.push('<span class="sap-pill sap-pill-info">alternatif reçete</span>');
    if (o.recipe_id) subs.push('başka reçete: <b>' + esc(recipeLabel(o.recipe_id)) + '</b>');
    if (o.scale && o.scale !== 1) subs.push('×' + fmt(o.scale, 3) + ' ölçek');
    if (o.use_pkg === false) subs.push('reçete ambalajı kullanılmıyor');
    if (o.extra && o.extra.length) subs.push('+' + o.extra.length + ' ek ambalaj');
    if (o.faces) subs.push(o.faces + ' etiket yüzü');
    if (o.display_name) subs.push('görünen ad: ' + esc(o.display_name));
    if (o.note) subs.push('not: ' + esc(o.note));
    var lm = S.options.label_mode;
    if ((lm === 'TR' || lm === 'EN') && !o.recipe_id && p.label_langs && p.label_langs.length &&
        p.label_langs.indexOf(lm) === -1) {
      subs.push('<span style="color:var(--sap-warn-fg);">' + lm + ' etiketi tanımlı değil</span>');
    }
    if (S.rowWarn[p.key]) subs.push('<span style="color:var(--sap-warn-fg);">' + esc(S.rowWarn[p.key]) + '</span>');
    var badges;
    if (p.recipeless) {
      badges = o.recipe_id ? '<span class="sap-pill sap-pill-info" title="Başka ürünün reçetesiyle tahmin">tahmini</span>'
        : '<span class="sap-pill sap-pill-warn" title="Reçetesi yok — ⚙ ile başka ürünün reçetesini seçebilirsiniz">reçetesiz</span>';
    } else {
      badges = '<span class="sap-pill sap-pill-ok">reçeteli</span>' +
        (!p.has_packaging ? '<span class="sap-pill sap-pill-warn" title="Reçetede ambalaj yok">ambalajsız</span>' : '');
    }
    var stock = p.finished_stock == null ? '—' : fmt(p.finished_stock) + ' ' + esc(p.unit || 'adet');
    var needQty = r.on && !(r.qty > 0);
    var set = hasOpts(o);
    return '<tr data-key="' + esc(p.key) + '" class="' + (r.on ? 'sap-row-on' : '') + '">' +
      '<td><input type="checkbox" class="sap-ck"' + (r.on ? ' checked' : '') + ' aria-label="Seç"></td>' +
      '<td><span class="sap-brand">' + esc(p.brand) + '</span></td>' +
      '<td><div class="sap-pname">' + esc(p.name) + '</div>' + (subs.length ? '<div class="sap-psub">' + subs.join(' · ') + '</div>' : '') + '</td>' +
      '<td class="num">' + stock + '</td>' +
      '<td>' + badges + '</td>' +
      '<td class="num"><input type="number" class="sap-in sap-qty' + (needQty ? ' sap-need' : '') + '" min="0" step="1" value="' +
        (r.qty > 0 ? r.qty : '') + '" aria-label="Adet"></td>' +
      '<td><button type="button" class="sap-gear' + (set ? ' set' : '') + '" title="Satır ayarları"><i class="bi bi-gear' + (set ? '-fill' : '') + '"></i></button></td>' +
      '</tr>';
  }

  function renderTable() {
    var body = $('sapProdBody');
    if (!S.products.length) {
      body.innerHTML = '<tr><td colspan="7" class="sap-empty">Bu panelde reçeteli ya da bitmiş ürün yok.</td></tr>';
    } else {
      var vis = visibleProducts();
      body.innerHTML = vis.length ? vis.map(rowHtml).join('')
        : '<tr><td colspan="7" class="sap-empty">Aramaya uyan ürün yok.</td></tr>';
    }
    renderCounts();
  }

  function renderCounts() {
    var n = 0, units = 0, rl = 0, noQty = 0;
    S.products.forEach(function (p) {
      var r = S.rows[p.key];
      if (!r || !r.on) return;
      if (!(r.qty > 0)) { noQty++; return; }
      n++; units += r.qty;
      if (p.recipeless && !(r.o && r.o.recipe_id)) rl++;
    });
    var extra = S.orphans.filter(function (o) { return o.computable; }).length;
    $('sapProdFoot').innerHTML = '<b>' + n + '</b> ürün · <b>' + fmt(units) + '</b> adet · <b>' + rl + '</b> reçetesiz' +
      (extra ? ' · +' + extra + ' tekrar eden satır' : '') +
      (noQty ? ' · <span style="color:var(--sap-warn-fg);">' + noQty + ' seçili ürünün adedi boş</span>' : '');
    // Marka çipleri: hepsi seçili → on, bir kısmı → part
    document.querySelectorAll('#sapBrands .sap-chip').forEach(function (ch) {
      var ps = S.products.filter(function (p) { return p.brand_key === ch.dataset.brand; });
      var on = ps.filter(function (p) { return S.rows[p.key] && S.rows[p.key].on; }).length;
      ch.classList.toggle('on', on > 0 && on === ps.length);
      ch.classList.toggle('part', on > 0 && on < ps.length);
    });
    var vis = visibleProducts();
    var ckAll = $('sapCkAll');
    var onVis = vis.filter(function (p) { return S.rows[p.key] && S.rows[p.key].on; }).length;
    ckAll.checked = vis.length > 0 && onVis === vis.length;
    ckAll.indeterminate = onVis > 0 && onVis < vis.length;
    renderMissing();
  }

  function renderBrands() {
    $('sapBrands').innerHTML = S.brands.map(function (b) {
      return '<button type="button" class="sap-chip" data-brand="' + esc(b.key) + '" title="Markanın bütün ürünlerini seç / bırak">' +
        esc(b.label) + ' <span class="sap-cnt">' + b.count + '</span></button>';
    }).join('');
    $('sapBulkBrand').innerHTML = S.brands.map(function (b) {
      return '<option value="' + esc(b.key) + '">' + esc(b.label) + '</option>';
    }).join('');
  }

  function setOn(keys, on) {
    keys.forEach(function (k) { rowOf(k).on = on; });
    touch();
  }

  function bindTable() {
    var body = $('sapProdBody');
    body.addEventListener('change', function (e) {
      var tr = e.target.closest('tr[data-key]');
      if (!tr || !e.target.classList.contains('sap-ck')) return;
      var key = tr.dataset.key;
      setOn([key], e.target.checked);
      if (S.onlySel && !e.target.checked) { renderTable(); return; }
      tr.classList.toggle('sap-row-on', e.target.checked);
      var q = tr.querySelector('.sap-qty');
      q.classList.toggle('sap-need', e.target.checked && !(rowOf(key).qty > 0));
      renderCounts();
    });
    body.addEventListener('input', function (e) {
      if (!e.target.classList.contains('sap-qty')) return;
      var tr = e.target.closest('tr[data-key]');
      var r = rowOf(tr.dataset.key);
      var v = num(e.target.value);
      r.qty = (v != null && v > 0) ? v : null;
      if (r.qty && !r.on) {
        r.on = true;
        tr.querySelector('.sap-ck').checked = true;
        tr.classList.add('sap-row-on');
      }
      e.target.classList.toggle('sap-need', r.on && !r.qty);
      touch();
      renderCounts();
    });
    body.addEventListener('click', function (e) {
      var g = e.target.closest('.sap-gear');
      if (!g) return;
      openRowOptions(g.closest('tr[data-key]').dataset.key);
    });
    $('sapCkAll').addEventListener('change', function (e) {
      setOn(visibleProducts().map(function (p) { return p.key; }), e.target.checked);
      renderTable();
    });
    $('sapBrands').addEventListener('click', function (e) {
      var ch = e.target.closest('.sap-chip');
      if (!ch) return;
      var ps = S.products.filter(function (p) { return p.brand_key === ch.dataset.brand; });
      var allOn = ps.every(function (p) { return S.rows[p.key] && S.rows[p.key].on; });
      setOn(ps.map(function (p) { return p.key; }), !allOn);
      renderTable();
    });
    $('sapAllOn').addEventListener('click', function () {
      setOn(visibleProducts().map(function (p) { return p.key; }), true);
      renderTable();
    });
    $('sapAllOff').addEventListener('click', function () {
      setOn(S.products.map(function (p) { return p.key; }), false);
      renderTable();
    });
    var searchSoon = debounce(renderTable, 150);
    $('sapSearch').addEventListener('input', function (e) { S.filterQ = e.target.value || ''; searchSoon(); });
    $('sapOnlySel').addEventListener('change', function (e) { S.onlySel = e.target.checked; renderTable(); });

    function bulk(keys, turnOn) {
      var v = num($('sapBulkQty').value);
      if (!(v > 0)) { toast('Önce toplu adet girin.'); $('sapBulkQty').focus(); return; }
      if (!keys.length) { toast('Uygulanacak ürün yok.'); return; }
      keys.forEach(function (k) { var r = rowOf(k); r.qty = v; if (turnOn) r.on = true; });
      touch();
      renderTable();
      toast(keys.length + ' ürüne ' + fmt(v) + ' adet yazıldı.', 'success');
    }
    $('sapBulkSel').addEventListener('click', function () {
      bulk(S.products.filter(function (p) { return S.rows[p.key] && S.rows[p.key].on; }).map(function (p) { return p.key; }), false);
    });
    $('sapBulkBrandBtn').addEventListener('click', function () {
      var b = $('sapBulkBrand').value;
      bulk(S.products.filter(function (p) { return p.brand_key === b; }).map(function (p) { return p.key; }), true);
    });
    $('sapBulkAll').addEventListener('click', function () {
      bulk(visibleProducts().map(function (p) { return p.key; }), true);
    });

    var today = new Date();
    var yearAgo = new Date(today.getFullYear() - 1, today.getMonth(), today.getDate() + 1);
    $('sapHistStart').value = isoDate(yearAgo);
    $('sapHistEnd').value = isoDate(today);
    $('sapHistBtn').addEventListener('click', historyFill);
  }

  async function historyFill() {
    var s = $('sapHistStart').value, e = $('sapHistEnd').value;
    var qs = new URLSearchParams();
    if (s) qs.set('start', s);
    if (e) qs.set('end', e);
    var btn = $('sapHistBtn');
    btn.disabled = true;
    try {
      var r = await fetch(API + '/history-fill?' + qs.toString());
      if (!r.ok) { toast(await errText(r, 'Geçmiş üretim alınamadı.')); return; }
      var hist = await r.json();
      var onKeys = S.products.filter(function (p) { return S.rows[p.key] && S.rows[p.key].on; });
      var targets = onKeys.length ? onKeys : S.products.filter(function (p) { return p.primary; });
      var filled = 0, none = 0;
      targets.forEach(function (p) {
        var q = p.target_item_id != null ? hist[String(p.target_item_id)] : undefined;
        if (q > 0) {
          var row = rowOf(p.key);
          row.qty = Math.ceil(q - 1e-9);
          row.on = true;
          filled++;
        } else if (onKeys.length) {
          none++;
        }
      });
      if (!filled) { toast('Bu tarih aralığında ' + (onKeys.length ? 'seçili ürünler için ' : '') + 'üretim kaydı yok.'); return; }
      touch();
      renderTable();
      toast(filled + ' ürüne geçmiş üretim adedi yazıldı' + (none ? '; ' + none + ' seçili üründe kayıt yok (adet değişmedi)' : '') + '.', 'success');
    } catch (err) {
      toast('Geçmiş üretim alınamadı.');
    } finally {
      btn.disabled = false;
    }
  }

  // ─── Seçenekler ────────────────────────────────────────────────────────────
  function syncOptionsUI() {
    var o = S.options;
    document.querySelectorAll('input[name="sapStockMode"]').forEach(function (el) { el.checked = el.value === o.stock_mode; });
    $('sapOpenOrders').checked = !!o.subtract_open_orders;
    $('sapFinished').checked = !!o.subtract_finished_stock;
    document.querySelectorAll('input[name="sapLabel"]').forEach(function (el) { el.checked = el.value === o.label_mode; });
    document.querySelectorAll('#sapLabelSeg label').forEach(function (l) { l.classList.toggle('on', l.dataset.v === o.label_mode); });
    $('sapNewLabelWrap').style.display = o.label_mode === 'new' ? '' : 'none';
    $('sapNewLabelTitle').value = o.new_label_title || '';
    $('sapWasteRaw').value = o.extra_waste_pct.raw || 0;
    $('sapWastePkg').value = o.extra_waste_pct.packaging || 0;
    $('sapWasteLbl').value = o.extra_waste_pct.label || 0;
    $('sapMerge').checked = !!o.merge_duplicates;
    $('sapSafe').checked = !!o.safe_rounding;
    $('sapPkgRound').checked = !!o.round_to_package;
    $('sapLookalike').checked = !!o.lookalike_check;
    $('sapCurrency').value = o.currency || 'USD';
    $('sapTitle').value = S.title || '';
    $('sapOwner').value = o.checklist_owner || '';
    $('sapNotes').value = S.notes || '';
    renderExcluded();
    renderHeld();
    renderManual();
  }

  function bindOptions() {
    document.querySelectorAll('input[name="sapStockMode"]').forEach(function (el) {
      el.addEventListener('change', function () { if (el.checked) { S.options.stock_mode = el.value; touch(); } });
    });
    var flags = { sapOpenOrders: 'subtract_open_orders', sapFinished: 'subtract_finished_stock',
      sapMerge: 'merge_duplicates', sapSafe: 'safe_rounding', sapPkgRound: 'round_to_package',
      sapLookalike: 'lookalike_check' };
    Object.keys(flags).forEach(function (id) {
      $(id).addEventListener('change', function (e) { S.options[flags[id]] = e.target.checked; touch(); });
    });
    document.querySelectorAll('input[name="sapLabel"]').forEach(function (el) {
      el.addEventListener('change', function () {
        if (!el.checked) return;
        S.options.label_mode = el.value;
        document.querySelectorAll('#sapLabelSeg label').forEach(function (l) { l.classList.toggle('on', l.dataset.v === el.value); });
        $('sapNewLabelWrap').style.display = el.value === 'new' ? '' : 'none';
        touch();
        renderTable();
      });
    });
    $('sapNewLabelTitle').addEventListener('input', function (e) { S.options.new_label_title = e.target.value; touch(); });
    var waste = { sapWasteRaw: 'raw', sapWastePkg: 'packaging', sapWasteLbl: 'label' };
    Object.keys(waste).forEach(function (id) {
      $(id).addEventListener('input', function (e) {
        var v = num(e.target.value);
        S.options.extra_waste_pct[waste[id]] = (v == null || v < 0) ? 0 : Math.min(v, 100);
        touch();
      });
    });
    $('sapCurrency').addEventListener('change', function (e) { S.options.currency = e.target.value; touch(); });
    $('sapTitle').addEventListener('input', function (e) { S.title = e.target.value; touch(); });
    $('sapOwner').addEventListener('input', function (e) { S.options.checklist_owner = e.target.value; touch(); });
    $('sapNotes').addEventListener('input', function (e) { S.notes = e.target.value; touch(); });

    makePicker($('sapExPick'), function (it) { return it.category !== 'Bitmiş Ürün'; }, function (it) {
      if (S.excluded === null) S.excluded = S.defaults.excluded_items.map(function (x) { return { id: x.id, name: x.name, unit: x.unit }; });
      if (S.excluded.some(function (x) { return x.id === it.id; })) { toast('Bu kalem zaten hariç.'); return; }
      S.excluded.push({ id: it.id, name: it.name, unit: it.unit });
      touch(); renderExcluded();
    });
    makePicker($('sapHeldPick'), function (it) { return it.category !== 'Bitmiş Ürün'; }, function (it) {
      if (S.held.some(function (x) { return x.id === it.id; })) { toast('Bu kalem zaten bekletiliyor.'); return; }
      S.held.push({ id: it.id, name: it.name, reason: '' });
      touch(); renderHeld();
      var inp = document.querySelector('#sapHeldChips [data-reason="' + it.id + '"]');
      if (inp) inp.focus();
    });
    $('sapExChips').addEventListener('click', function (e) {
      var x = e.target.closest('[data-del]');
      if (x) {
        if (S.excluded === null) S.excluded = S.defaults.excluded_items.map(function (y) { return { id: y.id, name: y.name, unit: y.unit }; });
        var id = parseInt(x.dataset.del, 10);
        S.excluded = S.excluded.filter(function (y) { return y.id !== id; });
        touch(); renderExcluded(); renderMissing();
      }
    });
    $('sapExHint').addEventListener('click', function (e) {
      if (e.target.closest('[data-reset-ex]')) { S.excluded = null; touch(); renderExcluded(); renderMissing(); }
    });
    $('sapHeldChips').addEventListener('click', function (e) {
      var x = e.target.closest('[data-del]');
      if (x) {
        var id = parseInt(x.dataset.del, 10);
        S.held = S.held.filter(function (y) { return y.id !== id; });
        touch(); renderHeld(); renderMissing();
      }
    });
    $('sapHeldChips').addEventListener('input', function (e) {
      var id = e.target.dataset.reason;
      if (!id) return;
      var h = S.held.find(function (y) { return String(y.id) === id; });
      if (h) { h.reason = e.target.value; touch(); }
    });

    $('sapManualAdd').addEventListener('click', function () {
      S.manual.push({ section: 'shipping', name: '', qty: null, qty_text: '', unit: 'adet', note: '' });
      touch(); renderManual();
      var last = document.querySelector('#sapManualBody tr:last-child input[data-f="name"]');
      if (last) last.focus();
    });
    var mb = $('sapManualBody');
    mb.addEventListener('input', function (e) {
      var tr = e.target.closest('tr[data-i]');
      if (!tr || !e.target.dataset.f) return;
      var m = S.manual[parseInt(tr.dataset.i, 10)];
      var f = e.target.dataset.f;
      m[f] = f === 'qty' ? num(e.target.value) : e.target.value;
      touch();
    });
    mb.addEventListener('change', function (e) {
      if (e.target.dataset.f !== 'section') return;
      S.manual[parseInt(e.target.closest('tr[data-i]').dataset.i, 10)].section = e.target.value;
      touch();
    });
    mb.addEventListener('click', function (e) {
      var x = e.target.closest('[data-del]');
      if (!x) return;
      S.manual.splice(parseInt(x.closest('tr[data-i]').dataset.i, 10), 1);
      touch(); renderManual();
    });
  }

  function renderExcluded() {
    var list = S.excluded === null ? S.defaults.excluded_items : S.excluded;
    $('sapExChips').innerHTML = list.length ? list.map(function (x) {
      return '<span class="sap-ichip' + (x.missing ? ' missing' : '') + '" title="' + (x.missing ? 'Kart silinmiş ya da bu panelde değil — hesaba katılmıyor' : '') + '">' +
        esc(x.name) + (x.missing ? ' (bulunamadı)' : '') +
        '<button type="button" class="sap-x" data-del="' + x.id + '" aria-label="Çıkar">✕</button></span>';
    }).join('') : '<span class="sap-faint">Hariç kalem yok.</span>';
    $('sapExHint').innerHTML = S.excluded === null
      ? 'Varsayılan hariçler (sistem ayarı; saf su kendi üretimimiz). Notlarda ihtiyaç miktarıyla görünür.'
      : 'Bu plana özel liste. <a href="#" data-reset-ex onclick="return false;">Varsayılana dön</a>';
  }

  function renderHeld() {
    $('sapHeldChips').innerHTML = S.held.length ? S.held.map(function (h) {
      return '<span class="sap-ichip' + (h.missing ? ' missing' : '') + '">' + esc(h.name) + (h.missing ? ' (bulunamadı)' : '') +
        ' <input type="text" data-reason="' + h.id + '" maxlength="300" placeholder="gerekçe" value="' + esc(h.reason || '') + '">' +
        '<button type="button" class="sap-x" data-del="' + h.id + '" aria-label="Çıkar">✕</button></span>';
    }).join('') : '<span class="sap-faint">Bekletilen kalem yok.</span>';
  }

  function renderManual() {
    var secs = [['shipping', 'Koli / palet'], ['label', 'Etiket'], ['other', 'Diğer']];
    $('sapManualBody').innerHTML = S.manual.length ? S.manual.map(function (m, i) {
      return '<tr data-i="' + i + '">' +
        '<td><select class="sap-sel" data-f="section">' + secs.map(function (s) {
          return '<option value="' + s[0] + '"' + (m.section === s[0] ? ' selected' : '') + '>' + s[1] + '</option>';
        }).join('') + '</select></td>' +
        '<td><input type="text" class="sap-in" data-f="name" maxlength="200" value="' + esc(m.name) + '" placeholder="ör. 40×30×30 koli"></td>' +
        '<td><input type="number" class="sap-in" data-f="qty" min="0" step="any" value="' + (m.qty == null ? '' : m.qty) + '"></td>' +
        '<td><input type="text" class="sap-in" data-f="qty_text" maxlength="80" value="' + esc(m.qty_text) + '" placeholder="ör. 2 palet için"></td>' +
        '<td><input type="text" class="sap-in" data-f="unit" maxlength="20" value="' + esc(m.unit) + '"></td>' +
        '<td><input type="text" class="sap-in" data-f="note" maxlength="300" value="' + esc(m.note) + '"></td>' +
        '<td><button type="button" class="sap-gear" data-del title="Satırı sil"><i class="bi bi-trash3"></i></button></td></tr>';
    }).join('') : '<tr><td colspan="7" class="sap-faint" style="text-align:center;">Serbest satır yok. Sistemde kartı olmayan koli, palet, ara levha gibi kalemler buraya.</td></tr>';
  }

  // ─── Kalem arama (açılır liste) ────────────────────────────────────────────
  function makePicker(root, filterFn, onPick) {
    var inp = root.querySelector('input');
    var drop = root.querySelector('.sap-drop');
    var matches = [], act = -1;
    function close() { drop.classList.remove('open'); act = -1; }
    function paint() {
      drop.innerHTML = matches.length ? matches.map(function (it, i) {
        return '<button type="button" data-i="' + i + '" class="' + (i === act ? 'act' : '') + '">' + esc(it.name) +
          ' <span class="sap-drop-sub">· ' + esc(it.category || '') + (it.pkg_type ? ' / ' + esc(it.pkg_type) : '') +
          ' · stok ' + fmt(it.current_stock) + ' ' + esc(it.unit || '') + '</span></button>';
      }).join('') : '<div class="sap-faint" style="padding:0.5rem 0.7rem;">Eşleşen kalem yok.</div>';
      drop.classList.add('open');
    }
    async function update() {
      var q = fold(inp.value.trim());
      if (q.length < 2) { close(); return; }
      var items = await loadItems();
      if (!items.length) {
        drop.innerHTML = '<div class="sap-faint" style="padding:0.5rem 0.7rem;">Kalem listesi alınamadı.</div>';
        drop.classList.add('open');
        return;
      }
      matches = items.filter(function (it) { return filterFn(it) && it._fold.indexOf(q) !== -1; }).slice(0, 30);
      act = matches.length ? 0 : -1;
      paint();
    }
    function pick(i) {
      var it = matches[i];
      if (!it) return;
      inp.value = '';
      close();
      onPick(it);
    }
    inp.addEventListener('input', debounce(update, 120));
    inp.addEventListener('keydown', function (e) {
      if (!drop.classList.contains('open')) return;
      if (e.key === 'ArrowDown') { act = Math.min(act + 1, matches.length - 1); paint(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { act = Math.max(act - 1, 0); paint(); e.preventDefault(); }
      else if (e.key === 'Enter') { pick(act); e.preventDefault(); }
      else if (e.key === 'Escape') { close(); e.stopPropagation(); }
    });
    inp.addEventListener('blur', function () { setTimeout(close, 180); });
    drop.addEventListener('mousedown', function (e) {
      var b = e.target.closest('button[data-i]');
      if (b) { e.preventDefault(); pick(parseInt(b.dataset.i, 10)); }
    });
  }

  // ─── Modal ─────────────────────────────────────────────────────────────────
  var modal = { onOk: null, onCancel: null };
  function openModal(cfg) {
    $('sapModalTitle').textContent = cfg.title || '';
    $('sapModalBody').innerHTML = cfg.body || '';
    var foot = (cfg.leftButton ? '<button type="button" class="sap-b sap-left" data-left>' + esc(cfg.leftButton) + '</button>' : '') +
      '<button type="button" class="sap-b" data-close>Vazgeç</button>' +
      '<button type="button" class="sap-b sap-b-main" data-ok>' + esc(cfg.okText || 'Tamam') + '</button>';
    $('sapModalFoot').innerHTML = foot;
    modal.onOk = cfg.onOk; modal.onCancel = cfg.onCancel || null; modal.onLeft = cfg.onLeft || null;
    var ov = $('sapModal');
    ov.classList.add('open');
    ov.setAttribute('aria-hidden', 'false');
    if (cfg.onOpen) cfg.onOpen();
    var f = cfg.focus ? $(cfg.focus) : null;
    if (f) { f.focus(); if (f.select) f.select(); }
  }
  function closeModal(cancelled) {
    var ov = $('sapModal');
    ov.classList.remove('open');
    ov.setAttribute('aria-hidden', 'true');
    if (cancelled && modal.onCancel) modal.onCancel();
    modal.onOk = modal.onCancel = modal.onLeft = null;
    $('sapModalBody').innerHTML = '';
  }
  function bindModal() {
    var ov = $('sapModal');
    ov.addEventListener('click', function (e) {
      if (e.target === ov || e.target.closest('[data-close]')) { closeModal(true); return; }
      if (e.target.closest('[data-ok]')) {
        if (!modal.onOk || modal.onOk() !== false) closeModal(false);
        return;
      }
      if (e.target.closest('[data-left]') && modal.onLeft) {
        if (modal.onLeft() !== false) closeModal(false);
      }
    });
    document.addEventListener('keydown', function (e) {
      if (!ov.classList.contains('open')) return;
      if (e.key === 'Escape') closeModal(true);
      else if (e.key === 'Enter' && e.target && e.target.id === 'sapNameIn') {
        e.preventDefault();
        if (!modal.onOk || modal.onOk() !== false) closeModal(false);
      }
    });
  }

  // ─── Satır ayarları (⚙) ────────────────────────────────────────────────────
  function openRowOptions(key) {
    var p = S.byKey[key];
    if (!p) return;
    var r = rowOf(key);
    var o = clone(r.o || defRowOpts());
    o.extra = o.extra || [];
    var recOpts = '<option value="">' + (p.recipeless ? '— Reçetesi yok (tahmin için seçin) —' : '(Kendi reçetesi)') + '</option>' +
      S.recipeList.filter(function (x) { return x.id !== p.recipe_id; }).map(function (x) {
        return '<option value="' + x.id + '"' + (o.recipe_id === x.id ? ' selected' : '') + '>' + esc(x.label) + '</option>';
      }).join('');
    var body =
      '<div class="sap-faint"><b>' + esc(p.brand) + '</b> · ' + esc(p.name) + (p.size_ml ? ' · ' + fmt(p.size_ml) + ' ml' : '') + '</div>' +
      '<div><label class="sap-lbl" for="sapOptRecipe">Başka ürünün reçetesi</label>' +
      '<select id="sapOptRecipe" class="sap-sel sap-full">' + recOpts + '</select>' +
      '<div class="sap-faint" style="margin-top:0.2rem;">Reçetesi olmayan (ya da farklı boydaki) ürün için tahmin; raporda “tahmini” diye işaretlenir.</div></div>' +
      '<div class="sap-2col">' +
        '<div><label class="sap-lbl" for="sapOptScale">Ölçek ×</label><input type="number" id="sapOptScale" class="sap-in" min="0.001" max="1000" step="0.05" value="' + (o.scale || 1) + '">' +
        '<div class="sap-faint" id="sapOptScaleHint" style="margin-top:0.2rem;">Yalnız hammaddeler çarpılır (ambalaj/etiket değişmez).</div></div>' +
        '<div><label class="sap-lbl" for="sapOptFaces">Etiket yüzü</label><input type="number" id="sapOptFaces" class="sap-in" min="1" max="6" step="1" placeholder="otomatik" value="' + (o.faces || '') + '">' +
        '<div class="sap-faint" style="margin-top:0.2rem;">“Yeni basılacak” etiket modunda ürün başına etiket sayısı.</div></div>' +
      '</div>' +
      '<label class="sap-check"><input type="checkbox" id="sapOptPkg"' + (o.use_pkg !== false ? ' checked' : '') + '>' +
        '<span>Reçete ambalajını kullan<small>Kapalıyken reçetedeki kavanoz/kapak/kutu düşer (etiket kalır); yerine ek ambalaj ekleyin.</small></span></label>' +
      '<div><label class="sap-lbl">Ek ambalaj (ürün başına)</label><div id="sapOptExtra"></div>' +
        '<div class="sap-2col" style="margin-top:0.35rem;">' +
          '<div class="sap-pick" id="sapOptExtraPick"><input type="text" class="sap-in" placeholder="Ambalaj kartı ara…" autocomplete="off"><div class="sap-drop"></div></div>' +
          '<div style="display:flex;gap:0.35rem;"><input type="text" id="sapOptNewName" class="sap-in" style="flex:1;" maxlength="150" placeholder="ya da yeni kalem adı (kartı yok)">' +
          '<button type="button" class="sap-b sap-b-sm" id="sapOptNewAdd">Ekle</button></div>' +
        '</div></div>' +
      '<div class="sap-2col">' +
        '<div><label class="sap-lbl" for="sapOptName">Görünen ad</label><input type="text" id="sapOptName" class="sap-in" maxlength="200" placeholder="' + esc(p.name) + '" value="' + esc(o.display_name || '') + '"></div>' +
        '<div><label class="sap-lbl" for="sapOptNote">Not</label><input type="text" id="sapOptNote" class="sap-in" maxlength="500" value="' + esc(o.note || '') + '"></div>' +
      '</div>';

    function paintExtra() {
      var box = $('sapOptExtra');
      box.innerHTML = o.extra.length ? o.extra.map(function (e, i) {
        return '<div class="sap-xrow" data-i="' + i + '">' +
          '<span' + (e.missing ? ' style="color:var(--sap-err-fg);"' : '') + '>' + (e.item_id ? esc(e.name) : 'Yeni: <b>' + esc(e.new_name) + '</b>') +
            (e.missing ? ' (bulunamadı)' : '') + '</span>' +
          '<input type="number" class="sap-in" data-f="per_unit" min="0.001" max="100" step="any" value="' + (e.per_unit || 1) + '" title="Ürün başına adet">' +
          '<input type="text" class="sap-in sap-xnote" data-f="note" maxlength="300" placeholder="not" value="' + esc(e.note || '') + '">' +
          '<button type="button" class="sap-gear" data-xdel title="Çıkar"><i class="bi bi-x-lg"></i></button></div>';
      }).join('') : '<div class="sap-faint">Ek ambalaj yok.</div>';
    }
    function scaleHint() {
      var rid = num($('sapOptRecipe').value);
      var hint = $('sapOptScaleHint');
      var src = rid ? S.recipeList.find(function (x) { return x.id === rid; }) : null;
      if (src && src.size && p.size_ml && src.size !== p.size_ml) {
        var ratio = Math.round(p.size_ml / src.size * 1000) / 1000;
        hint.innerHTML = 'Boy oranı ' + fmt(p.size_ml) + ' ml / ' + fmt(src.size) + ' ml = <b>' + fmt(ratio, 3) + '</b> ' +
          '<a href="#" id="sapOptScaleApply">uygula</a>';
        $('sapOptScaleApply').addEventListener('click', function (ev) { ev.preventDefault(); $('sapOptScale').value = ratio; });
      } else {
        hint.textContent = 'Yalnız hammaddeler çarpılır (ambalaj/etiket değişmez).';
      }
    }

    openModal({
      title: 'Satır ayarları',
      body: body,
      okText: 'Uygula',
      leftButton: 'Varsayılana dön',
      onOpen: function () {
        paintExtra();
        scaleHint();
        $('sapOptRecipe').addEventListener('change', scaleHint);
        makePicker($('sapOptExtraPick'), function (it) { return it.category === 'Ambalaj'; }, function (it) {
          o.extra.push({ item_id: it.id, new_name: null, name: it.name, per_unit: 1, note: '' });
          paintExtra();
        });
        $('sapOptNewAdd').addEventListener('click', function () {
          var nm = ($('sapOptNewName').value || '').trim();
          if (!nm) { toast('Yeni kalem adını yazın.'); return; }
          o.extra.push({ item_id: null, new_name: nm, name: nm, per_unit: 1, note: '' });
          $('sapOptNewName').value = '';
          paintExtra();
        });
        $('sapOptExtra').addEventListener('input', function (ev) {
          var row = ev.target.closest('[data-i]');
          if (!row || !ev.target.dataset.f) return;
          var e = o.extra[parseInt(row.dataset.i, 10)];
          e[ev.target.dataset.f] = ev.target.dataset.f === 'per_unit' ? num(ev.target.value) : ev.target.value;
        });
        $('sapOptExtra').addEventListener('click', function (ev) {
          if (!ev.target.closest('[data-xdel]')) return;
          o.extra.splice(parseInt(ev.target.closest('[data-i]').dataset.i, 10), 1);
          paintExtra();
        });
      },
      onLeft: function () {
        r.o = defRowOpts();
        touch(); renderTable();
        return true;
      },
      onOk: function () {
        var scale = num($('sapOptScale').value);
        if (scale == null) scale = 1;
        if (!(scale > 0 && scale <= 1000)) { toast('Ölçek 0 ile 1000 arasında olmalı.'); return false; }
        var faces = num($('sapOptFaces').value);
        if (faces != null && !(faces >= 1 && faces <= 6 && Math.floor(faces) === faces)) { toast('Etiket yüzü 1–6 arası tam sayı olmalı.'); return false; }
        for (var i = 0; i < o.extra.length; i++) {
          var pu = o.extra[i].per_unit;
          if (!(pu > 0 && pu <= 100)) { toast('Ek ambalajda ürün başına adet 0 ile 100 arasında olmalı.'); return false; }
        }
        o.recipe_id = num($('sapOptRecipe').value) || null;
        o.scale = scale;
        o.faces = faces || null;
        o.use_pkg = $('sapOptPkg').checked;
        o.display_name = ($('sapOptName').value || '').trim();
        o.note = ($('sapOptNote').value || '').trim();
        r.o = o;
        delete S.rowWarn[key];
        touch(); renderTable();
        return true;
      }
    });
  }

  // ─── Önizleme ──────────────────────────────────────────────────────────────
  function clearPreview() {
    S.report = null;
    $('sapStale').classList.remove('on');
    $('sapPreview').innerHTML = '<div class="sap-empty"><i class="bi bi-cart-check"></i>Ürün ve adet seçip <b>Önizle</b>\'ye basın.</div>';
  }

  function checkRequest(req) {
    if (!req.lines.length) {
      var noQty = S.products.some(function (p) { return S.rows[p.key] && S.rows[p.key].on && !(S.rows[p.key].qty > 0); });
      toast(noQty ? 'Seçili ürünlerin adedini girin.' : 'En az bir ürün seçip adet girin.');
      return false;
    }
    return true;
  }
  function scenarioQS(sep) {
    return S.scenario ? sep + 'scenario_id=' + S.scenario.id : '';
  }

  async function runPreview() {
    var req = buildRequest(false);
    if (!checkRequest(req)) return;
    var btn = $('sapRun');
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-hourglass-split"></i>Hesaplanıyor…';
    try {
      var r = await api('POST', API + '/preview' + scenarioQS('?'), req);
      if (!r.ok) { toast(await errText(r, 'Önizleme üretilemedi.')); return; }
      S.report = await r.json();
      $('sapStale').classList.remove('on');
      renderPreview();
      if (S.scenario) loadScenarios();
    } catch (e) {
      toast('Sunucuya bağlanılamadı.');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-play-fill"></i>Önizle';
    }
  }

  function fileNameFrom(cd, fallback) {
    if (!cd) return fallback;
    var m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    if (m) { try { return decodeURIComponent(m[1]); } catch (e) { /* düz ada düş */ } }
    m = /filename="([^"]+)"/i.exec(cd);
    return m ? m[1] : fallback;
  }

  async function runExport(format) {
    var req = buildRequest(false);
    if (!checkRequest(req)) return;
    var btn = $(format === 'pdf' ? 'sapPdf' : 'sapXls');
    var old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-hourglass-split"></i>Hazırlanıyor…';
    try {
      var r = await api('POST', API + '/export?format=' + format + scenarioQS('&'), req);
      if (!r.ok) { toast(await errText(r, (format === 'pdf' ? 'PDF' : 'Excel') + ' üretilemedi.')); return; }
      var blob = await r.blob();
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = fileNameFrom(r.headers.get('content-disposition'), 'satin_alma_plani.' + format);
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      if (S.scenario) loadScenarios();
    } catch (e) {
      toast('Sunucuya bağlanılamadı.');
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  }

  function cautionsHtml(list, skipCodes) {
    return (list || []).filter(function (c) { return !skipCodes || skipCodes.indexOf(c.code) === -1; }).map(function (c) {
      return '<div class="sap-caut ' + (c.severity === 'warn' ? 'warn' : 'info') + '">' +
        (c.severity === 'warn' ? '<i class="bi bi-exclamation-triangle"></i> ' : '<i class="bi bi-info-circle"></i> ') + esc(c.text) + '</div>';
    }).join('');
  }

  function supHtml(m) {
    return '<div class="sap-sup">' + ((m.ui && m.ui.sup) || []).map(function (l) {
      return '<div class="' + (l.s || 'g') + '">' + esc(l.t) + '</div>';
    }).join('') + '</div>';
  }

  // "Tedarikçiler (N)" — bizim kayıtlarımızdaki firmalar (stok kartı, alım,
  // numune, sipariş, fiyat listesi) + aynı malzeme grubundaki diğer kartlar.
  // Metinler sunucuda biçimlenir (m.ui.firms — core/purchase_pricing.firms_view).
  function firmsHtml(m) {
    var f = m.ui && m.ui.firms;
    if (!f) return '';
    var li = function (x) {
      return '<li' + (x.w ? ' class="w"' : '') + '><b>' + esc(x.t) + '</b>' +
        ((x.d || []).length ? ' — ' + (x.d || []).map(esc).join(' · ') : '') + '</li>';
    };
    var html = '<details class="sap-firms"><summary>' + esc(f.title) + '</summary>';
    if ((f.firms || []).length) html += '<ul>' + f.firms.map(li).join('') + '</ul>';
    if ((f.alts || []).length) {
      html += '<div class="sap-firms-h">' + esc(f.alts_title || 'Aynı malzeme — diğer kartlar') + '</div>' +
        '<ul>' + f.alts.map(li).join('') + '</ul>';
    }
    return html + '</details>';
  }

  function perProductHtml(m) {
    var pp = m.per_product || [];
    if (!pp.length) return '';
    return '<details><summary>Kullanıldığı ürünler (' + pp.length + ')</summary><ul>' + pp.map(function (x) {
      return '<li>' + esc(x.product) + ' — ' + fmt(x.need, 2) + ' ' + esc(m.unit) + (x.extra ? ' (ek ambalaj)' : '') + '</li>';
    }).join('') + '</ul></details>';
  }

  function matTable(rows, rep, opts) {
    var gross = rep.meta.options.stock_mode === 'gross';
    var openOn = !!rep.meta.options.subtract_open_orders;
    var head = '<tr><th>Malzeme</th><th class="num">Toplam gereken</th><th class="num">' + (gross ? 'Elimizde (bilgi)' : 'Elimizde') + '</th>' +
      (openOn ? '<th class="num">' + (gross ? 'Yolda (bilgi)' : 'Yolda') + '</th>' : '') +
      '<th class="num">Alınacak</th>' + (opts.noSup ? '' : '<th>Tedarikçi ve birim fiyat</th><th class="num">Tutar</th>') + '</tr>';
    var body = rows.map(function (m) {
      var d = m.display || {};
      var nameNotes = ((m.ui && m.ui.name_notes) || []).map(function (t) { return ' <span class="sap-faint">(' + esc(t.replace(/\.$/, '').toLocaleLowerCase('tr')) + ')</span>'; }).join('');
      return '<tr><td><b>' + esc(m.name) + '</b>' + nameNotes + cautionsHtml(m.cautions, ['estimated']) + perProductHtml(m) + '</td>' +
        '<td class="num">' + esc(d.need_text) + '</td><td class="num">' + esc(d.stock_text) + '</td>' +
        (openOn ? '<td class="num">' + esc(d.open_text) + '</td>' : '') +
        '<td class="num"><b>' + esc(d.buy_text) + '</b></td>' +
        (opts.noSup ? '' : '<td>' + supHtml(m) + firmsHtml(m) + '</td><td class="num">' + (m.ui && m.ui.amount ? '<b>' + esc(m.ui.amount) + '</b>' : '—') + '</td>') + '</tr>';
    }).join('');
    var total = (opts.total_text && !opts.noSup)
      ? '<tr class="sap-total"><td colspan="' + (openOn ? 6 : 5) + '">Toplam</td><td class="num">' + esc(opts.total_text) + '</td></tr>' : '';
    return '<div style="overflow-x:auto;"><table class="sap-tbl">' + '<thead>' + head + '</thead><tbody>' + body + total + '</tbody></table></div>';
  }

  function secHead(s) {
    return '<h4>' + s.no + '. ' + esc(s.title) + (s.summary ? ' <span class="sap-faint" style="font-family:Jost,sans-serif;font-weight:500;">(' + esc(s.summary) + ')</span>' : '') + '</h4>' +
      (s.subtitle ? '<p class="sap-sub">' + esc(s.subtitle) + '</p>' : '');
  }

  function newItemsHtml(s) {
    var rows = s.rows || [];
    return '<div style="overflow-x:auto;"><table class="sap-tbl"><thead><tr><th>Kalem</th><th class="num">Miktar</th><th>Ayrıntı</th></tr></thead><tbody>' +
      rows.map(function (r) {
        if (r.type === 'label') {
          return '<tr><td><b>' + esc(r.name) + '</b></td><td class="num"><b>' + fmt(r.total, 0) + ' adet</b></td><td class="sap-muted">' +
            fmt(r.units, 0) + ' ürün × ' + r.faces + ' yüz · ' + esc(r.face_text) + '</td></tr>';
        }
        return '<tr><td><b>' + esc(r.name) + '</b></td><td class="num">' + esc(r.qty_text || '—') + '</td><td class="sap-muted">' + esc(r.note || '') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  function supplierBlocks(rows, withTotal, sym) {
    return rows.map(function (b) {
      var contact = b.contact_line
        ? '<div class="sap-contact">' + esc(b.contact_line) + '</div>'
        : (b.none ? '' : '<div class="sap-contact none">İletişim bilgisi sistemde yok</div>');
      var items = (b.items || []).map(function (it) {
        var bits = [esc(it.name), esc(it.buy_text)];
        if (withTotal && it.price != null) bits.push(priceText(it.price) + ' ' + sym + '/' + (UNIT_TEXT[it.price_unit] || esc(it.price_unit)));
        if (withTotal && it.amount != null) bits.push('<b>' + fmt(it.amount, 0) + ' ' + sym + '</b>');
        if (it.reasons && it.reasons.length) bits.push('<span class="sap-faint">' + esc(it.reasons.join(', ')) + '</span>');
        return '<li>' + bits.join(' — ') + '</li>';
      }).join('');
      return '<div class="sap-supblk"><h5><span>' + esc(b.name) + (b.none ? '' : ' <span class="sap-faint">(' + (b.count || (b.items || []).length) + ' kalem)</span>') + '</span>' +
        (withTotal ? '<span>' + fmt(b.total, 0) + ' ' + sym + '</span>' : '') + '</h5>' +
        (b.none && b.contact_line ? '<div class="sap-contact">' + esc(b.contact_line) + '</div>' : contact) + '<ul>' + items + '</ul></div>';
    }).join('');
  }

  function renderPreview() {
    var rep = S.report;
    if (!rep) { clearPreview(); return; }
    var meta = rep.meta || {}, counts = meta.counts || {}, pr = rep.pricing || {};
    var sym = pr.symbol || SYM[pr.currency] || '';
    var byKey = {};
    (rep.materials || []).forEach(function (m) { byKey[m.key] = m; });
    var secs = rep.sections || [];
    var sec = function (k) { return secs.find(function (s) { return s.key === k; }); };
    var pc = pr.counts || {};
    var unpriced = (pc.raw_unpriced || 0) + (pc.packaging_unpriced || 0) + (pc.label_unpriced || 0);
    // Uyarılar sekmesinin listesi — sekme sayacı ve KPI kutusu BUNDAN sayılır
    // (summary.warnings yalnız 'warn' derecesini sayıyor, bilgi notlarını ve
    // bekletilenleri saymıyordu → sekme "2" derken tabloda 8 satır çıkıyordu).
    var w = [];
    (rep.products || []).forEach(function (p) {
      (p.cautions || []).forEach(function (c) { w.push([c.severity, p.no + '. ' + p.name, c.text]); });
    });
    (rep.materials || []).concat(rep.held || []).forEach(function (m) {
      (m.cautions || []).forEach(function (c) { if (c.code !== 'estimated') w.push([c.severity, m.name, c.text]); });
    });
    w.sort(function (a, b) { return (a[0] === 'warn' ? 0 : 1) - (b[0] === 'warn' ? 0 : 1); });
    var warnN = w.filter(function (x) { return x[0] === 'warn'; }).length;
    var infoN = w.length - warnN;

    // KPI
    var kpis = [
      [fmt(counts.products, 0), 'Ürün', fmt(counts.units, 0) + ' adet' + (counts.produce_units !== counts.units ? ' · üretilecek ' + fmt(counts.produce_units, 0) : '')],
      [fmt(counts.to_buy, 0), 'Alınacak kalem', fmt(counts.materials, 0) + ' malzemeden'],
      [fmt((pr.totals || {}).all, 0) + ' ' + sym, 'Fiyatlı toplam', 'hammadde ' + fmt((pr.totals || {}).raw, 0) + ' · ambalaj ' + fmt((pr.totals || {}).packaging, 0)],
      [fmt(unpriced, 0), 'Fiyatı olmayan', 'teklif alınacak'],
      [meta.options.stock_mode === 'net' ? fmt(counts.sufficient, 0) : '—', 'Yeterli', meta.options.stock_mode === 'net' ? 'stok yetiyor' : 'brüt modda yok'],
      [fmt(warnN, 0), 'Uyarı', fmt(infoN, 0) + ' bilgi notu · ' + fmt(counts.recipeless, 0) + ' reçetesiz ürün']
    ];
    var html = '<div class="sap-kpis">' + kpis.map(function (k) {
      return '<div class="sap-kpi"><div class="v">' + k[0] + '</div><div class="l">' + k[1] + '</div><div class="s">' + esc(k[2]) + '</div></div>';
    }).join('') + '</div>';
    html += '<p class="sap-scope">' + esc(meta.scope_text || '') + '</p>';

    var warns = [];
    if (pr.fx && pr.fx.warning) warns.push(pr.fx.warning);
    if (counts.recipeless) warns.push(counts.recipeless + ' ürünün reçetesi yok; malzeme ihtiyacı hesaplanmadı.');
    if (counts.held) warns.push(counts.held + ' kalem bekletiliyor; listeye ve toplama girmedi (Uyarılar sekmesi).');
    (meta.warnings || []).forEach(function (w) { warns.push(w); });
    if ((rep.skipped_labels || []).length) warns.push(rep.skipped_labels.length + ' etiket seçilen dilde bulunamadı; listeye eklenmedi.');
    if (warns.length) html += '<div class="sap-box sap-box-warn"><i class="bi bi-exclamation-triangle-fill"></i> ' + warns.map(esc).join('<br>') + '</div>';
    var boxBits = [];
    if ((pr.totals || {}).all) {
      boxBits.push('Fiyatı listede olanların toplamı <b>' + fmt(pr.totals.all, 0) + ' ' + sym + '</b>' +
        ' (hammadde ' + fmt(pr.totals.raw, 0) + ', ambalaj ' + fmt(pr.totals.packaging, 0) + ', etiket ' + fmt(pr.totals.label, 0) + ' ' + sym + ').');
    }
    if (unpriced) boxBits.push(unpriced + ' kalemin fiyatı yok. Gerçek toplam, fiyatı olmayan kalemler eklenince bundan yüksek olacak.');
    if (boxBits.length) html += '<div class="sap-box sap-box-ok">' + boxBits.join(' ') + '</div>';

    // Sekmeler
    var suff = sec('sufficient');
    var tabs = [['buy', 'Alınacaklar', counts.to_buy], ['ok', 'Yeterli', suff ? suff.count : 0],
      ['prod', 'Ürünler & kapasite', counts.products], ['warn', 'Uyarılar', w.length],
      ['sup', 'Tedarikçiler', ((sec('suppliers') || {}).count || 0) + ((sec('candidates') || {}).count || 0)]];
    html += '<div class="sap-tabs" role="tablist">' + tabs.map(function (t, i) {
      return '<button type="button" class="sap-tab' + (i === 0 ? ' on' : '') + '" data-tab="' + t[0] + '">' + t[1] +
        '<span class="sap-cnt">' + fmt(t[2], 0) + '</span></button>';
    }).join('') + '</div>';

    // 1) Alınacaklar
    var buy = '';
    secs.forEach(function (s) {
      if (MAT_SECTIONS.indexOf(s.key) !== -1) {
        var rows = (s.row_keys || []).map(function (k) { return byKey[k]; }).filter(Boolean);
        buy += '<div class="sap-sec">' + secHead(s) + matTable(rows, rep, { total_text: s.total_text }) + '</div>';
      } else if (s.key === 'new_items') {
        buy += '<div class="sap-sec">' + secHead(s) + newItemsHtml(s) + '</div>';
      }
    });
    var notes = sec('notes');
    if (notes) buy += '<div class="sap-sec">' + secHead(notes) + '<ul class="sap-notes">' + (notes.rows || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul></div>';
    if (!buy) buy = '<div class="sap-empty">Alınacak kalem yok — elimizdeki stok yetiyor.</div>';

    // 2) Yeterli
    var okHtml;
    if (suff) {
      okHtml = '<div class="sap-sec">' + secHead(suff) + matTable((suff.row_keys || []).map(function (k) { return byKey[k]; }).filter(Boolean), rep, { noSup: true }) + '</div>';
    } else {
      okHtml = '<div class="sap-empty">' + (meta.options.stock_mode === 'gross' ? 'Brüt modda stok düşülmez; yeterli listesi yok.' : 'Elimizdeki stokla karşılanan kalem yok.') + '</div>';
    }

    // 3) Ürünler & kapasite
    var prodSec = sec('products');
    var prodHtml = '<div class="sap-sec">' + (prodSec ? secHead(prodSec) : '') + '<div style="overflow-x:auto;"><table class="sap-tbl"><thead><tr><th class="num">No</th><th>Ürün</th><th class="num">Hedef</th><th class="num">Üretilecek</th><th class="num">Bitmiş stok</th><th class="num">Elimizdekiyle üretilebilir</th><th>Kısıtlayan malzeme</th></tr></thead><tbody>' +
      (rep.products || []).map(function (p) {
        var cap = p.capacity || {};
        var lim = (cap.limiting || []).map(function (l) { return esc(l.name) + ' <span class="sap-faint">(' + fmt(l.stock, 2) + ' ' + esc(l.unit) + ')</span>'; }).join('<br>');
        return '<tr><td class="num">' + p.no + '</td><td><b>' + esc(p.name) + '</b> <span class="sap-brand">' + esc(p.brand || '') + '</span>' +
          (p.estimated ? ' <span class="sap-pill sap-pill-info">tahmini</span>' : '') +
          (p.note ? '<div class="sap-faint">Not: ' + esc(p.note) + '</div>' : '') + cautionsHtml(p.cautions) + '</td>' +
          '<td class="num">' + fmt(p.qty, 0) + '</td><td class="num">' + fmt(p.produce_qty, 0) + '</td>' +
          '<td class="num">' + fmt(p.finished_stock, 0) + '</td>' +
          '<td class="num">' + (cap.producible == null ? '—' : '<b>' + fmt(cap.producible, 0) + '</b>') + '</td><td>' + (lim || '—') + '</td></tr>';
      }).join('') + '</tbody></table></div></div>';

    // 4) Uyarılar (liste `w` yukarıda kuruldu)
    var warnHtml = w.length ? '<div style="overflow-x:auto;"><table class="sap-tbl"><thead><tr><th style="width:28px;"></th><th>Kayıt</th><th>Uyarı</th></tr></thead><tbody>' +
      w.map(function (x) {
        return '<tr><td>' + (x[0] === 'warn' ? '<i class="bi bi-exclamation-triangle-fill" style="color:var(--sap-warn-fg);"></i>' : '<i class="bi bi-info-circle" style="color:var(--sap-muted);"></i>') +
          '</td><td><b>' + esc(x[1]) + '</b></td><td>' + esc(x[2]) + '</td></tr>';
      }).join('') + '</tbody></table></div>' : '<div class="sap-empty">Uyarı yok.</div>';
    if ((rep.held || []).length) {
      warnHtml += '<div class="sap-sec" style="margin-top:1rem;"><h4>Bekletilen kalemler</h4><p class="sap-sub">Listeye ve toplama girmedi; “alınırsa” en ucuz teklifle tutar.</p>' +
        matTable(rep.held, rep, {}) + '</div>';
    }
    if ((rep.excluded || []).length) {
      warnHtml += '<div class="sap-sec"><h4>Hariç tutulanlar</h4><ul class="sap-notes">' + rep.excluded.map(function (e) {
        return '<li><b>' + esc(e.name) + '</b> ' + esc(e.need_text) + ' (' + esc(e.reason) + ')</li>';
      }).join('') + '</ul></div>';
    }

    // 5) Tedarikçiler
    var supHtmlAll = '';
    var ss = sec('suppliers'), cs = sec('candidates'), ck = sec('checklist');
    if (ss) supHtmlAll += '<div class="sap-sec">' + secHead(ss) + supplierBlocks(ss.rows || [], true, sym) + '</div>';
    if (cs) supHtmlAll += '<div class="sap-sec">' + secHead(cs) + supplierBlocks(cs.rows || [], false, sym) + '</div>';
    if (ck) supHtmlAll += '<div class="sap-sec">' + secHead(ck) + '<ul class="sap-notes">' + (ck.rows || []).map(function (r) { return '<li>' + esc(r.text) + '</li>'; }).join('') + '</ul></div>';
    if (!supHtmlAll) supHtmlAll = '<div class="sap-empty">Alınacak kalem olmadığı için tedarikçi listesi yok.</div>';

    html += '<div class="sap-pane on" data-pane="buy">' + buy + '</div>' +
      '<div class="sap-pane" data-pane="ok">' + okHtml + '</div>' +
      '<div class="sap-pane" data-pane="prod">' + prodHtml + '</div>' +
      '<div class="sap-pane" data-pane="warn">' + warnHtml + '</div>' +
      '<div class="sap-pane" data-pane="sup">' + supHtmlAll + '</div>';
    $('sapPreview').innerHTML = html;
  }

  function bindPreview() {
    $('sapRun').addEventListener('click', runPreview);
    $('sapPdf').addEventListener('click', function () { runExport('pdf'); });
    $('sapXls').addEventListener('click', function () { runExport('xlsx'); });
    $('sapPreview').addEventListener('click', function (e) {
      var t = e.target.closest('.sap-tab');
      if (!t) return;
      document.querySelectorAll('#sapPreview .sap-tab').forEach(function (x) { x.classList.toggle('on', x === t); });
      document.querySelectorAll('#sapPreview .sap-pane').forEach(function (p) { p.classList.toggle('on', p.dataset.pane === t.dataset.tab); });
    });
  }

  // ─── Senaryo çubuğu olayları ───────────────────────────────────────────────
  function bindScenarioBar() {
    var sel = $('sapScenario');
    sel.addEventListener('change', async function () {
      var id = sel.value;
      var cur = S.scenario ? String(S.scenario.id) : '';
      if (id === cur) return;
      if (S.dirty && id && !window.confirm('Ekrandaki planda kaydedilmemiş değişiklikler var. Senaryo açılırsa kaybolacak. Devam edilsin mi?')) {
        sel.value = cur;
        return;
      }
      if (!id) {                      // taslağa dön: ekrandaki plan kalır, senaryodan kopar
        S.scenario = null;
        S.dirty = true;
        renderScenarioInfo();
        saveDraft();
        return;
      }
      await openScenario(id);
    });
    $('sapScSave').addEventListener('click', function () { saveScenario(false); });
    $('sapScSaveAs').addEventListener('click', function () { saveScenario(true); });
    $('sapScDelete').addEventListener('click', deleteScenario);
    $('sapNew').addEventListener('click', function () {
      if (S.dirty && !window.confirm('Ekrandaki plan sıfırlansın mı? Kaydedilmemiş değişiklikler kaybolur.')) return;
      resetPlan();
      S.scenario = null;
      S.dirty = false;
      renderAll();
      clearPreview();
      saveDraft();
    });
    $('sapMissing').addEventListener('click', function (e) {
      var b = e.target.closest('[data-orphan]');
      if (!b) return;
      S.orphans.splice(parseInt(b.dataset.orphan, 10), 1);
      touch();
      renderCounts();
    });
  }

  function renderAll() {
    syncOptionsUI();
    renderScenarioSelect();
    renderTable();
  }

  // ─── Başlangıç ─────────────────────────────────────────────────────────────
  async function init() {
    bindTable();
    bindOptions();
    bindModal();
    bindPreview();
    bindScenarioBar();
    try {
      await Promise.all([loadProducts(), loadScenarios(), loadItems()]);
    } catch (e) {
      $('sapProdBody').innerHTML = '<tr><td colspan="7" class="sap-empty" style="color:var(--sap-err-fg);">' + esc(e.message || 'Ürün listesi alınamadı.') + '</td></tr>';
      return;
    }
    renderBrands();
    var draft = loadDraft();
    if (draft) {
      var sc = draft.scenario && S.scenarios.find(function (x) { return x.id === draft.scenario.id; });
      applyConfig(draft.req, await draftMissing(draft.req));
      (draft.checked || []).forEach(function (k) { if (S.byKey[k]) rowOf(k).on = true; });
      S.scenario = sc ? { id: sc.id, name: sc.name, can_edit: sc.can_edit } : null;
      S.dirty = !!draft.dirty || !!(draft.scenario && !sc);
    }
    renderAll();
  }

  init();
})();
