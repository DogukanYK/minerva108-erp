/* ===========================================================================
 * Minerva 108 — CRM ön yüz mantığı.
 * Tek sayfa: Pano · Firmalar · Kişiler · Pipeline (Kanban) · Görevler.
 * Tüm veri /api/crm/* uçlarından gelir; paylaşımlı (herkes herkesi görür).
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
  function money(v, cur) {
    try {
      return new Intl.NumberFormat("tr-TR", { style: "currency", currency: cur || "TRY", maximumFractionDigits: 0 }).format(v || 0);
    } catch (e) { return (v || 0) + " " + (cur || "TRY"); }
  }
  function initials(name) { return (name || "?").trim().charAt(0).toUpperCase(); }

  async function api(path, opts) {
    const r = await fetch("/api/crm" + path, opts || {});
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

  // ── Durum / önbellek ──────────────────────────────────────────────────────
  const state = { tab: "dashboard", stages: [], users: [], drawer: null, dwTab: "summary" };

  // ── Sekme yönetimi ──────────────────────────────────────────────────────
  const TABS = {
    dashboard: { title: "Pano", render: renderDashboard },
    companies: { title: "Firmalar", render: renderCompanies },
    contacts: { title: "Kişiler", render: renderContacts },
    pipeline: { title: "Pipeline", render: renderPipeline },
    tasks: { title: "Görevler", render: renderTasks },
  };

  function switchTab(tab) {
    if (!TABS[tab]) tab = "dashboard";
    state.tab = tab;
    Object.keys(TABS).forEach((t) => {
      const v = el("view-" + t);
      if (v) v.style.display = t === tab ? "" : "none";
    });
    document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    el("topTitle").textContent = TABS[tab].title;
    TABS[tab].render();
  }

  // ── Pano ──────────────────────────────────────────────────────────────────
  async function renderDashboard() {
    const v = el("view-dashboard");
    v.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const d = await api("/dashboard");
      const c = d.counts;
      const stat = (val, lbl, warn) =>
        `<div class="stat${warn ? " warn" : ""}"><div class="v">${val}</div><div class="l">${lbl}</div></div>`;
      let html = '<div class="stat-grid">';
      html += stat(c.companies, "Firma");
      html += stat(c.contacts, "Kişi");
      html += stat(c.open_deals, "Açık Fırsat");
      html += stat(money(c.open_value), "Açık Pipeline");
      html += stat(c.won_month + " · " + money(c.won_month_value), "Bu Ay Kazanılan");
      html += stat(c.overdue_tasks, "Geciken Görev", c.overdue_tasks > 0);
      html += "</div>";

      // Pipeline aşama dağılımı
      html += '<div class="card2"><div class="card2-head"><i class="bi bi-kanban"></i><h2>Pipeline — Aşamalara Göre</h2></div><div class="card2-body">';
      if (!d.pipeline_by_stage.length) {
        html += '<div class="muted">Aşama yok.</div>';
      } else {
        const max = Math.max.apply(null, d.pipeline_by_stage.map((s) => s.value).concat([1]));
        d.pipeline_by_stage.forEach((s) => {
          const pct = Math.round((s.value / max) * 100);
          html += `<div style="margin-bottom:0.7rem;">
            <div style="display:flex;justify-content:space-between;font-size:0.84rem;margin-bottom:0.2rem;">
              <span>${esc(s.stage)} <span class="muted">(${s.count})</span></span><b>${money(s.value)}</b></div>
            <div style="height:8px;background:#efe9dd;border-radius:6px;overflow:hidden;">
              <div style="height:100%;width:${pct}%;background:var(--gold);"></div></div></div>`;
        });
      }
      html += "</div></div>";

      // İki sütun: görevlerim + son aktiviteler
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.2rem;">';
      html += '<div class="card2"><div class="card2-head"><i class="bi bi-check2-square"></i><h2>Bugünkü Görevlerim</h2></div><div class="card2-body">';
      if (!d.my_tasks.length) html += '<div class="muted">Bugün için görev yok. 🎉</div>';
      else html += d.my_tasks.map(taskRow).join("");
      html += "</div></div>";

      html += '<div class="card2"><div class="card2-head"><i class="bi bi-clock-history"></i><h2>Son Aktiviteler</h2></div><div class="card2-body">';
      if (!d.recent_activities.length) html += '<div class="muted">Henüz aktivite yok.</div>';
      else html += '<ul class="tl">' + d.recent_activities.map(tlItem).join("") + "</ul>";
      html += "</div></div></div>";

      v.innerHTML = html;
    } catch (e) { v.innerHTML = '<div class="empty">Pano yüklenemedi: ' + esc(e.message) + "</div>"; }
  }

  function taskRow(t) {
    const cls = t.overdue ? "pill pill-o" : "pill pill-gray";
    return `<div style="display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0;border-bottom:1px solid #f3f0ea;">
      <button class="ico-btn" title="Tamamla" onclick="CRM.toggleTask(${t.id})"><i class="bi bi-circle"></i></button>
      <div style="flex:1;"><div style="font-size:0.88rem;">${esc(t.title)}</div>
      <div class="muted" style="font-size:0.74rem;">${t.due_label ? esc(t.due_label) : "tarih yok"} · ${esc(t.assigned_to_name)}</div></div>
      ${t.due_label ? `<span class="${cls}">${t.overdue ? "Gecikti" : "Bugün"}</span>` : ""}</div>`;
  }

  // ── Firmalar ──────────────────────────────────────────────────────────────
  async function renderCompanies() {
    const v = el("view-companies");
    if (!v.dataset.init) {
      v.dataset.init = "1";
      v.innerHTML = `<div class="toolbar">
        <div class="search"><i class="bi bi-search"></i><input class="ipt" id="coSearch" placeholder="Firma ara…"></div>
        ${can("crm", "create") ? '<button class="btn-g" onclick="CRM.companyModal()"><i class="bi bi-plus-lg"></i> Yeni Firma</button>' : ""}
      </div><div class="card2"><div class="card2-body" style="overflow-x:auto;"><div id="coList"></div></div></div>`;
      let tmr;
      el("coSearch").addEventListener("input", function () { clearTimeout(tmr); tmr = setTimeout(loadCompanies, 250); });
    }
    loadCompanies();
  }
  async function loadCompanies() {
    const box = el("coList");
    box.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const q = (el("coSearch").value || "").trim();
      const rows = await api("/companies" + (q ? "?q=" + encodeURIComponent(q) : ""));
      if (!rows.length) { box.innerHTML = '<div class="empty">Firma yok. Yeni firma ekleyin.</div>'; return; }
      box.innerHTML = '<table><thead><tr><th>Firma</th><th>Şehir</th><th>Kişi</th><th>Açık Fırsat</th><th>Sorumlu</th><th></th></tr></thead><tbody>' +
        rows.map((c) => `<tr class="clickable" onclick="CRM.openCompany(${c.id})">
          <td><span class="av">${initials(c.name)}</span><b>${esc(c.name)}</b>${c.sector ? '<div class="muted" style="font-size:0.76rem;margin-left:2.4rem;">' + esc(c.sector) + "</div>" : ""}</td>
          <td class="muted">${esc(c.city) || "—"}</td>
          <td>${c.contact_count}</td>
          <td>${c.open_deal_count ? '<span class="pill pill-b">' + c.open_deal_count + "</span>" : '<span class="muted">—</span>'}</td>
          <td class="muted">${esc(c.owner_name) || "—"}</td>
          <td onclick="event.stopPropagation();">${c.wa_link ? '<a class="ico-btn wa" href="' + esc(c.wa_link) + '" target="_blank" title="WhatsApp"><i class="bi bi-whatsapp"></i></a>' : ""}</td>
        </tr>`).join("") + "</tbody></table>";
    } catch (e) { box.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  // ── Kişiler ─────────────────────────────────────────────────────────────
  async function renderContacts() {
    const v = el("view-contacts");
    if (!v.dataset.init) {
      v.dataset.init = "1";
      v.innerHTML = `<div class="toolbar">
        <div class="search"><i class="bi bi-search"></i><input class="ipt" id="ctSearch" placeholder="Kişi ara…"></div>
        ${can("crm", "create") ? '<button class="btn-g" onclick="CRM.contactModal()"><i class="bi bi-plus-lg"></i> Yeni Kişi</button>' : ""}
      </div><div class="card2"><div class="card2-body" style="overflow-x:auto;"><div id="ctList"></div></div></div>`;
      let tmr;
      el("ctSearch").addEventListener("input", function () { clearTimeout(tmr); tmr = setTimeout(loadContacts, 250); });
    }
    loadContacts();
  }
  async function loadContacts() {
    const box = el("ctList");
    box.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const q = (el("ctSearch").value || "").trim();
      const rows = await api("/contacts" + (q ? "?q=" + encodeURIComponent(q) : ""));
      if (!rows.length) { box.innerHTML = '<div class="empty">Kişi yok.</div>'; return; }
      box.innerHTML = '<table><thead><tr><th>Kişi</th><th>Firma</th><th>Telefon</th><th>E-posta</th><th></th></tr></thead><tbody>' +
        rows.map((c) => `<tr class="clickable" onclick="CRM.openContact(${c.id})">
          <td><span class="av">${initials(c.full_name)}</span><b>${esc(c.full_name)}</b>${c.title ? '<div class="muted" style="font-size:0.76rem;margin-left:2.4rem;">' + esc(c.title) + "</div>" : ""}</td>
          <td class="muted">${esc(c.company_name) || "—"}</td>
          <td class="muted">${esc(c.mobile || c.phone) || "—"}</td>
          <td class="muted">${esc(c.email) || "—"}</td>
          <td onclick="event.stopPropagation();">${c.wa_link ? '<a class="ico-btn wa" href="' + esc(c.wa_link) + '" target="_blank" title="WhatsApp"><i class="bi bi-whatsapp"></i></a>' : ""}</td>
        </tr>`).join("") + "</tbody></table>";
    } catch (e) { box.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  // ── Pipeline (Kanban) ──────────────────────────────────────────────────────
  async function renderPipeline() {
    const v = el("view-pipeline");
    v.innerHTML = `<div class="toolbar"><h2 class="page-title" style="margin:0;flex:1;">Satış Pipeline</h2>
      ${can("crm", "create") ? '<button class="btn-g" onclick="CRM.dealModal()"><i class="bi bi-plus-lg"></i> Yeni Fırsat</button>' : ""}
      </div><div id="kanban" class="kanban"><div class="empty">Yükleniyor…</div></div>`;
    try {
      const d = await api("/pipeline");
      const editable = can("crm", "edit");
      let html = "";
      d.stages.forEach((s) => {
        const deals = d.deals_by_stage[s.id] || [];
        const total = d.totals[s.id] || 0;
        html += `<div class="kcol" data-stage="${s.id}"
          ${editable ? 'ondragover="CRM.allowDrop(event,this)" ondragleave="this.classList.remove(\'dragover\')" ondrop="CRM.dropDeal(event,' + s.id + ')"' : ""}>
          <div class="kcol-head"><span class="nm">${esc(s.name)}</span><span class="tot">${deals.length} · ${money(total)}</span></div>
          <div class="kcol-body">${deals.map((dl) => dealCard(dl, editable)).join("")}</div></div>`;
      });
      el("kanban").innerHTML = html || '<div class="empty">Aşama tanımlı değil.</div>';
    } catch (e) { el("kanban").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }
  function dealCard(d, editable) {
    return `<div class="kcard" ${editable ? 'draggable="true" ondragstart="CRM.dragDeal(event,' + d.id + ')"' : ""} onclick="CRM.openDeal(${d.id})">
      <div class="t">${esc(d.title)}</div>
      <div class="sub">${esc(d.company_name || d.contact_name || "—")}</div>
      <div class="v">${money(d.value, d.currency)}${d.probability ? ' · %' + d.probability : ""}</div>
      ${d.expected_close_label ? '<div class="sub" style="margin-top:0.2rem;"><i class="bi bi-calendar3"></i> ' + esc(d.expected_close_label) + "</div>" : ""}
    </div>`;
  }

  // ── Görevler ────────────────────────────────────────────────────────────
  async function renderTasks() {
    const v = el("view-tasks");
    if (!v.dataset.init) {
      v.dataset.init = "1";
      v.innerHTML = `<div class="toolbar">
        <select class="ipt" id="tkScope" style="max-width:180px;">
          <option value="all">Tümü</option><option value="mine">Bana atananlar</option>
          <option value="today">Bugün/Geciken</option><option value="overdue">Sadece geciken</option>
          <option value="open">Sadece açık</option></select>
        ${can("crm", "create") ? '<button class="btn-g" style="margin-left:auto;" onclick="CRM.taskModal()"><i class="bi bi-plus-lg"></i> Yeni Görev</button>' : ""}
      </div><div class="card2"><div class="card2-body"><div id="tkList"></div></div></div>`;
      el("tkScope").addEventListener("change", loadTasks);
    }
    loadTasks();
  }
  async function loadTasks() {
    const box = el("tkList");
    box.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const scope = el("tkScope").value;
      const rows = await api("/tasks?scope=" + encodeURIComponent(scope));
      if (!rows.length) { box.innerHTML = '<div class="empty">Görev yok.</div>'; return; }
      box.innerHTML = rows.map((t) => {
        const done = t.status === "done";
        return `<div style="display:flex;align-items:center;gap:0.6rem;padding:0.55rem 0;border-bottom:1px solid #f3f0ea;">
          <button class="ico-btn" onclick="CRM.toggleTask(${t.id})" title="${done ? "Geri al" : "Tamamla"}"><i class="bi bi-${done ? "check-circle-fill" : "circle"}" style="${done ? "color:#22c55e;" : ""}"></i></button>
          <div style="flex:1;${done ? "opacity:0.55;text-decoration:line-through;" : ""}">
            <div style="font-size:0.9rem;">${esc(t.title)}</div>
            <div class="muted" style="font-size:0.76rem;">${t.due_label ? esc(t.due_label) : "tarih yok"} · ${esc(t.assigned_to_name)}</div></div>
          ${t.overdue && !done ? '<span class="pill pill-o">Gecikti</span>' : ""}
          ${can("crm", "edit") ? `<button class="ico-btn" onclick="CRM.taskModal(${t.id})"><i class="bi bi-pencil"></i></button>` : ""}
          ${can("crm", "delete") ? `<button class="ico-btn del" onclick="CRM.delTask(${t.id})"><i class="bi bi-trash"></i></button>` : ""}
        </div>`;
      }).join("");
    } catch (e) { box.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  // ── Zaman çizelgesi öğesi (paylaşımlı) ─────────────────────────────────────
  const TYPE_ICON = { note: "sticky", call: "telephone", meeting: "people", email: "envelope", whatsapp: "whatsapp" };
  const TYPE_LABEL = { note: "Not", call: "Arama", meeting: "Toplantı", email: "E-posta", whatsapp: "WhatsApp" };
  function tlItem(a) {
    return `<li class="tl-item"><span class="tl-dot"><i class="bi bi-${TYPE_ICON[a.type] || "sticky"}"></i></span>
      <div class="tl-meta">${a.is_pinned ? '<i class="bi bi-pin-angle-fill tl-pin"></i> ' : ""}${esc(TYPE_LABEL[a.type] || a.type)} · ${esc(a.author_name)} · ${esc(a.created_at)}
        ${can("crm", "edit") ? ` · <a href="#" onclick="CRM.togglePin(${a.id});return false;">${a.is_pinned ? "sabit kaldır" : "sabitle"}</a>` : ""}
        ${can("crm", "delete") ? ` · <a href="#" onclick="CRM.delActivity(${a.id});return false;">sil</a>` : ""}</div>
      ${a.subject ? "<b>" + esc(a.subject) + "</b><br>" : ""}<div class="tl-body">${esc(a.body)}</div></li>`;
  }

  // ── Detay slide-over ──────────────────────────────────────────────────────
  function openDrawer() { el("scrim").classList.add("open"); el("drawer").classList.add("open"); }
  function closeDrawer() { el("scrim").classList.remove("open"); el("drawer").classList.remove("open"); state.drawer = null; }

  async function openCompany(id) { await openEntity("company", id); }
  async function openContact(id) { await openEntity("contact", id); }
  async function openDeal(id) { await openEntity("deal", id); }

  async function openEntity(kind, id) {
    state.drawer = { kind: kind, id: id, data: null };
    state.dwTab = "summary";
    openDrawer();
    el("dwTitle").textContent = "Yükleniyor…"; el("dwSub").textContent = ""; el("dwTabs").innerHTML = ""; el("dwBody").innerHTML = "";
    try {
      const path = kind === "company" ? "/companies/" : kind === "contact" ? "/contacts/" : "/deals/";
      const d = await api(path + id);
      state.drawer.data = d;
      renderDrawer();
    } catch (e) { el("dwBody").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  function renderDrawer() {
    const dr = state.drawer; if (!dr || !dr.data) return;
    const kind = dr.kind, d = dr.data;
    const ent = d.company || d.contact || d.deal;
    const title = ent.name || ent.full_name || ent.title;
    el("dwTitle").textContent = title;
    let sub = "";
    if (kind === "company") sub = ent.sector || ent.city || "";
    else if (kind === "contact") sub = [ent.title, ent.company_name].filter(Boolean).join(" · ");
    else sub = [ent.company_name, ent.stage_name, money(ent.value, ent.currency)].filter(Boolean).join(" · ");
    el("dwSub").textContent = sub;

    const tabs = [["summary", "Özet"], ["notes", "Zaman Çizelgesi"], ["tasks", "Görevler"]];
    el("dwTabs").innerHTML = tabs.map((t) =>
      `<button class="drawer-tab ${state.dwTab === t[0] ? "active" : ""}" onclick="CRM.dwSwitch('${t[0]}')">${t[1]}</button>`).join("");

    if (state.dwTab === "summary") renderDrawerSummary(kind, d);
    else if (state.dwTab === "notes") renderDrawerNotes(d);
    else renderDrawerTasks(d);
  }

  function dwSwitch(tab) { state.dwTab = tab; renderDrawer(); }

  function kv(k, v) { return v ? `<div class="kv"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>` : ""; }

  function renderDrawerSummary(kind, d) {
    const e = d.company || d.contact || d.deal;
    let html = "";
    // Üst aksiyonlar
    html += '<div style="display:flex;gap:0.5rem;margin-bottom:1rem;flex-wrap:wrap;">';
    if (e.wa_link) html += `<a class="btn-g btn-sm" style="background:#25d366;" href="${esc(e.wa_link)}" target="_blank"><i class="bi bi-whatsapp"></i> WhatsApp</a>`;
    if (can("crm", "edit")) {
      const editFn = kind === "company" ? "companyModal" : kind === "contact" ? "contactModal" : "dealModal";
      html += `<button class="btn-g btn-sm btn-o" onclick="CRM.${editFn}(${e.id})"><i class="bi bi-pencil"></i> Düzenle</button>`;
    }
    if (kind === "deal" && can("crm", "edit") && d.deal.status === "open") {
      html += `<button class="btn-g btn-sm" style="background:#16a34a;" onclick="CRM.closeDeal(${e.id},'won')"><i class="bi bi-trophy"></i> Kazanıldı</button>`;
      html += `<button class="btn-g btn-sm btn-o" onclick="CRM.closeDeal(${e.id},'lost')">Kaybedildi</button>`;
    }
    if (can("crm", "delete")) {
      const delFn = kind === "company" ? "delCompany" : kind === "contact" ? "delContact" : "delDeal";
      html += `<button class="btn-g btn-sm btn-o" style="color:#ef4444;border-color:#fecaca;" onclick="CRM.${delFn}(${e.id})"><i class="bi bi-trash"></i></button>`;
    }
    html += "</div>";

    if (kind === "company") {
      html += kv("Sektör", esc(e.sector)) + kv("Telefon", esc(e.phone)) + kv("E-posta", esc(e.email)) +
        kv("Web", e.website ? `<a href="${esc(e.website)}" target="_blank">${esc(e.website)}</a>` : "") +
        kv("Adres", esc([e.address, e.city, e.country].filter(Boolean).join(", "))) +
        kv("Vergi", esc([e.tax_office, e.tax_no].filter(Boolean).join(" / "))) +
        kv("Sorumlu", esc(e.owner_name)) + kv("Not", esc(e.notes));
      // Kişiler
      html += sectionList("Kişiler", d.contacts, (c) => `<div class="kv"><div class="v"><a href="#" onclick="CRM.openContact(${c.id});return false;"><b>${esc(c.full_name)}</b></a> <span class="muted">${esc(c.title)}</span></div></div>`, can("crm", "create") ? `<button class="btn-g btn-sm" onclick="CRM.contactModal(null,${e.id})"><i class="bi bi-plus"></i> Kişi</button>` : "");
      // Fırsatlar
      html += sectionList("Fırsatlar", d.deals, (dl) => `<div class="kv"><div class="v"><a href="#" onclick="CRM.openDeal(${dl.id});return false;">${esc(dl.title)}</a> <span class="muted">${esc(dl.stage_name)} · ${money(dl.value, dl.currency)}</span></div></div>`, can("crm", "create") ? `<button class="btn-g btn-sm" onclick="CRM.dealModal(null,${e.id})"><i class="bi bi-plus"></i> Fırsat</button>` : "");
    } else if (kind === "contact") {
      html += kv("Firma", e.company_id ? `<a href="#" onclick="CRM.openCompany(${e.company_id});return false;">${esc(e.company_name)}</a>` : "") +
        kv("Unvan", esc(e.title)) + kv("Telefon", esc(e.phone)) + kv("Mobil", esc(e.mobile)) +
        kv("WhatsApp", esc(e.whatsapp_number)) + kv("E-posta", esc(e.email)) +
        kv("Kaynak", esc(e.source)) + kv("Sorumlu", esc(e.owner_name)) + kv("Not", esc(e.notes));
    } else {
      const dl = d.deal;
      html += kv("Firma", dl.company_id ? `<a href="#" onclick="CRM.openCompany(${dl.company_id});return false;">${esc(dl.company_name)}</a>` : "") +
        kv("Kişi", dl.contact_id ? `<a href="#" onclick="CRM.openContact(${dl.contact_id});return false;">${esc(dl.contact_name)}</a>` : "") +
        kv("Aşama", esc(dl.stage_name)) + kv("Değer", money(dl.value, dl.currency)) +
        kv("Olasılık", dl.probability ? "%" + dl.probability : "") +
        kv("Beklenen Kapanış", esc(dl.expected_close_label)) +
        kv("Durum", '<span class="pill ' + (dl.status === "won" ? "pill-g" : dl.status === "lost" ? "pill-o" : "pill-b") + '">' + (dl.status === "won" ? "Kazanıldı" : dl.status === "lost" ? "Kaybedildi" : "Açık") + "</span>") +
        kv("Sorumlu", esc(dl.owner_name)) + (dl.lost_reason ? kv("Kayıp Nedeni", esc(dl.lost_reason)) : "");
    }
    el("dwBody").innerHTML = html;
  }

  function sectionList(title, rows, rowFn, addBtn) {
    let h = `<div style="margin-top:1.1rem;display:flex;align-items:center;justify-content:space-between;"><b style="color:var(--navy);">${esc(title)}</b>${addBtn || ""}</div>`;
    if (!rows || !rows.length) h += '<div class="muted" style="font-size:0.82rem;padding:0.4rem 0;">Kayıt yok.</div>';
    else h += rows.map(rowFn).join("");
    return h;
  }

  function renderDrawerNotes(d) {
    const dr = state.drawer;
    const acts = d.activities || [];
    let html = "";
    if (can("crm", "create")) {
      html += `<div class="card2" style="margin-bottom:1rem;"><div class="card2-body">
        <div class="mb"><select class="ipt" id="acType">
          <option value="note">Not</option><option value="call">Arama</option>
          <option value="meeting">Toplantı</option><option value="email">E-posta</option>
          <option value="whatsapp">WhatsApp</option></select></div>
        <div class="mb"><textarea class="ipt" id="acBody" rows="3" placeholder="Bir not / görüşme kaydı yaz… (herkes görecek)"></textarea></div>
        <button class="btn-g btn-sm" onclick="CRM.addActivity()"><i class="bi bi-plus-lg"></i> Ekle</button></div></div>`;
    }
    html += acts.length ? '<ul class="tl">' + acts.map(tlItem).join("") + "</ul>" : '<div class="muted">Henüz kayıt yok.</div>';
    el("dwBody").innerHTML = html;
  }

  function renderDrawerTasks(d) {
    const tasks = d.tasks || [];
    let html = "";
    if (can("crm", "create")) {
      const sc = drawerScope();
      html += `<button class="btn-g btn-sm" style="margin-bottom:1rem;" onclick="CRM.taskModal(null,${JSON.stringify(sc).replace(/"/g, "&quot;")})"><i class="bi bi-plus-lg"></i> Görev Ekle</button>`;
    }
    if (!tasks.length) html += '<div class="muted">Görev yok.</div>';
    else html += tasks.map((t) => {
      const done = t.status === "done";
      return `<div style="display:flex;align-items:center;gap:0.6rem;padding:0.5rem 0;border-bottom:1px solid #efe9dd;">
        <button class="ico-btn" onclick="CRM.toggleTask(${t.id},true)"><i class="bi bi-${done ? "check-circle-fill" : "circle"}" style="${done ? "color:#22c55e;" : ""}"></i></button>
        <div style="flex:1;${done ? "opacity:0.55;text-decoration:line-through;" : ""}"><div style="font-size:0.88rem;">${esc(t.title)}</div>
        <div class="muted" style="font-size:0.74rem;">${t.due_label ? esc(t.due_label) : "tarih yok"} · ${esc(t.assigned_to_name)}</div></div>
        ${t.overdue && !done ? '<span class="pill pill-o">Gecikti</span>' : ""}</div>`;
    }).join("");
    el("dwBody").innerHTML = html;
  }

  function drawerScope() {
    const dr = state.drawer; if (!dr) return {};
    if (dr.kind === "company") return { company_id: dr.id };
    if (dr.kind === "contact") return { contact_id: dr.id };
    return { deal_id: dr.id };
  }

  async function refreshDrawer() {
    const dr = state.drawer; if (!dr) return;
    await openEntity(dr.kind, dr.id);
  }

  // ── Aktivite ekle / pin / sil ──────────────────────────────────────────────
  async function addActivity() {
    const body = (el("acBody").value || "").trim();
    if (!body) { toast("Bir şeyler yazın."); return; }
    const payload = Object.assign({ type: el("acType").value, body: body }, drawerScope());
    try { await api("/activities", jbody(payload)); toast("Eklendi.", "success"); state.dwTab = "notes"; await refreshDrawer(); if (state.tab === "dashboard") renderDashboard(); }
    catch (e) { toast(e.message); }
  }
  async function togglePin(id) { try { await api("/activities/" + id + "/pin", { method: "POST" }); await refreshDrawer(); } catch (e) { toast(e.message); } }
  async function delActivity(id) { if (!confirm("Bu kayıt silinsin mi?")) return; try { await api("/activities/" + id, { method: "DELETE" }); await refreshDrawer(); } catch (e) { toast(e.message); } }

  // ── Kanban sürükle-bırak ────────────────────────────────────────────────
  let _dragId = null;
  function dragDeal(ev, id) { _dragId = id; ev.dataTransfer.effectAllowed = "move"; }
  function allowDrop(ev, col) { ev.preventDefault(); col.classList.add("dragover"); }
  async function dropDeal(ev, stageId) {
    ev.preventDefault();
    document.querySelectorAll(".kcol").forEach((c) => c.classList.remove("dragover"));
    if (!_dragId) return;
    const id = _dragId; _dragId = null;
    try { await api("/deals/" + id + "/move", jbody({ stage_id: stageId, sort_order: 0 })); renderPipeline(); }
    catch (e) { toast(e.message); }
  }

  // ── Modallar (oluştur / düzenle) ───────────────────────────────────────────
  let _modalSave = null;
  function openModal(title, bodyHtml, onSave) {
    el("mTitle").textContent = title; el("mBody").innerHTML = bodyHtml;
    _modalSave = onSave; el("modal").classList.add("open");
  }
  function closeModal() { el("modal").classList.remove("open"); _modalSave = null; }

  function userOptions(sel) {
    return '<option value="">— Sorumlu —</option>' + state.users.map((u) =>
      `<option value="${u.id}" ${sel == u.id ? "selected" : ""}>${esc(u.full_name)}</option>`).join("");
  }
  function companyOptions(sel) {
    return '<option value="">— Firma —</option>' + (state._companies || []).map((c) =>
      `<option value="${c.id}" ${sel == c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  }
  function stageOptions(sel) {
    return state.stages.map((s) => `<option value="${s.id}" ${sel == s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  }
  async function ensureCompanies() {
    if (!state._companies) { try { state._companies = await api("/companies"); } catch (e) { state._companies = []; } }
  }
  function fval(id) { const e = el(id); return e ? e.value.trim() : ""; }
  function fnum(id) { const v = fval(id); return v === "" ? null : parseInt(v, 10); }

  // Firma modalı
  async function companyModal(id) {
    let c = {};
    if (id) { try { const d = await api("/companies/" + id); c = d.company; } catch (e) { toast(e.message); return; } }
    openModal(id ? "Firmayı Düzenle" : "Yeni Firma", `
      <div class="mb"><label class="fld">Firma adı *</label><input class="ipt" id="f_name" value="${esc(c.name)}"></div>
      <div class="frow">
        <div><label class="fld">Sektör</label><input class="ipt" id="f_sector" value="${esc(c.sector)}"></div>
        <div><label class="fld">Telefon</label><input class="ipt" id="f_phone" value="${esc(c.phone)}"></div>
        <div><label class="fld">E-posta</label><input class="ipt" id="f_email" value="${esc(c.email)}"></div>
        <div><label class="fld">Web</label><input class="ipt" id="f_website" value="${esc(c.website)}"></div>
        <div><label class="fld">Şehir</label><input class="ipt" id="f_city" value="${esc(c.city)}"></div>
        <div><label class="fld">Ülke</label><input class="ipt" id="f_country" value="${esc(c.country)}"></div>
        <div><label class="fld">Vergi Dairesi</label><input class="ipt" id="f_taxoff" value="${esc(c.tax_office)}"></div>
        <div><label class="fld">Vergi No</label><input class="ipt" id="f_taxno" value="${esc(c.tax_no)}"></div>
        <div class="full"><label class="fld">Adres</label><textarea class="ipt" id="f_address" rows="2">${esc(c.address)}</textarea></div>
        <div class="full"><label class="fld">Sorumlu</label><select class="ipt" id="f_owner">${userOptions(c.owner_user_id)}</select></div>
        <div class="full"><label class="fld">Not</label><textarea class="ipt" id="f_notes" rows="2">${esc(c.notes)}</textarea></div>
      </div>`, async function () {
      const name = fval("f_name"); if (!name) { toast("Firma adı gerekli."); return; }
      const payload = { name: name, sector: fval("f_sector"), phone: fval("f_phone"), email: fval("f_email"), website: fval("f_website"), city: fval("f_city"), country: fval("f_country"), tax_office: fval("f_taxoff"), tax_no: fval("f_taxno"), address: fval("f_address"), notes: fval("f_notes"), owner_user_id: fnum("f_owner") };
      try {
        await api(id ? "/companies/" + id : "/companies", jbody(payload, id ? "PUT" : "POST"));
        closeModal(); toast("Kaydedildi.", "success"); state._companies = null;
        if (state.drawer && state.drawer.kind === "company") refreshDrawer(); else loadCompanies();
      } catch (e) { toast(e.message); }
    });
  }

  // Kişi modalı
  async function contactModal(id, companyId) {
    await ensureCompanies();
    let c = {};
    if (id) { try { const d = await api("/contacts/" + id); c = d.contact; } catch (e) { toast(e.message); return; } }
    if (companyId && !id) c.company_id = companyId;
    openModal(id ? "Kişiyi Düzenle" : "Yeni Kişi", `
      <div class="mb"><label class="fld">Ad Soyad *</label><input class="ipt" id="k_name" value="${esc(c.full_name)}"></div>
      <div class="frow">
        <div class="full"><label class="fld">Firma</label><select class="ipt" id="k_company">${companyOptions(c.company_id)}</select></div>
        <div><label class="fld">Unvan</label><input class="ipt" id="k_title" value="${esc(c.title)}"></div>
        <div><label class="fld">Telefon</label><input class="ipt" id="k_phone" value="${esc(c.phone)}"></div>
        <div><label class="fld">Mobil</label><input class="ipt" id="k_mobile" value="${esc(c.mobile)}"></div>
        <div><label class="fld">WhatsApp (+90…)</label><input class="ipt" id="k_wa" value="${esc(c.whatsapp_number)}"></div>
        <div><label class="fld">E-posta</label><input class="ipt" id="k_email" value="${esc(c.email)}"></div>
        <div><label class="fld">Kaynak</label><input class="ipt" id="k_source" value="${esc(c.source)}" placeholder="manual / referral …"></div>
        <div class="full"><label class="fld">Sorumlu</label><select class="ipt" id="k_owner">${userOptions(c.owner_user_id)}</select></div>
        <div class="full"><label class="fld">Not</label><textarea class="ipt" id="k_notes" rows="2">${esc(c.notes)}</textarea></div>
      </div>`, async function () {
      const name = fval("k_name"); if (!name) { toast("Ad Soyad gerekli."); return; }
      const payload = { full_name: name, company_id: fnum("k_company"), title: fval("k_title"), phone: fval("k_phone"), mobile: fval("k_mobile"), whatsapp_number: fval("k_wa"), email: fval("k_email"), source: fval("k_source"), notes: fval("k_notes"), owner_user_id: fnum("k_owner") };
      try {
        await api(id ? "/contacts/" + id : "/contacts", jbody(payload, id ? "PUT" : "POST"));
        closeModal(); toast("Kaydedildi.", "success");
        if (state.drawer) refreshDrawer(); if (state.tab === "contacts") loadContacts();
      } catch (e) { toast(e.message); }
    });
  }

  // Fırsat modalı
  async function dealModal(id, companyId) {
    await ensureCompanies();
    let c = { currency: "TRY", probability: 0 };
    if (id) { try { const d = await api("/deals/" + id); c = d.deal; } catch (e) { toast(e.message); return; } }
    if (companyId && !id) c.company_id = companyId;
    openModal(id ? "Fırsatı Düzenle" : "Yeni Fırsat", `
      <div class="mb"><label class="fld">Başlık *</label><input class="ipt" id="d_title" value="${esc(c.title)}"></div>
      <div class="frow">
        <div class="full"><label class="fld">Firma</label><select class="ipt" id="d_company">${companyOptions(c.company_id)}</select></div>
        <div><label class="fld">Aşama</label><select class="ipt" id="d_stage">${stageOptions(c.stage_id)}</select></div>
        <div><label class="fld">Sorumlu</label><select class="ipt" id="d_owner">${userOptions(c.owner_user_id)}</select></div>
        <div><label class="fld">Değer</label><input class="ipt" id="d_value" type="number" min="0" step="any" value="${c.value || ""}"></div>
        <div><label class="fld">Para Birimi</label><select class="ipt" id="d_cur">
          <option ${c.currency === "TRY" ? "selected" : ""}>TRY</option><option ${c.currency === "USD" ? "selected" : ""}>USD</option>
          <option ${c.currency === "EUR" ? "selected" : ""}>EUR</option></select></div>
        <div><label class="fld">Olasılık %</label><input class="ipt" id="d_prob" type="number" min="0" max="100" value="${c.probability || 0}"></div>
        <div><label class="fld">Beklenen Kapanış</label><input class="ipt" id="d_close" type="datetime-local" value="${esc(c.expected_close_at)}"></div>
      </div>`, async function () {
      const title = fval("d_title"); if (!title) { toast("Başlık gerekli."); return; }
      const payload = { title: title, company_id: fnum("d_company"), stage_id: fnum("d_stage"), owner_user_id: fnum("d_owner"), value: parseFloat(fval("d_value")) || 0, currency: fval("d_cur") || "TRY", probability: parseInt(fval("d_prob"), 10) || 0, expected_close_at: fval("d_close") || null };
      try {
        await api(id ? "/deals/" + id : "/deals", jbody(payload, id ? "PUT" : "POST"));
        closeModal(); toast("Kaydedildi.", "success");
        if (state.drawer && state.drawer.kind === "deal") refreshDrawer(); else if (state.tab === "pipeline") renderPipeline();
      } catch (e) { toast(e.message); }
    });
  }

  // Görev modalı
  async function taskModal(id, scope) {
    let c = {};
    if (id) {
      try { const rows = await api("/tasks?scope=all"); c = rows.find((t) => t.id === id) || {}; } catch (e) { /* ignore */ }
    }
    if (scope && typeof scope === "object") c = Object.assign(c, scope);
    openModal(id ? "Görevi Düzenle" : "Yeni Görev", `
      <div class="mb"><label class="fld">Başlık *</label><input class="ipt" id="g_title" value="${esc(c.title)}"></div>
      <div class="frow">
        <div><label class="fld">Son Tarih</label><input class="ipt" id="g_due" type="datetime-local" value="${esc(c.due_at)}"></div>
        <div><label class="fld">Atanan</label><select class="ipt" id="g_owner">${userOptions(c.assigned_to_user_id || window.ME.id)}</select></div>
        <div class="full"><label class="fld">Not</label><textarea class="ipt" id="g_notes" rows="2">${esc(c.notes)}</textarea></div>
      </div>`, async function () {
      const title = fval("g_title"); if (!title) { toast("Başlık gerekli."); return; }
      const payload = { title: title, due_at: fval("g_due") || null, assigned_to_user_id: fnum("g_owner"), notes: fval("g_notes"), company_id: c.company_id || null, contact_id: c.contact_id || null, deal_id: c.deal_id || null };
      try {
        await api(id ? "/tasks/" + id : "/tasks", jbody(payload, id ? "PUT" : "POST"));
        closeModal(); toast("Kaydedildi.", "success");
        if (state.drawer) refreshDrawer(); if (state.tab === "tasks") loadTasks(); if (state.tab === "dashboard") renderDashboard();
        refreshBadge();
      } catch (e) { toast(e.message); }
    });
  }

  // ── Silme / kapatma / tamamlama ─────────────────────────────────────────────
  async function delCompany(id) { if (!confirm("Firma arşivlensin mi? (Kayıtlar saklanır)")) return; try { await api("/companies/" + id, { method: "DELETE" }); toast("Arşivlendi.", "success"); closeDrawer(); loadCompanies(); } catch (e) { toast(e.message); } }
  async function delContact(id) { if (!confirm("Kişi arşivlensin mi?")) return; try { await api("/contacts/" + id, { method: "DELETE" }); toast("Arşivlendi.", "success"); closeDrawer(); if (state.tab === "contacts") loadContacts(); } catch (e) { toast(e.message); } }
  async function delDeal(id) { if (!confirm("Fırsat silinsin mi?")) return; try { await api("/deals/" + id, { method: "DELETE" }); toast("Silindi.", "success"); closeDrawer(); renderPipeline(); } catch (e) { toast(e.message); } }
  async function delTask(id) { if (!confirm("Görev silinsin mi?")) return; try { await api("/tasks/" + id, { method: "DELETE" }); loadTasks(); refreshBadge(); } catch (e) { toast(e.message); } }

  async function toggleTask(id, inDrawer) {
    try { await api("/tasks/" + id + "/complete", { method: "POST" }); if (inDrawer) refreshDrawer(); if (state.tab === "tasks") loadTasks(); if (state.tab === "dashboard") renderDashboard(); refreshBadge(); }
    catch (e) { toast(e.message); }
  }

  async function closeDeal(id, result) {
    let reason = null;
    if (result === "lost") { reason = prompt("Kayıp nedeni (opsiyonel):", ""); if (reason === null) return; }
    try { await api("/deals/" + id + "/close", jbody({ result: result, lost_reason: reason })); toast(result === "won" ? "Kazanıldı! 🎉" : "Kaybedildi olarak işaretlendi.", "success"); if (state.drawer) refreshDrawer(); } catch (e) { toast(e.message); }
  }

  // ── Görev rozeti (geciken sayısı) ──────────────────────────────────────────
  async function refreshBadge() {
    try {
      const rows = await api("/tasks?scope=overdue");
      const n = rows.length;
      ["navTaskBadge", "navTaskBadgeM"].forEach((bid) => {
        const b = el(bid); if (!b) return;
        if (n > 0) { b.textContent = n; b.style.display = ""; } else { b.style.display = "none"; }
      });
    } catch (e) { /* sessiz */ }
  }

  // ── Açılış ────────────────────────────────────────────────────────────────
  async function boot() {
    document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    el("logoutBtn").addEventListener("click", async function () {
      this.disabled = true;
      try { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; }
      catch (e) { toast("Çıkış hatası."); this.disabled = false; }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });
    el("mSave").addEventListener("click", function () { if (_modalSave) _modalSave(); });

    // Referans verileri yükle (aşamalar + kullanıcılar)
    try { state.stages = await api("/stages"); } catch (e) { state.stages = []; }
    try { state.users = await api("/users"); } catch (e) { state.users = []; }

    switchTab("dashboard");
    refreshBadge();
    setInterval(refreshBadge, 120000);
  }

  // Onclick köprüsü — render edilen HTML global CRM.* çağırır
  window.CRM = {
    openCompany, openContact, openDeal, dwSwitch,
    companyModal, contactModal, dealModal, taskModal,
    delCompany, delContact, delDeal, delTask,
    toggleTask, closeDeal, addActivity, togglePin, delActivity,
    dragDeal, allowDrop, dropDeal,
  };
  window.closeDrawer = closeDrawer;
  window.closeModal = closeModal;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
