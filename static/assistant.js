/**
 * Minerva108 — Minerva Asistan istemcisi (Claude tabanlı, yalnız okuma)
 * ======================================================================
 *
 * Her iç sayfaya tek satırla yüklenir (scripts/add_assistant_widget.py →
 * idle-watch.js satırının altına):
 *
 *     <script src="/static/assistant.js?v=4" defer></script>
 *
 * 13.10.2026 yeniden tasarım (kullanıcı: "AI slop gibi duruyor"): sağ alttaki
 * yuvarlak düğme + lacivert sohbet balonu KALKTI.  Yerine:
 *
 *   1. ÜST ÇUBUKTA "Ara ya da sor" kutusu (.topnav-title'ın yanına kurulur;
 *      Ctrl/⌘K — /crm'de Ctrl+K CRM aramasına ait, orada yalnız "/").  Yazdıkça
 *      GET /api/assistant/search → Ürünler · Lotlar · Reçeteler · Tedarikçiler
 *      · Sayfalar (YAPAY ZEKÂ ÇAĞRISI YOK, maliyetsiz); en altta daima
 *      "Minerva'ya sor" satırı.  Soru gibi görünen metinde (soru eki, "?",
 *      4+ kelime) Enter doğrudan sorar; Ctrl/⌘+Enter her zaman sorar.
 *      ARIA combobox/listbox, ↑↓ Enter Esc.  Mobilde büyüteç düğmesi → tam
 *      ekran arama katmanı.
 *   2. YAN CEVAP PANELİ (aynı sayfada sağdan): beyaz yüzey, ince kenarlık, solda
 *      altın çizgi + "Minerva" başlığı; soru başlık satırı + belge gibi dizilmiş
 *      cevap (balon yok), tek sessiz etkinlik satırı ("Teslimat kayıtlarına
 *      bakılıyor…"), bitince "Kaynak:" bağlantıları + Kopyala; takip sorusu
 *      kutusu; "Asistanda aç" → /asistan?c=<id>.  Esc kapatır; mobilde tam
 *      ekran (aria-modal + odak tuzağı), masaüstünde sayfayla birlikte kullanılır.
 *   3. window.M108AI — /asistan sayfasının (static/asistan-page.js) paylaştığı
 *      çekirdek: createThread (akış + çizim), markdown, ready (bootstrap).
 *
 * Değişmeyen sözleşmeler:
 *   - Public / kiosk / bayi portalı yollarında (login, /s/, /yorum/, /basvuru,
 *     /pdks-qr, /portal) HİÇ çalışmaz.  /bootstrap 401/403 → DOM'a hiçbir şey
 *     eklenmez.  enabled=false → arama çalışır, "sor" satırı sebebiyle kapalı.
 *   - POST /api/assistant/chat → NDJSON (`meta`, `ping`, `tool`, `token`,
 *     `done`, `error`) ReadableStream ile satır satır; `token`'lar ~80 ms'de
 *     bir yeniden çizilir.  "Durdur" yalnız TARAYICI tarafını keser (sunucu
 *     cevabı sohbete yazar ve bütçeye işler).  404 conversation_not_found →
 *     bir kez yeni sohbetle; 402/409/503 → giriş kapanır + sebep; 429 → süre
 *     notu, metin kutuya geri.
 *   - KAÇIŞ-ÖNCE mini markdown: her şey createElement + textContent; innerHTML
 *     HİÇ yok (araç sonucundan gelen ürün adı/not metni modelden ekrana
 *     düşer — XSS kapısı olmasın).  Bağlantılar yalnız iç yol (isInternalHref).
 *   - Sınıf öneki `m108-ai-` (Bootstrap ile çakışan ad yok; `btn-*` YASAK).
 *     Koyu tema: dark.css div/span/p rengini `inherit`e zorlar (özgüllük
 *     0,2,1) — renk taşıyan öğeler ya div/span DIŞI etiket (section, aside,
 *     header, a, button, label, h2, kbd, time) ya da `c-*` yardımcı sınıfı
 *     (koyu temada :root[data-theme] önekli kopyası var).
 *   - Son kullanıcıya dolar gösterilmez (yalnız sınıra yaklaşınca kibar
 *     uyarı); aylık rakam yalnız finans rolüne (bootstrap.can_see_usage).
 *
 * Durum: sessionStorage `m108ai.open` (panel açık mı) + `m108ai.conv` (panel
 * sohbeti).  Sekme kapanınca sıfırlanır; sohbet geçmişi sunucuda durur.
 */
(function () {
  'use strict';

  // Public / kiosk / bayi portalı: hiç çalışma.  /yorumlar (iç sayfa) ile
  // /yorum/{token} (public) ayrımı için "yorum(/|$)".
  if (/^\/(login|s\/|yorum(\/|$)|basvuru|pdks-qr|portal)/.test(location.pathname)) return;
  if (window.__m108AssistantLoaded) return;      // iki kez enjekte edilirse
  window.__m108AssistantLoaded = true;

  var P = 'm108-ai-';
  var API = '/api/assistant';
  var SS_OPEN = 'm108ai.open';
  var SS_CONV = 'm108ai.conv';
  var RENDER_MS = 80;                      // token akışında yeniden çizim aralığı
  var SEARCH_DELAY = 200;                  // yazarken arama gecikmesi (ms)
  var MOBILE_MQ = '(max-width: 768px)';
  var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  var ON_CRM = /^\/crm(\/|$)/.test(location.pathname);
  var ON_PAGE = /^\/asistan(\/|$)/.test(location.pathname);

  var TOOL_LABELS = {
    find_item: 'Ürünler', item_detail: 'Ürün kartı', find_lot: 'Lot kayıtları',
    find_recipe: 'Reçeteler', recipe_detail: 'Reçete',
    production_summary: 'Üretim özeti', recent_productions: 'Son üretimler',
    find_supplier: 'Tedarikçiler', supplier_detail: 'Tedarikçi kartı',
    stock_gaps: 'Stok eksikleri', expiring_lots: 'SKT yaklaşan lotlar',
    drive_search: 'Dosya Paylaşım', search_help: 'Rehber',
    deliveries: 'Teslimat', product_returns: 'İadeler', b2b_orders: 'B2B siparişleri',
    quotations: 'Teklifler', qc_pending: 'QC bekleyenler', outsourcing_jobs: 'Fason işleri',
    stock_movements: 'Stok hareketleri', consumption: 'Tüketim', month_end_stock: 'Ay sonu stoğu',
    production_capacity: 'Üretim kapasitesi', retention_samples: 'Şahit numune',
    sample_analyses: 'Numune analizi', trendyol_orders: 'Trendyol', shopify_orders: 'Shopify',
    influencer_overview: 'Creator programı', reviews_overview: 'Yorumlar',
    check_product_text: 'Metin denetimi'
  };
  var TOOL_ACTIVITY = {
    find_item: 'Ürün kartları aranıyor', item_detail: 'Ürün kartı okunuyor',
    find_lot: 'Lot kayıtları aranıyor', find_recipe: 'Reçeteler aranıyor', recipe_detail: 'Reçete okunuyor',
    production_summary: 'Üretim kayıtları toplanıyor', recent_productions: 'Son üretimlere bakılıyor',
    find_supplier: 'Tedarikçiler aranıyor', supplier_detail: 'Tedarikçi kartı okunuyor',
    stock_gaps: 'Stok eksiklerine bakılıyor', expiring_lots: 'SKT tarihlerine bakılıyor',
    drive_search: 'Dosyalar aranıyor', search_help: 'Rehbere bakılıyor',
    deliveries: 'Teslimat kayıtlarına bakılıyor', product_returns: 'İade kayıtlarına bakılıyor',
    b2b_orders: 'B2B siparişlerine bakılıyor', quotations: 'Tekliflere bakılıyor',
    qc_pending: 'QC kuyruğuna bakılıyor', outsourcing_jobs: 'Fason işlerine bakılıyor',
    stock_movements: 'Stok hareketleri okunuyor', consumption: 'Tüketim hesaplanıyor',
    month_end_stock: 'Ay sonu stoğu hesaplanıyor', production_capacity: 'Üretim kapasitesi hesaplanıyor',
    retention_samples: 'Şahit numunelere bakılıyor', sample_analyses: 'Numune analizlerine bakılıyor',
    trendyol_orders: 'Trendyol siparişlerine bakılıyor', shopify_orders: 'Shopify siparişlerine bakılıyor',
    influencer_overview: 'Creator programına bakılıyor', reviews_overview: 'Yorumlara bakılıyor',
    check_product_text: 'Metin kurallara göre denetleniyor'
  };
  var ROTATION_LABELS = {
    too_long: 'sohbet uzadı', model_changed: 'model değişti',
    prompt_changed: 'asistan güncellendi', domain_changed: 'panel değişti'
  };
  var EXAMPLES = [
    { tag: 'Stok', q: 'Stearil alkol ne kadar kaldı, hangi lotta?' },
    { tag: 'Sevkiyat', q: 'Kargolanmayı bekleyen gönderiler hangileri?' },
    { tag: 'Üretim', q: 'Bu ay hangi ürünlerden kaç adet ürettik?' },
    { tag: 'Nasıl yapılır', q: 'Numuneyi stoğa nasıl çeviririm?' }
  ];
  var QUESTION_RE = /\?\s*$|^(ne|neden|niye|nasıl|kaç|kim|hangi|nerede|nereye|ne zaman|var mı|mı|mi|mu|mü)\b|\s(mı|mi|mu|mü|mıdır|midir|mudur|müdür)(\s|\?|$)/i;

  var boot = null;            // /bootstrap cevabı
  var readyResolve = null;
  var ready = new Promise(function (res) { readyResolve = res; });
  var askHandler = null;      // /asistan sayfası üst çubuk sorularını kendi akışına alır
  var panel = null;           // yan cevap paneli (tembel kurulur)
  var search = null;          // üst çubuk araması

  // ─── Küçük yardımcılar ──────────────────────────────────────────────────

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls.split(' ').map(function (c) { return P + c; }).join(' ');
    if (text != null) n.textContent = String(text);
    return n;
  }
  function icon(name) {
    var i = document.createElement('i');
    if (/^bi-[a-z0-9-]+$/.test(name || '')) i.className = 'bi ' + name;
    i.setAttribute('aria-hidden', 'true');
    return i;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }
  function ssSet(k, v) { try { if (v == null || v === '') sessionStorage.removeItem(k); else sessionStorage.setItem(k, String(v)); } catch (_) { /* özel mod */ } }
  function fmtUsd(v) { return '$' + (Math.round((Number(v) || 0) * 100) / 100).toFixed(2); }
  function toast(msg, lvl) { if (typeof window.showToast === 'function') window.showToast(msg, lvl || 'error'); }
  function isMobile() { return !!(window.matchMedia && window.matchMedia(MOBILE_MQ).matches); }
  function nowLabel() {              // TR saati (sabit UTC+3) — sunucunun gösterdiğiyle aynı
    var d = new Date(Date.now() + 3 * 3600 * 1000);
    return ('0' + d.getUTCHours()).slice(-2) + ':' + ('0' + d.getUTCMinutes()).slice(-2);
  }
  function timeOf(stamp) {          // "13.10.2026 14:05" → "14:05"
    var m = /(\d{1,2}:\d{2})\s*$/.exec(String(stamp || ''));
    return m ? m[1] : String(stamp || '');
  }
  function looksLikeQuestion(q) {
    q = String(q || '').trim();
    return QUESTION_RE.test(q) || q.split(/\s+/).length >= 4;
  }
  function plainButton(cls, text, iconName) {
    var b = el('button', cls);
    b.type = 'button';
    if (iconName) b.appendChild(icon(iconName));
    if (text) b.appendChild(el('span', null, text));
    return b;
  }
  function copyText(text) {
    text = String(text || '');
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.opacity = '0'; ta.style.top = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      document.body.removeChild(ta);
      if (ok) resolve(); else reject(new Error('copy'));
    });
  }
  // Ham <input>/<textarea> kenarlık ve zemini kutudan gelir — dark.css'in
  // input/textarea için !important kurallarını satır içi !important ezer.
  function bareField(n) {
    n.style.setProperty('border', '0', 'important');
    n.style.setProperty('background', 'transparent', 'important');
    n.style.setProperty('box-shadow', 'none', 'important');
    n.style.setProperty('outline', 'none', 'important');
  }

  // ─── Stil (tek blok; renkler değişkenden → koyu tema) ──────────────────

  function injectStyles() {
    if (document.getElementById(P + 'style')) return;
    var css = [
      ':root{--m108-ai-ink:#1f2937;--m108-ai-muted:#6b7280;--m108-ai-faint:#9aa0ab;--m108-ai-line:#e8e3d9;',
      '--m108-ai-line2:#d8d0c0;--m108-ai-field:#f7f5f0;--m108-ai-surface:#ffffff;--m108-ai-hover:rgba(201,172,111,.15);',
      '--m108-ai-accent:#232E6E;--m108-ai-on-accent:#ffffff;--m108-ai-gold:#C9AC6F;--m108-ai-ring:rgba(201,172,111,.35);',
      '--m108-ai-tint:rgba(35,46,110,.06);--m108-ai-warn-bg:#fbf4e4;--m108-ai-warn:#8a5a00;--m108-ai-bad-bg:#fdecec;--m108-ai-bad:#a61b1b;',
      '--m108-ai-shadow:0 18px 40px rgba(26,36,86,.12),0 2px 6px rgba(26,36,86,.06)}',
      ':root[data-theme="dark"]{--m108-ai-ink:#e2e5ec;--m108-ai-muted:#9aa3ba;--m108-ai-faint:#6b7593;--m108-ai-line:#2a3454;',
      '--m108-ai-line2:#36426a;--m108-ai-field:#151b30;--m108-ai-surface:#1a2138;--m108-ai-hover:rgba(217,190,133,.12);',
      '--m108-ai-accent:#d9be85;--m108-ai-on-accent:#131b40;--m108-ai-gold:#d9be85;--m108-ai-ring:rgba(217,190,133,.32);',
      '--m108-ai-tint:rgba(217,190,133,.10);--m108-ai-warn-bg:rgba(217,190,133,.12);--m108-ai-warn:#e6cfa0;',
      '--m108-ai-bad-bg:rgba(239,68,68,.14);--m108-ai-bad:#fca5a5;--m108-ai-shadow:0 18px 40px rgba(0,0,0,.45)}',

      // Renk yardımcıları (div/span için) + koyu tema özgüllük kopyaları
      '.m108-ai-c-ink{color:var(--m108-ai-ink)}.m108-ai-c-muted{color:var(--m108-ai-muted)}',
      '.m108-ai-c-faint{color:var(--m108-ai-faint)}.m108-ai-c-accent{color:var(--m108-ai-accent)}',
      '.m108-ai-c-warn{color:var(--m108-ai-warn)}.m108-ai-c-bad{color:var(--m108-ai-bad)}',
      ':root[data-theme="dark"] .m108-ai-c-ink{color:var(--m108-ai-ink)}',
      ':root[data-theme="dark"] .m108-ai-c-muted{color:var(--m108-ai-muted)}',
      ':root[data-theme="dark"] .m108-ai-c-faint{color:var(--m108-ai-faint)}',
      ':root[data-theme="dark"] .m108-ai-c-accent{color:var(--m108-ai-accent)}',
      ':root[data-theme="dark"] .m108-ai-c-warn{color:var(--m108-ai-warn)}',
      ':root[data-theme="dark"] .m108-ai-c-bad{color:var(--m108-ai-bad)}',

      // ── Üst çubuk araması ──
      '.m108-ai-search{position:relative;flex:1 1 auto;min-width:200px;max-width:560px;margin:0 1.25rem;font-family:"Jost",system-ui,sans-serif}',
      '.m108-ai-field{display:flex;align-items:center;gap:.55rem;height:40px;padding:0 .7rem 0 .8rem;border:1px solid var(--m108-ai-line);',
      'border-radius:10px;background:var(--m108-ai-field);transition:border-color .15s,box-shadow .15s,background .15s}',
      '.m108-ai-field:hover{border-color:var(--m108-ai-line2)}',
      '.m108-ai-field:focus-within{border-color:var(--m108-ai-gold);background:var(--m108-ai-surface);box-shadow:0 0 0 3px var(--m108-ai-ring)}',
      '.m108-ai-glass{flex:none;width:16px;height:16px;color:var(--m108-ai-muted)}',
      '.m108-ai-unread{position:relative}.m108-ai-unread::after{content:"";position:absolute;top:-2px;right:-3px;width:7px;height:7px;border-radius:50%;background:var(--m108-ai-gold)}',
      '.m108-ai-input{flex:1;min-width:0;height:100%;padding:0;font:400 .9rem/1 "Jost",system-ui,sans-serif;color:var(--m108-ai-ink)}',
      '.m108-ai-input::placeholder{color:var(--m108-ai-faint);opacity:1}',
      '.m108-ai-kbd{flex:none;display:inline-flex;align-items:center;gap:.2rem;padding:.08rem .38rem;border:1px solid var(--m108-ai-line);',
      'border-radius:6px;background:var(--m108-ai-surface);color:var(--m108-ai-muted);font:500 .68rem/1.4 "Jost",system-ui,sans-serif;letter-spacing:.02em;box-shadow:none}',
      '.m108-ai-x{flex:none;display:none;align-items:center;justify-content:center;width:24px;height:24px;border:0;border-radius:6px;',
      'background:transparent;color:var(--m108-ai-muted);cursor:pointer;padding:0}',
      '.m108-ai-x:hover{background:var(--m108-ai-tint);color:var(--m108-ai-ink)}',
      '.m108-ai-has-q .m108-ai-x{display:inline-flex}.m108-ai-has-q .m108-ai-kbd{display:none}',
      '.m108-ai-search-open{display:none;align-items:center;justify-content:center;width:40px;height:40px;border:1px solid var(--m108-ai-line);',
      'border-radius:10px;background:var(--m108-ai-field);color:var(--m108-ai-ink);cursor:pointer;padding:0;flex:none}',
      '.m108-ai-search-back{display:none;border:0;background:transparent;color:var(--m108-ai-ink);width:36px;height:36px;border-radius:8px;cursor:pointer;padding:0;flex:none}',

      // Açılır sonuç listesi
      '.m108-ai-pop{position:absolute;top:calc(100% + 6px);left:0;right:0;min-width:420px;max-height:min(70vh,560px);overflow-y:auto;',
      'background:var(--m108-ai-surface);color:var(--m108-ai-ink);border:1px solid var(--m108-ai-line);border-radius:12px;',
      'box-shadow:var(--m108-ai-shadow);padding:6px;z-index:1060;display:none;text-align:left}',
      '.m108-ai-pop.m108-ai-show{display:block}',
      '.m108-ai-glabel{font-size:.66rem;font-weight:600;letter-spacing:.09em;text-transform:uppercase;padding:.6rem .6rem .3rem}',
      '.m108-ai-opt{display:flex;align-items:center;gap:.65rem;padding:.45rem .6rem;border-radius:8px;cursor:pointer;color:var(--m108-ai-ink);',
      'text-decoration:none;min-height:44px}',
      '.m108-ai-opt:hover{color:var(--m108-ai-ink);text-decoration:none}',
      '.m108-ai-opt.m108-ai-active{background:var(--m108-ai-hover)}',
      '.m108-ai-opt[aria-disabled="true"]{cursor:default;opacity:.7}',
      '.m108-ai-oicon{flex:none;width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;',
      'background:var(--m108-ai-tint);color:var(--m108-ai-accent);font-size:.95rem}',
      '.m108-ai-otext{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}',
      '.m108-ai-otitle{font-size:.86rem;font-weight:500;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.m108-ai-osub{font-size:.74rem;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.m108-ai-ometa{flex:none;font-size:.78rem;font-variant-numeric:tabular-nums;white-space:nowrap}',
      '.m108-ai-ask{margin-top:4px;border-top:1px solid var(--m108-ai-line);border-radius:0 0 8px 8px;padding-top:.55rem}',
      '.m108-ai-ask .m108-ai-oicon{background:var(--m108-ai-accent);color:var(--m108-ai-on-accent)}',
      '.m108-ai-ask .m108-ai-otitle{font-weight:600}',
      '.m108-ai-enter{flex:none;visibility:hidden}.m108-ai-active .m108-ai-enter{visibility:visible}',
      '.m108-ai-pfoot{display:flex;gap:1rem;flex-wrap:wrap;padding:.45rem .6rem .2rem;font-size:.68rem}',
      '.m108-ai-pnote{padding:.6rem .6rem;font-size:.8rem}',

      // ── Yan cevap paneli ──
      '.m108-ai-sheet{position:fixed;top:var(--navbar-height,64px);right:0;bottom:0;width:min(480px,100vw);z-index:1040;',
      'display:flex;flex-direction:column;background:var(--m108-ai-surface);color:var(--m108-ai-ink);border-left:1px solid var(--m108-ai-line);',
      'box-shadow:-18px 0 40px rgba(26,36,86,.08);transform:translateX(102%);visibility:hidden;',
      'transition:transform .2s ease,visibility 0s linear .2s;font-family:"Jost",system-ui,sans-serif}',
      '.m108-ai-sheet.m108-ai-open{transform:translateX(0);visibility:visible;transition:transform .2s ease,visibility 0s}',
      '.m108-ai-shead{display:flex;align-items:center;gap:.6rem;padding:.8rem .9rem .8rem 1.1rem;border-bottom:1px solid var(--m108-ai-line);color:var(--m108-ai-ink)}',
      '.m108-ai-mark{flex:none;width:3px;height:30px;border-radius:2px;background:var(--m108-ai-gold)}',
      '.m108-ai-stitles{flex:1;min-width:0}',
      '.m108-ai-stitle{margin:0;font:600 1rem/1.2 "Jost",system-ui,sans-serif;color:var(--m108-ai-accent);letter-spacing:.01em}',
      '.m108-ai-ssub{font-size:.72rem;line-height:1.35;margin-top:1px}',
      '.m108-ai-hbtn{display:inline-flex;align-items:center;gap:.35rem;height:32px;padding:0 .55rem;border:1px solid transparent;border-radius:8px;',
      'background:transparent;color:var(--m108-ai-muted);font:500 .78rem "Jost",system-ui,sans-serif;cursor:pointer;text-decoration:none;white-space:nowrap}',
      '.m108-ai-hbtn:hover{background:var(--m108-ai-tint);color:var(--m108-ai-ink);text-decoration:none}',
      '.m108-ai-hbtn:disabled{opacity:.45;cursor:not-allowed}',
      // undo-watch.js düğmesi satır içi stilli (right:1.25rem; z-index:9999) → !important ile kaydır
      'body.m108-ai-sheet-on #undoFloatBtn{right:calc(min(480px,100vw) + 1.25rem)!important;transition:right .2s ease}',

      // ── Akış (panel + sayfa) ──
      '.m108-ai-thread{flex:1;min-height:0;display:flex;flex-direction:column;color:var(--m108-ai-ink)}',
      '.m108-ai-scroll{flex:1;min-height:0;overflow-y:auto;padding:1.1rem 1.2rem .6rem;scroll-behavior:smooth}',
      '@media (prefers-reduced-motion:reduce){.m108-ai-scroll{scroll-behavior:auto}.m108-ai-sheet,.m108-ai-field{transition:none}}',
      '.m108-ai-turn{padding:0 0 1.1rem;margin:0 0 1.1rem;border-bottom:1px solid var(--m108-ai-line)}',
      '.m108-ai-turn:last-child{border-bottom:0;margin-bottom:0}',
      '.m108-ai-qmeta{display:flex;align-items:center;gap:.45rem;font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;margin-bottom:.25rem}',
      '.m108-ai-qtext{font-size:.98rem;font-weight:600;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}',
      '.m108-ai-qmore{margin-top:.2rem;font-size:.8rem;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}',
      '.m108-ai-act{display:flex;align-items:center;gap:.5rem;font-size:.8rem;margin:.7rem 0 .2rem}',
      '.m108-ai-spin{flex:none;width:12px;height:12px;border-radius:50%;border:2px solid var(--m108-ai-line2);border-top-color:var(--m108-ai-gold);',
      'animation:m108-ai-rot .8s linear infinite}',
      '@keyframes m108-ai-rot{to{transform:rotate(360deg)}}',
      '@media (prefers-reduced-motion:reduce){.m108-ai-spin{animation:none;border-color:var(--m108-ai-gold)}}',
      '.m108-ai-md{margin-top:.6rem;font-size:.93rem;line-height:1.62;max-width:72ch;overflow-wrap:anywhere}',
      '.m108-ai-md p{margin:0 0 .6rem;white-space:pre-wrap}.m108-ai-md p:last-child{margin-bottom:0}',
      '.m108-ai-md ul,.m108-ai-md ol{margin:0 0 .6rem;padding-left:1.25rem}.m108-ai-md li{margin:.15rem 0;white-space:pre-wrap}',
      '.m108-ai-md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84em;background:var(--m108-ai-tint);padding:.05em .32em;border-radius:4px;color:inherit}',
      '.m108-ai-md pre{margin:0 0 .6rem;padding:.6rem .7rem;border-radius:8px;background:var(--m108-ai-tint);overflow-x:auto;white-space:pre-wrap;',
      'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82em;color:inherit}',
      '.m108-ai-md a{color:var(--m108-ai-accent);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px}',
      '.m108-ai-md strong{font-weight:600}',
      '.m108-ai-md .m108-ai-h{font-weight:600;margin:.3rem 0 .35rem}',
      '.m108-ai-afoot{display:flex;align-items:center;gap:.4rem .75rem;flex-wrap:wrap;margin-top:.75rem;font-size:.76rem}',
      '.m108-ai-src{display:flex;align-items:center;gap:.35rem;flex-wrap:wrap;flex:1;min-width:0}',
      '.m108-ai-src a{color:var(--m108-ai-accent);text-decoration:none;border-bottom:1px solid var(--m108-ai-line2)}',
      '.m108-ai-src a:hover{border-bottom-color:var(--m108-ai-accent)}',
      '.m108-ai-copy{display:inline-flex;align-items:center;gap:.3rem;height:28px;padding:0 .5rem;border:1px solid var(--m108-ai-line);border-radius:7px;',
      'background:var(--m108-ai-surface);color:var(--m108-ai-muted);font:500 .74rem "Jost",system-ui,sans-serif;cursor:pointer}',
      '.m108-ai-copy:hover{color:var(--m108-ai-ink);border-color:var(--m108-ai-line2)}',
      '.m108-ai-cost{font-variant-numeric:tabular-nums}',
      '.m108-ai-err{margin-top:.5rem;font-size:.84rem}',
      '.m108-ai-notice{display:block;margin:0 0 1rem;padding:.55rem .75rem;border:1px solid var(--m108-ai-line);border-left:3px solid var(--m108-ai-gold);',
      'border-radius:8px;font-size:.8rem;line-height:1.45;color:var(--m108-ai-ink);background:var(--m108-ai-field)}',
      '.m108-ai-notice.m108-ai-warn{background:var(--m108-ai-warn-bg);color:var(--m108-ai-warn);border-left-color:var(--m108-ai-warn)}',
      '.m108-ai-notice.m108-ai-bad{background:var(--m108-ai-bad-bg);color:var(--m108-ai-bad);border-left-color:var(--m108-ai-bad)}',
      '.m108-ai-intro{padding:.2rem 0 .4rem}',
      '.m108-ai-intro-title{margin:0 0 .3rem;font:600 1rem/1.3 "Jost",system-ui,sans-serif;color:var(--m108-ai-ink)}',
      '.m108-ai-intro-text{font-size:.84rem;line-height:1.5;margin:0 0 .8rem}',
      '.m108-ai-exgrid{display:grid;grid-template-columns:1fr;gap:.45rem}',
      '.m108-ai-ex{display:flex;flex-direction:column;align-items:flex-start;gap:.15rem;width:100%;padding:.6rem .75rem;border:1px solid var(--m108-ai-line);',
      'border-radius:10px;background:var(--m108-ai-surface);color:var(--m108-ai-ink);font:400 .85rem/1.4 "Jost",system-ui,sans-serif;text-align:left;cursor:pointer}',
      '.m108-ai-ex:hover{border-color:var(--m108-ai-gold);background:var(--m108-ai-field)}',
      '.m108-ai-extag{font-size:.66rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase}',

      // Sayfa düzeni (/asistan): okunur ölçü, ortalanmış akış + yazma kutusu
      '.m108-ai-thread-page .m108-ai-scroll{padding:1.6rem 1.5rem .8rem}',
      '.m108-ai-thread-page .m108-ai-turns,.m108-ai-thread-page .m108-ai-box,.m108-ai-thread-page .m108-ai-hint{max-width:780px;margin-left:auto;margin-right:auto}',
      '.m108-ai-thread-page .m108-ai-composer{padding:.9rem 1.5rem 1rem}',
      '.m108-ai-thread-page .m108-ai-intro{padding-top:1.5rem}',
      '.m108-ai-thread-page .m108-ai-intro-title{font-size:1.15rem}',
      '.m108-ai-exgrid-2{grid-template-columns:repeat(2,minmax(0,1fr))}',
      '@media (max-width:640px){.m108-ai-exgrid-2{grid-template-columns:1fr}.m108-ai-thread-page .m108-ai-scroll{padding:1rem .9rem .6rem}.m108-ai-thread-page .m108-ai-composer{padding:.7rem .8rem .8rem}}',

      // Yazma kutusu
      '.m108-ai-composer{flex:none;padding:.7rem .9rem .75rem;border-top:1px solid var(--m108-ai-line);background:var(--m108-ai-surface)}',
      '.m108-ai-box{display:flex;align-items:flex-end;gap:.5rem;padding:.45rem .45rem .45rem .75rem;border:1px solid var(--m108-ai-line2);',
      'border-radius:12px;background:var(--m108-ai-surface);transition:border-color .15s,box-shadow .15s}',
      '.m108-ai-box:focus-within{border-color:var(--m108-ai-gold);box-shadow:0 0 0 3px var(--m108-ai-ring)}',
      '.m108-ai-ta{flex:1;min-height:24px;max-height:180px;resize:none;padding:.3rem 0;font:400 .92rem/1.45 "Jost",system-ui,sans-serif;color:var(--m108-ai-ink)}',
      '.m108-ai-ta::placeholder{color:var(--m108-ai-faint);opacity:1}',
      '.m108-ai-ta:disabled{cursor:not-allowed}',
      '.m108-ai-send{flex:none;width:34px;height:34px;border:0;border-radius:9px;background:var(--m108-ai-accent);color:var(--m108-ai-on-accent);',
      'display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:1rem;padding:0}',
      '.m108-ai-send:disabled{opacity:.35;cursor:not-allowed}',
      '.m108-ai-send.m108-ai-stop{background:var(--m108-ai-bad);color:#fff}',
      '.m108-ai-hint{display:flex;justify-content:space-between;gap:.75rem;flex-wrap:wrap;margin-top:.4rem;font-size:.7rem}',
      '.m108-ai-count{font-variant-numeric:tabular-nums}',

      '.m108-ai-sr{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}',

      // Ortak odak halkası (klavye)
      '.m108-ai-opt:focus-visible,.m108-ai-hbtn:focus-visible,.m108-ai-copy:focus-visible,.m108-ai-ex:focus-visible,.m108-ai-send:focus-visible,',
      '.m108-ai-x:focus-visible,.m108-ai-search-open:focus-visible,.m108-ai-search-back:focus-visible,.m108-ai-src a:focus-visible,.m108-ai-md a:focus-visible',
      '{outline:2px solid var(--m108-ai-gold);outline-offset:2px}',

      // Dar ekran: kısayol ipucu gizli; mobil: büyüteç + tam ekran arama, panel tam ekran
      '@media (max-width:1180px){.m108-ai-kbd{display:none}.m108-ai-search{max-width:380px;margin:0 .75rem}}',
      '@media (max-width:900px){.m108-ai-search{min-width:160px}.m108-ai-pop{min-width:0}}',
      '@media ' + MOBILE_MQ + '{',
      '.m108-ai-search{display:none;margin:0}.m108-ai-search-open{display:inline-flex}',
      '.m108-ai-search.m108-ai-msearch{display:flex;flex-direction:column;position:fixed;inset:0;z-index:1065;max-width:none;',
      'background:var(--m108-ai-surface);padding:.6rem .7rem;gap:.5rem}',
      '.m108-ai-msearch .m108-ai-row{display:flex;align-items:center;gap:.35rem}',
      '.m108-ai-msearch .m108-ai-search-back{display:inline-flex;align-items:center;justify-content:center}',
      '.m108-ai-msearch .m108-ai-field{flex:1;height:44px}',
      '.m108-ai-msearch .m108-ai-pop{position:static;display:block;flex:1;max-height:none;min-width:0;border:0;box-shadow:none;padding:0}',
      '.m108-ai-sheet{top:0;width:100vw;border-left:0}',
      'body.m108-ai-sheet-on #undoFloatBtn{display:none!important}',
      '.m108-ai-hide-sm{display:none}',
      '.m108-ai-pfoot{display:none}',
      '}',
      '@media print{.m108-ai-search,.m108-ai-search-open,.m108-ai-sheet{display:none!important}}'
    ].join('');
    var st = document.createElement('style');
    st.id = P + 'style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  // ─── Kaçış-önce mini markdown ────────────────────────────────────────────
  // Her şey text node olarak yazılır; yalnız **kalın**, `kod` ve "/" ile
  // başlayan [etiket](/yol) bağlantıları elemente çevrilir.  Dış/protocol-
  // relative linkler (http…, //…) düz metin kalır.

  // Kalın | kod | [etiket](yol).  Her çağrıda taze RegExp: renderInline kalın
  // metin için kendini çağırır, paylaşılan `lastIndex` dış döngüyü bozardı.
  var INLINE_SRC = '(\\*\\*([^*\\n]+?)\\*\\*)|(`([^`\\n]+?)`)|(\\[([^\\]\\n]+?)\\]\\(([^)\\s]+?)\\))';

  /* Yalnız iç bağlantı: "/" ile başlar, ikinci karakter "/" ya da "\\" DEĞİL
     (WHATWG: özel şemalarda "\\" = "/", yani "/\\evil.com" → "https://evil.com")
     ve çözümlenen origin sayfanınkiyle aynı.  Sunucu tarafı tools.is_internal_href
     ile aynı kural. */
  function isInternalHref(href) {
    if (typeof href !== 'string' || !/^\/(?![\/\\])/.test(href) || href.indexOf('\\') !== -1) return false;
    try { return new URL(href, location.origin).origin === location.origin; } catch (_) { return false; }
  }

  function renderInline(text, into) {
    var re = new RegExp(INLINE_SRC, 'g');
    var last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) into.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1]) {
        var b = document.createElement('strong');
        renderInline(m[2], b);
        into.appendChild(b);
      } else if (m[3]) {
        var c = document.createElement('code');
        c.textContent = m[4];
        into.appendChild(c);
      } else {
        var href = m[7];
        if (isInternalHref(href)) {
          var a = document.createElement('a');
          a.href = href;
          a.textContent = m[6];
          into.appendChild(a);
        } else {
          into.appendChild(document.createTextNode(m[0]));
        }
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) into.appendChild(document.createTextNode(text.slice(last)));
  }

  function renderMarkdown(text, container) {
    clear(container);
    var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    var para = [], list = null, pre = null;
    function flushPara() {
      if (!para.length) return;
      var p = document.createElement('p');
      renderInline(para.join('\n'), p);
      container.appendChild(p);
      para = [];
    }
    function flushList() { list = null; }
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      if (pre) {
        if (/^\s*```/.test(ln)) { container.appendChild(pre); pre = null; }
        else pre.textContent += (pre.textContent ? '\n' : '') + ln;
        continue;
      }
      if (/^\s*```/.test(ln)) { flushPara(); flushList(); pre = document.createElement('pre'); continue; }
      var mh = /^\s*#{1,6}\s+(.*)$/.exec(ln);
      if (mh) {
        flushPara(); flushList();
        var h = document.createElement('div');
        h.className = P + 'h';
        renderInline(mh[1], h);
        container.appendChild(h);
        continue;
      }
      var mu = /^\s*(?:[-*•])\s+(.*)$/.exec(ln);
      var mo = /^\s*(\d{1,3})[.)]\s+(.*)$/.exec(ln);
      if (mu || mo) {
        flushPara();
        var tag = mu ? 'ul' : 'ol';
        if (!list || list.tagName.toLowerCase() !== tag) {
          list = document.createElement(tag);
          if (mo) list.start = parseInt(mo[1], 10) || 1;
          container.appendChild(list);
        }
        var li = document.createElement('li');
        renderInline(mu ? mu[1] : mo[2], li);
        list.appendChild(li);
        continue;
      }
      if (!ln.trim()) { flushPara(); flushList(); continue; }
      if (list && /^\s{2,}/.test(ln) && list.lastChild) {   // liste maddesinin devam satırı
        list.lastChild.appendChild(document.createTextNode('\n'));
        renderInline(ln.trim(), list.lastChild);
        continue;
      }
      flushList();
      para.push(ln);
    }
    if (pre) container.appendChild(pre);
    flushPara();
  }

  // ─── Akış (panel ve /asistan sayfası aynı çekirdeği kullanır) ───────────

  /*
   * createThread(opts) → {root, ask, load, reset, focus, setEnabled, busy, convId}
   *   opts.variant      'panel' | 'page'
   *   opts.onConversation(id|null)   sohbet değişince (sessionStorage / geçmiş listesi)
   *   opts.onTurnDone()              tur bitince (geçmiş listesini tazele)
   *   opts.emptyState(container, thread)  boş akış içeriği (yoksa kısa giriş + örnekler)
   *   opts.extraBody()               /chat gövdesine eklenecek alanlar (belge / yazma modu)
   *   opts.placeholder               yazma kutusu ipucu
   */
  function createThread(opts) {
    opts = opts || {};
    var variant = opts.variant || 'panel';
    var convId = null;
    var streaming = false;
    var abortCtl = null;
    var enabled = true;
    var reason = '';
    var displayed = null;     // ekranda turları çizili sohbet

    var root = el('div', 'thread thread-' + variant);
    var scroll = el('div', 'scroll');
    var turns = el('div', 'turns');
    scroll.appendChild(turns);
    var composer = el('div', 'composer');
    var box = el('div', 'box');
    var ta = el('textarea', 'ta');
    ta.rows = 1;
    ta.setAttribute('aria-label', 'Soru');
    bareField(ta);
    var bSend = el('button', 'send'); bSend.type = 'button';
    box.appendChild(ta); box.appendChild(bSend);
    var hint = el('div', 'hint');
    var hintL = el('span', 'c-faint', 'Enter gönderir · Shift+Enter yeni satır');
    var hintR = el('span', 'count c-faint', '');
    hint.appendChild(hintL); hint.appendChild(hintR);
    composer.appendChild(box); composer.appendChild(hint);
    var live = el('div', 'sr');                 // ekran okuyucuya yalnız "cevap hazır"
    live.setAttribute('role', 'status');
    root.appendChild(scroll); root.appendChild(composer); root.appendChild(live);

    function placeholder() { return opts.placeholder || 'Bir soru sorun…'; }
    function setSendIcon(stop) {
      clear(bSend);
      bSend.appendChild(icon(stop ? 'bi-stop-fill' : 'bi-arrow-up'));
      bSend.classList.toggle(P + 'stop', !!stop);
      bSend.title = stop ? 'Durdur' : 'Gönder (Enter)';
      bSend.setAttribute('aria-label', stop ? 'Durdur' : 'Gönder');
    }
    setSendIcon(false);
    ta.placeholder = placeholder();

    function autoGrow() {
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, 180) + 'px';
    }
    function updateCount() {
      var max = (boot && boot.max_message_chars) || 2000;
      var n = ta.value.length;
      hintR.textContent = n > max * 0.8 ? (n + ' / ' + max) : usageText();
      hintR.className = P + 'count ' + P + (n > max ? 'c-bad' : 'c-faint');
    }
    function usageText() {
      if (!boot || !boot.can_see_usage || !boot.month) return '';
      return 'Bu ay ' + fmtUsd(boot.month.spent_usd) + ' / ' + fmtUsd(boot.month.cap_usd);
    }
    function nearBottom() { return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 90; }
    function scrollDown(force) { if (force || nearBottom()) scroll.scrollTop = scroll.scrollHeight; }

    function setEnabled(on, why) {
      enabled = !!on;
      reason = why || '';
      ta.disabled = !enabled || streaming;
      bSend.disabled = !enabled && !streaming;
      ta.placeholder = enabled ? placeholder() : (reason || 'Minerva Asistan şu anda kapalı.');
    }
    function setStreaming(on) {
      streaming = on;
      setSendIcon(on);
      bSend.disabled = !on && !enabled;
      ta.disabled = on || !enabled;
      if (opts.onBusy) opts.onBusy(on);
    }

    function addNotice(text, level) {
      var n = el('section', 'notice' + (level ? ' ' + level : ''));
      n.setAttribute('role', level === 'bad' ? 'alert' : 'status');
      n.textContent = text;
      turns.appendChild(n);
      scrollDown(true);
      return n;
    }

    function showEmpty() {
      clear(turns);
      if (typeof opts.emptyState === 'function') {
        opts.emptyState(turns, api);
      } else {
        var intro = el('section', 'intro');
        intro.appendChild(el('h3', 'intro-title', 'Ne öğrenmek istersiniz?'));
        intro.appendChild(el('div', 'intro-text c-muted',
          'Stok, lot, üretim, sevkiyat, sipariş ve “nasıl yapılır” soruları. Sistem kayıtlarına bakarak cevap verir, kayıt değiştirmez.'));
        var grid = el('div', 'exgrid');
        EXAMPLES.slice(0, 3).forEach(function (ex) {
          var b = el('button', 'ex'); b.type = 'button';
          b.appendChild(el('span', 'extag c-faint', ex.tag));
          b.appendChild(el('span', null, ex.q));
          b.addEventListener('click', function () { fill(ex.q); });
          grid.appendChild(b);
        });
        intro.appendChild(grid);
        turns.appendChild(intro);
      }
      if (!enabled) addNotice(reason || 'Minerva Asistan şu anda kapalı.', 'warn');
      if (boot && boot.warning) addNotice(boot.warning, 'warn');
    }

    function fill(text) {
      if (streaming || ta.disabled) return;
      ta.value = text; autoGrow(); updateCount();
      try { ta.focus(); } catch (_) { /* yok */ }
    }

    function newTurn(question, when) {
      var intro = turns.querySelector('.' + P + 'intro');
      if (intro) clear(turns);
      var turn = el('article', 'turn');
      var q = el('header', 'q');
      var qmeta = el('div', 'qmeta c-faint');
      qmeta.appendChild(el('span', null, 'Soru'));
      var t = document.createElement('time');
      t.textContent = when || nowLabel();
      qmeta.appendChild(t);
      q.appendChild(qmeta);
      // Çok satırlı istek (Metin Yaz formu, Shift+Enter): ilk satır başlık,
      // kalanı küçük gri ayrıntı — altı satırlık kalın blok cevabı ezmesin.
      var qs = String(question == null ? '' : question);
      var nl = qs.indexOf('\n');
      q.appendChild(el('div', 'qtext c-ink', nl === -1 ? qs : qs.slice(0, nl)));
      var rest = nl === -1 ? '' : qs.slice(nl + 1).replace(/^\s+|\s+$/g, '');
      if (rest) q.appendChild(el('div', 'qmore c-muted', rest));
      var a = el('section', 'a');
      var act = el('div', 'act c-muted'); act.style.display = 'none';
      var md = el('div', 'md');
      var foot = el('footer', 'afoot c-muted'); foot.style.display = 'none';
      a.appendChild(act); a.appendChild(md); a.appendChild(foot);
      turn.appendChild(q); turn.appendChild(a);
      turns.appendChild(turn);
      scrollDown(true);
      return { turn: turn, act: act, md: md, foot: foot, text: '', tools: [] };
    }

    function setActivity(t, text) {
      clear(t.act);
      if (!text) { t.act.style.display = 'none'; return; }
      var sp = el('span', 'spin'); sp.setAttribute('aria-hidden', 'true');
      t.act.appendChild(sp);
      t.act.appendChild(el('span', null, text + '…'));
      t.act.style.display = '';
      live.textContent = '';
    }

    function renderFoot(t, sources, cost) {
      clear(t.foot);
      var src = el('div', 'src');
      var seen = {}, n = 0;
      (Array.isArray(sources) ? sources : []).forEach(function (s) {
        if (!s || !isInternalHref(s.href) || seen[s.href]) return;
        seen[s.href] = true;
        if (!n) src.appendChild(el('span', 'c-faint', 'Kaynak:'));
        var a = document.createElement('a');
        a.href = s.href; a.textContent = s.label || s.href;
        src.appendChild(a);
        n++;
      });
      if (!n && t.tools.length) {
        var names = [];
        t.tools.forEach(function (x) { var l = TOOL_LABELS[x.name] || x.name; if (names.indexOf(l) < 0) names.push(l); });
        src.appendChild(el('span', 'c-faint', 'Bakılan: ' + names.join(', ')));
      }
      t.foot.appendChild(src);
      if (cost) t.foot.appendChild(el('span', 'cost c-faint', cost));
      if (t.text && t.text.trim()) {
        var cp = el('button', 'copy'); cp.type = 'button';
        cp.appendChild(icon('bi-clipboard')); cp.appendChild(el('span', null, 'Kopyala'));
        cp.setAttribute('aria-label', 'Cevabı kopyala');
        cp.addEventListener('click', function () {
          copyText(t.md.innerText || t.text).then(function () {
            clear(cp); cp.appendChild(icon('bi-check2')); cp.appendChild(el('span', null, 'Kopyalandı'));
            setTimeout(function () { clear(cp); cp.appendChild(icon('bi-clipboard')); cp.appendChild(el('span', null, 'Kopyala')); }, 1600);
          }, function () { toast('Kopyalanamadı.', 'error'); });
        });
        t.foot.appendChild(cp);
      }
      t.foot.style.display = (n || t.tools.length || t.text) ? '' : 'none';
    }

    function load(id) {
      if (streaming) return Promise.resolve(false);
      return fetch(API + '/conversations/' + encodeURIComponent(id), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) {
          convId = d.id;
          displayed = d.id;
          if (opts.onConversation) opts.onConversation(convId);
          clear(turns);
          // Bir tur sunucuda birden çok asistan satırı olabilir (araç çağrısı
          // satırı + metin satırı); canlı akışta tek blok → burada da birleşir.
          var cur = null, links = [];
          (d.messages || []).forEach(function (m) {
            if (m.role === 'user') {
              if (cur) renderFoot(cur, links);
              cur = newTurn(m.text || '', timeOf(m.created_at)); links = [];
              return;
            }
            if (!cur) { cur = newTurn('—', timeOf(m.created_at)); links = []; }
            (m.tools || []).forEach(function (x) {
              cur.tools.push({ name: x.name });
              (x.links || []).forEach(function (l) { links.push(l); });
            });
            if (m.text && m.text.trim()) {
              cur.text = cur.text ? cur.text + '\n\n' + m.text : m.text;
              renderMarkdown(cur.text, cur.md);
            }
          });
          if (cur) renderFoot(cur, links);
          if (!(d.messages || []).length) showEmpty();
          if (!enabled) addNotice(reason || 'Minerva Asistan şu anda kapalı.', 'warn');
          scrollDown(true);
          return true;
        })
        .catch(function (st) {
          convId = null;
          displayed = null;
          if (opts.onConversation) opts.onConversation(null);
          showEmpty();
          addNotice(st === 404 ? 'Sohbet bulunamadı.' : 'Sohbet yüklenemedi.', st === 404 ? 'warn' : 'bad');
          return false;
        });
    }

    function reset() {
      if (streaming) return;
      convId = null;
      displayed = null;
      if (opts.onConversation) opts.onConversation(null);
      showEmpty();
    }

    function stop() { if (abortCtl) { try { abortCtl.abort(); } catch (_) { /* yok */ } } }

    function ask(text, isRetry) {
      if (streaming || !boot) return false;
      if (!enabled) { addNotice(reason || 'Minerva Asistan şu anda kapalı.', 'warn'); return false; }
      text = String(text || '').trim();
      if (!text) return false;
      var max = boot.max_message_chars || 2000;
      if (text.length > max) { addNotice('Mesaj en fazla ' + max + ' karakter olabilir.', 'warn'); return false; }

      var t = newTurn(text);
      setActivity(t, 'Düşünüyor');
      setStreaming(true);
      abortCtl = (typeof AbortController === 'function') ? new AbortController() : null;

      var body = { message: text };
      if (convId) body.conversation_id = convId;
      if (typeof opts.extraBody === 'function') {
        var extra = opts.extraBody() || {};
        Object.keys(extra).forEach(function (k) { body[k] = extra[k]; });
      }
      var gotTerminal = false;
      var renderTimer = null;
      var running = {};       // araç adı → sayaç (paralel çağrı)

      function scheduleRender() {
        if (renderTimer) return;
        renderTimer = setTimeout(function () {
          renderTimer = null;
          renderMarkdown(t.text, t.md);
          scrollDown(false);
        }, RENDER_MS);
      }
      function finish() {
        if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
        setActivity(t, '');
        setStreaming(false);
        abortCtl = null;
        if (opts.onTurnDone) opts.onTurnDone();
        try { if (variant === 'page' || (panel && panel.isOpen() && !isMobile())) ta.focus(); } catch (_) { /* yok */ }
      }
      function errorLine(msg) { t.md.appendChild(el('div', 'err c-bad', msg)); }

      function handle(ev) {
        if (!ev || typeof ev !== 'object') return;
        switch (ev.t) {
          case 'meta':
            if (ev.conversation_id) {
              convId = ev.conversation_id;
              displayed = convId;
              if (opts.onConversation) opts.onConversation(convId);
            }
            if (ev.rotated) {
              var why = ROTATION_LABELS[ev.rotation_reason] || 'uzun sohbet';
              var n = el('section', 'notice');
              n.textContent = 'Yeni sohbet başlatıldı (' + why + '); önceki sohbetin kısa özeti taşındı.';
              turns.insertBefore(n, t.turn);
            }
            break;
          case 'ping':
            break;
          case 'tool':
            if (ev.status === 'start') {
              running[ev.name] = (running[ev.name] || 0) + 1;
              if (!t.text) setActivity(t, TOOL_ACTIVITY[ev.name] || 'Kayıtlara bakılıyor');
            } else {
              running[ev.name] = Math.max(0, (running[ev.name] || 1) - 1);
              t.tools.push({ name: ev.name, summary: ev.summary, error: ev.status === 'error' });
              if (!t.text) setActivity(t, 'Cevap hazırlanıyor');
            }
            break;
          case 'token':
            if (!t.text) setActivity(t, '');
            t.text += (ev.v || '');
            scheduleRender();
            break;
          case 'done':
            gotTerminal = true;
            if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
            renderMarkdown(t.text || (ev.truncated ? '' : '(boş yanıt)'), t.md);
            if (ev.truncated) errorLine('Yanıt uzunluk sınırında kesildi; soruyu daraltıp tekrar sorabilirsiniz.');
            var cost = '';
            if (typeof ev.cost_usd === 'number') {
              if (boot.daily_remaining_usd != null) boot.daily_remaining_usd = Math.max(0, boot.daily_remaining_usd - ev.cost_usd);
              if (boot.can_see_usage) {
                if (boot.month) {
                  boot.month.spent_usd = (Number(boot.month.spent_usd) || 0) + ev.cost_usd;
                  boot.month.pct = boot.month.cap_usd > 0 ? Math.round(100 * boot.month.spent_usd / boot.month.cap_usd) : 0;
                }
                cost = '≈ ' + fmtUsd(ev.cost_usd);
              }
              updateCount();
              lowBudgetNotice();
            }
            renderFoot(t, ev.sources, cost);
            live.textContent = 'Minerva cevap verdi.';
            scrollDown(false);
            break;
          case 'error':
            gotTerminal = true;
            if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
            if (t.text) renderMarkdown(t.text, t.md);
            errorLine(ev.msg || 'Asistan hatası.');
            renderFoot(t, [], '');
            scrollDown(false);
            break;
          default:
            break;
        }
      }

      function readStream(res) {
        var reader = res.body.getReader();
        var dec = new TextDecoder('utf-8');
        var buf = '';
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) {
              buf += dec.decode();
              if (buf.trim()) { try { handle(JSON.parse(buf.trim())); } catch (_) { /* yarım satır */ } }
              return;
            }
            buf += dec.decode(r.value, { stream: true });
            var i;
            while ((i = buf.indexOf('\n')) >= 0) {
              var line = buf.slice(0, i).trim();
              buf = buf.slice(i + 1);
              if (!line) continue;
              var ev = null;
              try { ev = JSON.parse(line); } catch (_) { ev = null; }
              if (ev) handle(ev);
            }
            return pump();
          });
        }
        return pump();
      }

      fetch(API + '/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/x-ndjson' },
        body: JSON.stringify(body),
        credentials: 'same-origin',
        signal: abortCtl ? abortCtl.signal : undefined
      }).then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            data = data || {};
            if (res.status === 404 && data.code === 'conversation_not_found' && !isRetry) {
              // Sohbet başka sekmede silinmiş: yeni sohbet olarak bir kez daha dene
              convId = null;
              if (opts.onConversation) opts.onConversation(null);
              if (t.turn.parentNode) t.turn.parentNode.removeChild(t.turn);
              finish();
              ask(text, true);
              return;
            }
            var msg = data.detail || ('Hata (' + res.status + ')');
            if (res.status === 429) {
              var s = Number(data.retry_after_seconds);
              msg = (data.detail || 'Çok fazla istek.') + (s > 0 ? ' ' + Math.ceil(s) + ' sn sonra tekrar deneyin.' : '');
              ta.value = text; autoGrow(); updateCount();
            } else if (res.status === 401) {
              msg = 'Oturum süresi dolmuş; yeniden giriş yapın.';
            } else if (res.status === 402 || res.status === 409 || res.status === 503) {
              boot.enabled = false; boot.reason = msg;
            } else if (res.status === 422) {
              msg = 'Mesaj kabul edilmedi (boş ya da çok uzun).';
            }
            if (t.turn.parentNode) t.turn.parentNode.removeChild(t.turn);
            finish();
            addNotice(msg, 'bad');
            if (!boot.enabled) applyEnabled();
          });
        }
        if (!res.body || typeof res.body.getReader !== 'function') {
          // Çok eski tarayıcı: akış okunamıyor — tüm gövdeyi bekle
          return res.text().then(function (txt) {
            txt.split('\n').forEach(function (line) { line = line.trim(); if (!line) return; try { handle(JSON.parse(line)); } catch (_) { /* yok */ } });
            if (!gotTerminal) errorLine('Bağlantı kesildi; tam yanıt sohbet geçmişinde.');
            finish();
          });
        }
        return readStream(res).then(function () {
          if (!gotTerminal) {
            if (t.text) renderMarkdown(t.text, t.md);
            errorLine('Bağlantı kesildi; tam yanıt sohbet geçmişine kaydedilir.');
            renderFoot(t, [], '');
          }
          finish();
        });
      }).catch(function (err) {
        if (err && err.name === 'AbortError') {
          if (t.text) renderMarkdown(t.text, t.md);
          t.md.appendChild(el('div', 'err c-muted', 'Durduruldu. Tam yanıt sohbet geçmişine kaydedilir.'));
        } else {
          if (t.text) renderMarkdown(t.text, t.md);
          errorLine('Bağlantı hatası; biraz sonra tekrar deneyin.');
          if (!t.text) { ta.value = text; autoGrow(); updateCount(); }
        }
        renderFoot(t, [], '');
        finish();
      });
      return true;
    }

    function lowBudgetNotice() {
      if (!boot || boot.daily_remaining_usd == null || !(boot.daily_cap_usd > 0)) return;
      if (boot.daily_remaining_usd <= 0.15 * boot.daily_cap_usd && !root.getAttribute('data-low')) {
        root.setAttribute('data-low', '1');
        addNotice('Bugünkü kullanım sınırınıza yaklaştınız; sınır dolarsa yarın yeniden açılır.', 'warn');
      }
    }

    function applyEnabled() { setEnabled(!!(boot && boot.enabled), boot && boot.reason); }

    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!streaming) send();
      }
    });
    ta.addEventListener('input', function () { autoGrow(); updateCount(); });
    bSend.addEventListener('click', function () { if (streaming) stop(); else send(); });

    function send() {
      var text = ta.value.trim();
      if (!text) return;
      if (ask(text)) { ta.value = ''; autoGrow(); updateCount(); }
    }

    var api = {
      root: root,
      ask: function (text) { return ask(text); },
      fill: fill,
      load: load,
      reset: reset,
      stop: stop,
      notice: addNotice,
      focus: function () { try { ta.focus(); } catch (_) { /* yok */ } },
      busy: function () { return streaming; },
      convId: function () { return convId; },
      displayed: function () { return displayed; },
      setDraft: function (text) { ta.value = String(text || ''); autoGrow(); updateCount(); },
      setConvId: function (id) { convId = id || null; if (opts.onConversation) opts.onConversation(convId); },
      setEnabled: setEnabled,
      applyEnabled: applyEnabled,
      updateCount: updateCount
    };
    applyEnabled();
    updateCount();
    showEmpty();
    return api;
  }

  // ─── Yan cevap paneli ────────────────────────────────────────────────────

  function buildPanel() {
    if (panel) return panel;
    var openedFrom = null;
    var sheet = el('aside', 'sheet');
    sheet.id = P + 'sheet';
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-labelledby', P + 'sheet-title');
    sheet.setAttribute('aria-hidden', 'true');

    var head = el('header', 'shead');
    head.appendChild(el('span', 'mark'));
    var titles = el('div', 'stitles');
    var h = el('h2', 'stitle', 'Minerva');
    h.id = P + 'sheet-title';
    titles.appendChild(h);
    titles.appendChild(el('div', 'ssub c-muted', 'Kayıtlara bakar · kayıt değiştirmez'));
    head.appendChild(titles);
    var bNew = plainButton('hbtn', 'Yeni', 'bi-plus-lg');
    bNew.title = 'Yeni soru (yeni sohbet)';
    var bFull = el('a', 'hbtn');
    bFull.href = '/asistan';
    bFull.title = 'Sohbeti Asistan sayfasında aç';
    bFull.setAttribute('aria-label', 'Asistanda aç');
    bFull.appendChild(icon('bi-arrows-angle-expand'));
    bFull.appendChild(el('span', 'hide-sm', 'Asistanda aç'));
    var bClose = plainButton('hbtn', '', 'bi-x-lg');
    bClose.setAttribute('aria-label', 'Kapat'); bClose.title = 'Kapat (Esc)';
    head.appendChild(bNew); head.appendChild(bFull); head.appendChild(bClose);

    var thread = createThread({
      variant: 'panel',
      placeholder: 'Takip sorusu yazın…',
      onConversation: function (id) {
        ssSet(SS_CONV, id || '');
        bFull.href = id ? ('/asistan?c=' + encodeURIComponent(id)) : '/asistan';
      },
      onBusy: function (on) { bNew.disabled = on; },
      onTurnDone: function () {
        if (search) search.invalidateRecent();
        if (!isOpen() && search) search.markUnread(true);
      }
    });
    sheet.appendChild(head);
    sheet.appendChild(thread.root);
    document.body.appendChild(sheet);

    function isOpen() { return sheet.classList.contains(P + 'open'); }
    function open(focusComposer) {
      if (!isOpen()) openedFrom = document.activeElement;
      sheet.classList.add(P + 'open');
      document.body.classList.add(P + 'sheet-on');
      sheet.setAttribute('aria-hidden', 'false');
      sheet.setAttribute('aria-modal', isMobile() ? 'true' : 'false');
      ssSet(SS_OPEN, '1');
      if (search) search.markUnread(false);
      if (focusComposer !== false && !isMobile()) setTimeout(function () { thread.focus(); }, 220);
      else if (isMobile()) setTimeout(function () { try { bClose.focus(); } catch (_) { /* yok */ } }, 220);
    }
    function close() {
      sheet.classList.remove(P + 'open');
      document.body.classList.remove(P + 'sheet-on');
      sheet.setAttribute('aria-hidden', 'true');
      ssSet(SS_OPEN, '');
      if (openedFrom && typeof openedFrom.focus === 'function' && document.contains(openedFrom)) {
        try { openedFrom.focus(); } catch (_) { /* yok */ }
      }
      openedFrom = null;
    }
    bNew.addEventListener('click', function () { thread.reset(); thread.focus(); });
    bClose.addEventListener('click', close);

    // Esc kapatır; mobilde (tam ekran, aria-modal) odak panelin içinde döner
    sheet.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key !== 'Tab' || !isMobile()) return;
      var f = Array.prototype.filter.call(
        sheet.querySelectorAll('a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])'),
        function (n) { return n.offsetParent !== null; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });

    panel = {
      thread: thread, open: open, close: close, isOpen: isOpen,
      ask: function (q, fresh) {
        open(false);
        if (thread.busy()) {
          // Akış sürerken ikinci soru kaybolmasın: kutuya yaz, cevap bitince Enter
          thread.setDraft(q);
          thread.notice('Önceki cevap bitince gönderebilirsiniz; sorunuz yazma kutusunda.', 'warn');
          return;
        }
        if (fresh) thread.reset();
        thread.ask(q);
      },
      show: function (id) {
        var target = id || thread.convId();
        open();
        if (target && thread.displayed() !== target && !thread.busy()) thread.load(target);
      }
    };
    return panel;
  }

  function askMinerva(q, opts) {
    opts = opts || {};
    if (askHandler) { askHandler(q); return; }
    var p = buildPanel();
    // Panel kapalıyken üst çubuktan gelen soru yeni konu sayılır (kısa geçmiş,
    // ucuz); açık paneldeki sohbet takip sorusu olarak sürer.
    p.ask(q, opts.fresh != null ? opts.fresh : !p.isOpen());
  }

  // ─── Üst çubuk: "Ara ya da sor" ─────────────────────────────────────────

  function buildSearch() {
    var topnav = document.querySelector('.topnav');
    if (!topnav) return null;
    var listId = P + 'list';
    var wrap = el('div', 'search');
    wrap.setAttribute('role', 'search');
    var row = el('div', 'row');
    var back = plainButton('search-back', '', 'bi-arrow-left');
    back.setAttribute('aria-label', 'Aramayı kapat');
    var field = el('label', 'field');
    var glass = icon('bi-search');
    glass.className += ' ' + P + 'glass';
    var input = el('input', 'input');                 // type YOK: dark.css input[type] kuralı uygulanmasın
    input.id = P + 'q';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', listId);
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-label', 'Sistemde ara ya da Minerva’ya sor');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    input.setAttribute('enterkeyhint', 'search');
    input.maxLength = 2000;
    input.placeholder = 'Ara ya da Minerva’ya sor…';
    bareField(input);
    var bx = plainButton('x', '', 'bi-x-lg');
    bx.setAttribute('aria-label', 'Temizle');
    var kbd = el('kbd', 'kbd', ON_CRM ? '/' : (IS_MAC ? '⌘ K' : 'Ctrl K'));
    kbd.setAttribute('aria-hidden', 'true');
    field.appendChild(glass); field.appendChild(input); field.appendChild(bx); field.appendChild(kbd);
    row.appendChild(back); row.appendChild(field);
    var pop = el('section', 'pop');
    pop.id = listId;
    pop.setAttribute('role', 'listbox');
    pop.setAttribute('aria-label', 'Arama sonuçları');
    wrap.appendChild(row); wrap.appendChild(pop);

    var opener = plainButton('search-open', '', 'bi-search');
    opener.setAttribute('aria-label', 'Ara ya da sor');
    var right = topnav.querySelector('.topnav-right');
    topnav.insertBefore(wrap, right || null);
    if (right) right.insertBefore(opener, right.firstChild);
    else topnav.appendChild(opener);

    var options = [];           // {node, run, disabled}
    var active = -1;
    var timer = null;
    var ctl = null;
    var lastQ = null;
    var recent = null;          // son sohbetler (ilk odakta bir kez)
    var seq = 0;

    function isShown() { return pop.classList.contains(P + 'show'); }
    function show() { pop.classList.add(P + 'show'); input.setAttribute('aria-expanded', 'true'); }
    function hide() {
      pop.classList.remove(P + 'show');
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }
    function setActive(i, scrollIt) {
      options.forEach(function (o, k) {
        var on = k === i;
        o.node.classList.toggle(P + 'active', on);
        o.node.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      active = i;
      if (i >= 0 && options[i]) {
        input.setAttribute('aria-activedescendant', options[i].node.id);
        if (scrollIt) options[i].node.scrollIntoView({ block: 'nearest' });
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }
    function move(delta) {
      if (!options.length) return;
      var i = active, n = options.length;
      for (var step = 0; step < n; step++) {
        i = (i + delta + n) % n;
        if (!options[i].disabled) break;
      }
      setActive(i, true);
    }
    function addOption(node, run, disabled) {
      node.id = P + 'opt-' + (++seq);
      node.setAttribute('role', 'option');
      node.setAttribute('aria-selected', 'false');
      if (disabled) node.setAttribute('aria-disabled', 'true');
      var idx = options.length;
      options.push({ node: node, run: run, disabled: !!disabled });
      node.addEventListener('mousemove', function () { if (active !== idx && !disabled) setActive(idx, false); });
      node.addEventListener('mousedown', function (e) { e.preventDefault(); });   // odak kutuda kalsın
      node.addEventListener('click', function (e) {
        if (disabled) { e.preventDefault(); return; }
        if (run) { e.preventDefault(); run(); }
        else closeAll();
      });
      pop.appendChild(node);
    }
    function optionBody(node, iconName, title, sub, meta) {
      var ic = el('span', 'oicon');
      ic.appendChild(icon(iconName));
      var tx = el('span', 'otext');
      tx.appendChild(el('span', 'otitle', title));
      if (sub) tx.appendChild(el('span', 'osub c-muted', sub));
      node.appendChild(ic); node.appendChild(tx);
      if (meta) node.appendChild(el('span', 'ometa c-muted', meta));
    }
    function groupLabel(text) { pop.appendChild(el('div', 'glabel c-faint', text)); }

    function askRow(q) {
      var on = !!(boot && boot.enabled);
      var node = el('div', 'opt ask');
      optionBody(node, 'bi-chat-left-text', on ? 'Minerva’ya sor' : 'Minerva şu anda kapalı',
        on ? '“' + q + '”' : (boot && boot.reason) || '', null);
      if (on) {
        var ent = el('kbd', 'kbd enter', '↵');
        ent.setAttribute('aria-hidden', 'true');
        node.appendChild(ent);
      }
      addOption(node, function () { submitAsk(q); }, !on);
    }

    function footer() {
      var f = el('div', 'pfoot c-faint');
      f.appendChild(el('span', null, '↑↓ gezin'));
      f.appendChild(el('span', null, 'Enter aç'));
      f.appendChild(el('span', null, (IS_MAC ? '⌘' : 'Ctrl') + '+Enter sor'));
      f.appendChild(el('span', null, 'Esc kapat'));
      pop.appendChild(f);
    }

    function renderIdle() {
      clear(pop); options = []; active = -1;
      var cur = panel && panel.thread.convId();
      if (cur && !ON_PAGE) {
        groupLabel('Açık sohbet');
        var n0 = el('div', 'opt');
        optionBody(n0, 'bi-reply', 'Son cevaba dön', 'Yan paneli yeniden açar', null);
        addOption(n0, function () { closeAll(); panel.show(); });
      }
      if (recent && recent.length) {
        groupLabel('Son sorular');
        recent.slice(0, 4).forEach(function (c) {
          var n = el('div', 'opt');
          optionBody(n, 'bi-clock-history', c.title || '—', c.updated_at || '', null);
          addOption(n, function () { closeAll(); openConversation(c.id); });
        });
      }
      if (boot && boot.enabled) {
        groupLabel('Örnek sorular');
        EXAMPLES.forEach(function (ex) {
          var n = el('div', 'opt');
          optionBody(n, 'bi-chat-left-text', ex.q, ex.tag, null);
          addOption(n, function () { submitAsk(ex.q); });
        });
      }
      if (!ON_PAGE) {
        groupLabel('Yapay Zekâ');
        [['Asistan', '/asistan', 'bi-chat-left-text', 'Sohbet geçmişi ve tam sayfa'],
         ['Belge Oku', '/asistan/belge', 'bi-file-earmark-text', 'Fatura, proforma, CoA → form'],
         ['Metin Yaz', '/asistan/yaz', 'bi-pencil-square', 'Ürün açıklaması, çeviri, e-posta']].forEach(function (x) {
          var a = el('a', 'opt');
          a.href = x[1];
          optionBody(a, x[2], x[0], x[3], null);
          addOption(a, null);
        });
      }
      footer();
      show();
    }

    function renderResults(q, data) {
      clear(pop); options = []; active = -1;
      var groups = (data && data.groups) || [];
      var count = 0;
      groups.forEach(function (g) {
        var rows = (g.rows || []).filter(function (r) { return r && isInternalHref(r.href); });
        if (!rows.length) return;
        groupLabel(g.label);
        rows.forEach(function (r) {
          var a = el('a', 'opt');
          a.href = r.href;
          optionBody(a, r.icon || 'bi-arrow-right', r.title, r.sub, r.meta);
          addOption(a, null);
          count++;
        });
      });
      if (!count) pop.appendChild(el('div', 'pnote c-muted', 'Sistemde eşleşen kayıt yok.'));
      askRow(q);
      footer();
      show();
      var askIdx = options.length - 1;
      var preferAsk = looksLikeQuestion(q) || !count;
      setActive(preferAsk && !options[askIdx].disabled ? askIdx : (count ? 0 : -1), false);
    }

    function runSearch(q) {
      if (ctl) { try { ctl.abort(); } catch (_) { /* yok */ } }
      ctl = (typeof AbortController === 'function') ? new AbortController() : null;
      lastQ = q;
      fetch(API + '/search?q=' + encodeURIComponent(q), { credentials: 'same-origin', signal: ctl ? ctl.signal : undefined })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) { if (lastQ === q && input.value.trim() === q) renderResults(q, d); })
        .catch(function (err) {
          if (err && err.name === 'AbortError') return;
          if (input.value.trim() === q) renderResults(q, { groups: [] });
        });
    }

    function onInput() {
      var q = input.value.trim();
      wrap.classList.toggle(P + 'has-q', !!input.value);
      clearTimeout(timer);
      if (q.length < 2) {
        if (ctl) { try { ctl.abort(); } catch (_) { /* yok */ } }
        lastQ = null;
        if (q.length === 0) renderIdle();
        else { clear(pop); options = []; askRow(q); footer(); show(); setActive(0, false); }
        return;
      }
      timer = setTimeout(function () { runSearch(q); }, SEARCH_DELAY);
    }

    function loadRecent() {
      if (recent !== null) return Promise.resolve();
      recent = [];
      return fetch(API + '/conversations?limit=4', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (rows) { recent = Array.isArray(rows) ? rows : []; })
        .catch(function () { recent = []; });
    }

    function submitAsk(q) {
      q = String(q || '').trim();
      if (!q || !(boot && boot.enabled)) return;
      input.value = '';
      wrap.classList.remove(P + 'has-q');
      closeAll(true);
      askMinerva(q);
    }

    function openMobile() {
      wrap.classList.add(P + 'msearch');
      setTimeout(function () { try { input.focus(); } catch (_) { /* yok */ } }, 30);
    }
    function closeAll(keepFocus) {
      clearTimeout(timer);
      hide();
      if (wrap.classList.contains(P + 'msearch')) {
        wrap.classList.remove(P + 'msearch');
        if (!keepFocus) { try { opener.focus(); } catch (_) { /* yok */ } }
      }
      if (!keepFocus) { try { input.blur(); } catch (_) { /* yok */ } }
    }

    input.addEventListener('focus', function () {
      loadRecent().then(function () { if (document.activeElement === input && !input.value.trim()) renderIdle(); });
      if (input.value.trim()) onInput(); else renderIdle();
    });
    input.addEventListener('input', onInput);
    input.addEventListener('keydown', function (e) {
      if (e.isComposing) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); if (!isShown()) onInput(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        var q = input.value.trim();
        if ((e.ctrlKey || e.metaKey) && q) { submitAsk(q); return; }
        if (active >= 0 && options[active] && !options[active].disabled) {
          var o = options[active];
          if (o.run) o.run();
          else if (o.node.href) { closeAll(true); location.href = o.node.href; }
        } else if (q && looksLikeQuestion(q)) {
          submitAsk(q);
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        if (isShown() && !wrap.classList.contains(P + 'msearch')) hide();
        else closeAll();
      }
    });
    input.addEventListener('blur', function () {
      setTimeout(function () {
        if (!wrap.contains(document.activeElement) && !wrap.classList.contains(P + 'msearch')) hide();
      }, 120);
    });
    bx.addEventListener('click', function () { input.value = ''; onInput(); input.focus(); });
    back.addEventListener('click', function () { closeAll(); });
    opener.addEventListener('click', openMobile);
    document.addEventListener('click', function (e) {
      if (isShown() && !wrap.contains(e.target) && !wrap.classList.contains(P + 'msearch')) hide();
    });

    // Kısayol: Ctrl/⌘K (CRM'de Ctrl+K CRM aramasına ait) ve "/" (yazı alanı dışındayken)
    document.addEventListener('keydown', function (e) {
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      var typing = tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable);
      var k = (e.key || '').toLowerCase();
      if (((e.ctrlKey || e.metaKey) && k === 'k' && !ON_CRM) || (k === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey)) {
        if (document.querySelector('.modal.show')) return;      // açık Bootstrap modalı varken dokunma
        e.preventDefault();
        if (isMobile()) openMobile();
        else { input.focus(); input.select(); }
      }
    });

    return {
      invalidateRecent: function () { recent = null; },
      markUnread: function (on) { field.classList.toggle(P + 'unread', !!on); opener.classList.toggle(P + 'unread', !!on); }
    };
  }

  function openConversation(id) {
    if (askHandler && window.M108AI && typeof window.M108AI.onOpenConversation === 'function') {
      window.M108AI.onOpenConversation(id);
      return;
    }
    if (ON_PAGE) { location.href = '/asistan?c=' + encodeURIComponent(id); return; }
    buildPanel().show(id);
  }

  // ─── Açılış ──────────────────────────────────────────────────────────────

  window.M108AI = {
    ready: ready,
    createThread: createThread,
    setAskHandler: function (fn) { askHandler = (typeof fn === 'function') ? fn : null; },
    onOpenConversation: null,
    renderMarkdown: renderMarkdown,
    isInternalHref: isInternalHref,
    el: el, icon: icon, clear: clear, copyText: copyText, fmtUsd: fmtUsd,
    examples: EXAMPLES,
    toolLabel: function (n) { return TOOL_LABELS[n] || n; },
    boot: function () { return boot; }
  };

  function init() {
    fetch(API + '/bootstrap', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || typeof d !== 'object') { readyResolve(null); return; }     // 401/403: DOM yok
        boot = d;
        injectStyles();
        search = buildSearch();
        readyResolve(boot);
        if (ON_PAGE) return;                   // sayfa kendi akışını kurar (asistan-page.js)
        var saved = parseInt(ssGet(SS_CONV) || '', 10);
        if (ssGet(SS_OPEN) === '1') {
          var p = buildPanel();
          p.open(false);
          if (saved > 0) p.thread.load(saved);
        } else if (saved > 0) {
          // Panel kapalı ama açık sohbet var: üst çubuktaki "Son cevaba dön" için id'yi taşı
          buildPanel().thread.setConvId(saved);
        }
      })
      .catch(function () { readyResolve(null); /* ağ yok / bootstrap hatası: sessiz */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
