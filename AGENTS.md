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

**Kodlu fason üretim** (`/outsourcing`; `routers/outsourcing.py`, motor
`core/outsourcing.py`, PDF `core/outsourcing_documents.py`, rapor ayrımı
`core/outsourcing_reporting.py`, yetki backfill `core/outsourcing_access.py`, UI
`templates/outsourcing.html` + `static/outsourcing.js`; spec
`docs/superpowers/specs/2026-10-09-kodlu-fason-uretim-design.md`, 09.10.2026).
Dış üreticiye YALNIZ kodlu föy/etiket/sevk listesi/rapor PDF'i gider (portal yok);
gerçek malzeme ↔ kod eşleştirmesi (kart adı, spec, kaynak lot) yalnız
`outsourcing.mapping` ile döner. **Domain-scoped**; RBAC `outsourcing`
(`view/manage/mapping/approve/dispatch/record`; Manager + LabLead varsayılan;
Distributor yetki verilse bile 403 — `require_internal_user`). Akış: firma →
üreticiye özel kalıcı kod (kart + spec sha256; ad değişikliği kodu değiştirmez,
grup eşdeğerliği otomatik kabul EDİLMEZ) → iş (reçete × adet; `preview` =
`expand_recipe` + `production_plan._build_lines`, fire hammaddede, ambalaj/etiket
muaf; `dispatch_quantities` sevk fazlası tartım hedefinden ayrı) → kaplar (kap =
tek kaynak lot + tek dolum; hammaddede lot ZORUNLU ve APPROVED/QC'siz/numune
değil/SKT dolu-geçerli; hazırlıkta lot kapasitesi açık işlerin gönderilmemiş
kaplarıyla birlikte denetlenir) → her değişiklik yeni paket sürümü
(`outsourcing_packet_revisions` snapshot + hash) → **üç ayrı hesap onayı**
(technical=LabLead ÖNCE, owner=SuperAdmin, manager=Manager; `revision` +
`packet_hash` eşleşmeli; B2B'nin aksine çizilen imza / şifre YOK) → etaplı sevk
(`idempotency_key`; kap başına TEK `Output`, not öneki `Fason sevk`,
`OutsourcingShipmentLine.transaction_id`; lotsuz kap `stock_lots.draw_down` ile FIFO
lot düşer) → ilk sevkten sonra paket/kap DONAR → dış tüketim/fire
(`OutsourcingMovement`; yerel stok TEKRAR düşmez; g↔kg, ml↔l kesin, kg↔l YOK) →
fiziksel iade + mamul kabul (`quantity` toplam, `sample_quantity` şahit alt kümesi)
**ayrı karantina lotlarına** (`status='QUARANTINE'`, `inventory.outsourcing_receipt_id`;
iade özgün lot no + tedarikçiyi taşır, mamul `FS-{iş}-R{kabul}`) → QC → kapanış
(`gönderilen = tüketim + fire + iade`, dış bakiye 0, tüm QC kararları verilmiş).
İptal otomatik stok iadesi DEĞİLDİR (sevk sonrası bakiye sıfırlanmadan iptal yok).
**TUZAK — QUARANTINE→APPROVED legacy dalı `Input` YAZMAZ** (yalnız
`current_stock +=` + bilgi amaçlı `QC Approval`; aylık rekonstrüksiyon onu saymaz).
Bu yüzden iki QC ucu (`/qc/process`, `/inventory/{id}/qc-approve`) önce
`core.outsourcing.approve_receipt` çağırır: fason lotuysa TEK gerçek `Input` (not
`Fason kabul` / `Fason iade`) + `current_stock +=` + `receipt.input_transaction_id`
yazar ve legacy dalı atlatır; red stok yazmaz. **Fason şahidi `is_sample`
DEĞİLDİR** (o bayrak alternatif tedarikçi numunesi: numune listesi, "stoğa çevir",
Numune Analizi kaynağı — şahit oraya düşerse satılabilir stoğa çevrilebilirdi);
onaylı şahit `RETAINED` olur, `Input` yazılmaz, hiçbir APPROVED havuzuna girmez.
`OutsourcingError` jenerik 500 değil `{detail, code}` döner. Normal mal kabul upsert'i, `find_twin` ve lot taşıma fason satırlarını
dışlar; bekleyen fason lotuna elle düzeltme 400. **TUZAK — FK döngüsü**
`inventory → outsourcing_receipts → outsourcing_containers → inventory` KAP
kenarından kırılır (`OutsourcingContainer.inventory_id` `use_alter=True`, ad
`fk_outsourcing_container_inventory`). `inventory` tarafına use_alter KOYMA: eski
şemalı test DB'sinde `drop_all` olmayan kısıtı düşürmeye çalışıp her testi
patlatır (ChatGPT devralmasında yaşandı). `stock_lots.absorb_row` lotu silerken
bağlı kapları ikize yönlendirir; kart silme fason kodu/işi olan kartı arşivler.
Prod'a: tablolar `create_all`, `inventory.outsourcing_receipt_id` + adlı FK
`init_db` alter_safe (migration `e3f5a7c9b1d2` kayıt için). Raporlar: top-usage ve
aylık rapor sevk Output'larını tüketim saymaz (`transfer_transaction_ids`), fiilî
tüketim + fireyi `movement_totals`'tan ekler; aylık raporda ayrı "Fason Sevki
(transfer)" bölümü; `compute_stock_at` değişmez. Motor `log_admin_event`
KULLANMAZ (içeride commit eder) — `AdminAuditLog` transaction içinde eklenir,
router tek commit. Yetki: `outsourcing_access.backfill_permissions` override'lı
kullanıcılara kategoriyi YETKİSİZ ekler (sentinel `backfill.perm.outsourcing.v1`);
Işık'ın onayı `scripts/grant_outsourcing_isik_20261009.py [--commit]` (kuru
varsayılan, yalnız `view`+`approve`). İlk gerçek firma/ürün belli değil — canlıya
örnek veri yazma. Testler `tests/test_outsourcing.py` (motor),
`tests/test_outsourcing_api.py` (HTTP + QC + eşzamanlılık + rapor/defter),
`tests/test_outsourcing_documents.py` (PDF gizliliği); etiket 100×70 mm, QR yalnız
`container_uid` (reportlab `QrCodeWidget`, cv2 ile çözüldüğü doğrulandı).

**B2B sipariş akışı** (`/b2b-siparisler`; `routers/b2b_orders.py`, motor
`core/b2b_orders.py`, proforma `core/b2b_proforma.py`, iki aşamalı üretim
`core/production_run.py`, UI `templates/b2b_siparisler.html` + `static/b2b-orders.js`;
spec `docs/superpowers/specs/2026-10-09-b2b-onay-proforma-uretim-design.md`, 09.10.2026).
Taslak iç teklif Teklifler'de "Siparişe dönüştür" ile `B2BOrder`'a bağlanır, teklif
`status='ORDER'` olur — legacy "Onayla & stoktan düş" (`/quotations/{id}/confirm`)
yalnız DRAFT/PENDING kabul ettiği için bu teklifte ÇALIŞMAZ; yeni akış onu hiç
çağırmaz, onay/proforma stok hareketi YAZMAZ. Durumlar SUBMITTED (atanmış teknik kişi)
→ TECH_REVIEWED (atanmış imzacı) → APPROVED (proforma; ödeme · alım satırları · son
hazırlık · partiler) → SHIPPED | CANCELLED. **Domain-scoped**; RBAC `b2b_orders`
(`view/manage/tech_review/sign/payment/produce/ship`); fiyat/banka/ödeme/proforma/
ticari revizyon ayrıca `b2b.view` ister (teknik görünümde satış fiyatı ve alım tutarı
yok); Distributor 403. Yetki + ATANMIŞ kişi + panel + sürüm sunucuda denetlenir;
**dört göz**: siparişi açan imzacı olamaz. Ticari sürüm (müşteri, satırlar, fiyat,
şartlar, SEÇİLEN banka snapshot'ı) ve teknik sürüm (satır başı stoktan kullanım /
üretim, reçete, eksikler — `routers.purchase_plan.build_report`; ortak hammadde
sipariş genelinde toplanır, tedarikçi politikası korunur) AYRI, sha256'lı
`b2b_order_revisions`. Ürün/adet/etiket dili değişirse teknik + yönetim onayı
(SUBMITTED), yalnız fiyat/şart/banka değişirse yalnız imza (TECH_REVIEWED) yenilenir —
teknik geçerlilik revizyon numarasıyla DEĞİL kapsamla ölçülür (`technical_current`:
dil + (ürün, adet)). İmza (ticari, teknik) sürüm çiftine + `document_hash`'e aittir;
router sırası: atama/aşama (yan etkisiz) → şifre `verify_password_step_up` (giriş kilit
sayaçlarını PAYLAŞIR, sayaç kendi commit'iyle yazılır; 5 hata → 15 dk) → çizilen imza
PNG'si (≤300 KB, ölçü denetimli). Şifre SAKLANMAZ. Proforma sunucu PDF'i
(`render_order_proforma` → ortak proforma şablonu, aşağıda) İMZALI ticari snapshot'tan
basılır — banka profili/teklif sonradan değişse de aynı belge; imza "Best Regards."
altında. Banka: `bank_profiles` (`_seed_bank_profiles` ile BİR kez, sentinel
`seed.bank_profiles.v1` — silinen geri gelmez); siparişte 1–3 banka + şartlar
(bkz. Proforma şablonu). **İç teknik föy** `GET /api/b2b-orders/{id}/technical-sheet`
(`core/b2b_technical_sheet.py`, `b2b_orders.view`; "İÇ BELGE — müşteriye
gönderilmez"): satırlar, ONAYLANAN reçete bileşimi (teknik snapshot `lines[].recipe`;
09.10.2026 öncesi sürümde güncel reçete + not; reçete sonradan değiştiyse uyarı),
ihtiyaç/eksik, alım satırları, partiler + kaynak lot/tedarikçi, onay ve ödeme
kapıları — satış fiyatı ve banka YOK, alım tutarı yalnız `b2b.view`. Ödeme koşulu
`prepaid` (üretim için %100) / `advance` (%X üretim, tamamı sevkten önce) / `net`
(ödeme durdurmaz); ödeme proforma para biriminde.
**İki aşamalı parti** — `core/production_run.py` = `start_production`'ın tüketim ve
çıktı yarıları BİREBİR (legacy uç ikisini aynı transaction'da çağırır).
**BAŞLAT** yalnız tüketim: `production_plan.plan(lock=True, lot_filter=strict_lot_ok)`
— hammadde FIFO'su yalnız lot no'lu, APPROVED, QC'si bitmiş, numune olmayan, SKT'si
okunur ve geçmemiş lotu görür (`plan_fifo(accept=)`; SKT metin olduğu için SQL'de
değil); yetmezse `lot_missing` ve HİÇBİR şey yazılmaz. Lot no o an alınır
(`lots.next_sequence`; `lots.is_taken` B2B partisini de çakışma sayar — tamamlanmamış
ya da iptal parti no'yu tutar). Output notu üretim sözleşmesiyle aynı; döküm
`b2b_order_batches.plan_snapshot`. Bitmiş ürün ve `ProductionHistory` YOK. **TAMAMLA**
yalnız bitmiş ürün: `ProductionHistory` + dökümden `ProductionConsumption` (başlangıç
Output'larına bağlı) + `write_output` (showroom + `-S` şahit + RetentionSample);
malzeme İKİNCİ KEZ düşmez. Şahit müşteri adedinden düşer (`üretilen − şahit`), eksik
yeni partiyle. Başlamış parti İPTALİ iade ETMEZ; fiziksel iade gerekçeli, tüketimle
sınırlı `+Adjustment` (`B2B parti iadesi — …`, ilk kaynak lota). Normal üretim iptali
(`core/production_cancel`) B2B partisinin üretimini ENGELLER. Kaynak seçimi kapalı
(`production.source_choice.enabled=0` aynen). **Tek sevkiyat**: açık parti yok +
ödeme koşulu + her satırda sevke uygun stok ≥ sipariş; kartlar kilit altında yeniden
doğrulanır, `build_delivery` (kargo) + `ship_core(released_only=True)` BİR kez düşer.
Sevke uygun = `stock_lots.shippable_quantity` = kart stoğu − QC bekleyen − şahit
(aktif RetentionSample'a bağlı) lotlar; lot kaydı olmayan eski stok uygundur (QC
bekleyen ve şahit stok DAİMA lot satırıdır), o kısım lotsuz Output'la düşer. QC
`/qc/process` ile aynen; red stoğu geri alır → sevk hazır değil. Ana ekran "Benden
bekleyen işler" (`GET /api/b2b-orders/my-tasks` — `next_step` sahibi + fason
onayları; push `notify_b2b_step`), Üretim sayfasında "Müşteri Siparişleri" paneli.
Yetki backfill `_backfill_perm_b2b_orders` (sentinel `backfill.perm.b2b_orders.v1`):
override'lı kullanıcıya rol varsayılanı ANCAK mevcut alanıyla — manage/sign/payment ←
`b2b.view`, tech_review/produce ← `production.create`, ship ← `inventory.adjust`,
view ← `b2b.view|production.view`. Prod'a: tablolar `create_all` (migration
`f4a6c8e0b2d3` kayıt için). Motor commit ETMEZ (`AdminAuditLog` `b2b_order.*`
transaction içinde); kilit sırası sipariş → kart → lot. Nav linki
`scripts/add_b2b_orders_nav.py` (idempotent, Fason linkinin altı). Testler
`tests/test_b2b_orders.py`.

**Proforma şablonu + belge başına banka seçimi** (09.10.2026; beğenilen boş şablon
`Proforma_Sablon_Bos`). TEK çizici `core/proforma_template.py` (`render_proforma(doc)`;
logo `static/images/proforma_logo.png`, `COMPANY`, `DEFAULT_TERMS`, `STANDARD_NOTES`;
font depodaki Liberation Sans — `core/fonts/`, Arial ile birebir ölçülü, SIL OFL 1.1 —
Mac, test ve sunucu aynı çıktıyı verir, sunucuya font kurmak gerekmez):
B2B sipariş proforması (`core/b2b_proforma.py` adaptör), Teslimat proforması PRF-
(`core/proforma_invoice.py` adaptör — antetli kâğıt artık KULLANILMAZ) ve kayıtlı teklif
PDF'i (`GET /api/quotations/{id}/proforma`, `b2b.view` + panel). Teklifler sayfasının
HTML önizlemesi aynı düzeni CSS ile çizer; firma/notlar/varsayılan şartlar sayfa
route'undan `proforma` bağlamıyla gelir (iki kopya YOK). ≤24 kalem: numaralı boş
satırlarla tek sayfa TAM BOY — ek bloklar (navlun/KDV, RUB, not, imza, 3. banka)
sığmazsa önce boş satır eksilir (`_rows_that_fit` yüksekliği ölçer), kalemler de
sığmazsa `render_autofit` küçültür/sayfalar. Şartlar belge başına (`proforma_terms`
JSON: transportation / shipment / delivery_type / loading_days [+ payment]); boş alan
şablon varsayılanı; B2B'de PAYMENT TERMS ödeme koşulundan (`payment_terms_text`),
teklif/teslimatta serbest metin. Teklif formunun eski varsayılan "Notlar / Şartlar"
metni NOTE olarak tekrar basılmaz (`printable_notes` — ödeme satırıyla çelişmesin).
**Banka seçimi** `core/bank_accounts.py`: belge başına 1–3 profil (`bank_profile_ids`
JSON — `b2b_orders` / `quotations` / `deliveries`), aktif + en az bir IBAN, sıra =
sütun sırası; öneri = ülke + para birimi kuralı (`bank_rules`, `*` = tüm ülkeler),
yoksa `is_default` bankalar (Kuveyt Türk + Vakıfbank — `_backfill_bank_defaults`,
sentinel `backfill.bank_defaults.v1`; şablondaki Vakıfbank TRY IBAN'ı ve "TÜRKİYE"
yazımı da). Fiyat USD/EUR/TRY kalır; `iban_rub` (ör. Emlak Bank — Rusya ödemeleri)
satırı YALNIZ seçili bankada RUB varsa basılır. Seçim listesi `GET /api/bank-profiles
?country=&currency=` (`b2b.view` | `inventory.adjust`, iç kullanıcı); yönetim B2B →
Banka hesapları (`POST/PUT /api/b2b-orders/banks`; PUT KISMİ — gönderilmeyen alan
korunur; IBAN değişikliği eski → yeni audit'li). Şema: migration `a6c8e0b2d4f7` +
`init_db` alter_safe. Testler `tests/test_proforma_template.py`.

**SKT kontrol raporu** (İzlenebilirlik → "SKT sorunu olan lotlar"; `core/expiry_report.py`).
B2B partisi (`strict_lot_ok`) ve fason sevk SKT'si eksik / okunamayan / geçmiş
hammadde lotunu KULLANMAZ; rapor bunları listeler (`GET /api/traceability/
expiry-issues?scope=recipes|all` + `/export` Excel; `inventory.view`, panel): numune
olmayan, miktarı > 0, APPROVED/QUARANTINE, Ambalaj dışı aktif kart lotları;
`recipes` = aktif reçetede geçen kartlar. Düzeltme `PATCH /api/inventory/lots/{id}/
expiry` (`inventory.adjust`, panel; ISO saklar, eski → yeni `inventory.lot_expiry`
audit; numune/fason lotu 400; stok ve defter DEĞİŞMEZ). SKT ayrıştırıcı TEK:
`core/lots.parse_expiry` (YYYY-MM-DD · GG.AA.YYYY · GG/AA/YYYY) + `expiry_today()`
(TR takvim günü) — B2B havuzu, fason, rapor, "Yaklaşan SKT" ve günlük SKT taraması
aynısını kullanır (rapordaki karar = havuzdaki karar). Sayfa `tr-datetime.js` yükler
(tarih girişi gg.aa.yyyy). Testler `tests/test_expiry_report.py`.

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
