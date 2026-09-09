/* ===========================================================================
 * Minerva 108 — Influencer Programı ön yüz mantığı.
 * Tek sayfa: Başvurular · Creator'lar · Pano · Gönderimler · (Linkler & Satış,
 * Rapor → Faz 2) · Ayarlar.  Tüm veri /api/influencer/* uçlarından gelir.
 * Kalıp: static/crm.js (drawer/kanban) + templates/pdks.html (sekme/tembel yük).
 * Sınıf öneki 'inf-' — 'btn-*' YASAK (Bootstrap gölgeleme tuzağı).
 * ======================================================================== */
(function () {
  "use strict";

  // ── Kısa yardımcılar ─────────────────────────────────────────────────────
  const el = (id) => document.getElementById(id);
  const can = (c, a) => (window.can ? window.can(c, a) : false);
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function toast(msg, type) { (window.showToast || function (m) { alert(m); })(msg, type || "error"); }
  async function api(path, opts) {
    const r = await fetch("/api/influencer" + path, opts || {});
    let d = null;
    try { d = await r.json(); } catch (e) { /* boş gövde */ }
    if (!r.ok) throw new Error((d && d.detail) || ("İşlem başarısız (" + r.status + ")"));
    return d;
  }
  const jbody = (body, method) => ({
    method: method || "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  function num(v, frac) {
    if (v == null || v === "" || isNaN(Number(v))) return "—";
    try { return new Intl.NumberFormat("tr-TR", { maximumFractionDigits: frac == null ? 0 : frac }).format(Number(v)); }
    catch (e) { return String(v); }
  }
  function money(v) {
    if (v == null || v === "" || isNaN(Number(v))) return "—";
    try { return new Intl.NumberFormat("tr-TR", { style: "currency", currency: "TRY", maximumFractionDigits: 2 }).format(Number(v)); }
    catch (e) { return Number(v).toFixed(2) + " TL"; }
  }
  function pct(v, frac) { return v == null || v === "" ? "—" : "%" + num(v, frac == null ? 1 : frac); }
  function fmtK(n) {
    n = Number(n || 0);
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(".0", "") + "M";
    if (n >= 1000) return (n / 1000).toFixed(1).replace(".0", "") + "K";
    return String(n);
  }
  function today() {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function val(id) { const e = el(id); return e ? String(e.value == null ? "" : e.value).trim() : ""; }
  function numOrNull(id) { const s = val(id); if (s === "") return null; const n = Number(s.replace(",", ".")); return isNaN(n) ? null : n; }
  function intOrNull(id) { const n = numOrNull(id); return n == null ? null : Math.round(n); }
  function chk(id) { const e = el(id); return !!(e && e.checked); }
  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(() => toast("Kopyalandı.", "success"), () => toast("Kopyalanamadı — elle seçin."));
    } else { toast("Tarayıcı panoya erişemiyor — elle seçin."); }
  }
  function opt(list, sel, blank) {
    let h = blank != null ? `<option value="">${esc(blank)}</option>` : "";
    (list || []).forEach((o) => {
      const v = o.value != null ? o.value : o[0];
      const l = o.label != null ? o.label : o[1];
      h += `<option value="${esc(v)}"${String(v) === String(sel == null ? "" : sel) ? " selected" : ""}>${esc(l)}</option>`;
    });
    return h;
  }

  // ── Sabitler ─────────────────────────────────────────────────────────────
  const CFG = (window.INF_CFG && typeof window.INF_CFG === "object") ? window.INF_CFG : {};
  const CFG_KEY = {
    reminder: "influencer.reminder_days", noPost: "influencer.no_post_days",
    pack: "influencer.pack_cost_try", ship: "influencer.ship_cost_try",
    guideline: "influencer.guideline_version",
  };
  const STORES = { minerva: "Minerva 108", serenida: "Serenida", evanira: "Evanira" };
  const STORE_OPTS = Object.keys(STORES).map((k) => [k, STORES[k]]);
  const PLATFORMS = {
    instagram: { label: "Instagram", icon: "bi-instagram", url: (h) => "https://instagram.com/" + h },
    tiktok: { label: "TikTok", icon: "bi-tiktok", url: (h) => "https://tiktok.com/@" + h },
    youtube: { label: "YouTube", icon: "bi-youtube", url: (h) => "https://youtube.com/@" + h },
  };
  const PLATFORM_OPTS = Object.keys(PLATFORMS).map((k) => [k, PLATFORMS[k].label]);
  const MODELS = { barter: "Barter", link: "Link", karma: "Karma" };
  const MODEL_OPTS = Object.keys(MODELS).map((k) => [k, MODELS[k]]);
  const MARKET_OPTS = [["TR", "TR"], ["US", "US"], ["UK", "UK"], ["DE", "DE"], ["EU", "EU"]];
  const REL = { havuz: "Havuz", basvurdu: "Başvurdu", seeded: "Ürün Gönderildi", affiliate: "Affiliate", ambassador: "Ambassador", pasif: "Pasif", kara_liste: "Kara Liste" };
  const REL_PILL = { havuz: "gray", basvurdu: "b", seeded: "y", affiliate: "g", ambassador: "g", pasif: "gray", kara_liste: "r" };
  const REL_OPTS = Object.keys(REL).map((k) => [k, REL[k]]);
  // Yedek — gerçek liste /collabs/board yanıtından gelir (core.influencer.STAGES ile aynı)
  const STAGE_LABELS = {
    teklif_gonderildi: "Teklif Gönderildi", kabul_edildi: "Kabul Edildi", adres_bekleniyor: "Adres Bekleniyor",
    urun_hazirlaniyor: "Ürün Hazırlanıyor", kargoda: "Kargoda", teslim_edildi: "Teslim Edildi",
    icerik_bekleniyor: "İçerik Bekleniyor", taslak_incelemede: "Taslak İncelemede", revizyon: "Revizyon",
    onaylandi: "Onaylandı", yayinlandi: "Yayınlandı", metrik_toplandi: "Metrik Toplandı",
    tamamlandi: "Tamamlandı", iptal: "İptal",
  };
  const STAGE_PILL = { tamamlandi: "g", iptal: "r", yayinlandi: "g", onaylandi: "g", revizyon: "o", icerik_bekleniyor: "y", kargoda: "b", teslim_edildi: "b" };
  const APP_STATUS = { pending: ["Bekliyor", "y"], maybe: ["Belki", "b"], approved: ["Onaylandı", "g"], rejected: ["Reddedildi", "r"] };
  const TIER_FALLBACK = { nano: "Nano", mikro: "Mikro", orta: "Orta", makro: "Makro", ambassador: "Ambassador" };
  const TOKEN_PURPOSE = { address: "Adres formu", insights: "Insights yükleme", agreement: "Kılavuz onayı" };
  const TOKEN_STATE = { open: ["Açık", "g"], used: ["Kullanıldı", "gray"], expired: ["Süresi doldu", "r"], notfound: ["Yok", "gray"] };
  const CONTENT_STATUS = { taslak: ["Taslak", "gray"], incelemede: ["İncelemede", "y"], onaylandi: ["Onaylandı", "g"], revizyon: ["Revizyon", "o"], yayinlandi: ["Yayınlandı", "g"] };
  const CONTENT_TYPES = [["reel", "Reel"], ["story", "Story"], ["post", "Post"], ["video", "Video"], ["short", "Short"], ["live", "Canlı"]];
  const DECISIONS = { birak: ["BIRAK", "r"], tekrarla: ["TEKRARLA", "b"], yukselt: ["YÜKSELT", "g"] };
  const COMP_LABELS = {
    er_fit: "ER uyumu", comment_quality: "Yorum kalitesi", comment_like: "Yorum / beğeni",
    view_follower: "İzlenme / takipçi", geo: "Kitle coğrafyası", growth: "Büyüme",
    saves_shares: "Kaydet / paylaş", fake_pct: "Sahte takipçi",
  };
  const FLAG_LABELS = {
    gizli_abone_sayisi: "Gizli abone sayısı", izlenme_sabit: "İzlenmeler sabit (bot şüphesi)",
    sahte_takipci_yuksek: "Sahte takipçi yüksek", kitle_hedef_disi: "Kitle hedef pazar dışı",
  };
  const CFG_LABELS = {
    "influencer.reminder_days": "Hatırlatma günleri (teslimden sonra; virgülle)",
    "influencer.no_post_days": "Paylaşmama bayrağı (gün)",
    "influencer.min_payout_try": "Asgari ödeme (TL)",
    "influencer.pack_cost_try": "Paketleme maliyeti (TL / gönderim)",
    "influencer.ship_cost_try": "Kargo maliyeti (TL / gönderim)",
    "influencer.gross_margin_pct": "Brüt marj (%)",
    "influencer.brand_handles": "Marka hesapları (@, virgülle)",
    "influencer.forbidden_words": "Yasak kelimeler (regex)",
    "influencer.guideline_version": "Kılavuz sürümü",
  };
  const FILE_KINDS = [["insights", "Insights ekran kaydı"], ["media_kit", "Medya kiti"], ["draft", "Taslak içerik"], ["other", "Diğer"]];

  // ── Durum ────────────────────────────────────────────────────────────────
  const state = {
    loaded: {}, tiers: null, users: null, campaigns: null, items: null,
    apps: [], appFilter: { status: "pending", store: "", q: "" },
    creators: [], crFilter: { q: "", stage: "", tier: "", active: 1 },
    board: null, transitions: {}, boardFilter: { store: "", campaign: "" },
    ships: [], shipFilter: { status: "preparing" },
    settings: null, bmPlatform: "instagram",
    drawer: null, scoreResult: {}, cart: [], dragging: null,
  };

  // ── Rozet yardımcıları ───────────────────────────────────────────────────
  const pill = (txt, color) => `<span class="inf-pill inf-pill-${color || "gray"}">${esc(txt)}</span>`;
  const storePill = (k) => pill(STORES[k] || k || "—", "b");
  function tierLabel(k) {
    if (!k) return "—";
    const t = (state.tiers || []).find((x) => x.key === k);
    return t ? t.label : (TIER_FALLBACK[k] || k);
  }
  const tierPill = (k) => (k ? pill(tierLabel(k), k === "ambassador" ? "g" : "b") : '<span class="muted">—</span>');
  const relPill = (k) => pill(REL[k] || k || "—", REL_PILL[k] || "gray");
  const stagePill = (k, label) => pill(label || STAGE_LABELS[k] || k || "—", STAGE_PILL[k] || "gray");
  function scoreBadge(s, title) {
    if (s == null) return `<span class="inf-score na" title="${esc(title || "Skor yok")}">—</span>`;
    const c = s >= 70 ? "hi" : (s >= 45 ? "mid" : "lo");
    return `<span class="inf-score ${c}" title="${esc(title || "Özgünlük skoru")}">${num(s)}</span>`;
  }
  function platIcon(p) { const m = PLATFORMS[p]; return `<span class="inf-plat ${esc(p)}"><i class="bi ${m ? m.icon : "bi-globe"}"></i></span>`; }
  function accountChip(a) {
    const m = PLATFORMS[a.platform];
    const h = a.handle || "";
    const link = m && h ? m.url(h) : (a.url || "");
    const followers = a.followers != null ? a.followers : a.followers_claimed;
    return `<span class="inf-plat ${esc(a.platform)}" title="${esc((m ? m.label : a.platform) + (followers != null ? " · " + num(followers) + " takipçi" : ""))}">
      <i class="bi ${m ? m.icon : "bi-globe"}"></i>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener" onclick="event.stopPropagation();">@${esc(h || "?")}</a>` : "@" + esc(h || "?")}
      ${followers != null ? `<span class="muted">${esc(fmtK(followers))}</span>` : ""}</span>`;
  }
  function flagsHtml(flags) {
    return (flags || []).map((f) => {
      if (String(f).startsWith("eksik_veri:")) return `<span class="inf-flag warn">Eksik veri: ${esc(COMP_LABELS[f.split(":")[1]] || f.split(":")[1])}</span>`;
      return `<span class="inf-flag">${esc(FLAG_LABELS[f] || f)}</span>`;
    }).join("");
  }
  function scoreBox(res, title) {
    if (!res) return "";
    const comps = res.components || {};
    return `<div class="inf-box">
      <div style="display:flex;align-items:center;gap:0.7rem;"><span class="t">${esc(title || "Özgünlük skoru")}</span>${scoreBadge(res.score)}</div>
      <div class="inf-comp">${Object.keys(comps).map((k) => `<div><b>${comps[k] == null ? "—" : num(comps[k])}</b>${esc(COMP_LABELS[k] || k)}</div>`).join("")}</div>
      ${(res.flags || []).length ? `<div style="margin-top:0.5rem;">${flagsHtml(res.flags)}</div>` : ""}
    </div>`;
  }

  // ── Referans verileri ────────────────────────────────────────────────────
  async function ensureTiers() {
    if (state.tiers) return state.tiers;
    if (can("influencer", "settings")) {
      try { const d = await api("/settings"); state.settings = d; state.tiers = d.tiers || []; return state.tiers; } catch (e) { /* düş */ }
    }
    state.tiers = Object.keys(TIER_FALLBACK).map((k) => ({ key: k, label: TIER_FALLBACK[k], is_active: true }));
    return state.tiers;
  }
  async function ensureUsers() {
    if (state.users) return state.users;
    state.users = [];
    if (can("crm", "view")) {
      try { const r = await fetch("/api/crm/users"); if (r.ok) state.users = await r.json(); } catch (e) { /* */ }
    }
    return state.users;
  }
  async function ensureCampaigns(force) {
    if (state.campaigns && !force) return state.campaigns;
    try { const d = await api("/campaigns"); state.campaigns = d.items || (Array.isArray(d) ? d : []); } catch (e) { state.campaigns = []; }
    return state.campaigns;
  }
  function ownerSelect(id, sel) {
    const users = state.users || [];
    const me = window.ME || {};
    const list = users.length ? users : (me.id ? [{ id: me.id, full_name: me.name }] : []);
    return `<select class="ipt" id="${id}">${opt(list.map((u) => [u.id, u.full_name || u.username]), sel != null ? sel : (me.id || ""), "Sorumlu seçin")}</select>`;
  }
  function tierOptions(sel, blank) { return opt((state.tiers || []).map((t) => [t.key, t.label]), sel, blank); }

  // ── Sekmeler (pdks.html openTab kalıbı) ──────────────────────────────────
  const TABS = { apps: renderApps, creators: renderCreators, board: renderBoard, ship: renderShip, settings: renderSettings };
  function openTab(name) {
    document.querySelectorAll(".inf-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".inf-panel").forEach((p) => p.classList.toggle("open", p.id === "inf-panel-" + name));
    if (TABS[name]) TABS[name](!state.loaded[name]);
    state.loaded[name] = true;
    try { sessionStorage.setItem("inf_tab", name); } catch (e) { /* */ }
  }

  // ── Modal ────────────────────────────────────────────────────────────────
  let modalSave = null;
  function openModal(title, html, onSave, opts) {
    opts = opts || {};
    el("infMTitle").textContent = title;
    el("infMBody").innerHTML = html;
    el("infModalBox").classList.toggle("wide", !!opts.wide);
    const btn = el("infMSave");
    btn.style.display = onSave ? "" : "none";
    btn.textContent = opts.saveLabel || "Kaydet";
    btn.disabled = false;
    modalSave = onSave;
    el("infModal").classList.add("open");
    const f = el("infMBody").querySelector("input,select,textarea");
    if (f && !opts.noFocus) setTimeout(() => f.focus(), 50);
  }
  function closeModal() { el("infModal").classList.remove("open"); modalSave = null; }
  async function runSave() {
    if (!modalSave) return;
    const btn = el("infMSave");
    btn.disabled = true;
    try { await modalSave(); } catch (e) { toast(e.message || String(e)); }
    finally { btn.disabled = false; }
  }

  // ── Drawer ───────────────────────────────────────────────────────────────
  function openDrawerShell(title, sub, tabs, active) {
    el("infDwTitle").textContent = title || "—";
    el("infDwSub").innerHTML = sub || "";
    el("infDwTabs").innerHTML = tabs.map((t) => `<button class="inf-drawer-tab${t.key === active ? " active" : ""}" onclick="INF.dwTab('${t.key}')">${esc(t.label)}</button>`).join("");
    el("infScrim").classList.add("open");
    el("infDrawer").classList.add("open");
    el("infDrawer").setAttribute("aria-hidden", "false");
  }
  function closeDrawer() {
    el("infScrim").classList.remove("open");
    el("infDrawer").classList.remove("open");
    el("infDrawer").setAttribute("aria-hidden", "true");
    state.drawer = null;
  }
  function dwTab(key) { if (!state.drawer) return; state.drawer.tab = key; renderDrawer(); }
  function renderDrawer() {
    const d = state.drawer;
    if (!d) return;
    if (d.type === "creator") renderCreatorDrawer(); else renderCollabDrawer();
  }
  const kv = (k, v) => `<div class="inf-kv"><span class="k">${esc(k)}</span><span class="v">${v == null || v === "" ? '<span class="muted">—</span>' : v}</span></div>`;
  const sec = (t, right) => `<div class="inf-sec"><span>${esc(t)}</span><span>${right || ""}</span></div>`;

  // =========================================================================
  // 1) BAŞVURULAR
  // =========================================================================
  async function renderApps(first) {
    const v = el("inf-panel-apps");
    if (first || !el("infAppsTable")) {
      v.innerHTML = `<div class="inf-toolbar">
        <div class="inf-search"><i class="bi bi-search"></i><input class="ipt" id="infAppQ" placeholder="Ad / e-posta ara…" value="${esc(state.appFilter.q)}"></div>
        <select class="ipt" id="infAppStatus">${opt([["pending", "Bekleyen"], ["maybe", "Belki"], ["approved", "Onaylanan"], ["rejected", "Reddedilen"], ["", "Tümü"]], state.appFilter.status)}</select>
        <select class="ipt" id="infAppStore">${opt(STORE_OPTS, state.appFilter.store, "Tüm mağazalar")}</select>
        <button class="btn-o" onclick="INF.reloadApps()" title="Yenile"><i class="bi bi-arrow-clockwise"></i></button>
        ${can("influencer", "edit") ? '<button class="btn-g" onclick="INF.manualAppModal()"><i class="bi bi-person-plus"></i> Elle ekle</button>' : ""}
        <a class="btn-o" href="/basvuru" target="_blank" rel="noopener" title="Halka açık başvuru formu (bio linki)"><i class="bi bi-box-arrow-up-right"></i> Form</a>
      </div>
      <div class="card2"><div class="card2-body" id="infAppsTable"><div class="inf-empty">Yükleniyor…</div></div></div>`;
      let t = null;
      el("infAppQ").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { state.appFilter.q = val("infAppQ"); loadApps(); }, 300); });
      el("infAppStatus").addEventListener("change", () => { state.appFilter.status = val("infAppStatus"); loadApps(); });
      el("infAppStore").addEventListener("change", () => { state.appFilter.store = val("infAppStore"); loadApps(); });
    }
    await ensureTiers();
    loadApps();
  }
  async function loadApps() {
    const box = el("infAppsTable");
    if (!box) return;
    const f = state.appFilter;
    const p = [];
    if (f.status) p.push("status=" + encodeURIComponent(f.status));
    if (f.store) p.push("store_key=" + encodeURIComponent(f.store));
    if (f.q) p.push("q=" + encodeURIComponent(f.q));
    try {
      const d = await api("/applications" + (p.length ? "?" + p.join("&") : ""));
      state.apps = d.items || [];
      let pending = f.status === "pending" && !f.q && !f.store ? d.count : null;
      if (pending == null) { try { pending = (await api("/applications?status=pending")).count; } catch (e) { pending = null; } }
      const cnt = el("infCntApps");
      if (cnt) { cnt.textContent = String(pending || 0); cnt.style.display = pending ? "" : "none"; }
      if (!state.apps.length) { box.innerHTML = '<div class="inf-empty">Bu filtrede başvuru yok.</div>'; return; }
      const approve = can("influencer", "approve");
      box.innerHTML = `<div style="overflow-x:auto;"><table>
        <thead><tr><th>Başvuran</th><th>Mağaza</th><th>Hesaplar</th><th>Model</th><th>Tarih</th><th>Durum</th><th></th></tr></thead>
        <tbody>${state.apps.map((a) => {
          const st = APP_STATUS[a.status] || [a.status, "gray"];
          const openSt = a.status === "pending" || a.status === "maybe";
          const model = (a.form && a.form.accepted_model) || "—";
          let act = "";
          if (approve && openSt) {
            act = `<button class="btn-g inf-sm" onclick="INF.decide(${a.id},'approved')"><i class="bi bi-check-lg"></i> Onayla</button>
              ${a.status !== "maybe" ? `<button class="btn-o inf-sm" onclick="INF.decide(${a.id},'maybe')">Belki</button>` : ""}
              <button class="btn-o inf-sm" style="color:#b91c1c;" onclick="INF.decide(${a.id},'rejected')">Reddet</button>`;
          } else if (a.creator_id) {
            act = `<button class="btn-o inf-sm" onclick="INF.openCreator(${a.creator_id})"><i class="bi bi-person-badge"></i> Creator</button>`;
          }
          return `<tr class="inf-row" onclick="INF.openApp(${a.id})">
            <td><b>${esc(a.full_name)}</b><br><span class="muted">${esc(a.email || "")}${a.country ? " · " + esc(a.country) : ""}${a.city ? " / " + esc(a.city) : ""}</span>
              ${a.existing_creator_id && !a.creator_id ? `<br><span class="inf-pill inf-pill-o" title="Aynı e-postayla kayıtlı creator var — onayda o karta bağlanır">Mevcut creator #${a.existing_creator_id}</span>` : ""}</td>
            <td>${storePill(a.store_key)}</td>
            <td>${(a.accounts || []).length ? a.accounts.map(accountChip).join("<br>") : '<span class="muted">—</span>'}</td>
            <td>${esc(MODELS[model] || model)}</td>
            <td class="muted">${esc(a.created_at_label || "")}</td>
            <td>${pill(st[0], st[1])}${a.reject_reason ? `<br><span class="muted" title="${esc(a.reject_reason)}">${esc(a.reject_reason.slice(0, 40))}${a.reject_reason.length > 40 ? "…" : ""}</span>` : ""}</td>
            <td onclick="event.stopPropagation();" style="white-space:nowrap;">${act}</td>
          </tr>`;
        }).join("")}</tbody></table></div>`;
    } catch (e) { box.innerHTML = '<div class="inf-empty">' + esc(e.message) + "</div>"; }
  }
  function openApp(id) {
    const a = state.apps.find((x) => x.id === id);
    if (!a) return;
    const f = a.form || {};
    const st = APP_STATUS[a.status] || [a.status, "gray"];
    const openSt = a.status === "pending" || a.status === "maybe";
    const links = (f.recent_links || []).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>");
    openModal("Başvuru — " + a.full_name, `
      <div style="margin-bottom:0.6rem;">${pill(st[0], st[1])} ${storePill(a.store_key)} ${a.language ? pill(String(a.language).toUpperCase(), "gray") : ""}</div>
      ${a.existing_creator_id && !a.creator_id ? `<div class="inf-warn">Aynı e-postayla kayıtlı creator var (#${a.existing_creator_id}). Onaylarsanız başvuru o karta bağlanır, yeni kart açılmaz.</div>` : ""}
      ${kv("E-posta", esc(a.email))}${kv("Telefon", esc(a.phone))}${kv("Konum", esc([a.country, a.city].filter(Boolean).join(" / ")))}
      ${kv("Doğum yılı", a.birth_year ? esc(a.birth_year) + " (" + (new Date().getFullYear() - a.birth_year) + " yaş)" : "")}
      ${kv("Hesaplar", (a.accounts || []).map(accountChip).join("<br>"))}
      ${kv("Kategoriler", (f.niches || []).map((n) => pill(n, "gray")).join(" "))}
      ${kv("Cilt tipi", esc(f.skin_type))}${kv("Model", esc(MODELS[f.accepted_model] || f.accepted_model))}
      ${kv("Son içerikler", links)}${kv("Neden biz?", esc(f.why))}${kv("Geçmiş markalar", esc(f.past_brands))}${kv("Not", esc(f.notes))}
      ${kv("Kaynak", esc(f.source === "manual" ? "Elle girildi" : "Web formu"))}${kv("KVKK sürümü", esc(a.consent_text_version))}
      ${kv("Başvuru", esc(a.created_at_label))}${a.reviewed_at_label ? kv("İnceleme", esc(a.reviewed_by || "") + " · " + esc(a.reviewed_at_label)) : ""}
      ${a.reject_reason ? kv("Gerekçe", esc(a.reject_reason)) : ""}
      ${a.creator_id ? kv("Creator", `<button class="btn-o inf-sm" onclick="INF.closeModal();INF.openCreator(${a.creator_id})">Kartı aç #${a.creator_id}</button>`) : ""}
      ${can("influencer", "approve") && openSt ? `<div class="inf-actions" style="margin-top:1rem;">
        <button class="btn-g" onclick="INF.closeModal();INF.decide(${a.id},'approved')"><i class="bi bi-check-lg"></i> Onayla</button>
        ${a.status !== "maybe" ? `<button class="btn-o" onclick="INF.closeModal();INF.decide(${a.id},'maybe')">Belki</button>` : ""}
        <button class="btn-o" style="color:#b91c1c;" onclick="INF.closeModal();INF.decide(${a.id},'rejected')">Reddet</button></div>` : ""}
    `, null);
  }
  async function decide(id, decision) {
    const a = state.apps.find((x) => x.id === id);
    if (!a) return;
    if (decision === "maybe") {
      try { await api("/applications/" + id + "/decide", jbody({ decision: "maybe" })); toast("'Belki' olarak işaretlendi.", "success"); loadApps(); }
      catch (e) { toast(e.message); }
      return;
    }
    if (decision === "rejected") {
      openModal("Reddet — " + a.full_name, `
        <div class="inf-mb"><label class="lbl">Gerekçe (zorunlu — kayda yazılır, başvurana gönderilmez)</label>
          <textarea class="ipt" id="infRejReason" rows="3" placeholder="Ör. takipçi eşiğinin altında, kitle hedef pazar dışı, içerik uyumsuz…"></textarea></div>`, async () => {
        const reason = val("infRejReason");
        if (!reason) { toast("Gerekçe zorunlu."); return; }
        await api("/applications/" + id + "/decide", jbody({ decision: "rejected", reject_reason: reason }));
        closeModal(); toast("Başvuru reddedildi.", "info"); loadApps();
      }, { saveLabel: "Reddet" });
      return;
    }
    await ensureUsers();
    const ex = a.existing_creator_id;
    openModal("Onayla — " + a.full_name, `
      ${ex ? `<div class="inf-info">Aynı e-postayla kayıtlı creator var (#${ex}). Başvuru bu karta bağlanacak; hesaplar karta eklenir.</div>` : `<p class="muted">Yeni creator kartı açılır (ilişki aşaması: <b>Başvurdu</b>), hesaplar aktarılır, kademe takipçi sayısına göre atanır.</p>`}
      <div class="inf-frow">
        <div><label class="lbl">Sorumlu</label>${ownerSelect("infAppOwner", null)}</div>
        <div><label class="lbl">Mevcut creator'a bağla (ID, isteğe bağlı)</label><input class="ipt" id="infAppLink" type="number" min="1" value="${ex || ""}" placeholder="Boş = otomatik"></div>
      </div>`, async () => {
      const body = { decision: "approved" };
      const o = intOrNull("infAppOwner"); if (o) body.owner_user_id = o;
      const l = intOrNull("infAppLink"); if (l) body.link_creator_id = l;
      const r = await api("/applications/" + id + "/decide", jbody(body));
      closeModal(); toast("Onaylandı" + (r && r.creator_id ? " — creator #" + r.creator_id : "") + ".", "success");
      loadApps(); state.loaded.creators = false;
      if (r && r.creator_id) openCreator(r.creator_id);
    }, { saveLabel: "Onayla" });
  }
  function accountRows(prefix, n, existing) {
    let h = "";
    for (let i = 0; i < n; i++) {
      const a = (existing || [])[i] || {};
      h += `<div class="inf-frow3 inf-mb">
        <select class="ipt" id="${prefix}_p${i}">${opt(PLATFORM_OPTS, a.platform || (i === 0 ? "instagram" : ""), "Platform")}</select>
        <input class="ipt" id="${prefix}_u${i}" placeholder="Profil URL veya @kullanıcı" value="${esc(a.url || (a.handle ? "@" + a.handle : ""))}">
        <input class="ipt" id="${prefix}_f${i}" type="number" min="0" placeholder="Takipçi" value="${a.followers != null ? a.followers : (a.followers_claimed != null ? a.followers_claimed : "")}">
      </div>`;
    }
    return h;
  }
  function collectAccounts(prefix, n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const p = val(prefix + "_p" + i), u = val(prefix + "_u" + i);
      if (!p || !u) continue;
      const row = { platform: p, followers_claimed: intOrNull(prefix + "_f" + i) || 0 };
      if (/^https?:\/\//i.test(u)) row.url = u; else row.handle = u.replace(/^@/, "");
      out.push(row);
    }
    return out;
  }
  function manualAppModal() {
    openModal("Elle başvuru ekle", `
      <p class="muted" style="font-size:0.82rem;">Formu kullanmayan (DM / e-posta / organik etiketleyen) creator'lar için. Kayıt <b>Bekliyor</b> durumunda açılır; onay akışı aynıdır.</p>
      <div class="inf-frow">
        <div><label class="lbl">Ad Soyad *</label><input class="ipt" id="infMA_name"></div>
        <div><label class="lbl">E-posta *</label><input class="ipt" id="infMA_email" type="email"></div>
        <div><label class="lbl">Telefon</label><input class="ipt" id="infMA_phone"></div>
        <div><label class="lbl">Mağaza</label><select class="ipt" id="infMA_store">${opt(STORE_OPTS, "minerva")}</select></div>
        <div><label class="lbl">Ülke (ISO-2)</label><input class="ipt" id="infMA_country" value="TR" maxlength="2"></div>
        <div><label class="lbl">Şehir</label><input class="ipt" id="infMA_city"></div>
        <div><label class="lbl">Dil</label><select class="ipt" id="infMA_lang">${opt([["tr", "Türkçe"], ["en", "İngilizce"]], "tr")}</select></div>
        <div><label class="lbl">Doğum yılı</label><input class="ipt" id="infMA_by" type="number" min="1900" max="2100"></div>
        <div><label class="lbl">Model</label><select class="ipt" id="infMA_model">${opt(MODEL_OPTS, "barter")}</select></div>
        <div><label class="lbl">Cilt tipi</label><input class="ipt" id="infMA_skin" placeholder="karma / kuru / yağlı / hassas"></div>
        <div class="full"><label class="lbl">Kategoriler (virgülle)</label><input class="ipt" id="infMA_niches" placeholder="cilt bakımı, vegan, makyaj"></div>
      </div>
      <label class="lbl" style="margin-top:0.7rem;">Hesaplar</label>${accountRows("infMA", 3)}
      <div class="inf-mb"><label class="lbl">Neden biz? / Not</label><textarea class="ipt" id="infMA_why" rows="2"></textarea></div>
      <div class="inf-mb"><label class="lbl">Geçmiş markalar</label><input class="ipt" id="infMA_brands"></div>
    `, async () => {
      const body = {
        store_key: val("infMA_store"), full_name: val("infMA_name"), email: val("infMA_email"), phone: val("infMA_phone") || null,
        country: val("infMA_country") || null, city: val("infMA_city") || null, language: val("infMA_lang"),
        birth_year: intOrNull("infMA_by"), accounts: collectAccounts("infMA", 3),
        niches: val("infMA_niches").split(",").map((s) => s.trim()).filter(Boolean),
        skin_type: val("infMA_skin") || null, why: val("infMA_why") || null, past_brands: val("infMA_brands") || null,
        accepted_model: val("infMA_model"),
      };
      if (!body.full_name || !body.email) { toast("Ad ve e-posta zorunlu."); return; }
      await api("/applications/manual", jbody(body));
      closeModal(); toast("Başvuru eklendi.", "success"); state.appFilter.status = "pending"; renderApps(true);
    }, { wide: true });
  }

  // =========================================================================
  // 2) CREATOR'LAR
  // =========================================================================
  async function renderCreators(first) {
    const v = el("inf-panel-creators");
    await ensureTiers();
    if (first || !el("infCrTable")) {
      v.innerHTML = `<div class="inf-toolbar">
        <div class="inf-search"><i class="bi bi-search"></i><input class="ipt" id="infCrQ" placeholder="Ad / e-posta / slug ara…" value="${esc(state.crFilter.q)}"></div>
        <select class="ipt" id="infCrStage">${opt(REL_OPTS, state.crFilter.stage, "Tüm aşamalar")}</select>
        <select class="ipt" id="infCrTier">${tierOptions(state.crFilter.tier, "Tüm kademeler")}</select>
        <label class="muted" style="display:flex;align-items:center;gap:0.3rem;"><input type="checkbox" id="infCrActive" ${state.crFilter.active ? "checked" : ""}> Yalnız aktif</label>
        <button class="btn-o" onclick="INF.reloadCreators()" title="Yenile"><i class="bi bi-arrow-clockwise"></i></button>
        <a class="btn-o" href="/api/influencer/creators/export" title="Excel'e aktar"><i class="bi bi-file-earmark-excel"></i></a>
        ${can("influencer", "edit") ? '<button class="btn-g" onclick="INF.creatorModal()"><i class="bi bi-plus-lg"></i> Yeni creator</button>' : ""}
      </div>
      <div class="card2"><div class="card2-body" id="infCrTable"><div class="inf-empty">Yükleniyor…</div></div></div>`;
      let t = null;
      el("infCrQ").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { state.crFilter.q = val("infCrQ"); loadCreators(); }, 300); });
      el("infCrStage").addEventListener("change", () => { state.crFilter.stage = val("infCrStage"); loadCreators(); });
      el("infCrTier").addEventListener("change", () => { state.crFilter.tier = val("infCrTier"); loadCreators(); });
      el("infCrActive").addEventListener("change", () => { state.crFilter.active = chk("infCrActive") ? 1 : 0; loadCreators(); });
    }
    loadCreators();
  }
  async function loadCreators() {
    const box = el("infCrTable");
    if (!box) return;
    const f = state.crFilter;
    const p = ["active=" + (f.active ? 1 : 0)];
    if (f.q) p.push("q=" + encodeURIComponent(f.q));
    if (f.stage) p.push("stage=" + encodeURIComponent(f.stage));
    if (f.tier) p.push("tier=" + encodeURIComponent(f.tier));
    try {
      const d = await api("/creators?" + p.join("&"));
      state.creators = d.items || [];
      if (!state.creators.length) { box.innerHTML = '<div class="inf-empty">Creator yok. Başvuruları onaylayın ya da "Yeni creator" ile ekleyin.</div>'; return; }
      box.innerHTML = `<div style="overflow-x:auto;"><table>
        <thead><tr><th>Creator</th><th>Hesaplar</th><th>Takipçi</th><th>Kademe</th><th>İlişki</th><th>Skor</th><th>Sorumlu</th><th>Güncellendi</th></tr></thead>
        <tbody>${state.creators.map((c) => `<tr class="inf-row" onclick="INF.openCreator(${c.id})">
          <td><b>${esc(c.full_name)}</b>${c.do_not_resend ? ' <span class="inf-pill inf-pill-r" title="Tekrar gönderme">⛔</span>' : ""}<br><span class="muted">${esc(c.email || "")}${c.country ? " · " + esc(c.country) : ""}</span>
            ${(c.tags || []).length ? "<br>" + c.tags.map((t) => pill(t, "gray")).join(" ") : ""}</td>
          <td>${(c.accounts || []).length ? c.accounts.map(accountChip).join("<br>") : '<span class="muted">—</span>'}</td>
          <td>${num(c.followers)}</td>
          <td>${tierPill(c.effective_tier_key)}${c.tier_override_key ? ' <span class="muted" title="Elle atanmış">✎</span>' : ""}</td>
          <td>${relPill(c.relationship_stage)}</td>
          <td>${scoreBadge(c.authenticity_score)}${c.fake_pct != null ? `<br><span class="muted">sahte ${pct(c.fake_pct, 0)}</span>` : ""}</td>
          <td class="muted">${esc(c.owner_name || "—")}</td>
          <td class="muted">${esc(c.updated_at_label || c.created_at_label || "")}</td>
        </tr>`).join("")}</tbody></table></div>`;
    } catch (e) { box.innerHTML = '<div class="inf-empty">' + esc(e.message) + "</div>"; }
  }
  async function openCreator(id, tab) {
    try {
      const d = await api("/creators/" + id);
      state.drawer = { type: "creator", id: id, data: d, tab: tab || (state.drawer && state.drawer.type === "creator" && state.drawer.id === id ? state.drawer.tab : "profile") };
      renderDrawer();
    } catch (e) { toast(e.message); }
  }
  async function refreshCreator() { if (state.drawer && state.drawer.type === "creator") await openCreator(state.drawer.id, state.drawer.tab); }
  function renderCreatorDrawer() {
    const c = state.drawer.data;
    const tabs = [
      { key: "profile", label: "Profil" }, { key: "accounts", label: "Hesaplar" }, { key: "collabs", label: "İş birlikleri" },
      { key: "links", label: "Adres & Linkler" }, { key: "files", label: "Dosyalar" }, { key: "notes", label: "Notlar" },
    ];
    openDrawerShell(c.full_name, `${relPill(c.relationship_stage)} ${tierPill(c.effective_tier_key)} ${c.followers ? '<span style="opacity:.8;">' + esc(fmtK(c.followers)) + " takipçi</span>" : ""}`, tabs, state.drawer.tab);
    const b = el("infDwBody");
    const t = state.drawer.tab;
    if (t === "profile") b.innerHTML = creatorProfileHtml(c);
    else if (t === "accounts") b.innerHTML = creatorAccountsHtml(c);
    else if (t === "collabs") b.innerHTML = creatorCollabsHtml(c);
    else if (t === "links") b.innerHTML = creatorLinksHtml(c);
    else if (t === "files") b.innerHTML = creatorFilesHtml(c);
    else b.innerHTML = creatorNotesHtml(c);
  }
  function creatorProfileHtml(c) {
    const edit = can("influencer", "edit");
    const sc = c.score || null;
    return `
      ${c.do_not_resend ? '<div class="inf-warn"><i class="bi bi-exclamation-triangle"></i> <b>Tekrar gönderme</b> işaretli — bu creator\'a yeni gönderim açılamaz.</div>' : ""}
      ${sc ? scoreBox({ score: c.authenticity_score, components: sc.components, flags: sc.flags }, "Özgünlük skoru") : `<div class="inf-box"><span class="t">Özgünlük skoru</span> ${scoreBadge(null)} <span class="muted">— Hesaplar sekmesinden "Son 12 gönderi gir".</span></div>`}
      ${edit ? `<div class="inf-actions"><button class="btn-g inf-sm" onclick="INF.creatorModal(${c.id})"><i class="bi bi-pencil"></i> Düzenle</button>
        <button class="btn-o inf-sm" onclick="INF.tagsModal(${c.id})"><i class="bi bi-tags"></i> Etiketler</button>
        <button class="btn-o inf-sm" onclick="INF.rescore(${c.id})" title="Mevcut hesap özetleriyle skoru yeniden hesapla"><i class="bi bi-calculator"></i> Yeniden skorla</button>
        <button class="btn-o inf-sm" onclick="INF.toggleResend(${c.id},${c.do_not_resend ? "false" : "true"})">${c.do_not_resend ? "Gönderime aç" : "Tekrar gönderme"}</button></div>` : ""}
      ${sec("İlişki aşaması")}
      ${edit ? `<div class="inf-frow"><div><select class="ipt" id="infRelStage">${opt(REL_OPTS, c.relationship_stage)}</select></div>
        <div style="display:flex;gap:0.4rem;"><input class="ipt" id="infRelReason" placeholder="Gerekçe (isteğe bağlı)"><button class="btn-o" onclick="INF.setRelStage(${c.id})">Uygula</button></div></div>` : relPill(c.relationship_stage)}
      ${sec("Kimlik")}
      ${kv("E-posta", c.email ? `<a href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : "")}${kv("Telefon", esc(c.phone))}
      ${kv("Konum", esc([c.country, c.city].filter(Boolean).join(" / ")))}${kv("Dil", esc(c.language))}
      ${kv("Doğum yılı", c.birth_year ? esc(c.birth_year) : "")}${kv("Slug", esc(c.slug))}
      ${sec("Profil")}
      ${kv("Kategoriler", (c.categories || []).map((n) => pill(n, "gray")).join(" "))}${kv("Cilt tipi", esc(c.skin_type))}
      ${kv("Beden", esc(c.clothing_size))}${kv("Alerjiler", esc(c.allergies))}${kv("Model", esc(MODELS[c.accepted_model] || c.accepted_model))}
      ${sec("Program")}
      ${kv("Kademe", tierPill(c.effective_tier_key) + (c.tier_override_key ? ' <span class="muted">elle · otomatik: ' + esc(tierLabel(c.tier_key)) + "</span>" : ' <span class="muted">otomatik</span>'))}
      ${kv("Doğrulama", esc(c.verification_level))}${kv("AQS", c.aqs != null ? esc(c.aqs) : "")}${kv("Sahte takipçi", c.fake_pct != null ? pct(c.fake_pct, 0) : "")}
      ${kv("Puan", c.rating ? "★".repeat(c.rating) + "☆".repeat(5 - c.rating) : "")}${kv("Sorumlu", esc(c.owner_name))}${kv("Kaynak", esc(c.source))}
      ${kv("Etiketler", (c.tags || []).map((t) => pill(t, "gray")).join(" "))}
      ${kv("Kayıt", esc(c.created_at_label) + (c.created_by ? " · " + esc(c.created_by) : ""))}
      ${kv("Notlar", esc(c.notes))}`;
  }
  function creatorAccountsHtml(c) {
    const edit = can("influencer", "edit");
    const res = state.scoreResult[c.id];
    let h = res ? scoreBox(res, "Son hesaplama") : "";
    h += sec("Hesaplar", edit ? `<button class="btn-o inf-sm" onclick="INF.accountModal(${c.id})"><i class="bi bi-plus-lg"></i> Hesap</button>` : "");
    if (!(c.accounts || []).length) h += '<div class="inf-empty">Hesap yok.</div>';
    (c.accounts || []).forEach((a) => {
      const m = PLATFORMS[a.platform];
      h += `<div class="inf-box">
        <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">${platIcon(a.platform)}<span class="t"><a href="${esc(m ? m.url(a.handle) : "#")}" target="_blank" rel="noopener">@${esc(a.handle)}</a></span>
          ${a.is_primary ? pill("Birincil", "b") : ""}${a.hidden_subscriber_count ? pill("Abone gizli", "r") : ""}<span class="muted" style="margin-left:auto;">${esc(a.metrics_source || "manual")}${a.metrics_at_label ? " · " + esc(a.metrics_at_label) : ""}</span></div>
        <div class="inf-comp">
          <div><b>${num(a.followers)}</b>Takipçi</div><div><b>${num(a.following)}</b>Takip</div><div><b>${num(a.posts_count)}</b>Gönderi</div>
          <div><b>${a.er_follower != null ? pct(a.er_follower, 2) : "—"}</b>ER / takipçi</div><div><b>${a.er_view != null ? pct(a.er_view, 2) : "—"}</b>ER / izlenme</div>
          <div><b>${a.view_per_follower != null ? num(a.view_per_follower, 2) : "—"}</b>İzlenme / takipçi</div><div><b>${num(a.avg_views)}</b>Ort. izlenme</div><div><b>${num(a.avg_likes)}</b>Ort. beğeni</div>
          <div><b>${num(a.avg_comments)}</b>Ort. yorum</div><div><b>${a.views_cv != null ? num(a.views_cv, 2) : "—"}</b>İzlenme CV</div>
        </div>
        ${edit ? `<div class="inf-actions"><button class="btn-g inf-sm" onclick="INF.metricsModal(${c.id},${a.id})"><i class="bi bi-clipboard-data"></i> Son 12 gönderi gir</button>
          <button class="btn-o inf-sm" onclick="INF.accountModal(${c.id},${a.id})"><i class="bi bi-pencil"></i></button>
          <button class="btn-o inf-sm" style="color:#b91c1c;" onclick="INF.deleteAccount(${c.id},${a.id})"><i class="bi bi-trash"></i></button></div>` : ""}
      </div>`;
    });
    return h;
  }
  function creatorCollabsHtml(c) {
    let h = sec("İş birlikleri", can("influencer", "edit") && !c.do_not_resend ? `<button class="btn-o inf-sm" onclick="INF.collabModal(null,${c.id})"><i class="bi bi-plus-lg"></i> Yeni iş birliği</button>` : "");
    if (!(c.collabs || []).length) h += '<div class="inf-empty">İş birliği yok.</div>';
    (c.collabs || []).forEach((x) => {
      h += `<div class="inf-box" style="cursor:pointer;" onclick="INF.openCollab(${x.id})">
        <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;"><span class="t">${esc(x.code)}</span>${storePill(x.store_key)}${stagePill(x.stage, x.stage_label)}${x.decision ? pill(x.decision_label || x.decision, (DECISIONS[x.decision] || ["", "gray"])[1]) : ""}
          <span class="inf-kage${(x.days_in_stage || 0) > 14 && !["tamamlandi", "iptal"].includes(x.stage) ? " stale" : ""}">${x.days_in_stage != null ? x.days_in_stage + " g" : ""}</span></div>
        <div class="sub">${esc(MODELS[x.model] || x.model)} · ${esc(x.market || "")}${x.campaign_name ? " · " + esc(x.campaign_name) : ""}${x.content_due_on_label ? " · içerik: " + esc(x.content_due_on_label) : ""}</div>
      </div>`;
    });
    return h;
  }
  function creatorLinksHtml(c) {
    const edit = can("influencer", "edit");
    let h = sec("Adresler");
    if (!(c.addresses || []).length) h += '<div class="muted" style="margin-bottom:0.6rem;">Adres yok — creator\'a "Adres formu" linki gönderin; adres IMS\'e kendisi girer (KVKK: adres formda değil, ayrı linkte).</div>';
    (c.addresses || []).forEach((a) => {
      h += `<div class="inf-box"><div style="display:flex;gap:0.5rem;align-items:center;"><span class="t">${esc(a.label || "Adres")}</span>${a.is_default ? pill("Varsayılan", "b") : ""}${a.verified_at_label ? pill("Doğrulandı " + a.verified_at_label, "g") : ""}</div>
        <div class="sub">${esc(a.recipient_name || "")}${a.phone ? " · " + esc(a.phone) : ""}</div>
        <div style="font-size:0.85rem;">${esc([a.line1, a.line2, a.district, a.city, a.postal_code, a.country].filter(Boolean).join(", "))}</div></div>`;
    });
    h += sec("Claim linkleri", edit ? `<span><button class="btn-o inf-sm" onclick="INF.tokenModal(${c.id},'address')">Adres</button> <button class="btn-o inf-sm" onclick="INF.tokenModal(${c.id},'insights')">Insights</button> <button class="btn-o inf-sm" onclick="INF.tokenModal(${c.id},'agreement')">Kılavuz onayı</button></span>` : "");
    if (!(c.tokens || []).length) h += '<div class="muted">Henüz link üretilmedi.</div>';
    (c.tokens || []).forEach((t) => {
      const st = TOKEN_STATE[t.state] || [t.state, "gray"];
      const full = location.origin + t.url;
      h += `<div class="inf-box"><div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;"><span class="t">${esc(TOKEN_PURPOSE[t.purpose] || t.purpose)}</span>${pill(st[0], st[1])}${t.collab_id ? pill("#" + t.collab_id, "gray") : ""}
        <span class="muted" style="margin-left:auto;">${t.used_at_label ? "kullanıldı " + esc(t.used_at_label) : "son: " + esc(t.expires_at_label || "")}</span></div>
        ${t.state === "open" ? `<div style="display:flex;gap:0.4rem;align-items:center;margin-top:0.35rem;"><input class="ipt" readonly value="${esc(full)}" onclick="this.select()"><button class="btn-o inf-sm" onclick="INF.copy('${esc(full)}')"><i class="bi bi-clipboard"></i></button></div>` : ""}
      </div>`;
    });
    return h;
  }
  function creatorFilesHtml(c) {
    const edit = can("influencer", "edit");
    let h = "";
    if (edit) h += `<div class="inf-box"><span class="t">Dosya yükle</span><div class="inf-frow" style="margin-top:0.5rem;">
      <select class="ipt" id="infFileKind">${opt(FILE_KINDS, "insights")}</select><input class="ipt" type="file" id="infFileInput" accept="video/*,image/*,.pdf">
      <div class="full"><button class="btn-g inf-sm" onclick="INF.uploadFile('creator',${c.id})"><i class="bi bi-upload"></i> Yükle</button> <span class="muted">Video ≤ 60 MB, görsel ≤ 10 MB. Insights videoları 90 gün sonra silinir.</span></div></div></div>`;
    h += sec("Dosyalar");
    if (!(c.files || []).length) h += '<div class="inf-empty">Dosya yok.</div>';
    (c.files || []).forEach((f) => {
      h += `<div class="inf-box"><div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;"><i class="bi ${String(f.content_type || "").startsWith("video") ? "bi-camera-video" : (String(f.content_type || "").startsWith("image") ? "bi-image" : "bi-file-earmark")}"></i>
        <a class="t" href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.original_name)}</a>${pill((FILE_KINDS.find((k) => k[0] === f.kind) || [f.kind, f.kind])[1], "gray")}
        <span class="muted" style="margin-left:auto;">${num((f.size_bytes || 0) / 1048576, 1)} MB · ${esc(f.created_at_label || "")}${f.uploaded_by ? " · " + esc(f.uploaded_by) : ""}</span></div></div>`;
    });
    return h;
  }
  function creatorNotesHtml(c) {
    let h = "";
    if (can("influencer", "edit")) h += `<div class="inf-box"><textarea class="ipt" id="infNoteBody" rows="2" placeholder="Not ekle… (görüşme, WhatsApp, karar)"></textarea>
      <div class="inf-actions"><button class="btn-g inf-sm" onclick="INF.addNote(${c.id},null)"><i class="bi bi-plus-lg"></i> Not ekle</button></div></div>`;
    h += timelineHtml(c.activities || []);
    return h;
  }
  function timelineHtml(acts) {
    if (!acts.length) return '<div class="inf-empty">Kayıt yok.</div>';
    return '<ul class="inf-tl">' + acts.map((a) => `<li class="inf-tl-item"><span class="inf-tl-dot"></span>
      <div class="inf-tl-meta">${esc(a.created_at_label || "")} · ${esc(a.author_name || "sistem")}${a.type && a.type !== "note" ? " · " + esc(a.type) : ""}${a.collab_id ? ` · <a href="#" onclick="event.preventDefault();INF.openCollab(${a.collab_id})">#${a.collab_id}</a>` : ""}</div>
      ${a.subject ? `<div style="font-weight:600;font-size:0.86rem;">${esc(a.subject)}</div>` : ""}<div class="inf-tl-body">${esc(a.body || "")}</div></li>`).join("") + "</ul>";
  }
  async function addNote(creatorId, collabId) {
    const body = val("infNoteBody");
    if (!body) { toast("Not boş."); return; }
    try {
      await api("/creators/" + creatorId + "/activities", jbody({ type: "note", body: body, collab_id: collabId || null }));
      toast("Not eklendi.", "success");
      if (state.drawer && state.drawer.type === "collab") await openCollab(state.drawer.id, state.drawer.tab); else await refreshCreator();
    } catch (e) { toast(e.message); }
  }
  async function creatorModal(id) {
    await ensureTiers(); await ensureUsers();
    const c = id ? (state.drawer && state.drawer.type === "creator" && state.drawer.id === id ? state.drawer.data : await api("/creators/" + id)) : {};
    openModal(id ? "Creator düzenle — " + c.full_name : "Yeni creator", `
      <div class="inf-frow">
        <div><label class="lbl">Ad Soyad *</label><input class="ipt" id="infC_name" value="${esc(c.full_name || "")}"></div>
        <div><label class="lbl">E-posta</label><input class="ipt" id="infC_email" type="email" value="${esc(c.email || "")}"></div>
        <div><label class="lbl">Telefon</label><input class="ipt" id="infC_phone" value="${esc(c.phone || "")}"></div>
        <div><label class="lbl">Dil</label><select class="ipt" id="infC_lang">${opt([["tr", "Türkçe"], ["en", "İngilizce"]], c.language || "tr")}</select></div>
        <div><label class="lbl">Ülke (ISO-2)</label><input class="ipt" id="infC_country" maxlength="2" value="${esc(c.country || "TR")}"></div>
        <div><label class="lbl">Şehir</label><input class="ipt" id="infC_city" value="${esc(c.city || "")}"></div>
        <div><label class="lbl">Doğum yılı</label><input class="ipt" id="infC_by" type="number" min="1900" max="2100" value="${c.birth_year || ""}"></div>
        <div><label class="lbl">Model</label><select class="ipt" id="infC_model">${opt(MODEL_OPTS, c.accepted_model || "barter")}</select></div>
        <div class="full"><label class="lbl">Kategoriler (virgülle)</label><input class="ipt" id="infC_cats" value="${esc((c.categories || []).join(", "))}"></div>
        <div><label class="lbl">Cilt tipi</label><input class="ipt" id="infC_skin" value="${esc(c.skin_type || "")}"></div>
        <div><label class="lbl">Beden</label><input class="ipt" id="infC_size" value="${esc(c.clothing_size || "")}"></div>
        <div class="full"><label class="lbl">Alerjiler</label><input class="ipt" id="infC_allergy" value="${esc(c.allergies || "")}"></div>
        <div><label class="lbl">Kademe (elle)</label><select class="ipt" id="infC_tier">${tierOptions(c.tier_override_key || "", "Otomatik (takipçiye göre)")}</select></div>
        <div><label class="lbl">Doğrulama seviyesi</label><select class="ipt" id="infC_ver">${opt([["K1", "K1 — elle + ekran kaydı"], ["K2", "K2 — AQS raporu"], ["K3", "K3 — OAuth"]], c.verification_level || "", "—")}</select></div>
        <div><label class="lbl">Sahte takipçi (%)</label><input class="ipt" id="infC_fake" type="number" min="0" max="100" step="0.1" value="${c.fake_pct != null ? c.fake_pct : ""}"></div>
        <div><label class="lbl">Puan (1–5)</label><input class="ipt" id="infC_rating" type="number" min="1" max="5" value="${c.rating || ""}"></div>
        <div><label class="lbl">Sorumlu</label>${ownerSelect("infC_owner", c.owner_user_id)}</div>
        <div><label class="lbl">Kaynak</label><input class="ipt" id="infC_source" value="${esc(c.source || (id ? "" : "manual"))}"></div>
        ${id ? `<div><label class="muted" style="display:flex;gap:0.4rem;align-items:center;margin-top:1.4rem;"><input type="checkbox" id="infC_active" ${c.is_active !== false ? "checked" : ""}> Aktif</label></div>
        <div><label class="muted" style="display:flex;gap:0.4rem;align-items:center;margin-top:1.4rem;"><input type="checkbox" id="infC_dnr" ${c.do_not_resend ? "checked" : ""}> Tekrar gönderme</label></div>` : ""}
        <div class="full"><label class="lbl">Notlar</label><textarea class="ipt" id="infC_notes" rows="2">${esc(c.notes || "")}</textarea></div>
      </div>
      ${!id ? `<label class="lbl" style="margin-top:0.7rem;">Hesaplar</label>${accountRows("infC", 3)}` : ""}
    `, async () => {
      const body = {
        full_name: val("infC_name"), email: val("infC_email") || null, phone: val("infC_phone") || null, language: val("infC_lang"),
        country: val("infC_country") || null, city: val("infC_city") || null, birth_year: intOrNull("infC_by"), accepted_model: val("infC_model"),
        categories: val("infC_cats").split(",").map((s) => s.trim()).filter(Boolean), skin_type: val("infC_skin") || null,
        clothing_size: val("infC_size") || null, allergies: val("infC_allergy") || null,
        tier_override_key: val("infC_tier") || null, verification_level: val("infC_ver") || null, fake_pct: numOrNull("infC_fake"),
        rating: intOrNull("infC_rating"), owner_user_id: intOrNull("infC_owner"), source: val("infC_source") || null, notes: val("infC_notes") || null,
      };
      if (!body.full_name) { toast("Ad zorunlu."); return; }
      if (id) { body.is_active = chk("infC_active"); body.do_not_resend = chk("infC_dnr"); }
      else body.accounts = collectAccounts("infC", 3);
      const r = id ? await api("/creators/" + id, jbody(body, "PUT")) : await api("/creators", jbody(body));
      closeModal(); toast(id ? "Güncellendi." : "Creator oluşturuldu.", "success");
      loadCreators();
      openCreator(id || r.id);
    }, { wide: true });
  }
  async function setRelStage(id) {
    try {
      await api("/creators/" + id + "/stage", jbody({ stage: val("infRelStage"), reason: val("infRelReason") || null }));
      toast("İlişki aşaması güncellendi.", "success"); loadCreators(); refreshCreator();
    } catch (e) { toast(e.message); }
  }
  async function toggleResend(id, flag) {
    const c = state.drawer && state.drawer.data;
    if (!c) return;
    try {
      await api("/creators/" + id, jbody({ full_name: c.full_name, do_not_resend: flag }, "PUT"));
      toast(flag ? "Tekrar gönderme işaretlendi." : "Gönderime açıldı.", "success"); loadCreators(); refreshCreator();
    } catch (e) { toast(e.message); }
  }
  function tagsModal(id) {
    const c = state.drawer && state.drawer.data;
    openModal("Etiketler", `<div class="inf-mb"><label class="lbl">Etiketler (virgülle)</label><input class="ipt" id="infTags" value="${esc(((c && c.tags) || []).join(", "))}" placeholder="vegan, cilt bakımı, lansman"></div>`, async () => {
      await api("/creators/" + id + "/tags", jbody({ tags: val("infTags").split(",").map((s) => s.trim()).filter(Boolean) }));
      closeModal(); toast("Etiketler kaydedildi.", "success"); loadCreators(); refreshCreator();
    });
  }
  async function rescore(id) {
    try {
      const r = await api("/creators/" + id + "/score", jbody({}));
      state.scoreResult[id] = { score: r.score, components: r.components, flags: r.flags };
      toast("Skor: " + (r.score == null ? "—" : r.score), "success"); loadCreators(); openCreator(id, "accounts");
    } catch (e) { toast(e.message); }
  }
  function accountModal(cid, aid) {
    const c = state.drawer && state.drawer.data;
    const a = aid && c ? (c.accounts || []).find((x) => x.id === aid) || {} : {};
    openModal(aid ? "Hesap düzenle" : "Hesap ekle", `
      <div class="inf-frow">
        <div><label class="lbl">Platform *</label><select class="ipt" id="infA_plat">${opt(PLATFORM_OPTS, a.platform || "instagram")}</select></div>
        <div><label class="lbl">Kullanıcı adı *</label><input class="ipt" id="infA_handle" value="${esc(a.handle || "")}" placeholder="@'siz"></div>
        <div><label class="lbl">Takipçi</label><input class="ipt" id="infA_f" type="number" min="0" value="${a.followers != null ? a.followers : ""}"></div>
        <div><label class="lbl">Takip edilen</label><input class="ipt" id="infA_fg" type="number" min="0" value="${a.following != null ? a.following : ""}"></div>
        <div><label class="lbl">Gönderi sayısı</label><input class="ipt" id="infA_pc" type="number" min="0" value="${a.posts_count != null ? a.posts_count : ""}"></div>
        <div><label class="lbl">Dış kimlik (kanal id vb.)</label><input class="ipt" id="infA_ext" value="${esc(a.external_id || "")}"></div>
        <div><label class="muted" style="display:flex;gap:0.4rem;align-items:center;margin-top:1.2rem;"><input type="checkbox" id="infA_primary" ${a.is_primary ? "checked" : ""}> Birincil hesap</label></div>
        <div><label class="muted" style="display:flex;gap:0.4rem;align-items:center;margin-top:1.2rem;"><input type="checkbox" id="infA_hidden" ${a.hidden_subscriber_count ? "checked" : ""}> Abone sayısı gizli (YouTube)</label></div>
      </div>`, async () => {
      const body = { platform: val("infA_plat"), handle: val("infA_handle").replace(/^@/, ""), followers: intOrNull("infA_f"), following: intOrNull("infA_fg"),
        posts_count: intOrNull("infA_pc"), external_id: val("infA_ext") || null, is_primary: chk("infA_primary"), hidden_subscriber_count: chk("infA_hidden") };
      if (!body.handle) { toast("Kullanıcı adı zorunlu."); return; }
      if (aid) await api("/creators/" + cid + "/accounts/" + aid, jbody(body, "PUT")); else await api("/creators/" + cid + "/accounts", jbody(body));
      closeModal(); toast("Hesap kaydedildi.", "success"); loadCreators(); openCreator(cid, "accounts");
    });
  }
  async function deleteAccount(cid, aid) {
    if (!confirm("Hesap silinsin mi? Metrik geçmişi de silinir.")) return;
    try { await api("/creators/" + cid + "/accounts/" + aid, { method: "DELETE" }); toast("Hesap silindi.", "info"); loadCreators(); openCreator(cid, "accounts"); }
    catch (e) { toast(e.message); }
  }
  function metricsModal(cid, aid) {
    const c = state.drawer && state.drawer.data;
    const a = c ? (c.accounts || []).find((x) => x.id === aid) || {} : {};
    const N = 12;
    let rows = "";
    for (let i = 0; i < N; i++) {
      rows += `<tr><td class="muted">${i + 1}</td>${["l", "c", "v", "s", "sh"].map((k) => `<td><input type="number" min="0" id="infM_${k}${i}" ${k === "v" ? 'placeholder="—"' : ""}></td>`).join("")}</tr>`;
    }
    openModal("Son 12 gönderi — @" + (a.handle || ""), `
      <p class="muted" style="font-size:0.82rem;">Creator'ın profilindeki <b>en yeni 12 gönderiyi</b> sırayla girin (boş satırlar atlanır). İzlenme yalnız video/Reel'de vardır; Story sayılmaz. Motor: ER / takipçi, ER / izlenme, izlenme / takipçi, yorum-beğeni oranı, izlenme CV ve özgünlük skoru.</p>
      <div class="inf-frow3 inf-mb">
        <div><label class="lbl">Takipçi (bugün)</label><input class="ipt" id="infM_followers" type="number" min="0" value="${a.followers != null ? a.followers : ""}"></div>
        <div><label class="lbl">Takip edilen</label><input class="ipt" id="infM_following" type="number" min="0" value="${a.following != null ? a.following : ""}"></div>
        <div><label class="lbl">Gönderi sayısı</label><input class="ipt" id="infM_posts" type="number" min="0" value="${a.posts_count != null ? a.posts_count : ""}"></div>
      </div>
      <div style="overflow-x:auto;"><table class="inf-mtable"><thead><tr><th>#</th><th>Beğeni</th><th>Yorum</th><th>İzlenme</th><th>Kaydetme</th><th>Paylaşım</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="inf-frow3" style="margin-top:0.8rem;">
        <div><label class="lbl">Kitle hedef ülke %</label><input class="ipt" id="infM_geo" type="number" min="0" max="100" placeholder="Insights → Kitle"></div>
        <div><label class="lbl">Şüpheli yorum %</label><input class="ipt" id="infM_cq" type="number" min="0" max="100" placeholder="emoji-only / bot"></div>
        <div><label class="lbl">Sahte takipçi %</label><input class="ipt" id="infM_fake" type="number" min="0" max="100" step="0.1" value="${c && c.fake_pct != null ? c.fake_pct : ""}" placeholder="HypeAuditor / Modash"></div>
      </div>
      ${a.platform === "youtube" ? `<label class="muted" style="display:flex;gap:0.4rem;align-items:center;margin-top:0.6rem;"><input type="checkbox" id="infM_hidden" ${a.hidden_subscriber_count ? "checked" : ""}> Abone sayısı gizli</label>` : ""}
    `, async () => {
      const posts = [];
      for (let i = 0; i < N; i++) {
        const p = { likes: numOrNull("infM_l" + i), comments: numOrNull("infM_c" + i), views: numOrNull("infM_v" + i), saves: numOrNull("infM_s" + i), shares: numOrNull("infM_sh" + i) };
        if (Object.keys(p).some((k) => p[k] != null)) posts.push(p);
      }
      if (!posts.length) { toast("En az bir gönderi girin."); return; }
      const body = { followers: intOrNull("infM_followers"), following: intOrNull("infM_following"), posts_count: intOrNull("infM_posts"), posts: posts,
        geo_target_pct: numOrNull("infM_geo"), comment_quality_pct: numOrNull("infM_cq") == null ? null : 100 - numOrNull("infM_cq"), fake_pct: numOrNull("infM_fake"),
        hidden_subs: el("infM_hidden") ? chk("infM_hidden") : null, source: "manual" };
      const r = await api("/creators/" + cid + "/accounts/" + aid + "/metrics", jbody(body));
      state.scoreResult[cid] = { score: r.score, components: r.components, flags: r.flags };
      closeModal(); toast("Metrikler kaydedildi — skor " + (r.score == null ? "—" : r.score) + ".", "success");
      loadCreators(); openCreator(cid, "accounts");
    }, { wide: true, saveLabel: "Hesapla ve kaydet" });
  }
  function tokenModal(cid, purpose) {
    const c = state.drawer && state.drawer.data;
    const collabs = ((c && c.collabs) || []).filter((x) => !["tamamlandi", "iptal"].includes(x.stage));
    openModal("Link üret — " + (TOKEN_PURPOSE[purpose] || purpose), `
      <p class="muted" style="font-size:0.82rem;">${purpose === "address" ? "Creator kargo adresini bu linkten kendisi girer (tek kullanım)." : purpose === "insights" ? "Creator Insights ekran kaydını (≤60 MB) + 30 günlük erişim/etkileşim sayılarını yükler." : "Creator içerik kılavuzunu ve pazar etiketi şablonunu okuyup onaylar (reklam mevzuatı kanıtı)."}</p>
      <div class="inf-frow">
        <div><label class="lbl">Geçerlilik (gün)</label><input class="ipt" id="infT_days" type="number" min="1" max="90" value="14"></div>
        ${purpose !== "address" ? `<div><label class="lbl">İş birliği</label><select class="ipt" id="infT_collab">${opt(collabs.map((x) => [x.id, x.code + " · " + (x.stage_label || x.stage)]), collabs.length ? collabs[0].id : "", "—")}</select></div>` : ""}
      </div><div id="infT_result"></div>`, async () => {
      const body = { purpose: purpose, days: intOrNull("infT_days") || 14 };
      const cl = intOrNull("infT_collab"); if (cl) body.collab_id = cl;
      const r = await api("/creators/" + cid + "/tokens", jbody(body));
      const full = location.origin + r.url;
      el("infT_result").innerHTML = `<div class="inf-info" style="margin-top:0.7rem;">Link hazır (son: ${esc(r.expires_at_label || "")}):<br><div style="display:flex;gap:0.4rem;margin-top:0.3rem;"><input class="ipt" readonly value="${esc(full)}" onclick="this.select()"><button class="btn-o inf-sm" onclick="INF.copy('${esc(full)}')"><i class="bi bi-clipboard"></i></button></div></div>`;
      el("infMSave").style.display = "none";
      openCreator(cid, "links");
    }, { saveLabel: "Link üret" });
  }
  async function uploadFile(entity, entityId) {
    const inp = el("infFileInput");
    const f = inp && inp.files && inp.files[0];
    if (!f) { toast("Dosya seçin."); return; }
    const fd = new FormData();
    fd.append("entity", entity); fd.append("entity_id", String(entityId)); fd.append("kind", val("infFileKind") || "other"); fd.append("file", f);
    try {
      await api("/files/upload", { method: "POST", body: fd });
      toast("Yüklendi.", "success");
      if (state.drawer && state.drawer.type === "collab") openCollab(state.drawer.id, state.drawer.tab); else refreshCreator();
    } catch (e) { toast(e.message); }
  }

  // =========================================================================
  // 3) PANO (Kanban) + iş birliği drawer'ı
  // =========================================================================
  async function renderBoard(first) {
    const v = el("inf-panel-board");
    await ensureTiers(); await ensureCampaigns();
    if (first || !el("infKanban")) {
      v.innerHTML = `<div class="inf-toolbar">
        <select class="ipt" id="infBdStore">${opt(STORE_OPTS, state.boardFilter.store, "Tüm mağazalar")}</select>
        <select class="ipt" id="infBdCamp">${opt((state.campaigns || []).map((c) => [c.id, c.name]), state.boardFilter.campaign, "Tüm kampanyalar")}</select>
        <button class="btn-o" onclick="INF.reloadBoard()" title="Yenile"><i class="bi bi-arrow-clockwise"></i></button>
        <span class="muted" style="flex:1;">Kartı sürükleyerek aşama değiştirin; iptalde gerekçe zorunlu.</span>
        ${can("influencer", "edit") ? '<button class="btn-g" onclick="INF.collabModal()"><i class="bi bi-plus-lg"></i> Yeni iş birliği</button>' : ""}
      </div><div id="infKanban" class="inf-kanban"><div class="inf-empty">Yükleniyor…</div></div>`;
      el("infBdStore").addEventListener("change", () => { state.boardFilter.store = val("infBdStore"); loadBoard(); });
      el("infBdCamp").addEventListener("change", () => { state.boardFilter.campaign = val("infBdCamp"); loadBoard(); });
    }
    loadBoard();
  }
  async function loadBoard() {
    const box = el("infKanban");
    if (!box) return;
    const p = [];
    if (state.boardFilter.store) p.push("store_key=" + encodeURIComponent(state.boardFilter.store));
    if (state.boardFilter.campaign) p.push("campaign_id=" + encodeURIComponent(state.boardFilter.campaign));
    try {
      const d = await api("/collabs/board" + (p.length ? "?" + p.join("&") : ""));
      state.board = d; state.transitions = d.transitions || {};
      const editable = can("influencer", "edit");
      box.innerHTML = (d.columns || []).map((col) => `<div class="inf-kcol${col.terminal ? " terminal" : ""}" data-stage="${esc(col.stage)}"
          ${editable ? `ondragover="INF.allowDrop(event,this)" ondragleave="this.classList.remove('dragover')" ondrop="INF.dropCollab(event,'${esc(col.stage)}')"` : ""}>
          <div class="inf-kcol-head"><span class="nm">${esc(col.label)}</span><span class="tot">${col.count}</span></div>
          <div>${(col.items || []).map((x) => collabCard(x, editable)).join("")}</div></div>`).join("") || '<div class="inf-empty">Aşama yok.</div>';
    } catch (e) { box.innerHTML = '<div class="inf-empty">' + esc(e.message) + "</div>"; }
  }
  function collabCard(x, editable) {
    const terminal = ["tamamlandi", "iptal"].includes(x.stage);
    const stale = !terminal && (x.days_in_stage || 0) > 14;
    return `<div class="inf-kcard${x.no_post_flagged_at ? " nopost" : ""}" ${editable && !terminal ? `draggable="true" ondragstart="INF.dragCollab(event,${x.id},'${esc(x.stage)}')"` : ""} onclick="INF.openCollab(${x.id})">
      <div class="t">${esc(x.creator_name || "—")}</div>
      <div class="sub">${esc(x.code)} · ${esc(STORES[x.store_key] || x.store_key)} · ${esc(x.market || "")}</div>
      <div class="meta">${tierPill(x.creator_tier_key || x.tier_key_at_start)}${x.decision ? pill(x.decision_label || x.decision, (DECISIONS[x.decision] || ["", "gray"])[1]) : ""}${x.no_post_flagged_at ? pill("Paylaşmadı", "r") : ""}
        <span class="inf-kage${stale ? " stale" : ""}" title="Bu aşamada geçirilen gün${stale ? " — bayatlıyor" : ""}">${x.days_in_stage != null ? x.days_in_stage + " g" : ""}</span></div>
    </div>`;
  }
  function dragCollab(ev, id, stage) { state.dragging = { id: id, stage: stage }; try { ev.dataTransfer.setData("text/plain", String(id)); ev.dataTransfer.effectAllowed = "move"; } catch (e) { /* */ } }
  function allowDrop(ev, col) {
    const dg = state.dragging;
    const to = col.dataset.stage;
    if (!dg || dg.stage === to) return;
    const allowed = (state.transitions[dg.stage] || []).includes(to);
    if (!allowed) return;
    ev.preventDefault(); col.classList.add("dragover");
  }
  async function dropCollab(ev, to) {
    ev.preventDefault();
    document.querySelectorAll(".inf-kcol").forEach((c) => c.classList.remove("dragover"));
    const dg = state.dragging; state.dragging = null;
    if (!dg || dg.stage === to) return;
    if (!(state.transitions[dg.stage] || []).includes(to)) { toast("Geçersiz geçiş: " + (STAGE_LABELS[dg.stage] || dg.stage) + " → " + (STAGE_LABELS[to] || to)); return; }
    if (to === "iptal") { cancelModal(dg.id); return; }
    await moveStage(dg.id, to, null);
  }
  async function moveStage(id, to, reason) {
    try {
      await api("/collabs/" + id + "/stage", jbody({ stage: to, reason: reason || null }));
      toast((STAGE_LABELS[to] || to) + " aşamasına taşındı.", "success");
      loadBoard();
      if (state.drawer && state.drawer.type === "collab" && state.drawer.id === id) openCollab(id, state.drawer.tab);
      state.loaded.ship = false;
    } catch (e) { toast(e.message); loadBoard(); }
  }
  function cancelModal(id) {
    openModal("İş birliğini iptal et", `<div class="inf-mb"><label class="lbl">İptal gerekçesi (zorunlu)</label><textarea class="ipt" id="infCancelReason" rows="3" placeholder="Ör. creator yanıt vermedi, ürün aldı paylaşmadı, marka uyumsuzluğu…"></textarea></div>`, async () => {
      const r = val("infCancelReason");
      if (!r) { toast("Gerekçe zorunlu."); return; }
      closeModal(); await moveStage(id, "iptal", r);
    }, { saveLabel: "İptal et" });
  }
  async function openCollab(id, tab) {
    try {
      const d = await api("/collabs/" + id);
      state.drawer = { type: "collab", id: id, data: d, tab: tab || (state.drawer && state.drawer.type === "collab" && state.drawer.id === id ? state.drawer.tab : "summary") };
      renderDrawer();
    } catch (e) { toast(e.message); }
  }
  function renderCollabDrawer() {
    const x = state.drawer.data;
    const tabs = [{ key: "summary", label: "Özet" }, { key: "ship", label: "Gönderimler" }, { key: "content", label: "İçerikler" }, { key: "history", label: "Geçmiş & Notlar" }];
    openDrawerShell(x.code + " · " + (x.creator_name || ""), `${storePill(x.store_key)} ${stagePill(x.stage, x.stage_label)} <span style="opacity:.8;">${esc(MODELS[x.model] || x.model)} · ${esc(x.market || "")}</span>`, tabs, state.drawer.tab);
    const b = el("infDwBody");
    const t = state.drawer.tab;
    if (t === "summary") b.innerHTML = collabSummaryHtml(x);
    else if (t === "ship") b.innerHTML = collabShipHtml(x);
    else if (t === "content") b.innerHTML = collabContentHtml(x);
    else b.innerHTML = collabHistoryHtml(x);
  }
  function collabSummaryHtml(x) {
    const edit = can("influencer", "edit");
    const terminal = ["tamamlandi", "iptal"].includes(x.stage);
    const allowed = x.allowed_stages || [];
    const sug = x.decision_suggested;
    return `
      ${x.no_post_flagged_at ? `<div class="inf-warn"><i class="bi bi-exclamation-triangle"></i> Ürün alındı, ${CFG[CFG_KEY.noPost] || 45} gün içinde paylaşım yok (${esc(x.no_post_flagged_at_label || "")}).</div>` : ""}
      ${x.cancel_reason ? `<div class="inf-warn">İptal gerekçesi: ${esc(x.cancel_reason)}</div>` : ""}
      ${edit && !terminal && allowed.length ? `<div class="inf-box"><span class="t">Aşama değiştir</span><div class="inf-frow" style="margin-top:0.4rem;">
        <select class="ipt" id="infStTo">${opt(allowed.map((s) => [s, STAGE_LABELS[s] || s]))}</select>
        <div style="display:flex;gap:0.4rem;"><input class="ipt" id="infStReason" placeholder="Gerekçe (iptalde zorunlu)"><button class="btn-g" onclick="INF.stageFromDrawer(${x.id})">Taşı</button></div></div></div>` : ""}
      ${edit ? `<div class="inf-actions"><button class="btn-o inf-sm" onclick="INF.collabModal(${x.id})"><i class="bi bi-pencil"></i> Düzenle</button>
        ${!terminal && !x.no_post_flagged_at && x.stage === "icerik_bekleniyor" ? "" : ""}</div>` : ""}
      ${sec("Karar (30 gün sonrası)")}
      ${sug ? `<div class="inf-info">Motor önerisi: <b>${esc((DECISIONS[sug.decision] || [sug.decision])[0])}</b>${(sug.reasons || []).length ? " — " + esc(sug.reasons.join("; ")) : ""}</div>` : '<div class="muted" style="margin-bottom:0.5rem;">Öneri satış verisi UpPromote\'tan akınca (Faz 2) üretilir; kararı patron verir.</div>'}
      ${x.decision ? kv("Karar", pill(x.decision_label || x.decision, (DECISIONS[x.decision] || ["", "gray"])[1])) : ""}
      ${can("influencer", "approve") ? `<div class="inf-frow" style="margin-top:0.4rem;"><input class="ipt full" id="infDecNote" placeholder="Karar notu (isteğe bağlı)"></div>
        <div class="inf-actions">${Object.keys(DECISIONS).map((k) => `<button class="btn-o inf-sm" onclick="INF.setDecision(${x.id},'${k}')">${esc(DECISIONS[k][0])}</button>`).join("")}</div>` : ""}
      ${sec("Bilgiler")}
      ${kv("Creator", `<a href="#" onclick="event.preventDefault();INF.openCreator(${x.creator_id})">${esc(x.creator_name)}</a>`)}
      ${kv("Mağaza / pazar", esc(STORES[x.store_key] || x.store_key) + " · " + esc(x.market || ""))}${kv("Model", esc(MODELS[x.model] || x.model))}
      ${kv("Kampanya", esc(x.campaign_name))}${kv("Başlangıç kademesi", tierPill(x.tier_key_at_start))}
      ${kv("Aşama", stagePill(x.stage, x.stage_label) + ` <span class="muted">${x.days_in_stage != null ? x.days_in_stage + " gündür" : ""} · ${esc(x.stage_changed_at_label || "")}</span>`)}
      ${kv("Teklif / kabul", esc(x.offered_at_label || "—") + " / " + esc(x.accepted_at_label || "—"))}
      ${kv("İçerik son tarihi", esc(x.content_due_on_label))}${kv("Kullanım hakkı", esc(x.usage_rights))}${kv("Münhasırlık", x.exclusivity_days ? x.exclusivity_days + " gün" : "")}
      ${kv("Kılavuz onayı", x.guideline_ack_at_label ? pill("Onaylı " + x.guideline_ack_at_label + " · " + (x.guideline_version || ""), "g") : pill("Onay yok", "y"))}
      ${kv("Teslimatlar", (x.deliverables || []).length ? "<ul style='margin:0;padding-left:1rem;'>" + x.deliverables.map((d) => `<li>${esc([d.platform, d.type, d.count ? d.count + " adet" : ""].filter(Boolean).join(" · ") || JSON.stringify(d))}</li>`).join("") + "</ul>" : "")}
      ${kv("Sorumlu", esc(x.owner_name))}${kv("Notlar", esc(x.notes))}${kv("Açılış", esc(x.created_at_label) + (x.created_by ? " · " + esc(x.created_by) : ""))}`;
  }
  async function stageFromDrawer(id) {
    const to = val("infStTo"), reason = val("infStReason");
    if (to === "iptal" && !reason) { toast("İptal için gerekçe zorunlu."); return; }
    await moveStage(id, to, reason || null);
  }
  async function setDecision(id, decision) {
    try {
      await api("/collabs/" + id + "/decision", jbody({ decision: decision, note: val("infDecNote") || null }));
      toast("Karar kaydedildi: " + DECISIONS[decision][0], "success"); openCollab(id, "summary"); loadBoard();
    } catch (e) { toast(e.message); }
  }
  function collabShipHtml(x) {
    const terminal = ["tamamlandi", "iptal"].includes(x.stage);
    let h = sec("Gönderimler", can("influencer", "ship") && !terminal ? `<button class="btn-o inf-sm" onclick="INF.shipmentModal(${x.id})"><i class="bi bi-box-seam"></i> Yeni gönderim</button>` : "");
    if (!(x.shipments || []).length) h += '<div class="inf-empty">Gönderim yok.</div>';
    (x.shipments || []).forEach((s) => { h += shipmentBox(s); });
    return h;
  }
  function shipStatus(s) {
    if (s.delivered_at) return pill("Teslim edildi", "g");
    if (s.delivery_status === "shipped") return pill("Kargoda", "b");
    if (s.delivery_status === "preparing") return pill("Hazırlanıyor", "y");
    return pill(s.delivery_status || "—", "gray");
  }
  function shipActions(s) {
    const ship = can("influencer", "ship");
    let h = `<a class="btn-o inf-sm" href="${esc(s.document_url)}" target="_blank" rel="noopener" title="Şerhli teslimat belgesi (PDF)"><i class="bi bi-file-earmark-pdf"></i> PDF</a>`;
    if (ship && s.delivery_status === "preparing") h += ` <button class="btn-g inf-sm" onclick="INF.shipModal(${s.id})"><i class="bi bi-truck"></i> Kargola</button>`;
    if (ship && s.delivery_status === "shipped" && !s.delivered_at) h += ` <button class="btn-g inf-sm" onclick="INF.deliveredModal(${s.id})"><i class="bi bi-check2-circle"></i> Teslim edildi</button>`;
    return h;
  }
  function shipmentBox(s) {
    return `<div class="inf-box"><div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;"><span class="t">${esc(s.document_no || "—")}</span>${shipStatus(s)}<span class="muted" style="margin-left:auto;">${esc(s.created_at_label || "")}</span></div>
      <div class="sub">${(s.items || []).map((i) => esc(i.item_name) + " × " + num(i.quantity)).join(", ") || "—"}</div>
      <div class="sub">Yüklü maliyet <b>${money(s.loaded_cost)}</b> (COGS ${money(s.cogs_total)} + paket ${money(s.packaging_cost)} + kargo ${money(s.shipping_cost)})</div>
      ${s.tracking_no ? `<div class="sub">${esc(s.carrier || "")} ${esc(s.tracking_no)} · kargo ${esc(s.shipped_at_label || "")}</div>` : ""}
      ${s.delivered_at ? `<div class="sub">Teslim ${esc(s.delivered_at_label)} · hatırlatma ${esc(s.reminder1_due_label || "—")} / ${esc(s.reminder2_due_label || "—")}</div>` : ""}
      ${s.handwritten_note ? `<div class="sub">✍ ${esc(s.handwritten_note)}</div>` : ""}
      <div class="inf-actions">${shipActions(s)}</div></div>`;
  }
  function collabContentHtml(x) {
    const edit = can("influencer", "edit");
    let h = sec("İçerikler", edit ? `<button class="btn-o inf-sm" onclick="INF.contentModal(${x.id})"><i class="bi bi-plus-lg"></i> İçerik</button>` : "");
    if (edit) h += `<div class="inf-box"><span class="t">Taslak dosyası yükle</span><div class="inf-frow" style="margin-top:0.4rem;"><select class="ipt" id="infFileKind">${opt(FILE_KINDS, "draft")}</select><input class="ipt" type="file" id="infFileInput" accept="video/*,image/*">
      <div class="full"><button class="btn-o inf-sm" onclick="INF.uploadFile('collab',${x.id})"><i class="bi bi-upload"></i> Yükle</button></div></div></div>`;
    if (!(x.contents || []).length) h += '<div class="inf-empty">İçerik yok.</div>';
    (x.contents || []).forEach((c) => {
      const st = CONTENT_STATUS[c.status] || [c.status, "gray"];
      const comp = c.compliance || null;
      h += `<div class="inf-box"><div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;">${platIcon(c.platform)}<span class="t">${esc((CONTENT_TYPES.find((t) => t[0] === c.type) || [c.type, c.type])[1] || "İçerik")}</span>${pill(st[0], st[1])}
        ${c.compliance_score != null ? scoreBadge(c.compliance_score, "Uyum skoru") : ""}<span class="muted" style="margin-left:auto;">${esc(c.published_at_label || c.updated_at_label || c.created_at_label || "")}</span></div>
        ${c.url ? `<div class="sub"><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.url)}</a></div>` : ""}
        ${c.caption ? `<div class="sub" style="white-space:pre-wrap;">${esc(c.caption.slice(0, 220))}${c.caption.length > 220 ? "…" : ""}</div>` : ""}
        ${c.review_note ? `<div class="sub">İnceleme notu: ${esc(c.review_note)}</div>` : ""}
        ${comp ? `<div style="margin-top:0.3rem;">${comp.has_label ? pill("Etiket var" + (comp.label_first_line ? " · ilk satır" : ""), "g") : pill("Etiket yok", "r")} ${comp.brand_mentioned ? pill("Marka etiketli", "g") : pill("Marka etiketsiz", "y")} ${(comp.forbidden_hits || []).length ? pill("Yasak: " + comp.forbidden_hits.join(", "), "r") : ""}</div>` : ""}
        ${c.metrics_d7 || c.metrics_d30 ? `<div class="inf-comp">${["d7", "d30"].map((w) => { const m = c["metrics_" + w]; return m ? Object.keys(m).map((k) => `<div><b>${num(m[k])}</b>${esc(k)} (${w})</div>`).join("") : ""; }).join("")}</div>` : ""}
        ${edit ? `<div class="inf-actions">
          ${["taslak", "incelemede", "revizyon"].includes(c.status) ? `<button class="btn-o inf-sm" onclick="INF.reviewModal(${c.id})"><i class="bi bi-eye"></i> İncele</button>` : ""}
          ${c.status !== "yayinlandi" ? `<button class="btn-g inf-sm" onclick="INF.publishedModal(${c.id})"><i class="bi bi-megaphone"></i> Yayınlandı</button>` : ""}
          ${c.status === "yayinlandi" ? `<button class="btn-o inf-sm" onclick="INF.contentMetricsModal(${c.id})"><i class="bi bi-bar-chart"></i> Metrik</button>` : ""}</div>` : ""}
      </div>`;
    });
    return h;
  }
  function collabHistoryHtml(x) {
    let h = "";
    if (can("influencer", "edit")) h += `<div class="inf-box"><textarea class="ipt" id="infNoteBody" rows="2" placeholder="Not ekle…"></textarea><div class="inf-actions"><button class="btn-g inf-sm" onclick="INF.addNote(${x.creator_id},${x.id})"><i class="bi bi-plus-lg"></i> Not ekle</button></div></div>`;
    h += sec("Aşama geçmişi");
    h += (x.stage_log || []).length ? '<ul class="inf-tl">' + x.stage_log.map((l) => `<li class="inf-tl-item"><span class="inf-tl-dot"></span><div class="inf-tl-meta">${esc(l.at_label || "")} · ${esc(l.actor || "")}</div>
      <div class="inf-tl-body">${l.from_label ? esc(l.from_label) + " → " : ""}<b>${esc(l.to_label)}</b>${l.reason ? " — " + esc(l.reason) : ""}</div></li>`).join("") + "</ul>" : '<div class="muted">—</div>';
    h += sec("Notlar & olaylar");
    h += timelineHtml(x.activities || []);
    return h;
  }
  async function collabModal(id, creatorId) {
    await ensureTiers(); await ensureUsers(); await ensureCampaigns();
    const x = id ? (state.drawer && state.drawer.type === "collab" && state.drawer.id === id ? state.drawer.data : await api("/collabs/" + id)) : { creator_id: creatorId || null };
    const dl = (x.deliverables || []).map((d) => [d.platform, d.type, d.count].filter((v) => v != null && v !== "").join(":")).join("\n");
    openModal(id ? "İş birliği düzenle — " + x.code : "Yeni iş birliği", `
      ${!id ? `<div class="inf-mb inf-pick"><label class="lbl">Creator *</label><input class="ipt" id="infCo_q" placeholder="Ad / e-posta / @handle ile ara…" autocomplete="off" ${x.creator_id ? "disabled" : ""}><div class="inf-matches" id="infCo_matches"></div>
        <input type="hidden" id="infCo_creator" value="${x.creator_id || ""}"><div class="muted" id="infCo_sel" style="margin-top:0.3rem;">${x.creator_id ? "Seçili creator #" + x.creator_id : "Seçilmedi"}</div></div>` : ""}
      <div class="inf-frow">
        <div><label class="lbl">Mağaza</label><select class="ipt" id="infCo_store">${opt(STORE_OPTS, x.store_key || "minerva")}</select></div>
        <div><label class="lbl">Pazar</label><select class="ipt" id="infCo_market">${opt(MARKET_OPTS, x.market || "TR")}</select></div>
        <div><label class="lbl">Model</label><select class="ipt" id="infCo_model">${opt(MODEL_OPTS, x.model || "barter")}</select></div>
        <div><label class="lbl">Kampanya</label><select class="ipt" id="infCo_camp">${opt((state.campaigns || []).map((c) => [c.id, c.name]), x.campaign_id || "", "—")}</select></div>
        <div><label class="lbl">İçerik son tarihi</label><input class="ipt" type="date" id="infCo_due" value="${esc(x.content_due_on || "")}"></div>
        <div><label class="lbl">Kullanım hakkı</label><input class="ipt" id="infCo_rights" value="${esc(x.usage_rights || "")}" placeholder="ör. 6 ay organik + reklam"></div>
        <div><label class="lbl">Münhasırlık (gün)</label><input class="ipt" type="number" min="0" max="365" id="infCo_excl" value="${x.exclusivity_days != null ? x.exclusivity_days : 0}"></div>
        <div><label class="lbl">Sorumlu</label>${ownerSelect("infCo_owner", x.owner_user_id)}</div>
        ${!id ? `<div><label class="lbl">Başlangıç aşaması</label><select class="ipt" id="infCo_stage">${opt([["teklif_gonderildi", "Teklif Gönderildi"], ["kabul_edildi", "Kabul Edildi (teklif dışarıda kabul edildi)"]], "teklif_gonderildi")}</select></div>` : ""}
        <div class="full"><label class="lbl">Teslimatlar (satır başına platform:tip:adet)</label><textarea class="ipt" id="infCo_deliv" rows="2" placeholder="instagram:reel:1&#10;instagram:story:3">${esc(dl)}</textarea></div>
        <div class="full"><label class="lbl">Notlar</label><textarea class="ipt" id="infCo_notes" rows="2">${esc(x.notes || "")}</textarea></div>
      </div>`, async () => {
      const body = {
        creator_id: id ? x.creator_id : intOrNull("infCo_creator"), store_key: val("infCo_store"), market: val("infCo_market"), model: val("infCo_model"),
        campaign_id: intOrNull("infCo_camp"), content_due_on: val("infCo_due") || null, usage_rights: val("infCo_rights") || null,
        exclusivity_days: intOrNull("infCo_excl") || 0, owner_user_id: intOrNull("infCo_owner"), notes: val("infCo_notes") || null,
        deliverables: val("infCo_deliv").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const p = l.split(":"); return { platform: (p[0] || "").trim(), type: (p[1] || "").trim(), count: Number(p[2]) || 1 }; }),
      };
      if (!body.creator_id) { toast("Creator seçin."); return; }
      if (!id) body.stage = val("infCo_stage") || null;
      const r = id ? await api("/collabs/" + id, jbody(body, "PUT")) : await api("/collabs", jbody(body));
      closeModal(); toast(id ? "Güncellendi." : "İş birliği açıldı: " + (r.code || ""), "success");
      state.loaded.board = false; if (el("infKanban")) loadBoard();
      openCollab(id || r.id);
    }, { wide: true });
    if (!id && !x.creator_id) bindCreatorPicker();
  }
  function bindCreatorPicker() {
    const q = el("infCo_q"), m = el("infCo_matches");
    if (!q) return;
    let t = null;
    q.addEventListener("input", () => {
      clearTimeout(t);
      const s = q.value.trim();
      if (s.length < 2) { m.style.display = "none"; return; }
      t = setTimeout(async () => {
        try {
          const d = await api("/creators/lookup?q=" + encodeURIComponent(s));
          const items = d.items || [];
          m.innerHTML = items.length ? items.map((c) => `<div class="inf-match" onclick="INF.pickCreator(${c.id},'${esc(c.full_name)}',${c.do_not_resend ? "true" : "false"})"><span>${esc(c.full_name)} <span class="muted">${esc(c.email || "")}</span></span><span>${relPill(c.relationship_stage)}${c.do_not_resend ? " ⛔" : ""}</span></div>`).join("") : '<div class="inf-match muted">Eşleşme yok</div>';
          m.style.display = "block";
        } catch (e) { m.style.display = "none"; }
      }, 250);
    });
  }
  function pickCreator(id, name, dnr) {
    el("infCo_creator").value = String(id);
    el("infCo_sel").innerHTML = "Seçili: <b>" + esc(name) + "</b> #" + id + (dnr ? ' <span class="inf-pill inf-pill-r">tekrar gönderme</span>' : "");
    el("infCo_matches").style.display = "none";
    el("infCo_q").value = name;
  }
  function contentModal(collabId) {
    openModal("İçerik ekle", `<div class="inf-frow">
      <div><label class="lbl">Platform</label><select class="ipt" id="infCt_plat">${opt(PLATFORM_OPTS, "instagram")}</select></div>
      <div><label class="lbl">Tip</label><select class="ipt" id="infCt_type">${opt(CONTENT_TYPES, "reel")}</select></div>
      <div class="full"><label class="lbl">URL (varsa)</label><input class="ipt" id="infCt_url" placeholder="https://…"></div>
      <div class="full"><label class="lbl">Açıklama / caption taslağı</label><textarea class="ipt" id="infCt_cap" rows="3"></textarea></div></div>`, async () => {
      await api("/collabs/" + collabId + "/contents", jbody({ platform: val("infCt_plat"), type: val("infCt_type"), url: val("infCt_url") || null, caption: val("infCt_cap") || null }));
      closeModal(); toast("İçerik eklendi.", "success"); openCollab(collabId, "content"); loadBoard();
    });
  }
  function reviewModal(cid) {
    openModal("Taslak incelemesi", `<div class="inf-mb"><label class="lbl">Not (revizyonda zorunlu — creator'a iletilir)</label><textarea class="ipt" id="infRv_note" rows="3"></textarea></div>
      <div class="inf-actions"><button class="btn-g" onclick="INF.review(${cid},'approve')"><i class="bi bi-check-lg"></i> Onayla</button><button class="btn-o" onclick="INF.review(${cid},'revise')"><i class="bi bi-arrow-repeat"></i> Revizyon iste</button></div>`, null);
  }
  async function review(cid, action) {
    const note = val("infRv_note");
    if (action === "revise" && !note) { toast("Revizyon notu zorunlu."); return; }
    try {
      await api("/contents/" + cid + "/review", jbody({ action: action, note: note || null }));
      closeModal(); toast(action === "approve" ? "Taslak onaylandı." : "Revizyon istendi.", "success");
      if (state.drawer) openCollab(state.drawer.id, "content"); loadBoard();
    } catch (e) { toast(e.message); }
  }
  function publishedModal(cid) {
    openModal("Yayınlandı", `<p class="muted" style="font-size:0.82rem;">Yayın URL'si ve caption girildiğinde uyum kontrolü çalışır: pazar etiketi (#işbirliği / #ad) ilk satırda mı, marka etiketli mi, yasak kelime var mı.</p>
      <div class="inf-frow"><div class="full"><label class="lbl">Yayın URL'si</label><input class="ipt" id="infPb_url" placeholder="https://…"></div>
      <div><label class="lbl">Yayın tarihi</label><input class="ipt" type="date" id="infPb_on" value="${today()}"></div>
      <div class="full"><label class="lbl">Caption (yayındaki metin)</label><textarea class="ipt" id="infPb_cap" rows="4"></textarea></div></div>`, async () => {
      const r = await api("/contents/" + cid + "/published", jbody({ url: val("infPb_url") || null, caption: val("infPb_cap") || null, published_on: val("infPb_on") || null }));
      closeModal(); toast("Yayın kaydedildi" + (r && r.compliance_score != null ? " — uyum " + r.compliance_score : "") + ".", "success");
      if (state.drawer) openCollab(state.drawer.id, "content"); loadBoard();
    });
  }
  function contentMetricsModal(cid) {
    const F = [["views", "İzlenme"], ["reach", "Erişim"], ["likes", "Beğeni"], ["comments", "Yorum"], ["saves", "Kaydetme"], ["shares", "Paylaşım"], ["link_clicks", "Link tıklaması"]];
    openModal("İçerik metrikleri", `<div class="inf-frow"><div class="full"><label class="lbl">Pencere</label><select class="ipt" id="infCm_w">${opt([["d7", "7. gün"], ["d30", "30. gün"]], "d7")}</select></div>
      ${F.map((f) => `<div><label class="lbl">${esc(f[1])}</label><input class="ipt" type="number" min="0" id="infCm_${f[0]}"></div>`).join("")}</div>`, async () => {
      const metrics = {};
      F.forEach((f) => { const n = numOrNull("infCm_" + f[0]); if (n != null) metrics[f[0]] = n; });
      await api("/contents/" + cid + "/metrics", jbody({ window: val("infCm_w"), metrics: metrics }));
      closeModal(); toast("Metrikler kaydedildi.", "success");
      if (state.drawer) openCollab(state.drawer.id, "content"); loadBoard();
    });
  }

  // =========================================================================
  // 4) GÖNDERİMLER
  // =========================================================================
  async function renderShip(first) {
    const v = el("inf-panel-ship");
    if (first || !el("infShipTable")) {
      v.innerHTML = `<div class="inf-toolbar">
        <select class="ipt" id="infShStatus">${opt([["preparing", "Hazırlanıyor"], ["shipped", "Kargoda"], ["delivered", "Teslim edildi"], ["", "Tümü"]], state.shipFilter.status)}</select>
        <button class="btn-o" onclick="INF.reloadShip()" title="Yenile"><i class="bi bi-arrow-clockwise"></i></button>
        <span class="muted" style="flex:1;">Gönderim = Teslimat (kargo). Stok <b>Kargola</b> anında düşer; belge şerhli PDF'tir.</span>
        ${can("influencer", "ship") ? '<button class="btn-g" onclick="INF.pickCollabForShipment()"><i class="bi bi-box-seam"></i> Yeni gönderim</button>' : ""}
      </div><div class="card2"><div class="card2-body" id="infShipTable"><div class="inf-empty">Yükleniyor…</div></div></div>`;
      el("infShStatus").addEventListener("change", () => { state.shipFilter.status = val("infShStatus"); loadShip(); });
    }
    loadShip();
  }
  async function loadShip() {
    const box = el("infShipTable");
    if (!box) return;
    try {
      const d = await api("/shipments" + (state.shipFilter.status ? "?status=" + encodeURIComponent(state.shipFilter.status) : ""));
      state.ships = d.items || [];
      if (!state.ships.length) { box.innerHTML = '<div class="inf-empty">Bu kuyrukta gönderim yok.</div>'; return; }
      box.innerHTML = `<div style="overflow-x:auto;"><table>
        <thead><tr><th>Belge</th><th>İş birliği</th><th>Creator</th><th>Ürünler</th><th>Yüklü maliyet</th><th>Durum</th><th>Kargo</th><th>Tarihler</th><th></th></tr></thead>
        <tbody>${state.ships.map((s) => `<tr>
          <td><b>${esc(s.document_no || "—")}</b></td>
          <td><a href="#" onclick="event.preventDefault();INF.openCollab(${s.collab_id})">${esc(s.collab_code || "#" + s.collab_id)}</a></td>
          <td>${s.creator_id ? `<a href="#" onclick="event.preventDefault();INF.openCreator(${s.creator_id})">${esc(s.creator_name || "")}</a>` : esc(s.recipient_name || "")}</td>
          <td class="muted">${(s.items || []).map((i) => esc(i.item_name) + " × " + num(i.quantity)).join("<br>") || "—"}</td>
          <td title="COGS ${money(s.cogs_total)} + paket ${money(s.packaging_cost)} + kargo ${money(s.shipping_cost)}">${money(s.loaded_cost)}</td>
          <td>${shipStatus(s)}</td>
          <td class="muted">${s.tracking_no ? esc((s.carrier || "") + " " + s.tracking_no) : "—"}</td>
          <td class="muted">${esc(s.created_at_label || "")}${s.shipped_at_label ? "<br>kargo " + esc(s.shipped_at_label) : ""}${s.delivered_at_label ? "<br>teslim " + esc(s.delivered_at_label) : ""}</td>
          <td style="white-space:nowrap;">${shipActions(s)}</td>
        </tr>`).join("")}</tbody></table></div>`;
    } catch (e) { box.innerHTML = '<div class="inf-empty">' + esc(e.message) + "</div>"; }
  }
  async function pickCollabForShipment() {
    try {
      const d = await api("/collabs");
      const list = (d.items || []).filter((x) => x.is_active !== false && !["tamamlandi", "iptal"].includes(x.stage));
      if (!list.length) { toast("Açık iş birliği yok — önce Pano'dan iş birliği açın."); return; }
      openModal("Gönderim için iş birliği seç", `<div class="inf-mb"><label class="lbl">İş birliği</label><select class="ipt" id="infPk_collab">${opt(list.map((x) => [x.id, x.code + " · " + (x.creator_name || "") + " · " + (x.stage_label || x.stage)]))}</select></div>`, async () => {
        const id = intOrNull("infPk_collab");
        closeModal();
        if (id) shipmentModal(id);
      }, { saveLabel: "Devam" });
    } catch (e) { toast(e.message); }
  }
  async function shipmentModal(collabId) {
    let collab, creator;
    try {
      collab = (state.drawer && state.drawer.type === "collab" && state.drawer.id === collabId) ? state.drawer.data : await api("/collabs/" + collabId);
      creator = await api("/creators/" + collab.creator_id);
    } catch (e) { toast(e.message); return; }
    if (creator.do_not_resend) { toast("Bu creator 'tekrar gönderme' işaretli — gönderim engellendi."); return; }
    state.cart = [];
    const addrs = creator.addresses || [];
    const defAddr = addrs.find((a) => a.is_default) || addrs[0] || null;
    const packDef = Number(CFG[CFG_KEY.pack] || 0), shipDef = Number(CFG[CFG_KEY.ship] || 0);
    openModal("Yeni gönderim — " + collab.code + " · " + (collab.creator_name || ""), `
      ${!addrs.length ? '<div class="inf-warn">Kayıtlı adres yok — creator\'a "Adres formu" linki gönderin ya da alıcı bilgisini elle girin (belgeye yazılır).</div>' : ""}
      <div class="inf-frow">
        <div class="full"><label class="lbl">Adres</label><select class="ipt" id="infSh_addr" onchange="INF.shipAddrChange()">${opt(addrs.map((a) => [a.id, (a.label || "Adres") + " · " + [a.line1, a.district, a.city, a.country].filter(Boolean).join(", ")]), defAddr ? defAddr.id : "", "Adres seçilmedi (elle alıcı)")}</select></div>
        <div><label class="lbl">Alıcı adı</label><input class="ipt" id="infSh_name" value="${esc((defAddr && defAddr.recipient_name) || creator.full_name || "")}"></div>
        <div><label class="lbl">Alıcı telefon</label><input class="ipt" id="infSh_phone" value="${esc((defAddr && defAddr.phone) || creator.phone || "")}"></div>
        <div><label class="lbl">Belge dili</label><select class="ipt" id="infSh_lang">${opt([["TR", "Türkçe"], ["EN", "İngilizce"]], (collab.market || "TR").toUpperCase() === "TR" ? "TR" : "EN")}</select></div>
        <div><label class="lbl">Kademe azami ürün</label><input class="ipt" readonly value="${esc(maxGiftFor(collab.creator_tier_key || collab.tier_key_at_start))}"></div>
      </div>
      <label class="lbl" style="margin-top:0.8rem;">Ürünler</label>
      <div class="inf-pick"><input class="ipt" id="infSh_q" placeholder="Ürün adı / SKU yaz (en az 2 karakter)…" autocomplete="off"><div class="inf-matches" id="infSh_matches"></div></div>
      <div id="infSh_cart" style="margin-top:0.5rem;"></div>
      <div class="inf-frow" style="margin-top:0.7rem;">
        <div><label class="lbl">Paketleme maliyeti (TL)</label><input class="ipt" type="number" min="0" step="0.01" id="infSh_pack" value="${packDef}" oninput="INF.renderCart()"></div>
        <div><label class="lbl">Kargo maliyeti (TL)</label><input class="ipt" type="number" min="0" step="0.01" id="infSh_ship" value="${shipDef}" oninput="INF.renderCart()"></div>
      </div>
      <div class="inf-cost" id="infSh_cost"></div>
      <div class="inf-frow" style="margin-top:0.7rem;">
        <div class="full"><label class="lbl">El yazısı not (pakete)</label><input class="ipt" id="infSh_hand" placeholder="Ör. 'Merhaba Ayşe, cildinin keyfini çıkar 🌿'"></div>
        <div class="full"><label class="lbl">Belge notu</label><input class="ipt" id="infSh_note" placeholder="Teslimat belgesine ek not"></div>
      </div>`, async () => {
      if (!state.cart.length) { toast("En az bir ürün ekleyin."); return; }
      const body = {
        items: state.cart.map((c) => ({ item_id: c.item_id, quantity: Number(c.qty) || 1 })),
        address_id: intOrNull("infSh_addr"), recipient_name: val("infSh_name") || null, recipient_phone: val("infSh_phone") || null,
        doc_lang: val("infSh_lang"), handwritten_note: val("infSh_hand") || null, note: val("infSh_note") || null,
        packaging_cost: numOrNull("infSh_pack"), shipping_cost: numOrNull("infSh_ship"),
      };
      const r = await api("/collabs/" + collabId + "/shipments", jbody(body));
      closeModal();
      toast("Gönderim hazırlandı: " + r.document_no + " · yüklü maliyet " + money(r.loaded_cost), "success");
      (r.warnings || []).forEach((w) => toast(w, "info"));
      state.shipFilter.status = "preparing"; state.loaded.ship = false; if (el("infShipTable")) renderShip(true);
      loadBoard(); if (state.drawer && state.drawer.type === "collab") openCollab(collabId, "ship");
    }, { wide: true, saveLabel: "Hazırla (KRG belgesi)" });
    state.shipAddrs = addrs; state.shipCreator = creator;
    renderCart();
    bindItemPicker();
  }
  function maxGiftFor(tierKey) {
    const t = (state.tiers || []).find((x) => x.key === tierKey);
    return t && t.max_gift_items != null ? t.max_gift_items + " (" + t.label + ")" : "—";
  }
  function shipAddrChange() {
    const id = intOrNull("infSh_addr");
    const a = (state.shipAddrs || []).find((x) => x.id === id);
    if (a) { el("infSh_name").value = a.recipient_name || (state.shipCreator && state.shipCreator.full_name) || ""; el("infSh_phone").value = a.phone || (state.shipCreator && state.shipCreator.phone) || ""; }
  }
  function bindItemPicker() {
    const q = el("infSh_q"), m = el("infSh_matches");
    if (!q) return;
    let t = null;
    q.addEventListener("input", () => {
      clearTimeout(t);
      const s = q.value.trim().toLowerCase();
      if (s.length < 2) { m.style.display = "none"; return; }
      t = setTimeout(async () => {
        try {
          if (!state.items) state.items = window.fetchItems ? await window.fetchItems() : [];
          const hits = state.items.filter((x) => (String(x.name || "") + " " + String(x.name_tr || "") + " " + String(x.sku || "")).toLowerCase().includes(s)).slice(0, 25);
          m.innerHTML = hits.length ? hits.map((x) => `<div class="inf-match" onclick="INF.pickAdd(${x.id})"><span>${esc(x.name)}${x.name_tr ? ` <span class="muted">· ${esc(x.name_tr)}</span>` : ""}${x.sku ? ` <span class="muted">· ${esc(x.sku)}</span>` : ""}</span><span class="muted">${num(x.current_stock)} ${esc(x.unit || "")}</span></div>`).join("") : '<div class="inf-match muted">Eşleşme yok</div>';
          m.style.display = "block";
        } catch (e) { m.innerHTML = '<div class="inf-match muted">' + esc(e.message) + "</div>"; m.style.display = "block"; }
      }, 200);
    });
    document.addEventListener("click", (e) => { if (m && !m.contains(e.target) && e.target !== q) m.style.display = "none"; });
  }
  function pickAdd(id) {
    const it = (state.items || []).find((x) => x.id === id);
    if (!it) return;
    const ex = state.cart.find((c) => c.item_id === id);
    if (ex) ex.qty = (Number(ex.qty) || 0) + 1;
    else state.cart.push({ item_id: id, name: it.name, name_tr: it.name_tr || "", unit: it.unit || "", stock: Number(it.current_stock || 0), cost: Number(it.cost_price || 0), qty: 1 });
    el("infSh_q").value = ""; el("infSh_matches").style.display = "none";
    renderCart();
  }
  function cartQty(i, v) { if (state.cart[i]) { state.cart[i].qty = Math.max(0.001, Number(v) || 1); renderCart(); } }
  function cartDel(i) { state.cart.splice(i, 1); renderCart(); }
  function renderCart() {
    const box = el("infSh_cart");
    if (!box) return;
    if (!state.cart.length) box.innerHTML = '<div class="muted">Ürün eklenmedi.</div>';
    else box.innerHTML = `<table class="inf-cart"><thead><tr><th>Ürün</th><th>Stok</th><th>Adet</th><th>Birim maliyet</th><th>Tutar</th><th></th></tr></thead><tbody>${state.cart.map((c, i) => `<tr>
      <td>${esc(c.name)}${c.name_tr ? `<br><span class="muted">${esc(c.name_tr)}</span>` : ""}</td>
      <td class="${c.stock < c.qty ? "inf-pill-r" : "muted"}">${num(c.stock)} ${esc(c.unit)}</td>
      <td><input type="number" min="0.001" step="1" value="${c.qty}" oninput="INF.cartQty(${i},this.value)"></td>
      <td>${c.cost > 0 ? money(c.cost) : '<span class="inf-pill inf-pill-y" title="Item.cost_price sıfır — COGS eksik hesaplanır">0 ⚠</span>'}</td>
      <td>${money(c.cost * c.qty)}</td>
      <td><button class="ico-del" onclick="INF.cartDel(${i})"><i class="bi bi-x-lg"></i></button></td></tr>`).join("")}</tbody></table>`;
    const cogs = state.cart.reduce((s, c) => s + c.cost * c.qty, 0);
    const pack = numOrNull("infSh_pack") || 0, ship = numOrNull("infSh_ship") || 0;
    const qty = state.cart.reduce((s, c) => s + Number(c.qty || 0), 0);
    const cost = el("infSh_cost");
    if (cost) cost.innerHTML = `<div><b>${num(qty)}</b>Toplam adet</div><div><b>${money(cogs)}</b>COGS</div><div><b>${money(pack)}</b>Paket</div><div><b>${money(ship)}</b>Kargo</div><div><b>${money(cogs + pack + ship)}</b>Yüklü maliyet</div>`;
  }
  function shipModal(sid) {
    openModal("Kargola", `<p class="muted" style="font-size:0.82rem;">Takip numarası girildiğinde teslimat <b>kargoda</b> olur ve stok düşer (tek Output). İş birliği aşaması Kargoda'ya geçer.</p>
      <div class="inf-frow"><div><label class="lbl">Takip no *</label><input class="ipt" id="infSp_track"></div><div><label class="lbl">Kargo firması</label><input class="ipt" id="infSp_carrier" list="infCarriers" placeholder="Yurtiçi / Aras / DHL…"></div></div>`, async () => {
      const tr = val("infSp_track");
      if (!tr) { toast("Takip no zorunlu."); return; }
      const r = await api("/shipments/" + sid + "/ship", jbody({ tracking_no: tr, carrier: val("infSp_carrier") || null }));
      closeModal(); toast("Kargolandı: " + (r.document_no || "") + " · stok düşüldü.", "success");
      if (window.invalidateItemsCache) window.invalidateItemsCache(); state.items = null;
      loadShip(); loadBoard(); if (state.drawer && state.drawer.type === "collab") openCollab(state.drawer.id, "ship");
    }, { saveLabel: "Kargola" });
  }
  function deliveredModal(sid) {
    const days = String(CFG[CFG_KEY.reminder] || "7,14");
    openModal("Teslim edildi", `<p class="muted" style="font-size:0.82rem;">Teslim tarihine göre hatırlatma görevleri açılır (+${esc(days)} gün: "içerik bekleniyor" takibi, CRM görevi + push). Aşama Teslim Edildi → İçerik Bekleniyor.</p>
      <div class="inf-frow"><div><label class="lbl">Teslim tarihi</label><input class="ipt" type="date" id="infDv_on" value="${today()}"></div><div><label class="lbl">Görev atanan</label>${ownerSelect("infDv_owner", null)}</div></div>`, async () => {
      const r = await api("/shipments/" + sid + "/delivered", jbody({ delivered_on: val("infDv_on") || null, assigned_to_user_id: intOrNull("infDv_owner") }));
      closeModal(); toast("Teslim işlendi" + ((r.task_ids || []).length ? " — " + r.task_ids.length + " hatırlatma görevi açıldı." : "."), "success");
      loadShip(); loadBoard(); if (state.drawer && state.drawer.type === "collab") openCollab(state.drawer.id, "ship");
    }, { saveLabel: "Teslim edildi" });
    ensureUsers().then(() => { const s = el("infDv_owner"); if (s && s.options.length <= 1 && (state.users || []).length) s.outerHTML = ownerSelect("infDv_owner", null); });
  }

  // =========================================================================
  // 7) AYARLAR
  // =========================================================================
  async function renderSettings() {
    const v = el("inf-panel-settings");
    if (!v) return;
    try {
      const d = await api("/settings");
      state.settings = d; state.tiers = d.tiers || [];
      await ensureCampaigns(true);
      v.innerHTML = settingsScalarCard(d) + settingsTierCard(d) + settingsBenchmarkCard(d) + settingsCampaignCard() + settingsLabelCard(d);
    } catch (e) { v.innerHTML = '<div class="inf-empty">' + esc(e.message) + "</div>"; }
  }
  function settingsScalarCard(d) {
    const s = d.settings || {};
    const keys = d.keys || Object.keys(s);
    return `<div class="card2"><div class="card2-head"><i class="bi bi-sliders"></i><h2>Program ayarları</h2><span class="muted" style="margin-left:auto;">AppSetting · deploysuz</span></div>
      <div class="card2-body"><div class="inf-frow">${keys.map((k) => {
        const short = k.replace("influencer.", "");
        const isRe = k === "influencer.forbidden_words";
        return `<div class="${isRe ? "full" : ""}"><label class="lbl" title="${esc(k)}">${esc(CFG_LABELS[k] || short)}</label>${isRe ? `<textarea class="ipt" rows="2" data-cfg="${esc(k)}">${esc(s[k] == null ? "" : s[k])}</textarea>` : `<input class="ipt" data-cfg="${esc(k)}" value="${esc(s[k] == null ? "" : s[k])}" placeholder="${esc((d.defaults || {})[k] || "")}">`}</div>`;
      }).join("")}</div>
      <div class="inf-actions" style="margin-top:0.8rem;"><button class="btn-g" onclick="INF.saveSettings()"><i class="bi bi-save"></i> Kaydet</button><span class="muted">Yasak kelimeler regex; hatırlatma günleri "7,14" biçiminde.</span></div></div></div>`;
  }
  async function saveSettings() {
    const settings = {};
    document.querySelectorAll("[data-cfg]").forEach((i) => { settings[i.dataset.cfg] = i.value.trim(); });
    try {
      const r = await api("/settings", jbody({ settings: settings }, "PUT"));
      Object.assign(CFG, r.settings || settings);
      toast("Ayarlar kaydedildi.", "success");
    } catch (e) { toast(e.message); }
  }
  function settingsTierCard(d) {
    const tiers = d.tiers || [];
    return `<div class="card2"><div class="card2-head"><i class="bi bi-bar-chart-steps"></i><h2>Kademeler</h2><span class="muted" style="margin-left:auto;">Takipçi bandı → otomatik kademe; Ambassador yalnız elle</span></div>
      <div class="card2-body" style="overflow-x:auto;"><table class="inf-tiers"><thead><tr><th>Anahtar</th><th>Etiket</th><th>Min takipçi</th><th>Max takipçi</th><th>Komisyon %</th><th>Müşteri ind. %</th><th>Azami ürün</th><th>Lansman</th><th>Kod</th><th>Sıra</th><th>Aktif</th><th></th></tr></thead>
      <tbody>${tiers.map((t) => `<tr data-tier="${esc(t.key)}">
        <td><b>${esc(t.key)}</b></td><td><input value="${esc(t.label)}" data-f="label"></td>
        <td><input type="number" min="0" value="${t.min_followers != null ? t.min_followers : ""}" data-f="min_followers" ${t.key === "ambassador" ? "disabled" : ""}></td>
        <td><input type="number" min="0" value="${t.max_followers != null ? t.max_followers : ""}" data-f="max_followers" placeholder="∞" ${t.key === "ambassador" ? "disabled" : ""}></td>
        <td><input type="number" min="0" max="100" step="0.5" value="${t.commission_pct != null ? t.commission_pct : ""}" data-f="commission_pct"></td>
        <td><input type="number" min="0" max="100" step="0.5" value="${t.customer_discount_pct != null ? t.customer_discount_pct : ""}" data-f="customer_discount_pct"></td>
        <td><input type="number" min="0" value="${t.max_gift_items != null ? t.max_gift_items : ""}" data-f="max_gift_items"></td>
        <td style="text-align:center;"><input type="checkbox" ${t.launch_access ? "checked" : ""} data-f="launch_access"></td>
        <td style="text-align:center;"><input type="checkbox" ${t.code_allowed ? "checked" : ""} data-f="code_allowed"></td>
        <td><input type="number" value="${t.sort_order != null ? t.sort_order : ""}" data-f="sort_order" style="width:56px;"></td>
        <td style="text-align:center;"><input type="checkbox" ${t.is_active ? "checked" : ""} data-f="is_active"></td>
        <td><button class="btn-o inf-sm" onclick="INF.saveTier('${esc(t.key)}')"><i class="bi bi-save"></i></button></td></tr>`).join("")}</tbody></table>
      <p class="muted" style="margin:0.6rem 0 0;font-size:0.8rem;">Komisyon tüm kademelerde düz %10 (patron kararı 04.09.2026); ambassador için ileride %15 buradan açılabilir. Max takipçi boş = sınırsız.</p></div></div>`;
  }
  async function saveTier(key) {
    const tr = document.querySelector(`tr[data-tier="${key}"]`);
    if (!tr) return;
    const body = {};
    tr.querySelectorAll("[data-f]").forEach((i) => {
      const f = i.dataset.f;
      if (i.type === "checkbox") body[f] = i.checked;
      else if (i.disabled) return;
      else if (i.type === "number") { const v = i.value.trim(); if (v !== "") body[f] = Number(v); else if (f === "max_followers") body.clear_max = true; }
      else body[f] = i.value.trim();
    });
    try { await api("/settings/tiers/" + encodeURIComponent(key), jbody(body, "PUT")); toast("Kademe kaydedildi: " + key, "success"); state.tiers = null; renderSettings(); }
    catch (e) { toast(e.message); }
  }
  const BM_LABELS = { er_follower: "ER / takipçi %", er_view: "ER / izlenme %", view_per_follower: "İzlenme / takipçi", comment_like_ratio: "Yorum / beğeni", geo_target_pct: "Hedef ülke %", comment_quality_pct: "Yorum kalitesi %" };
  function settingsBenchmarkCard(d) {
    const platforms = d.platforms || Object.keys(PLATFORMS);
    const metrics = d.benchmark_metrics || Object.keys(BM_LABELS);
    const tiers = (d.tiers || []).filter((t) => t.key !== "ambassador");
    const map = {};
    (d.benchmarks || []).forEach((b) => { map[b.platform + "|" + b.tier_key + "|" + b.metric] = b; });
    const p = state.bmPlatform;
    return `<div class="card2"><div class="card2-head"><i class="bi bi-grid-3x3"></i><h2>Benchmark matrisi</h2><span class="muted" style="margin-left:auto;">Platform × kademe × metrik → sağlıklı alt/üst bant (özgünlük skoru bileşen uyumu)</span></div>
      <div class="card2-body">
        <div class="inf-ptabs">${platforms.map((pl) => `<button class="inf-ptab${pl === p ? " active" : ""}" onclick="INF.bmPlatform('${esc(pl)}')">${esc((PLATFORMS[pl] || { label: pl }).label)}</button>`).join("")}</div>
        <div class="inf-matrix" id="infBmGrid"><table><thead><tr><th>Kademe</th>${metrics.map((m) => `<th>${esc(BM_LABELS[m] || m)}<br><span class="muted" style="text-transform:none;font-weight:400;">alt – üst</span></th>`).join("")}</tr></thead>
          <tbody>${tiers.map((t) => `<tr><td><b>${esc(t.label)}</b></td>${metrics.map((m) => { const b = map[p + "|" + t.key + "|" + m] || {}; return `<td><div class="rng"><input type="number" step="0.01" data-bm="${esc(p)}|${esc(t.key)}|${esc(m)}|low" value="${b.low != null ? b.low : ""}"><span>–</span><input type="number" step="0.01" data-bm="${esc(p)}|${esc(t.key)}|${esc(m)}|high" value="${b.high != null ? b.high : ""}"></div></td>`; }).join("")}</tr>`).join("")}</tbody></table></div>
        <div class="inf-actions" style="margin-top:0.8rem;"><button class="btn-g" onclick="INF.saveBenchmarks()"><i class="bi bi-save"></i> ${esc((PLATFORMS[p] || { label: p }).label)} bantlarını kaydet</button><span class="muted">Yalnız alt ve üst ikisi de dolu hücreler yazılır.</span></div>
      </div></div>`;
  }
  function bmPlatform(p) { state.bmPlatform = p; const v = el("inf-panel-settings"); if (v && state.settings) { const card = v.querySelectorAll(".card2")[2]; if (card) card.outerHTML = settingsBenchmarkCard(state.settings); } }
  async function saveBenchmarks() {
    const cells = {};
    document.querySelectorAll("[data-bm]").forEach((i) => {
      const [pl, tier, metric, side] = i.dataset.bm.split("|");
      const k = pl + "|" + tier + "|" + metric;
      cells[k] = cells[k] || { platform: pl, tier_key: tier, metric: metric };
      const v = i.value.trim();
      cells[k][side] = v === "" ? null : Number(v);
    });
    const items = Object.values(cells).filter((c) => c.low != null && c.high != null);
    if (!items.length) { toast("Kaydedilecek dolu hücre yok."); return; }
    try { const r = await api("/settings/benchmarks", jbody({ items: items }, "PUT")); toast("Benchmark kaydedildi (" + (r.count != null ? r.count : items.length) + " hücre).", "success"); renderSettings(); }
    catch (e) { toast(e.message); }
  }
  function settingsCampaignCard() {
    const list = state.campaigns || [];
    return `<div class="card2"><div class="card2-head"><i class="bi bi-megaphone"></i><h2>Kampanyalar &amp; claim sheet</h2>
      <span style="margin-left:auto;"><button class="btn-g inf-sm" onclick="INF.campaignModal()"><i class="bi bi-plus-lg"></i> Kampanya</button></span></div>
      <div class="card2-body">${!list.length ? '<div class="inf-empty">Kampanya yok. Claim sheet (söylenebilir / söylenemez iddialar), brief ve hashtag\'ler kampanya üzerinde tutulur; iş birliği açarken seçilir.</div>' : `<div style="overflow-x:auto;"><table>
        <thead><tr><th>Kampanya</th><th>Mağaza</th><th>Pazar</th><th>Tarih</th><th>Claim sheet</th><th>Durum</th><th></th></tr></thead><tbody>${list.map((c) => `<tr>
          <td><b>${esc(c.name)}</b>${c.hashtags ? `<br><span class="muted">${esc(c.hashtags)}</span>` : ""}</td><td>${storePill(c.store_key)}</td><td>${esc(c.market || "")}</td>
          <td class="muted">${esc(c.start_on_label || "—")} – ${esc(c.end_on_label || "—")}</td><td class="muted">${(c.claim_sheet || []).length} madde</td>
          <td>${pill(c.status || "taslak", c.status === "aktif" ? "g" : "gray")}${c.is_active === false ? " " + pill("pasif", "r") : ""}</td>
          <td><button class="btn-o inf-sm" onclick="INF.campaignModal(${c.id})"><i class="bi bi-pencil"></i></button></td></tr>`).join("")}</tbody></table></div>`}</div></div>`;
  }
  function campaignModal(id) {
    const c = id ? (state.campaigns || []).find((x) => x.id === id) || {} : {};
    const dl = (c.default_deliverables || []).map((d) => [d.platform, d.type, d.count].filter((v) => v != null && v !== "").join(":")).join("\n");
    openModal(id ? "Kampanya düzenle" : "Yeni kampanya", `<div class="inf-frow">
      <div class="full"><label class="lbl">Ad *</label><input class="ipt" id="infCp_name" value="${esc(c.name || "")}"></div>
      <div><label class="lbl">Mağaza</label><select class="ipt" id="infCp_store">${opt(STORE_OPTS, c.store_key || "minerva")}</select></div>
      <div><label class="lbl">Pazar</label><select class="ipt" id="infCp_market">${opt(MARKET_OPTS, c.market || "TR")}</select></div>
      <div><label class="lbl">Başlangıç</label><input class="ipt" type="date" id="infCp_start" value="${esc(c.start_on || "")}"></div>
      <div><label class="lbl">Bitiş</label><input class="ipt" type="date" id="infCp_end" value="${esc(c.end_on || "")}"></div>
      <div><label class="lbl">Durum</label><select class="ipt" id="infCp_status">${opt([["taslak", "Taslak"], ["aktif", "Aktif"], ["bitti", "Bitti"]], c.status || "taslak")}</select></div>
      <div><label class="lbl">Hashtag'ler</label><input class="ipt" id="infCp_tags" value="${esc(c.hashtags || "")}" placeholder="#minerva108 #vegancilt"></div>
      <div class="full"><label class="lbl">Claim sheet (satır başına bir madde — "✓ söylenebilir" / "✗ söylenemez")</label><textarea class="ipt" id="infCp_claims" rows="5" placeholder="✓ Vegan ve cruelty-free&#10;✗ Akneyi tedavi eder">${esc((c.claim_sheet || []).join("\n"))}</textarea></div>
      <div class="full"><label class="lbl">Varsayılan teslimatlar (platform:tip:adet)</label><textarea class="ipt" id="infCp_deliv" rows="2">${esc(dl)}</textarea></div>
      <div class="full"><label class="lbl">Brief metni</label><textarea class="ipt" id="infCp_brief" rows="5">${esc(c.brief_text || "")}</textarea></div>
    </div>`, async () => {
      const body = { name: val("infCp_name"), store_key: val("infCp_store"), market: val("infCp_market"), start_on: val("infCp_start") || null, end_on: val("infCp_end") || null,
        status: val("infCp_status"), hashtags: val("infCp_tags") || null, brief_text: val("infCp_brief") || null,
        claim_sheet: val("infCp_claims").split("\n").map((s) => s.trim()).filter(Boolean),
        default_deliverables: val("infCp_deliv").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const p = l.split(":"); return { platform: (p[0] || "").trim(), type: (p[1] || "").trim(), count: Number(p[2]) || 1 }; }) };
      if (!body.name) { toast("Ad zorunlu."); return; }
      if (id) await api("/campaigns/" + id, jbody(body, "PUT")); else await api("/campaigns", jbody(body));
      closeModal(); toast("Kampanya kaydedildi.", "success"); await ensureCampaigns(true);
      if (el("inf-panel-settings") && state.settings) renderSettings();
    }, { wide: true });
  }
  function settingsLabelCard(d) {
    const lt = d.label_templates || {};
    return `<div class="card2"><div class="card2-head"><i class="bi bi-tag"></i><h2>Pazar etiket şablonları &amp; sürümler</h2></div>
      <div class="card2-body">
        <p class="muted" style="font-size:0.82rem;margin-top:0;">Reklam mevzuatı: etiket caption'ın <b>ilk satırında</b> ve okunur olmalı. Creator kılavuz onayında bu şablonu görür; yayın sonrası uyum kontrolü aynı şablona bakar. (Sabit — kod içinde; değişiklik için geliştiriciye.)</p>
        ${Object.keys(lt).map((m) => `<div class="inf-kv"><span class="k">${esc(m)}</span><span class="v" style="display:flex;gap:0.4rem;align-items:center;"><code style="flex:1;">${esc(lt[m])}</code><button class="btn-o inf-sm" onclick="INF.copy('${esc(lt[m])}')"><i class="bi bi-clipboard"></i></button></span></div>`).join("")}
        ${kv("KVKK metin sürümü", esc(d.consent_version || "—"))}
        ${kv("Kılavuz sürümü", esc((d.settings || {})["influencer.guideline_version"] || "—") + ' <span class="muted">(yukarıdaki ayardan değişir; onaylar sürümle saklanır)</span>')}
        ${kv("Başvuru formu", `<a href="/basvuru" target="_blank" rel="noopener">${esc(location.origin)}/basvuru</a> · <a href="/basvuru?lang=en" target="_blank" rel="noopener">EN</a>`)}
      </div></div>`;
  }

  // ── Başlangıç ────────────────────────────────────────────────────────────
  function init() {
    document.querySelectorAll(".inf-tab").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));
    el("infMSave").addEventListener("click", runSave);
    el("infModal").addEventListener("click", (e) => { if (e.target === el("infModal")) closeModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (el("infModal").classList.contains("open")) closeModal(); else if (state.drawer) closeDrawer(); } });
    let start = null;
    try { start = sessionStorage.getItem("inf_tab"); } catch (e) { /* */ }
    const hash = (location.hash || "").replace("#", "");
    const tabs = Array.from(document.querySelectorAll(".inf-tab")).map((b) => b.dataset.tab);
    const first = tabs.includes(hash) ? hash : (tabs.includes(start) ? start : tabs[0]);
    if (first) openTab(first);
    // Derin bağlantı: #creator-12 / #collab-7
    const m = hash.match(/^(creator|collab)-(\d+)$/);
    if (m) { openTab(m[1] === "creator" ? "creators" : "board"); (m[1] === "creator" ? openCreator : openCollab)(Number(m[2])); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();

  // ── Dışa açılan ad alanı (inline onclick'ler) ────────────────────────────
  window.INF = {
    openTab, closeModal, closeDrawer, dwTab, copy: copyText,
    reloadApps: loadApps, openApp, decide, manualAppModal,
    reloadCreators: loadCreators, openCreator, creatorModal, setRelStage, toggleResend, tagsModal, rescore,
    accountModal, deleteAccount, metricsModal, tokenModal, uploadFile, addNote,
    reloadBoard: loadBoard, dragCollab, allowDrop, dropCollab, openCollab, collabModal, pickCreator, stageFromDrawer, setDecision,
    contentModal, reviewModal, review, publishedModal, contentMetricsModal,
    reloadShip: loadShip, pickCollabForShipment, shipmentModal, shipAddrChange, pickAdd, cartQty, cartDel, renderCart, shipModal, deliveredModal,
    saveSettings, saveTier, bmPlatform, saveBenchmarks, campaignModal,
  };
})();
