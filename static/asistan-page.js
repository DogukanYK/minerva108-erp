/**
 * Minerva108 — /asistan çalışma alanı (Soru · Belge Oku · Metin Yaz)
 * ====================================================================
 *
 * templates/asistan.html iskeletini doldurur; akış ve çizim çekirdeği
 * static/assistant.js'in `window.M108AI`'sından gelir (aynı NDJSON akışı,
 * aynı kaçış-önce markdown — ikinci kopya YOK).  Defer sırası: assistant.js
 * önce yüklenir, `M108AI.ready` bootstrap'i bekler.
 *
 * Soru sekmesi:
 *   - Solda geçmiş: GET /api/assistant/conversations?limit=100 → Bugün / Dün /
 *     Bu hafta / Daha eski (TR takvim günü, `updated_date`); başlıkta arama
 *     (Türkçe katlamalı); sil (onaylı).  Mobilde "Geçmiş" düğmesiyle katman.
 *   - Sağda akış: M108AI.createThread({variant:'page'}); adres çubuğu
 *     `/asistan?c=<id>` ile senkron (yenileyince aynı sohbet açılır).
 *   - Üst çubuktan "Minerva'ya sor" bu sayfada yan panele DEĞİL buraya gelir
 *     (M108AI.setAskHandler).
 *
 * Belge Oku sekmesi: static/belge-oku.js (`M108Doc.mount`, context 'page');
 *   "Bu belge hakkında sor" → `/asistan?doc=<id>` → Soru sekmesinde belge çipi,
 *   İLK soru `document_id` taşır (çıkarım soruya `<belge>` bloğu olarak eklenir;
 *   sonraki sorular aynı sohbetin geçmişinde zaten görür).
 * Metin Yaz sekmesi: şablon seç (Amazon / Shopify açıklaması, etiket, çeviri,
 *   e-posta, WhatsApp) → alanlardan Türkçe istek metni kurulur → `mode:'write'`
 *   akışı (runner.WRITE_MAX_TOKENS; ürün metninde check_product_text).  Her
 *   taslak yeni sohbet; düzeltme isteği aynı akışın kutusundan.
 *
 * Güvenlik: DOM yalnız createElement + textContent; sınıf önekleri `aip-`
 * (şablon CSS'i) ve `m108-ai-` (çekirdek).  `btn-*` adı yok.
 */
(function () {
  'use strict';
  if (window.__aipLoaded) return;
  window.__aipLoaded = true;

  var API = '/api/assistant';
  var TAB = window.AI_TAB || 'soru';

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function bi(name) {
    var i = document.createElement('i');
    i.className = 'bi ' + name;
    i.setAttribute('aria-hidden', 'true');
    return i;
  }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function fold(s) {
    return String(s || '').toLocaleLowerCase('tr')
      .replace(/ı/g, 'i').replace(/ğ/g, 'g').replace(/ü/g, 'u').replace(/ş/g, 's').replace(/ö/g, 'o').replace(/ç/g, 'c');
  }
  function toast(msg, lvl) { if (typeof window.showToast === 'function') window.showToast(msg, lvl || 'error'); }
  // TR takvim günü (sabit UTC+3) — sunucunun `updated_date`'iyle aynı takvim
  function trDay(offsetDays) {
    var d = new Date(Date.now() + 3 * 3600 * 1000 - (offsetDays || 0) * 86400 * 1000);
    return d.getUTCFullYear() + '-' + ('0' + (d.getUTCMonth() + 1)).slice(-2) + '-' + ('0' + d.getUTCDate()).slice(-2);
  }

  // Çıkış (sayfa kendi düğmesini taşır)
  var lo = document.getElementById('aipLogout');
  if (lo) lo.addEventListener('click', function () {
    lo.disabled = true;
    fetch('/api/logout', { method: 'POST', credentials: 'same-origin' })
      .then(function () { location.href = '/login'; })
      .catch(function () { lo.disabled = false; toast('Çıkış yapılamadı.'); });
  });

  if (!window.M108AI) return;           // assistant.js yüklenmediyse sayfa iskelet kalır
  var AI = window.M108AI;

  AI.ready.then(function (boot) {
    if (!boot) {
      var host = document.getElementById('aipThreadHost') || document.getElementById('aipDocPane') ||
                 document.getElementById('aipWritePane');
      if (host) { clear(host); host.appendChild(h('div', 'aip-hempty', 'Minerva Asistan bu hesapta kullanılamıyor.')); }
      return;
    }
    if (TAB === 'soru') initAsk(boot);
    else if (TAB === 'belge') initDoc();
    else if (TAB === 'yaz') initWrite(boot);
  });

  // ─── Soru sekmesi ────────────────────────────────────────────────────────

  function initAsk(boot) {
    var host = document.getElementById('aipThreadHost');
    var list = document.getElementById('aipHistList');
    var q = document.getElementById('aipHistQ');
    var bNew = document.getElementById('aipNew');
    var ask = document.getElementById('aipAsk');
    var toggle = document.getElementById('aipHistToggle');
    if (!host || !list) return;

    var rows = [];
    var current = null;
    var pendingDoc = null;     // ilk soruyla gönderilecek belge id'si
    var chip = null;

    function setUrl(id) {
      var url = '/asistan' + (id ? ('?c=' + encodeURIComponent(id)) : '');
      try { history.replaceState(null, '', url); } catch (_) { /* yok */ }
    }

    var thread = AI.createThread({
      variant: 'page',
      placeholder: 'Bir soru sorun…',
      onConversation: function (id) {
        current = id || null;
        setUrl(current);
        markActive();
      },
      onTurnDone: function () { loadHistory(); },
      extraBody: function () {
        if (!pendingDoc) return {};
        var id = pendingDoc;
        pendingDoc = null;
        if (chip) {
          chip.firstChild.nextSibling.textContent = chip.getAttribute('data-name') + ' — bu sohbete eklendi';
          var xb = chip.querySelector('button');           // gönderildi: geri alınamaz
          if (xb) chip.removeChild(xb);
        }
        return { document_id: id };
      },
      emptyState: function (container, api) {
        var intro = AI.el('section', 'intro');
        intro.appendChild(AI.el('h3', 'intro-title', 'Ne öğrenmek istersiniz?'));
        intro.appendChild(AI.el('div', 'intro-text c-muted',
          'Stok, lot, üretim, sevkiyat, B2B sipariş, fason ve “nasıl yapılır” soruları. Minerva sistem kayıtlarına bakarak cevap verir ve kaynağını gösterir; hiçbir kaydı değiştirmez.'));
        var grid = AI.el('div', 'exgrid exgrid-2');
        AI.examples.forEach(function (ex) {
          var b = AI.el('button', 'ex'); b.type = 'button';
          b.appendChild(AI.el('span', 'extag c-faint', ex.tag));
          b.appendChild(AI.el('span', null, ex.q));
          b.addEventListener('click', function () { api.fill(ex.q); });
          grid.appendChild(b);
        });
        intro.appendChild(grid);
        container.appendChild(intro);
      }
    });
    clear(host);
    host.appendChild(thread.root);

    // Üst çubuktan gelen soru + "Son sorular" bu akışa
    AI.setAskHandler(function (text) {
      if (thread.busy()) {
        thread.setDraft(text);
        thread.notice('Önceki cevap bitince gönderebilirsiniz; sorunuz yazma kutusunda.', 'warn');
        return;
      }
      thread.reset();
      thread.ask(text);
    });
    AI.onOpenConversation = function (id) { openConv(id); };

    function openConv(id) {
      if (thread.busy()) { toast('Cevap bitmeden başka sohbete geçilemez.', 'info'); return; }
      showHistory(false);
      dropSentChip();
      thread.load(id).then(function () { markActive(); });
    }

    // Gönderilmiş belgenin çipi yalnız o sohbete aittir; henüz gönderilmemişse
    // yeni soruda da bekler ("Bu belge hakkında sor" → önce "Yeni soru" olsa da).
    function dropSentChip() {
      if (chip && !pendingDoc) {
        if (chip.parentNode) chip.parentNode.removeChild(chip);
        chip = null;
      }
    }

    function markActive() {
      Array.prototype.forEach.call(list.querySelectorAll('.aip-hitem'), function (n) {
        var on = String(n.getAttribute('data-id')) === String(current);
        n.classList.toggle('is-active', on);
        var b = n.querySelector('.aip-hopen');
        if (b) { if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current'); }
      });
    }

    function groupOf(day) {
      if (!day) return 'Daha eski';
      if (day === trDay(0)) return 'Bugün';
      if (day === trDay(1)) return 'Dün';
      if (day >= trDay(6)) return 'Bu hafta';
      return 'Daha eski';
    }

    function render() {
      clear(list);
      var f = fold(q ? q.value.trim() : '');
      var shown = rows.filter(function (r) { return !f || fold(r.title).indexOf(f) !== -1; });
      if (!shown.length) {
        list.appendChild(h('div', 'aip-hempty', rows.length ? 'Eşleşen sohbet yok.' : 'Henüz soru sorulmadı.'));
        return;
      }
      var lastGroup = null;
      shown.forEach(function (r) {
        var g = groupOf(r.updated_date);
        if (g !== lastGroup) { list.appendChild(h('div', 'aip-hgroup', g)); lastGroup = g; }
        var item = h('div', 'aip-hitem');
        item.setAttribute('role', 'listitem');
        item.setAttribute('data-id', r.id);
        var open = h('button', 'aip-hopen'); open.type = 'button';
        open.appendChild(h('span', 'aip-htitle', r.title || '—'));
        var time = /(\d{1,2}:\d{2})$/.exec(r.updated_at || '');
        var meta = (r.turns || 0) + ' soru' + (time ? ' · ' + (g === 'Bugün' || g === 'Dün' ? time[1] : r.updated_at.slice(0, 5)) : '');
        open.appendChild(h('span', 'aip-hmeta', meta));
        open.addEventListener('click', function () { openConv(r.id); });
        var del = h('button', 'aip-hdel'); del.type = 'button';
        del.setAttribute('aria-label', 'Sohbeti sil: ' + (r.title || ''));
        del.title = 'Sohbeti sil';
        del.appendChild(bi('bi-trash3'));
        del.addEventListener('click', function () { remove(r); });
        item.appendChild(open); item.appendChild(del);
        list.appendChild(item);
      });
      markActive();
    }

    function loadHistory() {
      return fetch(API + '/conversations?limit=100', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) { rows = Array.isArray(d) ? d : []; render(); })
        .catch(function () { clear(list); list.appendChild(h('div', 'aip-hempty', 'Geçmiş yüklenemedi.')); });
    }

    function remove(r) {
      if (!window.confirm('“' + (r.title || 'Sohbet') + '” silinsin mi?')) return;
      fetch(API + '/conversations/' + encodeURIComponent(r.id), { method: 'DELETE', credentials: 'same-origin' })
        .then(function (res) {
          if (!(res.status === 204 || res.status === 404)) return Promise.reject(res.status);
          rows = rows.filter(function (x) { return x.id !== r.id; });
          if (String(current) === String(r.id) && !thread.busy()) { dropSentChip(); thread.reset(); }
          render();
        })
        .catch(function () { toast('Sohbet silinemedi.'); });
    }

    function showHistory(on) {
      if (!ask || !toggle) return;
      ask.classList.toggle('show-history', !!on);
      toggle.setAttribute('aria-expanded', on ? 'true' : 'false');
    }

    if (bNew) bNew.addEventListener('click', function () {
      if (thread.busy()) return;
      dropSentChip();
      thread.reset(); showHistory(false); thread.focus();
    });
    if (q) q.addEventListener('input', render);
    if (toggle) toggle.addEventListener('click', function () { showHistory(!ask.classList.contains('show-history')); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && ask && ask.classList.contains('show-history')) showHistory(false);
    });

    function attachDoc(id) {
      fetch('/api/assistant/documents/' + encodeURIComponent(id), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) {
          pendingDoc = d.id;
          chip = h('div', 'aip-docchip');
          chip.setAttribute('data-name', d.file_name);
          chip.appendChild(bi('bi-paperclip'));
          chip.appendChild(h('span', null, d.file_name + ' (' + (d.doc_type_label || 'Belge') + ') — ilk sorunuzla birlikte gönderilir'));
          var x = h('button', null); x.type = 'button';
          x.setAttribute('aria-label', 'Belgeyi çıkar');
          x.appendChild(bi('bi-x-lg'));
          x.addEventListener('click', function () { pendingDoc = null; if (chip && chip.parentNode) chip.parentNode.removeChild(chip); chip = null; });
          chip.appendChild(x);
          host.insertBefore(chip, thread.root);
          thread.focus();
        })
        .catch(function () { toast('Belge bulunamadı (başka panelde ya da silinmiş olabilir).'); });
    }

    loadHistory();
    var qs = new URLSearchParams(location.search);
    var c = parseInt(qs.get('c') || '', 10);
    var docId = parseInt(qs.get('doc') || '', 10);
    if (c > 0) thread.load(c).then(function () { markActive(); });
    else thread.focus();
    if (docId > 0) attachDoc(docId);
  }

  // ─── Belge Oku sekmesi ───────────────────────────────────────────────────

  function initDoc() {
    var host = document.getElementById('aipDocPane');
    if (!host || !window.M108Doc) return;
    var id = parseInt(new URLSearchParams(location.search).get('id') || '', 10);
    window.M108Doc.mount(host, { context: 'page', docId: id > 0 ? id : null });
  }

  // ─── Metin Yaz sekmesi ───────────────────────────────────────────────────

  var LANGS = ['Türkçe', 'İngilizce', 'Rusça', 'Almanca', 'Arapça', 'Fransızca'];
  var KINDS = [
    { key: 'amazon', label: 'Amazon açıklaması', icon: 'bi-bag', fields: ['product', 'lang', 'inci', 'notes'],
      build: function (v) {
        return 'Amazon ürün metni yaz.\nÜrün: ' + v.product + '\nDil: ' + v.lang + '\nINCI: ' + (v.inci || '(verilmedi)') +
          '\nNotlar: ' + (v.notes || '—') + '\nBiçim: başlık (en çok 200 karakter), 5 madde (her biri en çok 250 karakter), ' +
          'ürün açıklaması (2–3 paragraf), arama terimleri (en çok 250 bayt).';
      } },
    { key: 'shopify', label: 'Web / Shopify metni', icon: 'bi-shop', fields: ['product', 'lang', 'inci', 'notes'],
      build: function (v) {
        return 'Web sitesi (Shopify) ürün metni yaz.\nÜrün: ' + v.product + '\nDil: ' + v.lang + '\nINCI: ' + (v.inci || '(verilmedi)') +
          '\nNotlar: ' + (v.notes || '—') + '\nBiçim: kısa giriş paragrafı, "Öne çıkan faydalar" (3–5 madde), "Kullanım", "Ürün detayları".';
      } },
    { key: 'label', label: 'Etiket metni', icon: 'bi-tag', fields: ['product', 'lang', 'inci', 'notes'],
      build: function (v) {
        return 'Ürün etiketi metni taslağı yaz.\nÜrün: ' + v.product + '\nDil: ' + v.lang + '\nINCI: ' + (v.inci || '(verilmedi)') +
          '\nNotlar: ' + (v.notes || '—') + '\nBiçim: ürün adı, kısa tanım, kullanım talimatı, uyarılar, içindekiler (INCI aynen), ' +
          'net miktar yeri. Bilinmeyen zorunlu bilgiyi uydurma, [boşluk] olarak işaretle.';
      } },
    { key: 'translate', label: 'Çeviri', icon: 'bi-translate', fields: ['target', 'text'],
      build: function (v) {
        return 'Aşağıdaki metni ' + v.target + ' diline çevir. Biçimi koru; marka, ürün ve INCI adlarını çevirme.\n---\n' + v.text;
      } },
    { key: 'email', label: 'Müşteri e-postası', icon: 'bi-envelope', fields: ['lang', 'purpose', 'notes'],
      build: function (v) {
        return 'Müşteriye gönderilecek e-posta taslağı yaz.\nDil: ' + v.lang + '\nKonu / amaç: ' + v.purpose +
          '\nNotlar: ' + (v.notes || '—') + '\nBiçim: "Konu:" satırı + gövde; kibar, kısa, profesyonel.';
      } },
    { key: 'whatsapp', label: 'WhatsApp mesajı', icon: 'bi-chat-dots', fields: ['lang', 'purpose', 'notes'],
      build: function (v) {
        return 'Müşteriye WhatsApp mesajı taslağı yaz.\nDil: ' + v.lang + '\nAmaç: ' + v.purpose +
          '\nNotlar: ' + (v.notes || '—') + '\nBiçim: kısa, sıcak ama profesyonel; en çok 600 karakter.';
      } }
  ];
  var FIELDS = {
    product: { label: 'Ürün', type: 'input', max: 120, ph: 'Sistemdeki ürün adı — ör. Nemlendirici Krem 50 ml', required: true },
    lang: { label: 'Dil', type: 'select', options: LANGS },
    target: { label: 'Hedef dil', type: 'select', options: LANGS.slice(1).concat(['Türkçe']) },
    inci: { label: 'INCI listesi', type: 'textarea', max: 900, ph: 'Aqua, Glycerin, …', hint: 'Boşsa bileşen / koku iddiası yazılmaz.' },
    notes: { label: 'Notlar', type: 'textarea', max: 500, ph: 'Öne çıkan özellikler, hedef kitle, ton…' },
    purpose: { label: 'Konu / amaç', type: 'input', max: 200, ph: 'ör. Siparişiniz kargoya verildi', required: true },
    text: { label: 'Çevrilecek metin', type: 'textarea', max: 1500, ph: 'Metni buraya yapıştırın', required: true, tall: true }
  };

  function initWrite(boot) {
    var pane = document.getElementById('aipWritePane');
    if (!pane) return;
    clear(pane);
    var form = h('section', 'aip-wform');
    form.setAttribute('aria-label', 'Metin türü ve bilgiler');
    form.appendChild(h('h2', 'aip-wtitle', 'Ne yazılsın?'));
    form.appendChild(h('p', 'aip-wsub', 'Taslak üretir; yayınlamadan önce kontrol edin. Ürün metinleri INCI kurallarıyla denetlenir.'));
    var kinds = h('div', 'aip-kinds');
    kinds.setAttribute('role', 'group');
    kinds.setAttribute('aria-label', 'Metin türü');
    var fieldsHost = h('div', null);
    var go = h('button', 'aip-go'); go.type = 'button';
    go.appendChild(bi('bi-pencil-square'));
    go.appendChild(h('span', null, 'Taslak yaz'));
    form.appendChild(kinds); form.appendChild(fieldsHost); form.appendChild(go);

    var threadBox = h('section', null);
    threadBox.id = 'aipWriteThread';
    threadBox.setAttribute('aria-label', 'Taslak');
    pane.appendChild(form); pane.appendChild(threadBox);

    var thread = AI.createThread({
      variant: 'page',
      placeholder: 'Düzeltme isteyin… ör. "daha kısa yap", "İngilizcesini de yaz"',
      extraBody: function () { return { mode: 'write' }; },
      emptyState: function (container) {
        var intro = AI.el('section', 'intro');
        intro.appendChild(AI.el('h3', 'intro-title', 'Taslak burada görünecek'));
        intro.appendChild(AI.el('div', 'intro-text c-muted',
          'Soldan metin türünü seçin, bilgileri girin ve “Taslak yaz”a basın. Ürün bilgisi sistemden okunur; reçete miktarı, maliyet ve tedarikçi müşteri metnine girmez. Cevabın altındaki “Kopyala” ile alın.'));
        container.appendChild(intro);
      }
    });
    threadBox.appendChild(thread.root);

    var kind = KINDS[0];
    var inputs = {};

    function renderFields() {
      clear(fieldsHost);
      inputs = {};
      kind.fields.forEach(function (key) {
        var spec = FIELDS[key];
        var wrap = h('label', 'aip-field');
        wrap.appendChild(h('span', 'aip-flabel', spec.label + (spec.required ? ' *' : '')));
        var input;
        if (spec.type === 'select') {
          input = h('select', 'aip-select');
          spec.options.forEach(function (o) { var op = document.createElement('option'); op.value = o; op.textContent = o; input.appendChild(op); });
        } else if (spec.type === 'textarea') {
          input = h('textarea', 'aip-textarea');
          if (spec.tall) input.style.minHeight = '180px';
        } else {
          input = h('input', 'aip-input');
          input.type = 'text';
        }
        if (spec.max) input.maxLength = spec.max;
        if (spec.ph) input.placeholder = spec.ph;
        wrap.appendChild(input);
        if (spec.hint) wrap.appendChild(h('span', 'aip-fhint', spec.hint));
        fieldsHost.appendChild(wrap);
        inputs[key] = input;
      });
    }
    KINDS.forEach(function (k, i) {
      var b = h('button', 'aip-kind'); b.type = 'button';
      b.setAttribute('aria-pressed', i === 0 ? 'true' : 'false');
      b.appendChild(bi(k.icon));
      b.appendChild(h('span', null, k.label));
      b.addEventListener('click', function () {
        kind = k;
        Array.prototype.forEach.call(kinds.children, function (n) { n.setAttribute('aria-pressed', n === b ? 'true' : 'false'); });
        renderFields();
      });
      kinds.appendChild(b);
    });
    renderFields();

    go.addEventListener('click', function () {
      if (thread.busy()) { toast('Önceki taslak bitmeden yenisi istenemez.', 'info'); return; }
      if (!boot.enabled) { toast(boot.reason || 'Minerva Asistan şu anda kapalı.'); return; }
      var v = {};
      for (var i = 0; i < kind.fields.length; i++) {
        var key = kind.fields[i];
        v[key] = (inputs[key].value || '').trim();
        if (FIELDS[key].required && !v[key]) { toast(FIELDS[key].label + ' boş olamaz.'); inputs[key].focus(); return; }
      }
      var text = kind.build(v);
      var max = boot.max_message_chars || 2000;
      if (text.length > max) { toast('İstek ' + text.length + ' karakter; en çok ' + max + '. Metni kısaltın.'); return; }
      thread.reset();
      thread.ask(text);
    });
  }
})();
