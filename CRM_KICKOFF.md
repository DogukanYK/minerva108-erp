# CRM_KICKOFF.md — Yeni CRM modülü için başlangıç brifingi

> **Amaç (TR):** Bu dosya, Minerva 108 için yeni bir **CRM platformu** kurmaya
> başlayacak YENİ bir Claude Code oturumu içindir. Mevcut sistemin tüm teknik
> ayrıntısı `CLAUDE.md`'de (bu oturumda otomatik yüklenir). Burası ise CRM'e
> özel: neyi yeniden kullan, neye dikkat et, hangi kararları plan modunda ver.
>
> **İlk iş:** `CLAUDE.md`'yi oku (zorunlu), sonra bu dosyayı oku, sonra
> **plan moduna geç** ve aşağıdaki "Open decisions"ları kullanıcıyla netleştir.

---

## 1. What this repo is (one paragraph)

Minerva 108 ERP/IMS — FastAPI + server-rendered Jinja2 + PostgreSQL, one box
(`turhost`), used **live** by a lab/office team. Lean hub `api_main.py` (middleware
+ Jinja page routes + `include_router`), all API endpoints in `routers/` by domain,
cross-cutting helpers in `core/`, models + `init_db()` in `database.py`. Full
architecture, commands, RBAC, deploy details: **read `CLAUDE.md` first** — do not
duplicate-learn it here.

## 2. Non-negotiables (read before writing any code)

- **Deploy only via `./deploy.sh`** (test gate → push → remote sync → restart → HTTP
  verify). A PreToolUse hook **blocks** ad-hoc `ssh … systemctl restart / git reset
  --hard / git pull`. Never bypass.
- **Live system.** Bundle related changes into ONE restart (~3-4 s). Take a `pg_dump`
  backup before any schema change (the established pattern; see prior `minerva_pre-*`
  dumps).
- **Dual schema rule.** Every new column → BOTH an Alembic migration AND
  `init_db()`'s `alter_safe(...)` block. New *tables* are auto-created by
  `init_db()`'s `Base.metadata.create_all` (still add a migration for record).
- **Confirm before hard-to-reverse prod writes.** Creating prod business data
  directly via ssh/DB is gated by the auto-mode classifier and needs explicit user
  go-ahead. Prefer the app's own endpoints/UI where possible.
- **Tests + JS check before deploy.** `make test` (full suite) must be green;
  inline-template JS edits must pass `node --check` (extract `<script>`, strip
  `{{ }}`/`{% %}`). `./deploy.sh` enforces the test gate anyway.

## 3. How features are added here — follow this pattern (Drive is the freshest worked example)

A self-contained subsystem = **models + router + page(s) + template(s) + tests**. The
"Minerva Drive" feature (`routers/drive.py`, `core/drive.py`, `templates/drive.html`
+ `share.html`, `tests/test_drive.py`, tables `drive_*`) is a clean, recent template
to copy for the CRM:

1. **Models** in `database.py` (new tables) + Alembic migration in `alembic/versions/`
   (chain from current head — check `down_revision`).
2. **Router** `routers/crm.py` (`prefix="/api/crm"`), gate endpoints with
   `require_permission("crm", "view"|"create"|…)`.
3. **Page routes** in `api_main.py` (`/crm`, sub-pages) — resolve user with
   `_get_user_context` + `_resolve_active_user`, gate with `_user_can`, render via
   `_page_ctx(request, payload, user)`.
4. **Templates** are standalone full HTML (NO Jinja inheritance). Copy an existing
   page's `<head>` + sidebar + topnav shell (e.g. `templates/system.html` or
   `drive.html`). Shared nav/JS is added across all pages with a **one-off Python
   injection script** (see how the Drive nav link / `window.DOMAIN` were injected).
5. **RBAC**: add a `"crm"` category to `PERMISSION_CATEGORIES` in `core/permissions.py`
   and per-role defaults in `_DEFAULT_PERMISSIONS`. Frontend gating: `{% if can(...) %}`
   / `window.can('crm','create')`; the admin permission-matrix test will check it.
6. **Tests** in `tests/test_*.py` (Postgres `minerva_test`, recreated per test).

## 4. The multi-panel "domain" pattern — and the CRM decision

The app already runs **two isolated product domains** (`cosmetics` / `supplement`) via
an `active_domain` cookie + a topnav switcher; every list endpoint filters by it and
every create stamps it (`core/domain.py`, `domain: str = Depends(active_domain)`). See
the **Domain section in `CLAUDE.md`**.

**Key CRM decision (resolve in plan mode):** Is the CRM…
- **(a) a cross-cutting module** (one CRM for the whole company, not split by
  cosmetics/supplement) — simplest; do NOT domain-scope it (like Drive, which is
  global). **Likely the right default.**
- **(b) domain-scoped** (separate pipelines per product line) — then add a `domain`
  column + `active_domain` dependency to CRM tables too.

## 5. Reusable building blocks (don't reinvent)

- **Auth/JWT** (`core/auth.py`), **RBAC** (`core/permissions.py`).
- **PDF/Excel** generation patterns: `core/monthly_report.py` (`_register_fonts` for
  Turkish), `core/qc_report.py` — reuse for CRM exports/quotes.
- **Static JS helpers** loaded `defer` on every page: `toast.js` (`showToast`),
  `tr-sort.js` (`trSortBy`, Turkish collation), `items-cache.js`, `fetch-guard.js`,
  `idle-watch.js`. Mind the **defer-script ordering gotcha** (CLAUDE.md).
- **Timezone**: DB stores naive UTC; wrap user-facing datetimes with `to_tr()` /
  `tr_now()` from `database.py`.
- **Audit log** (`core/audit.py`, `AdminAuditLog`) — pattern for tracking who-did-what.
- **File handling** (`core/drive.py`) — if CRM needs attachments, reuse storage/token
  helpers.

## 6. CRM integration points ALREADY in the system

- **`Quotation` / `QuotationItem`** (`routers/b2b.py`) already hold **customer data**
  per quote: `customer_name/contact/email/phone/address/country/vat`, currency, totals,
  `status` (DRAFT/CONFIRMED), `created_by/confirmed_by`. A CRM's *companies/contacts*
  and *deals* should likely **relate to or absorb** this — decide whether quotations
  become a CRM "deal" artifact or stay linked.
- **`User`** — for record ownership / assignment ("sorumlu kişi") and `created_by`
  audit fields (the app stamps `full_name`/`username` strings, not just IDs).
- **Notifications** (`core/notifications.py`, web push) — reuse for CRM
  reminders/follow-up alerts.
- **Scheduler** (`core/scheduler.py`, APScheduler) — for due-task / follow-up cron.

## 7. Suggested CRM scope + OPEN DECISIONS for plan mode

Typical CRM building blocks (let the user pick the v1 slice — don't build all at once):
- **Companies / Contacts** (kişiler & firmalar; phone/email/address; tags).
- **Leads → Opportunities/Deals** with a **pipeline** (stages, value, probability,
  expected close, owner).
- **Activities**: notes, tasks (due dates), calls, meetings, emails-log.
- **Reminders / follow-ups** (+ notifications).
- **Dashboard**: pipeline value by stage, activities due, win/loss.

**Open decisions to settle with the user (in plan mode, via AskUserQuestion):**
1. **Scope of v1** — start with Contacts+Companies+Activities? Or pipeline/deals first?
2. **Quotation/B2B link** — do existing quotations feed CRM deals, or keep separate?
3. **Domain** — cross-cutting (recommended) or per product line? (see §4)
4. **Access/RBAC** — who sees/edits CRM? New `crm` permission category defaults.
5. **Where it lives** — section under the same app at `/crm` (recommended, reuses
   auth/deploy) vs a separate sub-app/URL.

## 8. How to drive Claude Code well for this build (advice for the user)

- **Start in plan mode** (the new chat: ask it to enter plan mode). Let it explore +
  ask the §7 questions, then approve a phased plan before any code.
- **Ship in small, tested bundles**, one feature per deploy — exactly how this ERP was
  built (QC → samples → domains → reports → Drive, each its own bundle).
- **One topic per chat.** This chat got very long across many features; a fresh chat
  per major area stays sharper. (CRM = its own chat ✅.)
- **Keep `CLAUDE.md` updated** as the CRM grows (add a CRM section like the others).
- **Trust the guardrails:** deploy gate, backups, confirm-before-prod-writes.

## 9. First message to paste into the new chat

> "Aynı repodayız (Minerva 108 ERP). `CLAUDE.md` ve `CRM_KICKOFF.md`'yi oku.
> Bir CRM modülü kurmak istiyorum. Plan moduna geç, CRM_KICKOFF.md'deki 'Open
> decisions'ları bana sor, sonra fazlı bir plan çıkar — kod yazmadan önce onaylatacağım."
