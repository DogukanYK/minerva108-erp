/**
 * Minerva108 — Minerva Asistan widget'ı (Claude tabanlı soru-cevap, yalnız okuma)
 * ==============================================================================
 *
 * Her iç sayfaya tek satırla yüklenir (scripts/add_assistant_widget.py →
 * idle-watch.js satırının altına):
 *
 *     <script src="/static/assistant.js?v=1" defer></script>
 *
 * Ne yapar:
 *   1. Public / kiosk / bayi portalı yollarında (login, /s/, /yorum/, /basvuru,
 *      /pdks-qr, /portal) HİÇ çalışmaz — oturum da, yetki de yok.
 *   2. GET /api/assistant/bootstrap → 401/403 ise DOM'a hiçbir şey eklemez
 *      (bayi, kiosk, yetkisi kapatılmış kullanıcı). enabled=false ise düğme +
 *      panel gelir ama giriş alanı kapalıdır, sebep (bütçe doldu / kapalı /
 *      anahtar yok) panelde yazar.
 *   3. Sağ altta yuvarlak düğme (undo düğmesinin ÜSTÜNDE: bottom 5.25rem),
 *      sağdan kayan panel: başlık + model etiketi, "Yeni sohbet", "Sohbetler"
 *      (son 20 sohbet, aç / sil), mesaj listesi (kullanıcı/asistan balonları,
 *      araç çipleri, kaynak bağlantıları), giriş alanı (Enter gönderir,
 *      Shift+Enter satır), "Durdur" (AbortController), günlük kalan satırı ve
 *      finans rolünde aylık satır.
 *   4. POST /api/assistant/chat → NDJSON akışı (`meta`, `ping`, `tool`,
 *      `token`, `done`, `error`) ReadableStream ile satır satır okunur;
 *      `ping` yok sayılır, `token`'lar ~80 ms'de bir yeniden çizilir.
 *   5. Markdown: KAÇIŞ-ÖNCE mini renderer — paragraf, liste, **kalın**, `kod`,
 *      ``` blok, yalnız "/" ile başlayan iç bağlantılar. Her şey
 *      createElement + textContent; innerHTML HİÇ kullanılmaz (araç sonucundan
 *      gelen ürün adı / not metni modele, oradan ekrana düşer — XSS kapısı
 *      olmasın).
 *
 * Durum: sessionStorage `m108ai.open` (panel açık mı) + `m108ai.conv` (aktif
 * sohbet id). Sekme kapanınca sıfırlanır; sohbet geçmişi sunucuda durur.
 *
 * Stil: dosya içi tek <style> bloğu (sınıf öneki `m108-ai-`, Bootstrap ile
 * çakışan ad yok; `btn-*` YASAK — bkz. CLAUDE.md gölgeleme tuzağı). Renkler
 * sayfanın --navy/--gold/--cream/--white/--text/--border değişkenlerinden
 * gelir → koyu tema (static/dark.css) kendiliğinden uygulanır. Mobilde
 * (≤768 px) panel tam genişlik.
 *
 * Bilinçli kararlar:
 *   - "Durdur" yalnız TARAYICI tarafını keser; sunucu model çağrısını bitirir,
 *     cevabı sohbete yazar ve bütçeye işler (routers/assistant.py). Bu yüzden
 *     balonda "tam yanıt sohbet geçmişinde" notu çıkar.
 *   - Paneli kapatmak akışı KESMEZ — kullanıcı başka şeye bakıp dönünce cevap
 *     yerinde olsun; düğmede okunmamış noktası belirir.
 *   - 404 conversation_not_found (sohbet başka sekmede silinmiş) → bir kez
 *     yeni sohbet olarak yeniden gönderilir.
 *   - 402/409/503 → giriş alanı kapanır (sayfa yenilenene kadar); 429 → süre
 *     notuyla uyarı, metin alana geri konur.
 *
 * Bağımlılık yok (toast.js varsa yalnız hata bildirimi için kullanılır).
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
  var MOBILE_MQ = '(max-width: 768px)';

  var TOOL_LABELS = {
    find_item: 'Ürün arama', item_detail: 'Ürün detayı', find_lot: 'Lot arama',
    find_recipe: 'Reçete arama', recipe_detail: 'Reçete detayı',
    production_summary: 'Üretim özeti', recent_productions: 'Son üretimler',
    find_supplier: 'Tedarikçi arama', supplier_detail: 'Tedarikçi detayı',
    stock_gaps: 'Stok eksikleri', expiring_lots: 'SKT yaklaşan lotlar',
    drive_search: 'Drive arama', my_pdks_today: 'PDKS bugün', crm_search: 'CRM arama'
  };
  var ROTATION_LABELS = {
    too_long: 'sohbet uzadı', model_changed: 'model değişti',
    prompt_changed: 'asistan güncellendi', domain_changed: 'panel değişti'
  };
  var EXAMPLES = [
    'Stearil alkol ne kadar kaldı, hangi lotta?',
    'Bu ay kaç şampuan ürettik?',
    'Numuneyi stoğa nasıl çeviririm?',
    'SKT’si yaklaşan lotlar hangileri?'
  ];

  var boot = null;            // /bootstrap cevabı
  var convId = null;          // aktif sohbet id (sunucu)
  var streaming = false;
  var abortCtl = null;
  var dailyRemaining = null;  // yerel tahmin: done.cost_usd düşülür
  var ui = {};                // DOM referansları
  var pendingTools = {};      // name → [chip] (aynı araç paralel çağrılabilir)
  var drawerOpen = false;
  var unread = false;

  // ─── Küçük yardımcılar ──────────────────────────────────────────────────

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls.split(' ').map(function (c) { return P + c; }).join(' ');
    if (text != null) n.textContent = String(text);
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }
  function ssSet(k, v) { try { if (v == null || v === '') sessionStorage.removeItem(k); else sessionStorage.setItem(k, String(v)); } catch (_) { /* özel mod */ } }
  function fmtUsd(v) { return '$' + (Math.round((Number(v) || 0) * 100) / 100).toFixed(2); }
  function toast(msg, lvl) { if (typeof window.showToast === 'function') window.showToast(msg, lvl || 'error'); }
  function isMobile() { return !!(window.matchMedia && window.matchMedia(MOBILE_MQ).matches); }

  function svgIcon(kind) {
    var NS = 'http://www.w3.org/2000/svg';
    var s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('width', '22'); s.setAttribute('height', '22');
    s.setAttribute('fill', 'currentColor'); s.setAttribute('aria-hidden', 'true');
    var p = document.createElementNS(NS, 'path');
    if (kind === 'spark') {
      // dört köşeli yıldız + küçük yıldız (Minerva Asistan simgesi)
      p.setAttribute('d', 'M12 2l1.9 5.6L19.5 9.5l-5.6 1.9L12 17l-1.9-5.6L4.5 9.5l5.6-1.9L12 2zm7 11l.9 2.6 2.6.9-2.6.9L19 20l-.9-2.6-2.6-.9 2.6-.9L19 13z');
    } else if (kind === 'send') {
      p.setAttribute('d', 'M3 11.5L21 3l-8.5 18-2.3-7.2L3 11.5z');
    } else if (kind === 'stop') {
      p.setAttribute('d', 'M6 6h12v12H6z');
    } else if (kind === 'close') {
      p.setAttribute('d', 'M6.2 4.8L12 10.6l5.8-5.8 1.4 1.4-5.8 5.8 5.8 5.8-1.4 1.4-5.8-5.8-5.8 5.8-1.4-1.4 5.8-5.8-5.8-5.8z');
    } else if (kind === 'trash') {
      p.setAttribute('d', 'M9 3h6l1 2h4v2H4V5h4l1-2zm-3 6h12l-1 12H7L6 9zm3 2v8h2v-8H9zm4 0v8h2v-8h-2z');
    }
    s.appendChild(p);
    return s;
  }

  // ─── Stil (tek blok, var() tabanlı → koyu tema otomatik) ───────────────

  function injectStyles() {
    if (document.getElementById(P + 'style')) return;
    var css = [
      '.m108-ai-fab{position:fixed;bottom:5.25rem;right:1.25rem;width:48px;height:48px;border-radius:50%;border:none;',
      'background:var(--navy,#232E6E);color:var(--gold2,#D9BE85);box-shadow:0 6px 16px rgba(0,0,0,.22);z-index:9998;',
      'display:flex;align-items:center;justify-content:center;cursor:pointer;transition:transform .1s,opacity .15s}',
      '.m108-ai-fab:hover{transform:scale(1.06)}',
      '.m108-ai-fab:focus-visible{outline:3px solid var(--gold,#C9AC6F);outline-offset:2px}',
      '.m108-ai-dot{position:absolute;top:4px;right:4px;width:10px;height:10px;border-radius:50%;background:#ef4444;border:2px solid var(--white,#fff);display:none}',
      '.m108-ai-panel{position:fixed;top:0;right:0;height:100%;width:min(440px,100%);z-index:9990;display:flex;flex-direction:column;',
      'background:var(--white,#fff);color:var(--text,#1f2937);border-left:1px solid var(--border,#e5e7eb);box-shadow:-12px 0 32px rgba(0,0,0,.14);',
      'transform:translateX(100%);transition:transform .22s ease;font-family:"Jost",sans-serif;font-size:.92rem;line-height:1.45}',
      '.m108-ai-panel.m108-ai-open{transform:translateX(0)}',
      '.m108-ai-head{display:flex;align-items:center;gap:.5rem;padding:.7rem .85rem;border-bottom:1px solid var(--border,#e5e7eb);background:var(--navy,#232E6E);color:#fff}',
      '.m108-ai-head-title{font-family:"Playfair Display",serif;font-weight:600;font-size:1.05rem;line-height:1.1;color:var(--gold2,#D9BE85)}',
      '.m108-ai-head-sub{font-size:.72rem;opacity:.8;color:#fff;margin-top:2px}',
      '.m108-ai-grow{flex:1;min-width:0}',
      '.m108-ai-hbtn{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.25);color:#fff;border-radius:8px;padding:.3rem .55rem;font-size:.76rem;cursor:pointer;white-space:nowrap;font-family:inherit}',
      '.m108-ai-hbtn:hover{background:rgba(255,255,255,.2)}',
      '.m108-ai-hbtn.m108-ai-icon{padding:.25rem;display:flex;align-items:center}',
      '.m108-ai-body{position:relative;flex:1;min-height:0;display:flex;flex-direction:column}',
      '.m108-ai-msgs{flex:1;min-height:0;overflow-y:auto;padding:.9rem .85rem;display:flex;flex-direction:column;gap:.65rem;background:var(--cream,#f5f0e8)}',
      '.m108-ai-row{display:flex;flex-direction:column;max-width:100%}',
      '.m108-ai-row.m108-ai-user{align-items:flex-end}',
      '.m108-ai-bubble{max-width:92%;padding:.55rem .75rem;border-radius:14px;border:1px solid var(--border,#e5e7eb);background:var(--white,#fff);color:var(--text,#1f2937);overflow-wrap:anywhere;word-break:break-word}',
      '.m108-ai-user .m108-ai-bubble{background:var(--navy,#232E6E);color:#fff;border-color:transparent;border-bottom-right-radius:4px;white-space:pre-wrap}',
      '.m108-ai-assistant .m108-ai-bubble{border-bottom-left-radius:4px}',
      '.m108-ai-time{font-size:.68rem;opacity:.55;margin-top:.15rem;padding:0 .3rem}',
      '.m108-ai-md p{margin:0 0 .45rem;white-space:pre-wrap}.m108-ai-md p:last-child{margin-bottom:0}',
      '.m108-ai-md ul,.m108-ai-md ol{margin:0 0 .45rem;padding-left:1.25rem}.m108-ai-md li{margin:.1rem 0;white-space:pre-wrap}',
      '.m108-ai-md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84em;background:rgba(127,127,127,.14);padding:.05em .3em;border-radius:4px}',
      '.m108-ai-md pre{margin:0 0 .45rem;padding:.5rem .6rem;border-radius:8px;background:rgba(127,127,127,.12);overflow-x:auto;white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82em}',
      '.m108-ai-md a{color:var(--navy,#232E6E);text-decoration:underline;text-underline-offset:2px}',
      ':root[data-theme="dark"] .m108-ai-md a{color:var(--gold2,#D9BE85)}',
      '.m108-ai-md strong{font-weight:600}',
      '.m108-ai-md .m108-ai-h{font-weight:600;margin:.2rem 0 .35rem}',
      '.m108-ai-chips{display:flex;flex-wrap:wrap;gap:.3rem;margin-bottom:.4rem}',
      '.m108-ai-chip{display:inline-flex;align-items:center;gap:.3rem;font-size:.72rem;padding:.15rem .5rem;border-radius:999px;border:1px solid var(--border,#e5e7eb);background:rgba(127,127,127,.08);color:inherit;max-width:100%}',
      '.m108-ai-chip.m108-ai-chip-run{border-style:dashed}',
      '.m108-ai-chip.m108-ai-chip-err{border-color:#ef4444;color:#b91c1c}',
      ':root[data-theme="dark"] .m108-ai-chip.m108-ai-chip-err{color:#fca5a5}',
      '.m108-ai-chip a{color:inherit;text-decoration:underline}',
      '.m108-ai-chip-ms{opacity:.55}',
      '.m108-ai-sources{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.45rem;padding-top:.4rem;border-top:1px dashed var(--border,#e5e7eb)}',
      '.m108-ai-sources a{font-size:.74rem;padding:.12rem .5rem;border-radius:999px;background:var(--navy,#232E6E);color:#fff;text-decoration:none}',
      '.m108-ai-sources a:hover{opacity:.85}',
      '.m108-ai-srclabel{font-size:.7rem;opacity:.6;align-self:center}',
      '.m108-ai-meta{font-size:.68rem;opacity:.6;margin-top:.35rem}',
      '.m108-ai-wait{display:inline-block;opacity:.6;font-style:italic}',
      '.m108-ai-err{color:#b91c1c;font-size:.84rem;margin-top:.3rem}',
      ':root[data-theme="dark"] .m108-ai-err{color:#fca5a5}',
      '.m108-ai-notice{align-self:center;max-width:95%;text-align:center;font-size:.76rem;padding:.3rem .7rem;border-radius:8px;background:rgba(127,127,127,.12);opacity:.9}',
      '.m108-ai-notice.m108-ai-warn{background:#fef3c7;color:#92400e;opacity:1}',
      '.m108-ai-notice.m108-ai-bad{background:#fee2e2;color:#991b1b;opacity:1}',
      '.m108-ai-welcome{padding:.3rem .2rem;font-size:.86rem;opacity:.85}',
      '.m108-ai-examples{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.5rem}',
      '.m108-ai-ex{font-family:inherit;font-size:.76rem;padding:.25rem .6rem;border-radius:999px;border:1px solid var(--border,#e5e7eb);background:var(--white,#fff);color:inherit;cursor:pointer;text-align:left}',
      '.m108-ai-ex:hover{border-color:var(--gold,#C9AC6F)}',
      '.m108-ai-drawer{position:absolute;inset:0;background:var(--white,#fff);display:none;flex-direction:column;z-index:2}',
      '.m108-ai-drawer.m108-ai-open{display:flex}',
      '.m108-ai-drawer-head{display:flex;align-items:center;justify-content:space-between;padding:.6rem .85rem;border-bottom:1px solid var(--border,#e5e7eb);font-weight:600}',
      '.m108-ai-list{flex:1;overflow-y:auto}',
      '.m108-ai-conv{display:flex;align-items:center;gap:.5rem;padding:.55rem .85rem;border-bottom:1px solid var(--border,#e5e7eb);cursor:pointer}',
      '.m108-ai-conv:hover{background:var(--cream,#f5f0e8)}',
      '.m108-ai-conv.m108-ai-active{border-left:3px solid var(--gold,#C9AC6F)}',
      '.m108-ai-conv-title{font-size:.86rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.m108-ai-conv-meta{font-size:.7rem;opacity:.6}',
      '.m108-ai-del{background:none;border:none;color:inherit;opacity:.5;cursor:pointer;padding:.2rem;display:flex}',
      '.m108-ai-del:hover{opacity:1;color:#b91c1c}',
      '.m108-ai-empty{padding:1.2rem .85rem;font-size:.84rem;opacity:.65;text-align:center}',
      '.m108-ai-foot{border-top:1px solid var(--border,#e5e7eb);padding:.55rem .7rem .6rem;background:var(--white,#fff)}',
      '.m108-ai-compose{display:flex;align-items:flex-end;gap:.4rem}',
      '.m108-ai-ta{flex:1;min-height:40px;max-height:160px;resize:none;padding:.5rem .65rem;border-radius:10px;border:1px solid var(--border,#e5e7eb);',
      'background:var(--white,#fff);color:var(--text,#1f2937);font-family:inherit;font-size:.9rem;line-height:1.35;outline:none}',
      '.m108-ai-ta:focus{border-color:var(--gold,#C9AC6F);box-shadow:0 0 0 3px rgba(201,172,111,.25)}',
      '.m108-ai-ta:disabled{opacity:.6;cursor:not-allowed}',
      '.m108-ai-send{width:40px;height:40px;border-radius:10px;border:none;background:var(--navy,#232E6E);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}',
      '.m108-ai-send:disabled{opacity:.4;cursor:not-allowed}',
      '.m108-ai-send.m108-ai-stop{background:#b91c1c}',
      '.m108-ai-lines{display:flex;justify-content:space-between;gap:.5rem;font-size:.7rem;opacity:.65;margin-top:.35rem;flex-wrap:wrap}',
      '.m108-ai-warnline{font-size:.72rem;color:#92400e;background:#fef3c7;padding:.25rem .5rem;border-radius:6px;margin-top:.35rem}',
      '.m108-ai-count{font-variant-numeric:tabular-nums}',
      '.m108-ai-count.m108-ai-over{color:#b91c1c;font-weight:600}',
      '@media ' + MOBILE_MQ + '{.m108-ai-panel{width:100%;border-left:none}.m108-ai-bubble{max-width:96%}}',
      '@media print{.m108-ai-fab,.m108-ai-panel{display:none!important}}'
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

  // ─── DOM kurulumu ────────────────────────────────────────────────────────

  function buildDom() {
    injectStyles();

    // Düğme
    var fab = el('button', 'fab');
    fab.type = 'button';
    fab.id = P + 'fab';
    fab.title = 'Minerva Asistan';
    fab.setAttribute('aria-label', 'Minerva Asistan’ı aç');
    fab.appendChild(svgIcon('spark'));
    var dot = el('span', 'dot');
    fab.appendChild(dot);
    fab.addEventListener('click', function () { togglePanel(); });

    // Panel
    var panel = el('aside', 'panel');
    panel.id = P + 'panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Minerva Asistan');
    panel.setAttribute('aria-hidden', 'true');

    // Başlık
    var head = el('div', 'head');
    var ttlWrap = el('div', 'grow');
    var ttl = el('div', 'head-title', 'Minerva Asistan');
    var sub = el('div', 'head-sub', '');
    ttlWrap.appendChild(ttl); ttlWrap.appendChild(sub);
    var bNew = el('button', 'hbtn', 'Yeni sohbet'); bNew.type = 'button';
    var bList = el('button', 'hbtn', 'Sohbetler'); bList.type = 'button';
    var bClose = el('button', 'hbtn icon'); bClose.type = 'button';
    bClose.setAttribute('aria-label', 'Kapat'); bClose.title = 'Kapat (Esc)';
    bClose.appendChild(svgIcon('close'));
    bNew.addEventListener('click', newConversation);
    bList.addEventListener('click', function () { toggleDrawer(); });
    bClose.addEventListener('click', closePanel);
    head.appendChild(ttlWrap); head.appendChild(bNew); head.appendChild(bList); head.appendChild(bClose);

    // Gövde: mesajlar + sohbet çekmecesi
    var body = el('div', 'body');
    var msgs = el('div', 'msgs');
    var drawer = el('div', 'drawer');
    var dHead = el('div', 'drawer-head');
    dHead.appendChild(el('span', null, 'Sohbetlerim'));
    var dClose = el('button', 'hbtn'); dClose.type = 'button'; dClose.textContent = 'Geri';
    dClose.style.cssText = 'background:var(--navy,#232E6E);border-color:transparent';
    dClose.addEventListener('click', function () { toggleDrawer(false); });
    dHead.appendChild(dClose);
    var list = el('div', 'list');
    drawer.appendChild(dHead); drawer.appendChild(list);
    body.appendChild(msgs); body.appendChild(drawer);

    // Alt: giriş alanı
    var foot = el('div', 'foot');
    var compose = el('div', 'compose');
    var ta = el('textarea', 'ta');
    ta.rows = 1;
    ta.placeholder = 'Sorunuzu yazın… (Enter gönderir, Shift+Enter satır)';
    ta.setAttribute('aria-label', 'Soru');
    var bSend = el('button', 'send'); bSend.type = 'button';
    bSend.setAttribute('aria-label', 'Gönder'); bSend.title = 'Gönder (Enter)';
    bSend.appendChild(svgIcon('send'));
    compose.appendChild(ta); compose.appendChild(bSend);
    var lines = el('div', 'lines');
    var lDaily = el('span', null, '');
    var lCount = el('span', 'count', '');
    lines.appendChild(lDaily); lines.appendChild(lCount);
    var lMonth = el('div', 'lines'); lMonth.style.display = 'none';
    var warn = el('div', 'warnline'); warn.style.display = 'none';
    foot.appendChild(compose); foot.appendChild(lines); foot.appendChild(lMonth); foot.appendChild(warn);

    panel.appendChild(head); panel.appendChild(body); panel.appendChild(foot);
    document.body.appendChild(fab);
    document.body.appendChild(panel);

    ui = { fab: fab, dot: dot, panel: panel, sub: sub, msgs: msgs, drawer: drawer, list: list,
           ta: ta, send: bSend, daily: lDaily, count: lCount, month: lMonth, warn: warn, bNew: bNew, bList: bList };

    // Giriş alanı davranışı
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!streaming) send();
      }
    });
    ta.addEventListener('input', function () { autoGrow(); updateCount(); });
    bSend.addEventListener('click', function () { if (streaming) stop(); else send(); });

    // Esc kapatır (panel açıkken)
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panel.classList.contains(P + 'open')) {
        if (drawerOpen) { toggleDrawer(false); return; }
        closePanel();
      }
    });
  }

  function autoGrow() {
    var ta = ui.ta;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  }

  function updateCount() {
    var max = (boot && boot.max_message_chars) || 2000;
    var n = ui.ta.value.length;
    if (n > max * 0.8) {
      ui.count.textContent = n + ' / ' + max;
      ui.count.classList.toggle(P + 'over', n > max);
    } else {
      ui.count.textContent = '';
      ui.count.classList.remove(P + 'over');
    }
  }

  function updateFooter() {
    if (!boot) return;
    if (boot.daily_remaining_usd != null) {
      ui.daily.textContent = 'Günlük kalan: ' + fmtUsd(dailyRemaining) + ' / ' + fmtUsd(boot.daily_cap_usd);
    } else {
      ui.daily.textContent = '';
    }
    if (boot.can_see_usage && boot.month) {
      clear(ui.month);
      ui.month.appendChild(el('span', null,
        'Bu ay: ' + fmtUsd(boot.month.spent_usd) + ' / ' + fmtUsd(boot.month.cap_usd) + ' (%' + boot.month.pct + ')'));
      ui.month.appendChild(el('span', null, boot.user && boot.user.role_label ? boot.user.role_label : ''));
      ui.month.style.display = '';
    } else {
      ui.month.style.display = 'none';
    }
    if (boot.warning) { ui.warn.textContent = boot.warning; ui.warn.style.display = ''; }
    else ui.warn.style.display = 'none';
  }

  function setComposer(enabled, placeholder) {
    ui.ta.disabled = !enabled;
    ui.send.disabled = !enabled;
    ui.ta.placeholder = placeholder || 'Sorunuzu yazın… (Enter gönderir, Shift+Enter satır)';
  }

  function setStreaming(on) {
    streaming = on;
    ui.send.classList.toggle(P + 'stop', on);
    clear(ui.send);
    ui.send.appendChild(svgIcon(on ? 'stop' : 'send'));
    ui.send.title = on ? 'Durdur' : 'Gönder (Enter)';
    ui.send.setAttribute('aria-label', on ? 'Durdur' : 'Gönder');
    ui.send.disabled = false;
    ui.ta.disabled = on;
    ui.bNew.disabled = on;
  }

  // ─── Panel aç/kapat ──────────────────────────────────────────────────────

  function openPanel() {
    ui.panel.classList.add(P + 'open');
    ui.panel.setAttribute('aria-hidden', 'false');
    ui.fab.style.display = 'none';
    unread = false; ui.dot.style.display = 'none';
    ssSet(SS_OPEN, '1');
    if (!isMobile()) setTimeout(function () { try { ui.ta.focus(); } catch (_) { /* yok */ } }, 230);
  }
  function closePanel() {
    ui.panel.classList.remove(P + 'open');
    ui.panel.setAttribute('aria-hidden', 'true');
    ui.fab.style.display = 'flex';
    ssSet(SS_OPEN, '');
  }
  function togglePanel() {
    if (ui.panel.classList.contains(P + 'open')) closePanel(); else openPanel();
  }

  // ─── Mesaj balonları ─────────────────────────────────────────────────────

  function nearBottom() {
    var m = ui.msgs;
    return m.scrollHeight - m.scrollTop - m.clientHeight < 80;
  }
  function scrollDown(force) {
    if (force || nearBottom()) ui.msgs.scrollTop = ui.msgs.scrollHeight;
  }

  function addNotice(text, level) {
    var n = el('div', 'notice' + (level ? ' ' + level : ''), text);
    ui.msgs.appendChild(n);
    scrollDown(true);
    return n;
  }

  function addUserBubble(text, when) {
    var row = el('div', 'row user');
    var b = el('div', 'bubble', text);
    row.appendChild(b);
    if (when) row.appendChild(el('div', 'time', when));
    ui.msgs.appendChild(row);
    scrollDown(true);
    return row;
  }

  function addAssistantBubble(when) {
    var row = el('div', 'row assistant');
    var b = el('div', 'bubble');
    var chips = el('div', 'chips'); chips.style.display = 'none';
    var md = el('div', 'md');
    var sources = el('div', 'sources'); sources.style.display = 'none';
    var meta = el('div', 'meta'); meta.style.display = 'none';
    b.appendChild(chips); b.appendChild(md); b.appendChild(sources); b.appendChild(meta);
    row.appendChild(b);
    if (when) row.appendChild(el('div', 'time', when));
    ui.msgs.appendChild(row);
    scrollDown(true);
    return { row: row, bubble: b, chips: chips, md: md, sources: sources, meta: meta, text: '' };
  }

  function chipFor(ab, name, status, info) {
    var chip = el('span', 'chip');
    chip.appendChild(el('span', null, TOOL_LABELS[name] || name));
    ab.chips.style.display = '';
    ab.chips.appendChild(chip);
    updateChip(chip, status, info);
    return chip;
  }

  function updateChip(chip, status, info) {
    chip.classList.toggle(P + 'chip-run', status === 'start');
    chip.classList.toggle(P + 'chip-err', status === 'error');
    while (chip.childNodes.length > 1) chip.removeChild(chip.lastChild);
    info = info || {};
    if (status === 'start') {
      chip.appendChild(el('span', 'chip-ms', '…'));
      chip.title = info.args ? JSON.stringify(info.args) : '';
      return;
    }
    if (info.summary) chip.appendChild(el('span', null, '· ' + info.summary));
    var links = Array.isArray(info.links) ? info.links : [];
    for (var i = 0; i < links.length && i < 3; i++) {
      var l = links[i];
      if (!l || !isInternalHref(l.href)) continue;
      var a = document.createElement('a');
      a.href = l.href; a.textContent = l.label || 'Aç';
      chip.appendChild(a);
    }
    if (info.ms != null) chip.appendChild(el('span', 'chip-ms', Math.round(info.ms) + ' ms'));
    if (info.args) chip.title = JSON.stringify(info.args);
  }

  function renderSources(ab, sources) {
    clear(ab.sources);
    var seen = {}, n = 0;
    (Array.isArray(sources) ? sources : []).forEach(function (s) {
      if (!s || !isInternalHref(s.href) || seen[s.href]) return;
      seen[s.href] = true;
      var a = document.createElement('a');
      a.href = s.href; a.textContent = s.label || s.href;
      if (!n) ab.sources.appendChild(el('span', 'srclabel', 'Kaynaklar:'));
      ab.sources.appendChild(a);
      n++;
    });
    ab.sources.style.display = n ? '' : 'none';
  }

  function showWelcome() {
    clear(ui.msgs);
    var w = el('div', 'welcome');
    var name = boot && boot.user && boot.user.name ? boot.user.name.split(' ')[0] : '';
    w.appendChild(el('div', null, 'Merhaba' + (name ? ' ' + name : '') + '. Stok, lot, reçete, tedarikçi, üretim ve ' +
      '"nasıl yapılır" soruları sorabilirsin. Yalnız okurum — kayıt değiştirmem, yolu tarif ederim.'));
    var ex = el('div', 'examples');
    EXAMPLES.forEach(function (q) {
      var b = el('button', 'ex', q); b.type = 'button';
      b.addEventListener('click', function () {
        if (streaming || ui.ta.disabled) return;
        ui.ta.value = q; autoGrow(); updateCount(); ui.ta.focus();
      });
      ex.appendChild(b);
    });
    w.appendChild(ex);
    ui.msgs.appendChild(w);
    if (boot && !boot.enabled) addNotice(boot.reason || 'Minerva Asistan şu anda kapalı.', 'warn');
  }

  // ─── Sohbetler ───────────────────────────────────────────────────────────

  function newConversation() {
    if (streaming) return;
    convId = null;
    ssSet(SS_CONV, '');
    showWelcome();
    toggleDrawer(false);
    try { ui.ta.focus(); } catch (_) { /* yok */ }
  }

  function toggleDrawer(force) {
    drawerOpen = (typeof force === 'boolean') ? force : !drawerOpen;
    ui.drawer.classList.toggle(P + 'open', drawerOpen);
    ui.bList.textContent = drawerOpen ? 'Mesajlar' : 'Sohbetler';
    if (drawerOpen) loadConversations();
  }

  function loadConversations() {
    clear(ui.list);
    ui.list.appendChild(el('div', 'empty', 'Yükleniyor…'));
    fetch(API + '/conversations', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (rows) {
        clear(ui.list);
        if (!rows || !rows.length) { ui.list.appendChild(el('div', 'empty', 'Henüz sohbet yok.')); return; }
        rows.forEach(function (c) {
          var item = el('div', 'conv' + (c.id === convId ? ' active' : ''));
          item.setAttribute('role', 'button'); item.tabIndex = 0;
          var txt = el('div', 'grow');
          txt.appendChild(el('div', 'conv-title', c.title || '—'));
          txt.appendChild(el('div', 'conv-meta', (c.updated_at || '') + ' · ' + (c.turns || 0) + ' soru'));
          var del = el('button', 'del'); del.type = 'button';
          del.title = 'Sohbeti sil'; del.setAttribute('aria-label', 'Sohbeti sil');
          del.appendChild(svgIcon('trash'));
          del.addEventListener('click', function (e) { e.stopPropagation(); deleteConversation(c.id, item); });
          item.appendChild(txt); item.appendChild(del);
          item.addEventListener('click', function () { openConversation(c.id); });
          item.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openConversation(c.id); } });
          ui.list.appendChild(item);
        });
      })
      .catch(function () {
        clear(ui.list);
        ui.list.appendChild(el('div', 'empty', 'Sohbetler yüklenemedi.'));
      });
  }

  function openConversation(id) {
    if (streaming) return;
    fetch(API + '/conversations/' + encodeURIComponent(id), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) {
        convId = d.id;
        ssSet(SS_CONV, d.id);
        clear(ui.msgs);
        // Bir tur sunucuda birden çok asistan satırı olabilir (araç çağrısı
        // satırı + metin satırı); canlı akışta tek balondu → burada da art
        // arda gelen asistan satırları TEK balonda birleşir.
        var ab = null, links = [];
        (d.messages || []).forEach(function (m) {
          if (m.role === 'user') { ab = null; links = []; addUserBubble(m.text || '', m.created_at); return; }
          if (!ab) { ab = addAssistantBubble(m.created_at); links = []; ab.text = ''; }
          (m.tools || []).forEach(function (t) {
            chipFor(ab, t.name, t.is_error ? 'error' : 'done', t);
            (t.links || []).forEach(function (l) { links.push(l); });
          });
          if (m.text && m.text.trim()) {
            ab.text = ab.text ? ab.text + '\n\n' + m.text : m.text;
            renderMarkdown(ab.text, ab.md);
          }
          renderSources(ab, links);
          var tm = ab.row.querySelector('.' + P + 'time');
          if (tm && m.created_at) tm.textContent = m.created_at;
        });
        if (!(d.messages || []).length) addNotice('Bu sohbette mesaj yok.');
        if (boot && !boot.enabled) addNotice(boot.reason || 'Minerva Asistan şu anda kapalı.', 'warn');
        toggleDrawer(false);
        scrollDown(true);
      })
      .catch(function (st) {
        if (st === 404) { convId = null; ssSet(SS_CONV, ''); showWelcome(); addNotice('Sohbet bulunamadı.', 'warn'); toggleDrawer(false); }
        else addNotice('Sohbet yüklenemedi.', 'bad');
      });
  }

  function deleteConversation(id, item) {
    if (!window.confirm('Bu sohbet silinsin mi?')) return;
    fetch(API + '/conversations/' + encodeURIComponent(id), { method: 'DELETE', credentials: 'same-origin' })
      .then(function (r) {
        if (!(r.status === 204 || r.status === 404)) return Promise.reject(r.status);
        if (item && item.parentNode) item.parentNode.removeChild(item);
        if (!ui.list.childNodes.length) ui.list.appendChild(el('div', 'empty', 'Henüz sohbet yok.'));
        if (id === convId) { convId = null; ssSet(SS_CONV, ''); showWelcome(); }
      })
      .catch(function () { toast('Sohbet silinemedi.', 'error'); });
  }

  // ─── Gönderme + NDJSON akışı ─────────────────────────────────────────────

  function stop() {
    if (abortCtl) { try { abortCtl.abort(); } catch (_) { /* yok */ } }
  }

  function send(retryText, isRetry) {
    if (streaming || !boot || !boot.enabled) return;
    var text = (typeof retryText === 'string') ? retryText : ui.ta.value.trim();
    if (!text) return;
    var max = boot.max_message_chars || 2000;
    if (text.length > max) { addNotice('Mesaj en fazla ' + max + ' karakter olabilir.', 'warn'); return; }

    if (!isRetry) {
      // İlk sorudaysa karşılama metnini kaldır
      var w = ui.msgs.querySelector('.' + P + 'welcome');
      if (w) clear(ui.msgs);
      ui.ta.value = ''; autoGrow(); updateCount();
      addUserBubble(text);
    }
    var ab = addAssistantBubble();
    var wait = el('span', 'wait', 'Düşünüyor…');
    ab.md.appendChild(wait);
    pendingTools = {};
    setStreaming(true);
    abortCtl = (typeof AbortController === 'function') ? new AbortController() : null;

    var body = { message: text };
    if (convId) body.conversation_id = convId;
    var gotTerminal = false;
    var renderTimer = null;
    var waitDots = setInterval(function () {
      if (!wait.parentNode) { clearInterval(waitDots); return; }
      wait.textContent = wait.textContent.length >= 12 ? 'Düşünüyor' : wait.textContent + '.';
    }, 450);

    function scheduleRender() {
      if (renderTimer) return;
      renderTimer = setTimeout(function () {
        renderTimer = null;
        renderMarkdown(ab.text, ab.md);
        scrollDown(false);
      }, RENDER_MS);
    }

    function finish() {
      clearInterval(waitDots);
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      if (wait.parentNode) wait.parentNode.removeChild(wait);
      setStreaming(false);
      abortCtl = null;
      if (!ui.panel.classList.contains(P + 'open')) { unread = true; ui.dot.style.display = 'block'; }
      try { if (ui.panel.classList.contains(P + 'open') && !isMobile()) ui.ta.focus(); } catch (_) { /* yok */ }
    }

    function handle(ev) {
      if (!ev || typeof ev !== 'object') return;
      switch (ev.t) {
        case 'meta':
          if (ev.conversation_id) { convId = ev.conversation_id; ssSet(SS_CONV, convId); }
          if (ev.rotated) {
            var why = ROTATION_LABELS[ev.rotation_reason] || 'uzun sohbet';
            var n = el('div', 'notice', 'Yeni sohbet başlatıldı (' + why + '); önceki sohbetin kısa özeti taşındı.');
            ui.msgs.insertBefore(n, ab.row);
          }
          break;
        case 'ping':
          break;
        case 'tool':
          if (ev.status === 'start') {
            var chip = chipFor(ab, ev.name, 'start', ev);
            (pendingTools[ev.name] = pendingTools[ev.name] || []).push(chip);
          } else {
            var q = pendingTools[ev.name] || [];
            var c = q.length ? q.shift() : chipFor(ab, ev.name, ev.status, ev);
            updateChip(c, ev.status, ev);
          }
          scrollDown(false);
          break;
        case 'token':
          if (wait.parentNode) wait.parentNode.removeChild(wait);
          ab.text += (ev.v || '');
          scheduleRender();
          break;
        case 'done':
          gotTerminal = true;
          if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
          if (wait.parentNode) wait.parentNode.removeChild(wait);
          renderMarkdown(ab.text || (ev.truncated ? '' : '(boş yanıt)'), ab.md);
          renderSources(ab, ev.sources);
          if (ev.truncated) ab.md.appendChild(el('div', 'err', 'Yanıt uzunluk sınırında kesildi; soruyu daraltıp tekrar sorabilirsin.'));
          if (typeof ev.cost_usd === 'number') {
            if (dailyRemaining != null) dailyRemaining = Math.max(0, dailyRemaining - ev.cost_usd);
            if (boot.can_see_usage) {
              if (boot.month) { boot.month.spent_usd = (Number(boot.month.spent_usd) || 0) + ev.cost_usd;
                boot.month.pct = boot.month.cap_usd > 0 ? Math.round(100 * boot.month.spent_usd / boot.month.cap_usd) : 0; }
              ab.meta.textContent = '≈ ' + fmtUsd(ev.cost_usd) + ' · ' + (ev.calls || 1) + ' çağrı';
              ab.meta.style.display = '';
            }
            updateFooter();
          }
          scrollDown(false);
          break;
        case 'error':
          gotTerminal = true;
          if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
          if (wait.parentNode) wait.parentNode.removeChild(wait);
          if (ab.text) renderMarkdown(ab.text, ab.md);
          ab.md.appendChild(el('div', 'err', ev.msg || 'Asistan hatası.'));
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
            convId = null; ssSet(SS_CONV, '');
            if (ab.row.parentNode) ab.row.parentNode.removeChild(ab.row);
            finish();
            send(text, true);
            return;
          }
          var msg = data.detail || ('Hata (' + res.status + ')');
          if (res.status === 429) {
            var s = Number(data.retry_after_seconds);
            msg = (data.detail || 'Çok fazla istek.') + (s > 0 ? ' ' + Math.ceil(s) + ' sn sonra tekrar deneyin.' : '');
            ui.ta.value = text; autoGrow(); updateCount();
          } else if (res.status === 401) {
            msg = 'Oturum süresi dolmuş; yeniden giriş yapın.';
          } else if (res.status === 402 || res.status === 409 || res.status === 503) {
            boot.enabled = false; boot.reason = msg;
          } else if (res.status === 422) {
            msg = 'Mesaj kabul edilmedi (boş ya da çok uzun).';
          }
          if (ab.row.parentNode) ab.row.parentNode.removeChild(ab.row);
          addNotice(msg, 'bad');
          finish();
          if (!boot.enabled) setComposer(false, boot.reason);
        });
      }
      if (!res.body || typeof res.body.getReader !== 'function') {
        // Çok eski tarayıcı: akış okunamıyor — tüm gövdeyi bekle
        return res.text().then(function (t) {
          t.split('\n').forEach(function (line) { line = line.trim(); if (!line) return; try { handle(JSON.parse(line)); } catch (_) { /* yok */ } });
          if (!gotTerminal) ab.md.appendChild(el('div', 'err', 'Bağlantı kesildi; tam yanıt sohbet geçmişinde.'));
          finish();
        });
      }
      return readStream(res).then(function () {
        if (!gotTerminal) {
          if (wait.parentNode) wait.parentNode.removeChild(wait);
          if (ab.text) renderMarkdown(ab.text, ab.md);
          ab.md.appendChild(el('div', 'err', 'Bağlantı kesildi; tam yanıt sohbet geçmişine kaydedilir.'));
        }
        finish();
      });
    }).catch(function (err) {
      if (wait.parentNode) wait.parentNode.removeChild(wait);
      if (err && err.name === 'AbortError') {
        if (ab.text) renderMarkdown(ab.text, ab.md);
        ab.md.appendChild(el('div', 'meta', 'Durduruldu. Tam yanıt sohbet geçmişine kaydedilir.'));
      } else {
        if (ab.text) renderMarkdown(ab.text, ab.md);
        ab.md.appendChild(el('div', 'err', 'Bağlantı hatası; biraz sonra tekrar deneyin.'));
        if (!ab.text) { ui.ta.value = text; autoGrow(); updateCount(); }
      }
      finish();
    });
  }

  // ─── Açılış ──────────────────────────────────────────────────────────────

  function init() {
    fetch(API + '/bootstrap', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || typeof d !== 'object') return;        // 401/403: DOM yok
        boot = d;
        dailyRemaining = (d.daily_remaining_usd != null) ? Number(d.daily_remaining_usd) : null;
        buildDom();
        ui.sub.textContent = (d.model_label || d.model || '') + ' · yalnız okuma';
        updateFooter();
        if (!d.enabled) setComposer(false, d.reason || 'Minerva Asistan şu anda kapalı.');
        var saved = parseInt(ssGet(SS_CONV) || '', 10);
        if (saved > 0) { convId = saved; openConversation(saved); } else showWelcome();
        if (ssGet(SS_OPEN) === '1') openPanel();
      })
      .catch(function () { /* ağ yok / bootstrap hatası: sessiz */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
