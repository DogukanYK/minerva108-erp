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
    if (typeof window.__aipInitTab === 'function') window.__aipInitTab(TAB, boot, AI);
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
      thread.load(id).then(function () { markActive(); });
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
          if (String(current) === String(r.id) && !thread.busy()) thread.reset();
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
      thread.reset(); showHistory(false); thread.focus();
    });
    if (q) q.addEventListener('input', render);
    if (toggle) toggle.addEventListener('click', function () { showHistory(!ask.classList.contains('show-history')); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && ask && ask.classList.contains('show-history')) showHistory(false);
    });

    loadHistory();
    var c = parseInt(new URLSearchParams(location.search).get('c') || '', 10);
    if (c > 0) thread.load(c).then(function () { markActive(); });
    else thread.focus();
  }
})();
