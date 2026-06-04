# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Minerva 108 ERP/IMS — internal inventory + production system for a vegan cosmetics
company. FastAPI backend, server-rendered Jinja2 templates, PostgreSQL. Used live by
a lab team, so changes deploy as small bundles with a ~3-4 second restart.

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
`undo`, `debug`, `system`, `auth`, `users`). Page routes resolve the user, check
permission via `_user_can(...)`, and render templates; the template's JS then calls
`/api/*`.

**System health** — the `system` router exposes `GET /health` (unauthenticated,
lightweight DB-ping liveness probe → 200/503, for external uptime monitors) and
`GET /api/system/health` (SuperAdmin — rich report: app uptime, DB latency/size,
disk, scheduler jobs, last backup, last snapshot, system clock). The SuperAdmin-only
`/system` page renders this with colored status cards and auto-refreshes every 30 s.
The same router also produces the **monthly detailed system report** (SuperAdmin):
`GET /api/system/report?year=&month=&format=pdf|excel` builds a top-to-bottom report
on demand (production line, materials consumed, per-user activity, audit events,
restarts/estimated downtime, technical status); the scheduler auto-saves both formats
to `system_reports/` on the 1st of each month. PDF rendering uses `reportlab`
(`core/monthly_report.py`); app-restart tracking uses the `system_event` table, which
`init_db()` startup writes an `app_start` row to on every boot.

**`core/`** — cross-cutting helpers: `auth.py` (JWT + `require_role`),
`permissions.py` (RBAC), `audit.py` (`admin_audit_log`), `notifications.py` (web push +
low-stock alerts), `scheduler.py` (APScheduler — daily 09:00 expiry scan, monthly
stock snapshot on day 1 at 00:30, monthly system report on day 1 at 01:00, plus a
startup backfill), `snapshots.py` (month-end stock freeze / reconstruction),
`monthly_report.py` (aylık detaylı PDF/Excel sistem raporu üretici), `undo.py`
(undo log), `password_strength.py`, `limiter.py` (SlowAPI).

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
  aggregate (no lot picker). Items with no lots fall back to today's aggregate-only
  decrement (`_plan_lot_allocation` in `routers/production.py`). Available lots:
  `POST /api/inventory/available-lots`.
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
- Run `make test` (48 tests) before deploying.

## Deploy

Prod is `turhost` (SSH config alias → `root@136.144.251.26:23422`,
`/var/www/minerva`, systemd unit `minerva`, PostgreSQL `minerva_db`, public URL
`https://ims.minerva108.com`).

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
(`.claude/settings.json` → `.claude/hooks/block-adhoc-deploy.sh`) **blocks** any Bash
command that ssh's to prod (`turhost` / `136.144.251.26`) and runs `systemctl
restart`, `git reset --hard`, or `git pull`. Read-only ssh (`journalctl`, `systemctl
status`) is allowed; `./deploy.sh`'s own internal ssh is not affected (it runs as a
script subprocess the hook never sees). Emergency hotfix only: `./deploy.sh
--skip-tests` bypasses the test gate with a loud warning; still verify prod manually.

The lab works live — bundle related changes into one restart, and take a `pg_dump`
backup before any destructive DB operation. An hourly auto-snapshot timer
(`ops/snapshot/`) runs 07:00–19:00.
