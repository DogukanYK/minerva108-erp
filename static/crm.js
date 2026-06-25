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

  // Kaynak rozeti + filtre — Meta / Kommo / Elle
  const SOURCE_PILL = { meta: "pill-b", kommo: "pill-g", manual: "pill-gray" };
  function sourcePill(row) {
    const s = row.source || "manual";
    return `<span class="pill ${SOURCE_PILL[s] || "pill-gray"}">${esc(row.source_label || "Elle")}</span>`;
  }
  function sourceSelect(id) {
    return `<select class="ipt" id="${id}" style="max-width:150px;" title="Kaynağa göre filtrele">
      <option value="">Tüm kaynaklar</option>
      <option value="meta">Meta</option>
      <option value="kommo">Kommo</option>
      <option value="manual">Elle girilen</option></select>`;
  }
  function srcParam(id) {
    const e = el(id);
    return e && e.value ? e.value : "";
  }
  function inputVal(id) { const e = el(id); return e && e.value ? e.value.trim() : ""; }

  // ── İçe/dışa aktarma (Excel) ─────────────────────────────────────────────
  function ioButtons(entity) {
    let h = `<button class="btn-g btn-o btn-sm" onclick="CRM.exportXlsx('${entity}')" title="Görünümü Excel'e aktar"><i class="bi bi-file-earmark-excel"></i> Dışa</button>`;
    if (can("crm", "create") && (entity === "companies" || entity === "contacts"))
      h += `<button class="btn-g btn-o btn-sm" onclick="CRM.importModal('${entity}')" title="Excel/CSV içe aktar"><i class="bi bi-upload"></i> İçe</button>`;
    return h;
  }
  function exportXlsx(entity) {
    const p = [];
    if (entity === "companies") {
      if (inputVal("coSearch")) p.push("q=" + encodeURIComponent(inputVal("coSearch")));
      if (srcParam("coSource")) p.push("source=" + srcParam("coSource"));
    } else if (entity === "contacts") {
      if (inputVal("ctSearch")) p.push("q=" + encodeURIComponent(inputVal("ctSearch")));
      if (srcParam("ctSource")) p.push("source=" + srcParam("ctSource"));
    } else if (entity === "deals") {
      if (state.pipeSource) p.push("source=" + encodeURIComponent(state.pipeSource));
    }
    window.location = "/api/crm/export?entity=" + entity + (p.length ? "&" + p.join("&") : "");
  }
  function importModal(entity) {
    const label = entity === "companies" ? "Firma" : "Kişi";
    openModal(label + " İçe Aktar (Excel / CSV)", `
      <p class="muted" style="font-size:0.84rem;">Excel (.xlsx) veya CSV yükleyin. Başlık satırı tanınır (Firma/Ad Soyad, Telefon, E-posta…).
        <a href="/api/crm/import/template?entity=${entity}" target="_blank"><b>Boş şablon indir</b></a>.</p>
      <div class="mb"><input type="file" id="imp_file" class="ipt" accept=".xlsx,.csv"></div>
      <p class="muted" style="font-size:0.78rem;">Her satır yeni bir kayıt olur (kaynak: "Elle/İçe aktarma"). Kişilerde "Firma" sütunu mevcut firmaya eşlenir.</p>
    `, async function () {
      const f = el("imp_file").files[0];
      if (!f) { toast("Bir dosya seçin."); return; }
      const fd = new FormData(); fd.append("entity", entity); fd.append("file", f);
      const btn = el("mSave"); btn.disabled = true; const o = btn.textContent; btn.textContent = "İçe aktarılıyor…";
      try {
        const r = await fetch("/api/crm/import", { method: "POST", body: fd });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { toast(d.detail || "İçe aktarılamadı."); return; }
        closeModal(); toast(d.message || "Tamam.", "success");
        if (entity === "companies") { state._companies = null; if (state.tab === "companies") loadCompanies(); }
        else if (state.tab === "contacts") loadContacts();
      } catch (e) { toast("Yükleme hatası."); }
      finally { btn.disabled = false; btn.textContent = o; }
    });
  }

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
    reports: { title: "Raporlar", render: renderReports },
    integrations: { title: "Entegrasyonlar", render: renderIntegrations },
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
      let html = "";
      if (can("admin", "view")) {
        html += '<div style="margin-bottom:1rem;display:flex;gap:0.6rem;flex-wrap:wrap;">' +
          '<button class="btn-g btn-o btn-sm" onclick="CRM.go(\'integrations\')"><i class="bi bi-plug"></i> Entegrasyonlar (Kommo)</button>' +
          '<a class="btn-g btn-o btn-sm" href="/admin"><i class="bi bi-shield-lock"></i> Yönetim</a></div>';
      }
      html += '<div class="stat-grid">';
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
            <div class="bar-track"><div class="bar-fill" style="width:${pct}%;"></div></div></div>`;
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
    return `<div class="row-sep" style="display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0;">
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
        ${sourceSelect("coSource")}
        ${ownerSelect("coOwner")}
        ${tagSelect("coTag")}
        ${viewsSelect("companies")}
        ${can("crm", "create") ? '<button class="btn-g" onclick="CRM.companyModal()"><i class="bi bi-plus-lg"></i> Yeni Firma</button>' : ""}
        ${ioButtons("companies")}
      </div>
      ${bulkBar("companies")}
      <div class="card2"><div class="card2-body" style="overflow-x:auto;"><div id="coList"></div></div></div>`;
      let tmr;
      el("coSearch").addEventListener("input", function () { clearTimeout(tmr); tmr = setTimeout(loadCompanies, 250); });
      el("coSource").addEventListener("change", loadCompanies);
      el("coOwner").addEventListener("change", loadCompanies);
      const _coTag = el("coTag"); if (_coTag) _coTag.addEventListener("change", loadCompanies);
      loadViews("companies");
    }
    loadCompanies();
  }
  async function loadCompanies() {
    const box = el("coList");
    box.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const q = (el("coSearch").value || "").trim();
      const params = [];
      if (q) params.push("q=" + encodeURIComponent(q));
      if (srcParam("coSource")) params.push("source=" + encodeURIComponent(srcParam("coSource")));
      if (srcParam("coOwner")) params.push("owner=" + encodeURIComponent(srcParam("coOwner")));
      if (srcParam("coTag")) params.push("tag=" + encodeURIComponent(srcParam("coTag")));
      const rows = await api("/companies" + (params.length ? "?" + params.join("&") : ""));
      bulkSel.companies.clear(); updateBulkBar("companies");
      if (!rows.length) { box.innerHTML = '<div class="empty">Firma yok.</div>'; return; }
      const selCol = can("crm", "edit");
      box.innerHTML = '<table><thead><tr>' + (selCol ? '<th style="width:28px;"><input type="checkbox" onclick="CRM.bulkAll(\'companies\',this.checked)"></th>' : "") + '<th>Firma</th><th class="col-sec">Şehir</th><th class="col-sec">Kişi</th><th class="col-sec">Açık Fırsat</th><th>Kaynak</th><th class="col-sec">Sorumlu</th><th></th></tr></thead><tbody>' +
        rows.map((c) => `<tr class="clickable" onclick="CRM.openCompany(${c.id})">
          ${selCol ? `<td onclick="event.stopPropagation();"><input type="checkbox" class="companies_chk" value="${c.id}" onchange="CRM.bulkToggle('companies',${c.id},this.checked)"></td>` : ""}
          <td><span class="av">${initials(c.name)}</span><b>${esc(c.name)}</b>${c.sector ? '<div class="muted" style="font-size:0.76rem;margin-left:2.4rem;">' + esc(c.sector) + "</div>" : ""}</td>
          <td class="muted col-sec">${esc(c.city) || "—"}</td>
          <td class="col-sec">${c.contact_count}</td>
          <td class="col-sec">${c.open_deal_count ? '<span class="pill pill-b">' + c.open_deal_count + "</span>" : '<span class="muted">—</span>'}</td>
          <td>${sourcePill(c)}</td>
          <td class="muted col-sec">${esc(c.owner_name) || "—"}</td>
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
        ${sourceSelect("ctSource")}
        ${ownerSelect("ctOwner")}
        ${tagSelect("ctTag")}
        ${viewsSelect("contacts")}
        ${can("crm", "create") ? '<button class="btn-g" onclick="CRM.contactModal()"><i class="bi bi-plus-lg"></i> Yeni Kişi</button>' : ""}
        ${ioButtons("contacts")}
      </div>
      ${bulkBar("contacts")}
      <div class="card2"><div class="card2-body" style="overflow-x:auto;"><div id="ctList"></div></div></div>`;
      let tmr;
      el("ctSearch").addEventListener("input", function () { clearTimeout(tmr); tmr = setTimeout(loadContacts, 250); });
      el("ctSource").addEventListener("change", loadContacts);
      el("ctOwner").addEventListener("change", loadContacts);
      const _ctTag = el("ctTag"); if (_ctTag) _ctTag.addEventListener("change", loadContacts);
      loadViews("contacts");
    }
    loadContacts();
  }
  async function loadContacts() {
    const box = el("ctList");
    box.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const q = (el("ctSearch").value || "").trim();
      const params = [];
      if (q) params.push("q=" + encodeURIComponent(q));
      if (srcParam("ctSource")) params.push("source=" + encodeURIComponent(srcParam("ctSource")));
      if (srcParam("ctOwner")) params.push("owner=" + encodeURIComponent(srcParam("ctOwner")));
      if (srcParam("ctTag")) params.push("tag=" + encodeURIComponent(srcParam("ctTag")));
      const rows = await api("/contacts" + (params.length ? "?" + params.join("&") : ""));
      bulkSel.contacts.clear(); updateBulkBar("contacts");
      if (!rows.length) { box.innerHTML = '<div class="empty">Kişi yok.</div>'; return; }
      const selColC = can("crm", "edit");
      box.innerHTML = '<table><thead><tr>' + (selColC ? '<th style="width:28px;"><input type="checkbox" onclick="CRM.bulkAll(\'contacts\',this.checked)"></th>' : "") + '<th>Kişi</th><th class="col-sec">Firma</th><th>Telefon</th><th>Kaynak</th><th class="col-sec">E-posta</th><th></th></tr></thead><tbody>' +
        rows.map((c) => `<tr class="clickable" onclick="CRM.openContact(${c.id})">
          ${selColC ? `<td onclick="event.stopPropagation();"><input type="checkbox" class="contacts_chk" value="${c.id}" onchange="CRM.bulkToggle('contacts',${c.id},this.checked)"></td>` : ""}
          <td><span class="av">${initials(c.full_name)}</span><b>${esc(c.full_name)}</b>${c.title ? '<div class="muted" style="font-size:0.76rem;margin-left:2.4rem;">' + esc(c.title) + "</div>" : ""}</td>
          <td class="muted col-sec">${esc(c.company_name) || "—"}</td>
          <td class="muted">${esc(c.mobile || c.phone) || "—"}</td>
          <td>${sourcePill(c)}</td>
          <td class="muted col-sec">${esc(c.email) || "—"}</td>
          <td onclick="event.stopPropagation();">${c.wa_link ? '<a class="ico-btn wa" href="' + esc(c.wa_link) + '" target="_blank" title="WhatsApp"><i class="bi bi-whatsapp"></i></a>' : ""}</td>
        </tr>`).join("") + "</tbody></table>";
    } catch (e) { box.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  // ── Pipeline (Kanban) ──────────────────────────────────────────────────────
  async function renderPipeline() {
    const v = el("view-pipeline");
    v.innerHTML = `<div class="toolbar"><h2 class="page-title" style="margin:0;flex:1;min-width:140px;">Satış Pipeline</h2>
      ${sourceSelect("pipeSource")}
      ${tagSelect("pipeTag")}
      ${can("crm", "create") ? '<button class="btn-g" onclick="CRM.dealModal()"><i class="bi bi-plus-lg"></i> Yeni Fırsat</button>' : ""}
      ${ioButtons("deals")}
      </div><div id="kanban" class="kanban"><div class="empty">Yükleniyor…</div></div>`;
    const ps = el("pipeSource");
    if (ps) {
      ps.value = state.pipeSource || "";
      ps.addEventListener("change", () => { state.pipeSource = ps.value; renderPipeline(); });
    }
    const pt = el("pipeTag");
    if (pt) {
      pt.value = state.pipeTag || "";
      pt.addEventListener("change", () => { state.pipeTag = pt.value; renderPipeline(); });
    }
    try {
      const pp = [];
      if (state.pipeSource) pp.push("source=" + encodeURIComponent(state.pipeSource));
      if (state.pipeTag) pp.push("tag=" + encodeURIComponent(state.pipeTag));
      const d = await api("/pipeline" + (pp.length ? "?" + pp.join("&") : ""));
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
      <div style="margin:0.3rem 0;">${sourcePill(d)}</div>
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
        return `<div class="row-sep" style="display:flex;align-items:center;gap:0.6rem;padding:0.55rem 0;">
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

  // ── Entegrasyonlar (Kommo) ──────────────────────────────────────────────
  function kvRow(k, v) { return `<div class="kv"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`; }

  async function renderIntegrations() {
    const v = el("view-integrations");
    v.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const s = await api("/integrations/kommo/status");
      const webhookUrl = location.origin + "/api/crm/integrations/kommo/webhook/<KOMMO_WEBHOOK_SECRET>";
      let html = '<h2 class="page-title" style="margin-bottom:1rem;">Entegrasyonlar</h2>';
      html += '<div class="card2"><div class="card2-head"><i class="bi bi-diagram-3"></i><h2>Kommo CRM → Minerva (tek yön)</h2></div><div class="card2-body">';
      if (!s.configured) {
        html += '<div class="pill pill-o" style="margin-bottom:0.9rem;display:inline-block;">Yapılandırılmadı</div>';
        html += '<p class="muted">Sunucudaki <code>.env</code> dosyasına aşağıdakileri ekleyip uygulamayı yeniden başlatın (deploy):</p>';
        html += '<pre style="background:var(--surface-2);padding:0.8rem;border-radius:8px;white-space:pre-wrap;font-size:0.8rem;">KOMMO_SUBDOMAIN=hesabiniz      # örn. minerva (minerva.kommo.com)\nKOMMO_TOKEN=uzun-omurlu-token  # Kommo: Ayarlar → Entegrasyonlar → özel entegrasyon\nKOMMO_WEBHOOK_SECRET=rastgele-uzun-bir-dize</pre>';
      } else {
        html += `<div class="pill pill-g" style="margin-bottom:0.9rem;display:inline-block;">Bağlı · ${esc(s.subdomain)}.kommo.com</div>`;
        html += kvRow("Son senkron", s.last_sync_at || "—") + kvRow("Son durum", s.last_status || "—") + kvRow("Toplam içe aktarılan", s.imported_total);
        html += '<div style="display:flex;gap:0.6rem;margin:1.1rem 0;flex-wrap:wrap;">' +
          '<button class="btn-g" id="kommoImport"><i class="bi bi-cloud-download"></i> Tam İçe Aktar</button>' +
          '<button class="btn-g btn-o" id="kommoSync"><i class="bi bi-arrow-repeat"></i> Delta Senkron</button></div>';
        html += '<p class="muted" style="margin-top:0.6rem;">Gerçek-zamanlı akış için Kommo → <b>Ayarlar → Entegrasyonlar → Webhook\'lar</b>\'a şu URL\'i ekleyin:</p>';
        html += `<div class="ipt" style="word-break:break-all;font-family:monospace;font-size:0.78rem;">${esc(webhookUrl)}</div>`;
        html += '<p class="muted" style="font-size:0.78rem;margin-top:0.4rem;">' +
          (s.webhook_secret_set
            ? "✓ Webhook secret ayarlı — URL'deki &lt;KOMMO_WEBHOOK_SECRET&gt; yerine .env'deki değeri yazın."
            : "⚠ KOMMO_WEBHOOK_SECRET ayarlı değil — webhook çalışmaz (yine de periyodik senkron 15 dk'da bir çeker).") + '</p>';
      }
      html += '</div></div>';
      html += '<div class="card2"><div class="card2-head"><i class="bi bi-tags"></i><h2>Etiketler</h2></div><div class="card2-body" id="tagMgmt"></div></div>';
      html += '<div class="card2"><div class="card2-head"><i class="bi bi-input-cursor-text"></i><h2>Özel Alanlar</h2></div><div class="card2-body" id="fieldMgmt"></div></div>';
      v.innerHTML = html;
      const imp = el("kommoImport"); if (imp) imp.addEventListener("click", () => kommoRun("import", imp));
      const syn = el("kommoSync"); if (syn) syn.addEventListener("click", () => kommoRun("sync", syn));
      loadTagMgmt(); loadFieldMgmt();
    } catch (e) { v.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  async function loadTagMgmt() {
    const box = el("tagMgmt"); if (!box) return;
    try {
      const tags = await api("/tags");
      let h = '<div style="display:flex;gap:0.4rem;margin-bottom:0.8rem;flex-wrap:wrap;align-items:center;"><input class="ipt" id="mgTagName" placeholder="Etiket adı" style="max-width:200px;"><input type="color" id="mgTagColor" value="#6b7280" style="width:42px;height:38px;border:1px solid var(--border-2);border-radius:8px;padding:2px;"><button class="btn-g btn-sm" onclick="CRM.addTagMgmt()"><i class="bi bi-plus"></i> Ekle</button></div>';
      h += tags.length ? '<div style="display:flex;gap:0.4rem;flex-wrap:wrap;">' + tags.map((t) => `<span class="pill" style="background:${esc(t.color)}22;color:${esc(t.color)};border:1px solid ${esc(t.color)}55;">${esc(t.name)} <a href="#" onclick="CRM.delTagMgmt(${t.id});return false;" style="color:inherit;text-decoration:none;font-weight:700;">×</a></span>`).join("") + "</div>" : '<div class="muted">Henüz etiket yok.</div>';
      box.innerHTML = h;
    } catch (e) { box.innerHTML = '<div class="muted">' + esc(e.message) + "</div>"; }
  }
  async function addTagMgmt() {
    const nm = inputVal("mgTagName"); if (!nm) return;
    try { await api("/tags", jbody({ name: nm, color: el("mgTagColor").value })); loadTagMgmt(); }
    catch (e) { toast(e.message); }
  }
  async function delTagMgmt(id) {
    if (!confirm("Etiket silinsin mi? (Tüm kayıtlardan kaldırılır)")) return;
    try { await api("/tags/" + id, { method: "DELETE" }); loadTagMgmt(); } catch (e) { toast(e.message); }
  }
  async function loadFieldMgmt() {
    const box = el("fieldMgmt"); if (!box) return;
    try {
      let h = '<div style="display:flex;gap:0.4rem;margin-bottom:0.4rem;flex-wrap:wrap;align-items:flex-end;">' +
        '<div><label class="fld">Varlık</label><select class="ipt" id="mgFEnt" style="max-width:120px;"><option value="company">Firma</option><option value="contact">Kişi</option><option value="deal">Fırsat</option></select></div>' +
        '<div><label class="fld">Alan adı</label><input class="ipt" id="mgFLabel" style="max-width:180px;"></div>' +
        '<div><label class="fld">Tip</label><select class="ipt" id="mgFType" style="max-width:110px;"><option value="text">Metin</option><option value="number">Sayı</option><option value="date">Tarih</option><option value="select">Seçim</option></select></div>' +
        '<button class="btn-g btn-sm" onclick="CRM.addFieldMgmt()"><i class="bi bi-plus"></i> Ekle</button></div>' +
        '<input class="ipt mb" id="mgFOpts" placeholder="Seçim tipi için seçenekler: a, b, c">';
      const ents = [["company", "Firma"], ["contact", "Kişi"], ["deal", "Fırsat"]];
      for (const pair of ents) {
        const defs = await api("/fields?entity=" + pair[0]);
        h += `<div style="margin-top:0.7rem;"><b style="font-size:0.82rem;color:var(--heading);">${pair[1]}</b>`;
        h += defs.length ? defs.map((f) => `<div class="row-sep" style="display:flex;justify-content:space-between;align-items:center;padding:0.3rem 0;font-size:0.86rem;"><span>${esc(f.label)} <span class="muted">(${f.field_type})</span></span><button class="ico-btn del" onclick="CRM.delFieldMgmt(${f.id})"><i class="bi bi-trash"></i></button></div>`).join("") : '<div class="muted" style="font-size:0.8rem;">Alan yok.</div>';
        h += "</div>";
      }
      box.innerHTML = h;
    } catch (e) { box.innerHTML = '<div class="muted">' + esc(e.message) + "</div>"; }
  }
  async function addFieldMgmt() {
    const ent = el("mgFEnt").value, label = inputVal("mgFLabel"), type = el("mgFType").value;
    if (!label) { toast("Alan adı girin."); return; }
    const opts = type === "select" ? (inputVal("mgFOpts") || "").split(",").map((s) => s.trim()).filter(Boolean) : null;
    try { await api("/fields", jbody({ entity: ent, label: label, field_type: type, options: opts })); loadFieldMgmt(); }
    catch (e) { toast(e.message); }
  }
  async function delFieldMgmt(id) {
    if (!confirm("Alan silinsin mi? (Tüm değerleri silinir)")) return;
    try { await api("/fields/" + id, { method: "DELETE" }); loadFieldMgmt(); } catch (e) { toast(e.message); }
  }

  async function kommoRun(which, btn) {
    btn.disabled = true; const orig = btn.innerHTML; btn.innerHTML = "Çalışıyor…";
    try {
      const r = await api("/integrations/kommo/" + which, { method: "POST" });
      const c = r.counts || {};
      toast(`Tamam — firma ${c.companies || 0} · kişi ${c.contacts || 0} · fırsat ${c.leads || 0}`, "success");
      renderIntegrations();
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; btn.innerHTML = orig; }
  }

  // ── Raporlar (C2) ─────────────────────────────────────────────────────────
  function repBar(label, sub, value, max, color) {
    const pct = Math.round((value / (max || 1)) * 100);
    return `<div style="margin-bottom:0.65rem;">
      <div style="display:flex;justify-content:space-between;font-size:0.83rem;margin-bottom:0.2rem;gap:0.5rem;"><span>${label}</span><span>${sub}</span></div>
      <div class="rep-bar"><div class="rep-fill" style="width:${pct}%;background:${color};"></div></div></div>`;
  }
  async function renderReports() {
    const v = el("view-reports");
    v.innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const d = await api("/reports/summary");
      const t = d.totals;
      const sc = (val, lbl, warn) => `<div class="stat${warn ? " warn" : ""}"><div class="v">${val}</div><div class="l">${lbl}</div></div>`;
      let h = '<h2 class="page-title" style="margin-bottom:1rem;">Raporlar & Analitik</h2>';
      h += '<div class="stat-grid">' +
        sc(t.open_count, "Açık Fırsat") + sc(money(t.open_value), "Açık Değer") +
        sc(t.won_count, "Kazanılan") + sc(money(t.won_value), "Kazanılan Değer") +
        sc("%" + t.win_rate, "Kazanma Oranı") + sc(t.lost_count, "Kaybedilen", t.lost_count > 0) + "</div>";

      h += '<div class="card2"><div class="card2-head"><i class="bi bi-funnel"></i><h2>Satış Hunisi (açık)</h2></div><div class="card2-body">';
      if (!d.funnel.length) h += '<div class="muted">Aşama verisi yok.</div>';
      else { const mx = Math.max.apply(null, d.funnel.map((s) => s.value).concat([1]));
        d.funnel.forEach((s) => { h += repBar(`${esc(s.stage)} <span class="muted">(${s.count})</span>`, `<b>${money(s.value)}</b>`, s.value, mx, "var(--gold)"); }); }
      h += "</div></div>";

      h += '<div class="card2"><div class="card2-head"><i class="bi bi-bar-chart"></i><h2>Aylık Kazanılan / Kaybedilen</h2></div><div class="card2-body">';
      const mw = Math.max.apply(null, d.monthly.map((m) => m.won_value).concat([1]));
      d.monthly.forEach((m) => { h += repBar(m.month, `<b>${money(m.won_value)}</b> · ${m.won_count} kaz. · <span style="color:#c2410c;">${m.lost_count} kayıp</span>`, m.won_value, mw, "#16a34a"); });
      h += "</div></div>";

      h += '<div class="card2"><div class="card2-head"><i class="bi bi-graph-up"></i><h2>Tahmin (olasılık ağırlıklı)</h2></div><div class="card2-body">';
      if (!d.forecast.length) h += '<div class="muted">Beklenen kapanış tarihli açık fırsat yok.</div>';
      else { const mf = Math.max.apply(null, d.forecast.map((f) => f.weighted_value).concat([1]));
        d.forecast.forEach((f) => { h += repBar(esc(f.month), `<b>${money(f.weighted_value)}</b>`, f.weighted_value, mf, "var(--navy)"); }); }
      h += "</div></div>";

      h += '<div class="card2"><div class="card2-head"><i class="bi bi-trophy"></i><h2>Performans (kişi bazında)</h2></div><div class="card2-body" style="overflow-x:auto;">';
      if (!d.leaderboard.length) h += '<div class="muted">Veri yok.</div>';
      else h += '<table><thead><tr><th>Kişi</th><th>Kazanılan</th><th>Değer</th><th class="col-sec">Açık</th><th class="col-sec">Aktivite</th></tr></thead><tbody>' +
        d.leaderboard.map((u) => `<tr><td><b>${esc(u.name)}</b></td><td>${u.won}</td><td>${money(u.won_value)}</td><td class="col-sec">${u.open}</td><td class="col-sec">${u.activities}</td></tr>`).join("") + "</tbody></table>";
      h += "</div></div>";
      v.innerHTML = h;
    } catch (e) { v.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }

  // ── Global arama (C1b) ────────────────────────────────────────────────────
  function ownerSelect(id) {
    return `<select class="ipt" id="${id}" style="max-width:160px;" title="Sorumluya göre filtrele">
      <option value="">Tüm sorumlular</option>` +
      state.users.map((u) => `<option value="${u.id}">${esc(u.full_name)}</option>`).join("") + `</select>`;
  }
  function tagSelect(id) {
    if (!state.tags || !state.tags.length) return "";
    return `<select class="ipt" id="${id}" style="max-width:150px;" title="Etikete göre filtrele">
      <option value="">Tüm etiketler</option>` +
      state.tags.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("") + `</select>`;
  }
  // Kaydedilmiş görünümler
  function viewsSelect(entity) {
    return `<select class="ipt" id="${entity}_view" style="max-width:150px;" onchange="CRM.applyView('${entity}')" title="Kaydedilmiş görünüm"><option value="">Görünüm…</option></select>
      <button class="btn-g btn-o btn-sm" onclick="CRM.saveView('${entity}')" title="Bu filtreyi görünüm olarak kaydet"><i class="bi bi-bookmark-plus"></i></button>`;
  }
  async function loadViews(entity) {
    const sel = el(entity + "_view"); if (!sel) return;
    try {
      state._views = state._views || {};
      const vs = await api("/views?entity=" + entity);
      state._views[entity] = vs;
      sel.innerHTML = '<option value="">Görünüm…</option>' +
        vs.map((v) => `<option value="${v.id}">${esc(v.name)}</option>`).join("") +
        '<option value="__del">— Sil…</option>';
    } catch (e) { /* sessiz */ }
  }
  function _critIds(entity) { return entity === "companies" ? ["coSource", "coOwner", "coTag"] : ["ctSource", "ctOwner", "ctTag"]; }
  function applyView(entity) {
    const sel = el(entity + "_view"); const vid = sel.value;
    if (vid === "__del") {
      const vs = (state._views[entity] || []).filter((x) => x.id);
      if (!vs.length) { sel.value = ""; return; }
      const id = vs[0].id; // basitlik: ilkini sil seçeneği yerine prompt
      const name = prompt("Silinecek görünüm adı:", vs[0].name);
      const v = vs.find((x) => x.name === name);
      if (v) api("/views/" + v.id, { method: "DELETE" }).then(() => { toast("Silindi.", "success"); loadViews(entity); });
      sel.value = ""; return;
    }
    const v = (state._views[entity] || []).find((x) => String(x.id) === vid);
    if (!v) return;
    const c = v.criteria || {};
    const map = entity === "companies"
      ? { source: "coSource", owner: "coOwner", tag: "coTag", q: "coSearch" }
      : { source: "ctSource", owner: "ctOwner", tag: "ctTag", q: "ctSearch" };
    Object.keys(map).forEach((k) => { const e = el(map[k]); if (e) e.value = c[k] || ""; });
    entity === "companies" ? loadCompanies() : loadContacts();
  }
  async function saveView(entity) {
    const name = prompt("Görünüm adı:"); if (!name) return;
    const map = entity === "companies"
      ? { source: "coSource", owner: "coOwner", tag: "coTag", q: "coSearch" }
      : { source: "ctSource", owner: "ctOwner", tag: "ctTag", q: "ctSearch" };
    const criteria = {};
    Object.keys(map).forEach((k) => { const val = inputVal(map[k]) || srcParam(map[k]); if (val) criteria[k] = val; });
    try { await api("/views", jbody({ entity: entity, name: name, criteria: criteria })); toast("Görünüm kaydedildi.", "success"); loadViews(entity); }
    catch (e) { toast(e.message); }
  }

  // ── Toplu işlemler (C4) ────────────────────────────────────────────────────
  const bulkSel = { companies: new Set(), contacts: new Set() };
  function bulkToggle(entity, id, on) {
    if (on) bulkSel[entity].add(id); else bulkSel[entity].delete(id);
    updateBulkBar(entity);
  }
  function bulkAll(entity, on) {
    document.querySelectorAll("." + entity + "_chk").forEach((c) => { c.checked = on; bulkToggle(entity, parseInt(c.value, 10), on); });
  }
  function updateBulkBar(entity) {
    const bar = el(entity + "_bulkbar"); if (!bar) return;
    const n = bulkSel[entity].size;
    bar.style.display = n ? "flex" : "none";
    const cnt = el(entity + "_bulkcount"); if (cnt) cnt.textContent = n;
  }
  async function bulkRun(entity, action) {
    const ids = Array.from(bulkSel[entity]); if (!ids.length) return;
    let value = null;
    if (action === "assign") {
      const opts = state.users.map((u, i) => `${i + 1}) ${u.full_name}`).join("\n");
      const pick = prompt("Sorumlu seç (numara):\n" + opts); if (!pick) return;
      const u = state.users[parseInt(pick, 10) - 1]; if (!u) return; value = String(u.id);
    } else if (action === "source") {
      value = prompt("Kaynak (meta / kommo / manual):", "manual"); if (!value) return;
    } else if (action === "tag") {
      const opts = (state.tags || []).map((t, i) => `${i + 1}) ${t.name}`).join("\n");
      if (!opts) { toast("Önce etiket oluşturun (Entegrasyonlar)."); return; }
      const pick = prompt("Etiket seç (numara):\n" + opts); if (!pick) return;
      const t = state.tags[parseInt(pick, 10) - 1]; if (!t) return; value = String(t.id);
    } else if (action === "delete") {
      if (!confirm(ids.length + " kayıt arşivlensin mi?")) return;
    }
    try {
      const r = await api("/bulk", jbody({ entity: entity, ids: ids, action: action, value: value }));
      toast(r.message || "Tamam.", "success");
      bulkSel[entity].clear();
      if (entity === "companies") { state._companies = null; loadCompanies(); } else loadContacts();
    } catch (e) { toast(e.message); }
  }
  function bulkBar(entity) {
    if (!can("crm", "edit")) return "";
    return `<div id="${entity}_bulkbar" style="display:none;align-items:center;gap:0.5rem;flex-wrap:wrap;background:var(--surface-2);border-radius:8px;padding:0.5rem 0.8rem;margin-bottom:0.8rem;">
      <b><span id="${entity}_bulkcount">0</span> seçili</b>
      <button class="btn-g btn-sm btn-o" onclick="CRM.bulkRun('${entity}','assign')"><i class="bi bi-person-check"></i> Sorumlu</button>
      <button class="btn-g btn-sm btn-o" onclick="CRM.bulkRun('${entity}','tag')"><i class="bi bi-tag"></i> Etiketle</button>
      <button class="btn-g btn-sm btn-o" onclick="CRM.bulkRun('${entity}','source')"><i class="bi bi-funnel"></i> Kaynak</button>
      ${can("crm", "delete") ? `<button class="btn-g btn-sm btn-o" style="color:#ef4444;border-color:#fecaca;" onclick="CRM.bulkRun('${entity}','delete')"><i class="bi bi-trash"></i> Sil</button>` : ""}
    </div>`;
  }

  function searchOpen() {
    el("searchOv").classList.add("open");
    const i = el("searchInput"); i.value = ""; el("searchResults").innerHTML = "";
    setTimeout(() => i.focus(), 60);
  }
  function searchClose() { el("searchOv").classList.remove("open"); }
  let _searchTmr;
  async function searchRun() {
    const q = (el("searchInput").value || "").trim();
    const box = el("searchResults");
    if (q.length < 1) { box.innerHTML = ""; return; }
    try {
      const d = await api("/search?q=" + encodeURIComponent(q));
      const ic = { company: "building", contact: "person", deal: "briefcase" };
      const sec = (title, items, kind) => {
        if (!items.length) return "";
        return `<div class="sr-sec">${title}</div>` + items.map((it) =>
          `<div class="sr-item" onclick="CRM.searchGo('${kind}',${it.id})"><i class="bi bi-${ic[kind]}" style="color:var(--gold);"></i><div><div class="t">${esc(it.label)}</div>${it.sub ? '<div class="s">' + esc(it.sub) + "</div>" : ""}</div></div>`).join("");
      };
      const h = sec("Firmalar", d.companies, "company") + sec("Kişiler", d.contacts, "contact") + sec("Fırsatlar", d.deals, "deal");
      box.innerHTML = h || '<div class="empty">Sonuç yok.</div>';
    } catch (e) { box.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }
  function searchGo(kind, id) {
    searchClose();
    if (kind === "company") openCompany(id);
    else if (kind === "contact") openContact(id);
    else openDeal(id);
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

    const tabs = [["summary", "Özet"], ["notes", "Notlar"], ["tasks", "Görevler"], ["files", "Ekler"], ["history", "Geçmiş"]];
    el("dwTabs").innerHTML = tabs.map((t) =>
      `<button class="drawer-tab ${state.dwTab === t[0] ? "active" : ""}" onclick="CRM.dwSwitch('${t[0]}')">${t[1]}</button>`).join("");

    if (state.dwTab === "summary") renderDrawerSummary(kind, d);
    else if (state.dwTab === "notes") renderDrawerNotes(d);
    else if (state.dwTab === "tasks") renderDrawerTasks(d);
    else if (state.dwTab === "files") renderDrawerFiles();
    else renderDrawerHistory();
  }

  function dwSwitch(tab) { state.dwTab = tab; renderDrawer(); }

  function kv(k, v) { return v ? `<div class="kv"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>` : ""; }

  function renderDrawerSummary(kind, d) {
    const e = d.company || d.contact || d.deal;
    let html = "";
    // Üst aksiyonlar
    html += '<div style="display:flex;gap:0.5rem;margin-bottom:1rem;flex-wrap:wrap;">';
    if (e.wa_link) html += `<a class="btn-g btn-sm" style="background:#25d366;" href="${esc(e.wa_link)}" target="_blank"><i class="bi bi-whatsapp"></i> WhatsApp</a>`;
    if (e.kommo_url) html += `<a class="btn-g btn-sm btn-o" href="${esc(e.kommo_url)}" target="_blank" title="Sohbeti/kaydı Kommo'da aç — orada görüp cevaplayabilirsin"><i class="bi bi-chat-dots"></i> Kommo'da Aç</a>`;
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
        kv("Kaynak", esc(e.source_label)) +
        kv("Sorumlu", esc(e.owner_name)) + kv("Not", esc(e.notes));
      // Kişiler
      html += sectionList("Kişiler", d.contacts, (c) => `<div class="kv"><div class="v"><a href="#" onclick="CRM.openContact(${c.id});return false;"><b>${esc(c.full_name)}</b></a> <span class="muted">${esc(c.title)}</span></div></div>`, can("crm", "create") ? `<button class="btn-g btn-sm" onclick="CRM.contactModal(null,${e.id})"><i class="bi bi-plus"></i> Kişi</button>` : "");
      // Fırsatlar
      html += sectionList("Fırsatlar", d.deals, (dl) => `<div class="kv"><div class="v"><a href="#" onclick="CRM.openDeal(${dl.id});return false;">${esc(dl.title)}</a> <span class="muted">${esc(dl.stage_name)} · ${money(dl.value, dl.currency)}</span></div></div>`, can("crm", "create") ? `<button class="btn-g btn-sm" onclick="CRM.dealModal(null,${e.id})"><i class="bi bi-plus"></i> Fırsat</button>` : "");
    } else if (kind === "contact") {
      html += kv("Firma", e.company_id ? `<a href="#" onclick="CRM.openCompany(${e.company_id});return false;">${esc(e.company_name)}</a>` : "") +
        kv("Unvan", esc(e.title)) + kv("Telefon", esc(e.phone)) + kv("Mobil", esc(e.mobile)) +
        kv("WhatsApp", esc(e.whatsapp_number)) + kv("E-posta", esc(e.email)) +
        kv("Kaynak", esc(e.source_label)) + kv("Sorumlu", esc(e.owner_name)) + kv("Not", esc(e.notes));
    } else {
      const dl = d.deal;
      html += kv("Firma", dl.company_id ? `<a href="#" onclick="CRM.openCompany(${dl.company_id});return false;">${esc(dl.company_name)}</a>` : "") +
        kv("Kişi", dl.contact_id ? `<a href="#" onclick="CRM.openContact(${dl.contact_id});return false;">${esc(dl.contact_name)}</a>` : "") +
        kv("Aşama", esc(dl.stage_name)) + kv("Değer", money(dl.value, dl.currency)) +
        kv("Olasılık", dl.probability ? "%" + dl.probability : "") +
        kv("Beklenen Kapanış", esc(dl.expected_close_label)) +
        kv("Durum", '<span class="pill ' + (dl.status === "won" ? "pill-g" : dl.status === "lost" ? "pill-o" : "pill-b") + '">' + (dl.status === "won" ? "Kazanıldı" : dl.status === "lost" ? "Kaybedildi" : "Açık") + "</span>") +
        kv("Kaynak", esc(dl.source_label)) +
        kv("Sorumlu", esc(dl.owner_name)) + (dl.lost_reason ? kv("Kayıp Nedeni", esc(dl.lost_reason)) : "");
    }
    html += '<div id="dwTags" style="margin-top:1.1rem;"></div><div id="dwFields"></div>';
    el("dwBody").innerHTML = html;
    loadDrawerTags(); loadDrawerFields();
  }

  function sectionList(title, rows, rowFn, addBtn) {
    let h = `<div style="margin-top:1.1rem;display:flex;align-items:center;justify-content:space-between;"><b style="color:var(--heading);">${esc(title)}</b>${addBtn || ""}</div>`;
    if (!rows || !rows.length) h += '<div class="muted" style="font-size:0.82rem;padding:0.4rem 0;">Kayıt yok.</div>';
    else h += rows.map(rowFn).join("");
    return h;
  }

  function renderDrawerNotes(d) {
    const dr = state.drawer;
    const acts = d.activities || [];
    const ent = d.deal || d.contact || d.company || {};
    let html = "";
    if (d.wa && d.wa.count > 0) {
      html += `<div class="card2" style="margin-bottom:0.8rem;"><div class="card2-body" style="display:flex;align-items:center;gap:0.6rem;">
        <i class="bi bi-whatsapp" style="color:#25d366;font-size:1.4rem;"></i>
        <div style="flex:1;"><b>${d.wa.count} WhatsApp mesajı</b><div class="muted" style="font-size:0.78rem;">Son: ${esc(d.wa.last || "—")} · metni Kommo'da</div></div>
        ${ent.kommo_url ? `<a class="btn-g btn-sm btn-o" href="${esc(ent.kommo_url)}" target="_blank"><i class="bi bi-box-arrow-up-right"></i> Aç</a>` : ""}
      </div></div>`;
    }
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
      return `<div class="row-sep" style="display:flex;align-items:center;gap:0.6rem;padding:0.5rem 0;">
        <button class="ico-btn" onclick="CRM.toggleTask(${t.id},true)"><i class="bi bi-${done ? "check-circle-fill" : "circle"}" style="${done ? "color:#22c55e;" : ""}"></i></button>
        <div style="flex:1;${done ? "opacity:0.55;text-decoration:line-through;" : ""}"><div style="font-size:0.88rem;">${esc(t.title)}</div>
        <div class="muted" style="font-size:0.74rem;">${t.due_label ? esc(t.due_label) : "tarih yok"} · ${esc(t.assigned_to_name)}</div></div>
        ${t.overdue && !done ? '<span class="pill pill-o">Gecikti</span>' : ""}</div>`;
    }).join("");
    el("dwBody").innerHTML = html;
  }

  // Drawer: Etiketler (özet içinde)
  async function loadDrawerTags() {
    const dr = state.drawer; const box = el("dwTags"); if (!box || !dr) return;
    try {
      const tags = await api("/" + dr.kind + "/" + dr.id + "/tags");
      let h = '<div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.4rem;"><b style="color:var(--heading);font-size:0.84rem;">Etiketler</b>';
      if (can("crm", "edit")) h += `<button class="ico-btn" onclick="CRM.tagPicker()" title="Etiket düzenle"><i class="bi bi-pencil"></i></button>`;
      h += '</div><div style="display:flex;gap:0.35rem;flex-wrap:wrap;">';
      h += tags.length ? tags.map((t) => `<span class="pill" style="background:${esc(t.color)}22;color:${esc(t.color)};border:1px solid ${esc(t.color)}55;">${esc(t.name)}</span>`).join("") : '<span class="muted" style="font-size:0.82rem;">Etiket yok</span>';
      box.innerHTML = h + "</div>";
    } catch (e) { /* sessiz */ }
  }
  async function tagPicker() {
    const dr = state.drawer;
    try {
      const all = await api("/tags");
      const cur = await api("/" + dr.kind + "/" + dr.id + "/tags");
      const curIds = new Set(cur.map((t) => t.id));
      openModal("Etiketler", `
        <div id="tagList" style="display:flex;flex-direction:column;gap:0.35rem;max-height:40vh;overflow-y:auto;">
          ${all.map((t) => `<label style="display:flex;align-items:center;gap:0.5rem;"><input type="checkbox" class="tagck" value="${t.id}" ${curIds.has(t.id) ? "checked" : ""}> <span class="pill" style="background:${esc(t.color)}22;color:${esc(t.color)};">${esc(t.name)}</span></label>`).join("") || '<span class="muted">Henüz etiket yok.</span>'}
        </div>
        <div style="margin-top:0.8rem;display:flex;gap:0.4rem;"><input class="ipt" id="newTag" placeholder="Yeni etiket"><button class="btn-g btn-sm" onclick="CRM.addTag()">Ekle</button></div>`,
        async function () {
          const ids = Array.from(document.querySelectorAll(".tagck:checked")).map((c) => parseInt(c.value, 10));
          try { await api("/" + dr.kind + "/" + dr.id + "/tags", jbody({ tag_ids: ids }, "PUT")); closeModal(); toast("Etiketler güncellendi.", "success"); loadDrawerTags(); }
          catch (e) { toast(e.message); }
        });
    } catch (e) { toast(e.message); }
  }
  async function addTag() {
    const nm = inputVal("newTag"); if (!nm) return;
    try {
      const t = await api("/tags", jbody({ name: nm }));
      const lbl = document.createElement("label");
      lbl.style.cssText = "display:flex;align-items:center;gap:0.5rem;";
      lbl.innerHTML = `<input type="checkbox" class="tagck" value="${t.id}" checked> <span class="pill" style="background:${esc(t.color)}22;color:${esc(t.color)};">${esc(t.name)}</span>`;
      el("tagList").appendChild(lbl); el("newTag").value = "";
    } catch (e) { toast(e.message); }
  }

  // Drawer: Özel alanlar (özet içinde)
  async function loadDrawerFields() {
    const dr = state.drawer; const box = el("dwFields"); if (!box || !dr) return;
    try {
      const fields = await api("/" + dr.kind + "/" + dr.id + "/fields");
      if (!fields.length) { box.innerHTML = ""; return; }
      let h = '<div style="margin-top:1.1rem;margin-bottom:0.4rem;"><b style="color:var(--heading);font-size:0.84rem;">Özel Alanlar</b></div>';
      fields.forEach((f) => {
        const id = "cf_" + f.id; let inp;
        if (f.field_type === "select") inp = `<select class="ipt" id="${id}"><option value="">—</option>${f.options.map((o) => `<option ${f.value === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
        else inp = `<input class="ipt" id="${id}" type="${f.field_type === "number" ? "number" : f.field_type === "date" ? "date" : "text"}" value="${esc(f.value)}">`;
        h += `<div class="mb"><label class="fld">${esc(f.label)}</label>${inp}</div>`;
      });
      if (can("crm", "edit")) h += `<button class="btn-g btn-sm" onclick="CRM.saveFields()"><i class="bi bi-check2"></i> Alanları Kaydet</button>`;
      box.innerHTML = h; box.dataset.fieldIds = fields.map((f) => f.id).join(",");
    } catch (e) { /* sessiz */ }
  }
  async function saveFields() {
    const dr = state.drawer; const box = el("dwFields");
    const ids = (box.dataset.fieldIds || "").split(",").filter(Boolean);
    const values = {}; ids.forEach((fid) => { const e = el("cf_" + fid); if (e) values[fid] = e.value; });
    try { await api("/" + dr.kind + "/" + dr.id + "/fields", jbody({ values: values }, "PUT")); toast("Kaydedildi.", "success"); }
    catch (e) { toast(e.message); }
  }

  // Drawer: Ekler
  async function renderDrawerFiles() {
    const dr = state.drawer; if (!dr) return;
    el("dwBody").innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const rows = await api("/" + dr.kind + "/" + dr.id + "/attachments");
      let h = "";
      if (can("crm", "create")) h += `<div class="mb"><label class="btn-g btn-sm" style="cursor:pointer;"><i class="bi bi-paperclip"></i> Dosya Ekle<input type="file" id="attFile" style="display:none;" onchange="CRM.uploadAttachment()"></label></div>`;
      if (!rows.length) h += '<div class="muted">Henüz ek yok.</div>';
      else h += rows.map((a) => `<div class="row-sep" style="display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0;">
        <i class="bi bi-file-earmark" style="color:var(--gold);"></i>
        <div style="flex:1;min-width:0;"><a href="/api/crm/attachments/${a.id}/download"><b>${esc(a.name)}</b></a><div class="muted" style="font-size:0.74rem;">${esc(a.size_human)} · ${esc(a.uploaded_by)} · ${esc(a.created_at)}</div></div>
        ${can("crm", "delete") ? `<button class="ico-btn del" onclick="CRM.delAttachment(${a.id})"><i class="bi bi-trash"></i></button>` : ""}</div>`).join("");
      el("dwBody").innerHTML = h;
    } catch (e) { el("dwBody").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
  }
  async function uploadAttachment() {
    const dr = state.drawer; const f = el("attFile").files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await fetch("/api/crm/" + dr.kind + "/" + dr.id + "/attachments", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { toast(d.detail || "Yüklenemedi."); return; }
      toast("Eklendi.", "success"); renderDrawerFiles();
    } catch (e) { toast("Yükleme hatası."); }
  }
  async function delAttachment(id) {
    if (!confirm("Dosya silinsin mi?")) return;
    try { await api("/attachments/" + id, { method: "DELETE" }); renderDrawerFiles(); } catch (e) { toast(e.message); }
  }

  // Drawer: Geçmiş
  async function renderDrawerHistory() {
    const dr = state.drawer; if (!dr) return;
    el("dwBody").innerHTML = '<div class="empty">Yükleniyor…</div>';
    try {
      const rows = await api("/" + dr.kind + "/" + dr.id + "/history");
      if (!rows.length) { el("dwBody").innerHTML = '<div class="muted">Kayıt geçmişi yok.</div>'; return; }
      el("dwBody").innerHTML = '<ul class="tl">' + rows.map((r) =>
        `<li class="tl-item"><span class="tl-dot"><i class="bi bi-clock-history"></i></span><div class="tl-meta">${esc(r.action)} · ${esc(r.actor)} · ${esc(r.at)}</div></li>`).join("") + "</ul>";
    } catch (e) { el("dwBody").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
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

  // ── PWA "Ana ekrana ekle" ───────────────────────────────────────────────
  let _deferredPrompt = null;
  function setupInstall() {
    const btn = el("installBtn");
    if (!btn) return;
    const standalone = window.navigator.standalone === true ||
      (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
    if (standalone) return;  // zaten yüklü
    const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);

    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault(); _deferredPrompt = e; btn.style.display = "";
    });
    window.addEventListener("appinstalled", () => { btn.style.display = "none"; _deferredPrompt = null; });

    btn.addEventListener("click", async () => {
      if (_deferredPrompt) {
        _deferredPrompt.prompt();
        try { await _deferredPrompt.userChoice; } catch (e) { /* yoksay */ }
        _deferredPrompt = null; btn.style.display = "none";
      } else if (isIOS) {
        toast("iPhone/iPad: Safari'de alttaki Paylaş düğmesine dokunup 'Ana Ekrana Ekle'yi seçin.", "success");
      } else {
        toast("Tarayıcı menüsünden 'Uygulamayı yükle / Ana ekrana ekle' seçeneğini kullanın.", "success");
      }
    });

    // iOS'ta beforeinstallprompt yok — düğmeyi göster ki talimat verebilelim
    if (isIOS) btn.style.display = "";
  }

  // ── Açılış ────────────────────────────────────────────────────────────────
  async function boot() {
    document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
    el("logoutBtn").addEventListener("click", async function () {
      this.disabled = true;
      try { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; }
      catch (e) { toast("Çıkış hatası."); this.disabled = false; }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); searchClose(); } });
    el("mSave").addEventListener("click", function () { if (_modalSave) _modalSave(); });
    const si = el("searchInput");
    if (si) si.addEventListener("input", function () { clearTimeout(_searchTmr); _searchTmr = setTimeout(searchRun, 250); });
    const sov = el("searchOv");
    if (sov) sov.addEventListener("click", function (e) { if (e.target === sov) searchClose(); });
    setupInstall();

    // Referans verileri yükle (aşamalar + kullanıcılar)
    try { state.stages = await api("/stages"); } catch (e) { state.stages = []; }
    try { state.users = await api("/users"); } catch (e) { state.users = []; }
    try { state.tags = await api("/tags"); } catch (e) { state.tags = []; }

    switchTab("dashboard");
    refreshBadge();
    setInterval(refreshBadge, 120000);
  }

  // Onclick köprüsü — render edilen HTML global CRM.* çağırır
  window.CRM = {
    go: switchTab,
    openCompany, openContact, openDeal, dwSwitch,
    companyModal, contactModal, dealModal, taskModal,
    delCompany, delContact, delDeal, delTask,
    toggleTask, closeDeal, addActivity, togglePin, delActivity,
    dragDeal, allowDrop, dropDeal,
    exportXlsx, importModal,
    searchOpen, searchClose, searchGo,
    tagPicker, addTag, saveFields, uploadAttachment, delAttachment,
    addTagMgmt, delTagMgmt, addFieldMgmt, delFieldMgmt,
    applyView, saveView, bulkToggle, bulkAll, bulkRun,
  };
  window.closeDrawer = closeDrawer;
  window.closeModal = closeModal;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
