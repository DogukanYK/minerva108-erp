/* Ürün yorumları — davet üretimi, liste, moderasyon (dinle/transkript/onay/red).
   Onaylanan yorumun Shopify'a yayını (metaobject + Files) ayrı bir turda eklenecek. */
(function () {
  'use strict';

  var REVIEWS = {};   // id → son yüklenen yorum objesi (moderasyon modalı için)

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

  function inviteLink(token) { return window.location.origin + '/yorum/' + token; }

  function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString('tr-TR', { day: '2-digit', month: '2-digit', year: 'numeric' }); }
    catch (_e) { return iso; }
  }

  function inviteStatusBadge(inv) {
    if (inv.used_at) return '<span class="badge-on">Kullanıldı</span>';
    if (inv.is_expired) return '<span class="badge-off">Süresi geçti</span>';
    return '<span class="badge-warn">Bekliyor</span>';
  }

  async function loadInvites() {
    var body = el('inviteBody');
    try {
      var data = await api('/api/reviews/invites');
      var rows = data.invites || [];
      if (!rows.length) { body.innerHTML = '<tr><td colspan="6" class="muted" style="text-align:center;padding:2rem;">Henüz davet yok.</td></tr>'; return; }
      body.innerHTML = rows.map(function (inv) {
        var kind = inv.kind === 'gifted' ? 'Hediye' : 'Doğrulanmış alıcı';
        var recipient = esc(inv.recipient_name || inv.recipient_email || '—');
        var product = esc(inv.product_title || ('#' + inv.shopify_product_id));
        return '<tr>' +
          '<td><b>' + product + '</b></td>' +
          '<td>' + recipient + '</td>' +
          '<td>' + kind + '</td>' +
          '<td>' + inviteStatusBadge(inv) + '</td>' +
          '<td>' + fmtDate(inv.created_at) + '</td>' +
          '<td style="text-align:right;white-space:nowrap;">' +
            '<button class="btn-sm-soft" onclick="copyInviteLink(\'' + inv.token + '\')"><i class="bi bi-link-45deg"></i> Kopyala</button> ' +
            '<button class="btn-sm-soft" onclick="showQr(' + inv.id + ')"><i class="bi bi-qr-code"></i></button>' +
          '</td></tr>';
      }).join('');
    } catch (e) { body.innerHTML = '<tr><td colspan="6" class="muted" style="text-align:center;padding:2rem;">' + esc(e.message) + '</td></tr>'; }
  }

  async function loadReviews() {
    var body = el('reviewBody');
    try {
      var data = await api('/api/reviews');
      var rows = data.reviews || [];
      if (!rows.length) { body.innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;padding:2rem;">Henüz yorum yok.</td></tr>'; return; }
      var statusLabel = { pending: 'Bekliyor', approved: 'Onaylandı', publishing: 'Yayınlanıyor', published: 'Yayında', rejected: 'Reddedildi', unpublished: 'Kaldırıldı', publish_failed: 'Yayın hatası', deleted: 'Silindi' };
      var canModerate = window.can('reviews', 'moderate');
      REVIEWS = {};
      body.innerHTML = rows.map(function (r) {
        REVIEWS[r.id] = r;
        var stars = '★'.repeat(r.rating || 0) + '☆'.repeat(5 - (r.rating || 0));
        var source = r.source === 'gifted' ? 'Hediye' : 'Doğrulanmış alıcı';
        var canAct = canModerate && (r.status === 'pending' || r.status === 'publish_failed');
        return '<tr>' +
          '<td><b>' + esc(r.product_title || ('#' + r.shopify_product_id)) + '</b></td>' +
          '<td>' + esc(r.author_name) + (r.has_audio ? ' <i class="bi bi-mic-fill muted" title="Sesli not"></i>' : '') + '</td>' +
          '<td>' + stars + '</td>' +
          '<td>' + source + '</td>' +
          '<td>' + (statusLabel[r.status] || esc(r.status)) + '</td>' +
          '<td>' + fmtDate(r.created_at) + '</td>' +
          '<td style="text-align:right;">' +
            (canAct ? '<button class="btn-sm-soft" onclick="openReview(' + r.id + ')"><i class="bi bi-headphones"></i> İncele</button>' : '<span class="muted">—</span>') +
          '</td>' +
          '</tr>';
      }).join('');
    } catch (e) { body.innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;padding:2rem;">' + esc(e.message) + '</td></tr>'; }
  }

  window.copyInviteLink = function (token) {
    var link = inviteLink(token);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(function () { notify('Link kopyalandı.'); }, function () { notify(link, 'info'); });
    } else {
      notify(link, 'info');
    }
  };

  window.showQr = async function (inviteId) {
    try {
      var data = await api('/api/reviews/invites/' + inviteId + '/qr');
      el('qrBox').innerHTML = data.svg;
      el('qrUrl').textContent = data.url;
      el('qrModal').style.display = 'flex';
    } catch (e) { notify(e.message, 'error'); }
  };

  window.openInvite = function () {
    ['fProductId', 'fProductTitle', 'fProductHandle', 'fRecipientName', 'fRecipientEmail'].forEach(function (id) { el(id).value = ''; });
    el('fLocale').value = 'tr';
    el('inviteModal').style.display = 'flex';
  };

  window.createInvite = async function () {
    var productId = parseInt(el('fProductId').value, 10);
    if (!productId || productId <= 0) { notify('Geçerli bir Shopify Ürün ID girin.', 'error'); return; }
    try {
      await api('/api/reviews/invites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'gifted',
          store_key: 'minerva',
          shopify_product_id: productId,
          product_title: el('fProductTitle').value.trim() || null,
          product_handle: el('fProductHandle').value.trim() || null,
          recipient_name: el('fRecipientName').value.trim() || null,
          recipient_email: el('fRecipientEmail').value.trim() || null,
          locale: el('fLocale').value,
        }),
      });
      notify('Davet oluşturuldu.');
      closeModal('inviteModal');
      loadInvites();
    } catch (e) { notify(e.message, 'error'); }
  };

  // ── Moderasyon ────────────────────────────────────────────────────────
  var _revId = null;

  window.openReview = function (id) {
    var r = REVIEWS[id];
    if (!r) return;
    _revId = id;
    el('revModalProduct').textContent = r.product_title || ('#' + r.shopify_product_id);
    var source = r.source === 'gifted' ? 'Hediye' : 'Doğrulanmış alıcı';
    el('revModalMeta').textContent = r.author_name + ' · ' + '★'.repeat(r.rating) + ' · ' + source + ' · ' + (r.locale || 'tr').toUpperCase();

    if (r.has_audio) {
      el('revModalAudioWrap').style.display = 'block';
      el('revModalAudio').src = r.audio_url;
      el('revModalTranscriptReq').textContent = '(zorunlu — sesli not var)';
    } else {
      el('revModalAudioWrap').style.display = 'none';
      el('revModalAudio').removeAttribute('src');
      el('revModalTranscriptReq').textContent = '(opsiyonel)';
    }

    if (r.body) {
      el('revModalBodyWrap').style.display = 'block';
      el('revModalBody').textContent = r.body;
    } else {
      el('revModalBodyWrap').style.display = 'none';
    }

    el('revModalTranscript').value = r.transcript || '';
    el('revRejectBox').style.display = 'none';
    el('revRejectNote').value = '';
    el('reviewModal').style.display = 'flex';
  };

  window.approveReview = async function () {
    if (!_revId) return;
    try {
      await api('/api/reviews/' + _revId + '/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: el('revModalTranscript').value.trim() || null }),
      });
      notify('Yorum onaylandı.');
      closeModal('reviewModal');
      loadReviews();
    } catch (e) { notify(e.message, 'error'); }
  };

  window.openRejectPrompt = function () {
    el('revRejectBox').style.display = 'block';
    el('revRejectNote').focus();
  };

  window.rejectReview = async function () {
    if (!_revId) return;
    var note = el('revRejectNote').value.trim();
    if (!note) { notify('Red gerekçesi zorunlu.', 'error'); return; }
    try {
      await api('/api/reviews/' + _revId + '/reject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ moderation_note: note }),
      });
      notify('Yorum reddedildi.');
      closeModal('reviewModal');
      loadReviews();
    } catch (e) { notify(e.message, 'error'); }
  };

  loadInvites();
  loadReviews();
})();
