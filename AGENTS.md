# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

Minerva 108 ERP/IMS — internal inventory + production system for a vegan cosmetics
company. It runs two isolated product lines as in-app "domains" — **Kozmetik**
(cosmetics) and **Food Supplement** — sharing one login (see the Domain section).
FastAPI backend, server-rendered Jinja2 templates, PostgreSQL. Used live by a lab
team, so changes deploy as small bundles with a ~3-4 second restart.

## Commands

```bash
make install      # pip install -r requirements.txt into .venv
make dev          # uvicorn --reload on :8000, loads .env
make test         # full pytest suite (auth + RBAC + backup + pages)
make test-fast    # skips test_backup.py (no pg_dump subprocess)
make migrate      # alembic upgrade head (sources .env)
make stamp        # alembic stamp head — first-time setup on an existing DB
make shell        # Python REPL with a live DB session (Item/User imported)

# Single test
.venv/bin/pytest tests/test_rbac.py -v
.venv/bin/pytest tests/test_pages.py::test_recipes_page_renders -v
```

Tests require a **separate PostgreSQL database `minerva_test`** — `tests/conftest.py`
hardcodes `postgresql://minerva_user:devpass123@localhost:5432/minerva_test` and
drop/creates all tables per test function. It never touches dev/prod DBs.

## Environment

`.env` is git-ignored and required for dev. Critical vars, all read at **import time**
(missing/short ones crash the process before serving):
- `DATABASE_URL` — `postgresql://...` (dev: `minerva_dev`, prod: `minerva_db`)
- `SECRET_KEY` — 16+ chars, JWT signing
- `COOKIE_SECURE` — `false` for local http, default `true` (prod)
- `EXPOSE_API_DOCS` — `/api/docs` swagger, dev only
- `SEED_DEFAULT_USERS` — seeds 4 users (dogukan/isik/songul/meltem); `false` in prod
- `DISABLE_SCHEDULER` — APScheduler off (set in tests)

## Architecture

**`api_main.py`** is a deliberately lean hub: middleware wiring, the server-rendered
Jinja **page routes** (`/`, `/items`, `/recipes`, `/production`, `/stocks`, …), and
`include_router(...)` calls. All **API endpoints** live in `routers/` by domain
(`inventory`, `recipes`, `production`, `b2b`, `reports`, `notifications`, `backup`,
`undo`, `debug`, `system`, `domain`, `drive`, `auth`, `users`). Page routes resolve the
user, check permission via `_user_can(...)`, and render templates; the template's JS then
calls `/api/*`.

**System health** — the `system` router exposes `GET /health` (unauthenticated,
lightweight DB-ping liveness probe → 200/503, for external uptime monitors) and
`GET /api/system/health` (SuperAdmin — rich report: app uptime, DB latency/size,
disk, scheduler jobs, last backup, last snapshot, system clock). The SuperAdmin-only
`/system` page renders this with colored status cards and auto-refreshes every 30 s.

**Minerva Drive** (`routers/drive.py`, `core/drive.py`) — a self-hosted file-share
("mini Drive"). Login-gated management at `/drive`: upload individual files **or a
whole folder** (the "Klasör Yükle" button uses `<input webkitdirectory>`; the browser's
per-file `webkitRelativePath` is POSTed as `rel_path` and stored in `original_name` —
no schema change — so the subfolder path shows in listings while downloads use only the
basename via `safe_download_name`; `clean_rel_path` strips `..`/root escapes), group them
into named "collections" (links), each with an unguessable `share_token` + optional bcrypt
**password** and **expiry**. Public, **unauthenticated** share page `GET /s/{token}`
(+ `/s/{token}/unlock` password POST → HMAC unlock cookie, `/s/{token}/f/{id}`
download). Files live on disk under `DRIVE_DIR` (`drive_files/`, random names,
**not** in the DB or `pg_dump`) — the hourly snapshot script tars them daily.
Downloads are forced `attachment` + `octet-stream` (no inline render → XSS-safe).
Tables: `drive_file`, `drive_collection`, `drive_collection_file` (created by
`init_db` create_all). Not domain-scoped — it's a cross-cutting utility.
The same router also produces the **monthly detailed system report** (SuperAdmin):
`GET /api/system/report?year=&month=&format=pdf|excel` builds a top-to-bottom report
on demand (production line, materials consumed, per-user activity, audit events,
restarts/estimated downtime, technical status); the scheduler auto-saves both formats
to `system_reports/` on the 1st of each month. PDF rendering uses `reportlab`
(`core/monthly_report.py`); app-restart tracking uses the `system_event` table, which
`init_db()` startup writes an `app_start` row to on every boot.

**CRM** (`routers/crm.py`, `core/crm.py`, `templates/crm.html` + `static/crm.js`) —
Müşteri İlişkileri Yönetimi, served at its own subdomain **`crm.minerva108.com`**
from the *same app/process* (one deploy, one DB). Login-gated SPA at `/crm`
(tabs: Pano · Firmalar · Kişiler · Pipeline-Kanban · Görevler) with a slide-over
detail drawer + shared activity timeline. **Cross-cutting — NOT domain-scoped**
(no `domain` column; one unified CRM across Kozmetik/Supplement, like Drive).
Access is *shared*: every CRM user sees all records/activities; `owner_*`/
`assigned_to_*` are responsibility, not access control. Tables: `crm_company`,
`crm_contact`, `crm_stage` (seeded with default pipeline in `init_db`), `crm_deal`
(optional `quotation_id` bridge to B2B), `crm_activity` (note/call/meeting/email/
whatsapp timeline), `crm_task` (reminders). RBAC via the new **`crm`** category
(`view`/`create`/`edit`/`delete`) in `core/permissions.py`. Mutations write
`log_admin_event` audit rows; soft-delete (`is_active`) on companies/contacts.
A daily 08:00 scheduler job (`daily_crm_followup_scan`) web-pushes due/overdue
task reminders to assignees (`notify_crm_reminder`). **Subdomain serving** (nginx
server block + DNS A record + TLS) lives on the prod server outside `deploy.sh`; the app
itself is host-agnostic (`api_main._is_crm_host` redirects `crm.*` root → `/crm`).
**SSO**: set `COOKIE_DOMAIN=.minerva108.com` in prod `.env` so one login spans
`ims` + `crm` (handled in `core/auth.set_auth_cookie`; logout deletes with the
same domain). WhatsApp Cloud API + Meta (Lead Ads, Messenger/Instagram DM) are
planned later phases — they need Meta business setup + App Review and outbound
calls via `httpx` + signature-verified public webhooks.

**PDKS** (`routers/pdks.py`, `core/pdks.py`, `core/pdks_report.py`,
`templates/pdks.html`) — Personel Devam Takip Sistemi, `/pdks` sayfası.
Personel kendi hesabından "Giriş/Çıkış" basar (zaman damgası DAİMA sunucu
saati); kişi bazlı **versiyonlu haftalık program** (`pdks_schedules`,
`effective_from` — eski versiyon asla mutate edilmez, geçmiş puantaj sabit
kalır), izinler (`pdks_leaves`, aralık satırı) ve ortak resmi tatiller
(`pdks_holidays`) üzerinden aylık puantaj CANLI hesaplanır (saklanan agregat
yok). Hesap motoru `core/pdks.py` SAF fonksiyonlardır (DB'siz, unit-test
edilebilir): tek çift + brüt ≥6 sa → mola kesintisi, 5 dk geç/erken toleransı,
tatil/izin/hafta tatilinde çalışılan her dakika fazla mesai, açık çift ("çıkış
eksik") 0 sayılır ve yönetici düzeltene kadar toplam dışıdır. **work_date =
TR-yerel gün** — `'in'` kendi TR günü, `'out'` kapattığı açık `'in'` <16
saatlikse ONUN günü (gece yarısı kuralı; router `_work_date_for` + check
akışı aynı sabiti kullanır). **Cross-cutting — NOT domain-scoped** (CRM/Drive
gibi; domain kolonu/dependency yok). RBAC kategorisi **`pdks`**
(`check/view_own/view_all/manage/report`; Staff dahil herkes check+view_own,
Manager tümü). Manuel olay düzeltmeleri `correction_note` zorunlu +
`log_admin_event` audit'li; olay silme soft-delete. Excel puantaj:
`GET /api/pdks/report/excel` → `core/pdks_report.py` (Özet + personel başına
sayfa). Tablolar `pdks_*` — create_all ile gelir (migration
`f3a5c7e9b2d4`). Not: buradaki "izin" devamsızlık mazeretidir; RBAC
"permission" kavramıyla karıştırma.

**`core/`** — cross-cutting helpers: `auth.py` (JWT + `require_role`),
`permissions.py` (RBAC), `audit.py` (`admin_audit_log`), `notifications.py` (web push +
low-stock alerts + CRM task reminders), `scheduler.py` (APScheduler — daily 08:00 CRM
follow-up scan, daily 09:00 expiry scan, monthly stock snapshot on day 1 at 00:30,
monthly system report on day 1 at 01:00, plus a startup backfill), `crm.py` (CRM
serializers + `wa.me` helper), `snapshots.py` (month-end stock freeze / reconstruction),
`monthly_report.py` (aylık detaylı PDF/Excel sistem raporu üretici), `qc_report.py` +
`qc_questions.py` (QC form parse + PDF/Excel), `domain.py` (Kozmetik/Food Supplement
panel scoping — see Domain section), `undo.py` (undo log), `password_strength.py`,
`limiter.py` (SlowAPI).

**Middleware stack** (api_main.py): `SecurityHeadersMiddleware` (CSP + headers),
`CSRFMiddleware` (Origin/Referer check on mutating verbs; exempts `/api/login`,
`/api/logout`), `SlowAPIMiddleware` (rate limiting). Auth is JWT in an HttpOnly +
SameSite=Lax cookie.

### Database schema is managed in TWO places — keep them in sync

Every new column must be added to **both**:
1. An **Alembic migration** in `alembic/versions/` (run on prod via `alembic upgrade head`)
2. The `init_db()` function in `database.py` — its `alter_safe(...)` block runs
   idempotent `ALTER TABLE ... ADD COLUMN` statements on every startup

Alembic reads `DATABASE_URL` from env (`alembic.ini` has only a placeholder URL).
Models live in `database.py`; `init_db()` also seeds default users when enabled.

### RBAC

Roles: `SuperAdmin`, `Manager`, `LabLead`, `LabTech`, `Staff`. `_DEFAULT_PERMISSIONS`
in `core/permissions.py` maps role → `{category: {action: bool}}`. A user may carry a
per-user JSON **permission override** that fully replaces the role default
(`_resolve_permissions`). Gate API endpoints with `require_permission("category",
"action")`; gate Jinja blocks with `{% if can(...) %}`; gate client JS with
`window.can(category, action)` (server-injected `window.PERMS`). Finance data
(costs/margins/quotations) is additionally limited to `_FINANCE_ROLES`.

### Domain (Kozmetik / Food Supplement panels)

Two fully isolated product domains share one login. The active panel lives in a
non-secret `active_domain` cookie (`cosmetics` | `supplement`, default `cosmetics`);
the topnav switcher (`POST /api/domain/switch`) flips it and reloads. `core/domain.py`
is the single source: `active_domain` is a FastAPI dependency (`domain: str =
Depends(active_domain)`) and `_page_ctx` injects `window.DOMAIN` + `domain_label` into
every page. **Every list endpoint filters by the active domain and every create stamps
it** — `domain` columns exist on `Item`, `Supplier`, `Recipe`, `Inventory`,
`ProductionHistory`, `Quotation`, `StockSnapshot` (all default `'cosmetics'`, so every
pre-existing row + the cosmetics experience is unchanged). `Transaction` has no domain
column — scope it by joining `Item`. Inventory/production/recipe rows inherit their
parent's domain (lot ← item, output ← recipe). `snapshot_exists(…, domain)` is
domain-aware so one panel's monthly snapshot doesn't hide the other's live data. When
adding an endpoint that lists or creates domain-scoped data, **you must** add the
`active_domain` dependency + filter/stamp, or data leaks across panels.

### Production / recipe domain logic

- A recipe has ingredients (`recipe_ingredients`), a `waste_percentage` (fire), an
  `output_quantity`, an optional `phase` per ingredient and `production_notes`.
- Production consumes ingredients at **gross** quantity:
  `gross = net × multiplier × (1 + waste%/100)`. **Ambalaj (packaging) is exempt from
  fire** — factor stays 1.0.
- Labels (`category='Ambalaj'`, `pkg_type='etiket'`) carry `language` (`TR`/`EN`/NULL)
  and a shared `label_group`; production resolves the right-language sibling.
- `Item.current_stock` is the source of truth for stock; production/receiving/QC all
  update it directly and append immutable `Transaction` rows.
- **Samples (numune)**: `Inventory.is_sample=True` marks a lot received from an
  *alternate* supplier for an existing raw material (entered from the Items page
  "Numune" tab → `/api/inventory/receive` with `is_sample`). Sample lots never merge
  with normal lots (upsert key includes `is_sample`) and enter usable stock.
- **Lot/supplier-aware consumption**: production decrements `Item.current_stock`
  (authoritative gate) **and** specific Inventory lots. Per raw-material ingredient the
  user may pick a lot via `ingredient_lot_choices {item_id: inventory_id}` (production
  preview shows a picker when ≥2 lots exist); unpicked → FIFO (oldest APPROVED lot).
  A **picked lot that is short → hard 400 error** (never silently spills). Each
  consumed lot writes an `Output` `Transaction` whose `lot_number` is the *source* lot
  (+ supplier in notes), so `trace_lot` shows exact provenance. Ambalaj/etiket stay
  aggregate (no lot picker). Items with no lots fall back to aggregate-only
  decrement (`core/production_plan.py`). Available lots:
  `POST /api/inventory/available-lots`.
- **Production source choices**: preview (`POST /api/production/preview`) and
  start share `core/production_plan.plan`. If another compatible active card in
  the same material group has stock, the user must explicitly select sources
  via `ingredient_sources` (recipe item ID -> card/quantity/optional lot list).
  Splits must total the gross requirement. Stock is gated per actual card and
  lot allocations are reserved across all lines. Snapshots retain both recipe
  and actual source cards; exports and cancellation use those snapshots.
  Kill switch: AppSetting `production.source_choice.enabled=0` restores the
  recipe-card-only choices. Preview is read-only; production writes are audited.
  Live rollout keeps this setting at `0` until laboratory equivalence review.
  With the switch disabled, the purchase-plan API cannot deduct phase-out
  stock from other cards; it returns `source_choice_disabled` and preserves
  saved scenario options. Packaging and labels are separate consumption kinds.
- Item delete is **soft** (`is_active=False`) when audit/transaction rows exist;
  hard delete only when there are no references.
- **QC forms** are stored as JSON on `Inventory.qc_form_data` (written by
  `/api/inventory/{id}/qc-approve`). The checklist questions live once in
  `core/qc_questions.py` (injected into `qc.html` as `window.QC_QUESTIONS`). A lot's
  QC form is surfaced on the traceability page (`trace_lot` returns a labeled
  `qc_form` block) and exportable as PDF/Excel via
  `GET /api/qc/{inventory_id}/form/export?format=pdf|excel` — `core/qc_report.py`
  (`parse_qc_form` + reportlab/openpyxl, reusing `monthly_report._register_fonts`).
- **Monthly stock report** (`/api/reports/monthly-stock`) reads a frozen
  `StockSnapshot` row when one exists for that month (badge: *DONDURULMUŞ KAYIT*),
  otherwise reconstructs live (badge: *CANLI HESAP*). Reconstruction =
  `current_stock − (transactions after month-end)`, never a sum-from-zero.
- **Production stock simulation** (`core/production_sim.py`; `/api/reports/production-plan`,
  `/products`, `/export`; a panel on the Reports page) — "produce N of each selected
  product → how much material is consumed, what's left, what's short, per-product
  producible-in-isolation, and a purchase list" + 4-sheet Excel. Consumption rules are
  **identical to `start_production`** (fire on raw materials, ambalaj/etiket exempt,
  label-language resolution). Domain-scoped; `simulate()` is the reusable engine.
- **Supplier prices → enriched purchase list** (`core/supplier_prices.py`, table
  `supplier_prices`: per material × supplier → `package_size` + `unit_price`, domain-scoped).
  Maintained by **importing the lab's "Stok Son Durum" Excel** (`POST
  /api/supplier-prices/import`, finance-only; `parse_stok_son_durum` reads Işık Hanım's
  fixed column layout — supplier-1's package sits *before* its name; materials matched to
  `Item.name`, suppliers to `Supplier.name` via Turkish-folded `normalize`, unmatched
  suppliers kept as free text). The production-plan `/export` then auto-fills the **Satın
  Alma Listesi** sheet with up to 3 suppliers (cheapest-first) — `build_workbook(...,
  prices=…)`; empty → the old plain 6-column sheet. A "Tedarikçi Fiyatları" panel on the
  Reports page lists/imports/deletes (import+delete gated to `SuperAdmin`/`Manager`).

### Timezone

The DB stores naive UTC (`datetime.utcnow()`); the server runs UTC. Turkey is a fixed
UTC+3 (no DST since 2016). `database.py` exports `to_tr(dt)` and `tr_now()` — wrap any
datetime in **user-facing** output (`strftime` for display) with `to_tr()`. Leave
internal values (lot codes, backup filenames, DB comparisons) in UTC.

### Frontend conventions

Templates are standalone HTML with inline `<script>` + a shared set of `static/*.js`
helpers loaded with `defer` (`toast.js`, `tr-sort.js`, `items-cache.js`,
`fetch-guard.js`, `idle-watch.js`, `undo-watch.js`). New helpers are injected into all
templates with a one-off Python script.

**Script-ordering gotcha:** `defer` external scripts execute *after* a bottom-of-body
inline `<script>` runs. Code in an inline script that calls a `defer`-loaded global
(e.g. `window.fetchItems`) at parse time will hit `undefined`. Guard with a fallback:
`window.fetchItems ? window.fetchItems() : fetch('/api/items')...`.

Turkish alphabetical sorting uses `window.trSort` / `trSortBy` (Intl.Collator 'tr').

## Verifying changes

- Inline-JS edits: extract `<script>` blocks, strip `{{ }}`/`{% %}`, run
  `node --check`. A syntax error in one template's JS breaks the whole page.
- Run `make test` (full suite) before deploying — `./deploy.sh` gates on it anyway.

## Deploy

Prod is **Hetzner Cloud `minerva-ims`** (CPX22, Falkenstein; SSH config alias
`hetzner` → `minerva@2.28.131.158:22`, root login key-only; `/var/www/minerva`,
systemd unit `minerva` running as the unprivileged `minerva` user inside a systemd
sandbox, PostgreSQL 17 `minerva_db`, **Python 3.12** (Ubuntu's `/usr/bin/python3.12`,
security updates via apt; `venv` is a symlink to `venv312`, and `venv310` is kept as a
rollback target — switch the symlink and restart), public URL `https://ims.minerva108.com`). Migrated from Turhost
on **2026-09-24** — full runbook and lessons in `ops/hetzner/RUNBOOK.md`.
`deploy.sh` and the ad-hoc-deploy hook read the live server's IP/port/user/key from
`ops/hetzner/active-server.env`; a future move only changes that file. The old
Turhost VPS (`turhost`, `136.144.251.26`) is only an nginx reverse-proxy bridge until
Turhost shuts it down; its app unit is parked, so **never deploy or restart there**.
The `minerva` user may run exactly `sudo systemctl restart|status minerva` and
`sudo journalctl -u minerva -n 25 --no-pager` (see `/etc/sudoers.d/minerva-deploy`).

**Always deploy with `./deploy.sh` (or `make deploy`) — it is the only sanctioned
path, and it makes test + verification mandatory.** It is a 5-step fail-fast
pipeline:

1. **Test gate** — runs the full pytest suite; aborts *before* pushing if any test fails.
2. Local commit & push to `main`.
3. SSH connectivity pre-flight.
4. Remote force-sync to `origin/main` + dependency sync + `systemctl restart`.
5. **Verification** — re-checks `systemctl is-active` *and* curls
   `https://ims.minerva108.com/login`, retrying until HTTP 200. The deploy is only
   reported successful if prod actually serves traffic.

Never deploy ad-hoc (raw `git push` + `ssh`) — that skips the test gate and the HTTP
verification. This is enforced mechanically: a project PreToolUse hook
(`.Codex/settings.json` → `.Codex/hooks/block-adhoc-deploy.sh`) **blocks** any Bash
command that ssh's to prod (the server in `ops/hetzner/active-server.env`, plus the
old `turhost` / `136.144.251.26`) and runs `systemctl
restart`, `git reset --hard`, or `git pull`. Read-only ssh (`journalctl`, `systemctl
status`) is allowed; `./deploy.sh`'s own internal ssh is not affected (it runs as a
script subprocess the hook never sees). Emergency hotfix only: `./deploy.sh
--skip-tests` bypasses the test gate with a loud warning; still verify prod manually.

The lab works live — bundle related changes into one restart, and take a `pg_dump`
backup before any destructive DB operation. An hourly auto-snapshot timer
(`ops/snapshot/`) runs 07:00–19:00.
