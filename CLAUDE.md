# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
builds `postgresql://minerva_user:devpass123@localhost:5432/<db>` itself (the
caller's `DATABASE_URL` is deliberately ignored so a stray env var can never
`drop_all` dev/prod) and drop/creates all tables per test function.

**İki oturum aynı anda test koşarsa** biri diğerinin tablolarını siler
(`relation "users" does not exist`). Çözüm: DB adı `MINERVA_TEST_DB` ile
değiştirilebilir —
`MINERVA_TEST_DB=minerva_test2 .venv/bin/pytest tests/…`
(DB'yi bir kez oluştur: `psql -h localhost -d postgres -c "CREATE DATABASE
minerva_test2 OWNER minerva_user"`). `deploy.sh` varsayılan `minerva_test`'i
kullandığı için deploy öncesi yine `pgrep -f 'bin/pytest'` boş olmalı.

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
server block + DNS A record + TLS) lives on `turhost` outside `deploy.sh`; the app
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
edilebilir): tatil/izin/hafta tatilinde çalışılan her dakika fazla mesai, açık
çift ("çıkış eksik") 0 sayılır ve yönetici düzeltene kadar toplam dışıdır.

**İstihdam penceresi — `pdks_employees.start_date` / `end_date`.** `is_active`
puantaja GÖRÜNMEZ: kart pasifleştirilse bile `compute_month` yalnız programa
bakıp ayın kalanını "Devamsız" yazıyor ve ayrılanın son ay bordrosuna 8'er
saat eksik süre bindiriyordu (Ağustos 2026'da iki ayrılışta yaşandı).
`compute_day(..., started_on=, left_on=)` pencere dışı günü `baslamadi` /
`ayrildi` statüsüyle **toplam dışı** bırakır (beklenen 0, devamsızlık yok,
izin sayacına girmez) — öncelik sırasında EN ÜSTTE, tatil/izinden önce.
Ayrılış günü DAHİLDİR. `DELETE /api/pdks/employees/{id}` (pasifleştirme)
tarih boşsa bugüne damgalar; pencere dışına düşmüş OLAY yine gösterilir
(anomali görünsün). Kolon prod'a `init_db()` alter_safe satırıyla ulaşır.

**Unutulan çıkış — iki mekanizma:**
1. **Açık 'in' ne zaman düşer** (`core.pdks.open_in_still_valid`): 16 saatten
   (`OPEN_PAIR_MAX_HOURS`) eski olmayacak VE **önceki güne aitse** 12 saatten
   (`FORGOTTEN_AFTER_HOURS`) eski olmayacak. İkinci kural şart: akşam 17:00
   girip çıkışı unutan personel ertesi sabah 08:30'da hâlâ "içeride"
   sayılırsa bastığı çıkış dünkü güne 15 saatlik hayalî mesai yazardı.
   Bu kuralla dün "Çıkış eksik" kalır, bugün temiz giriş yapılır. Gece
   vardiyası (20:00→05:00 ≈ 9 sa) 12 saatin altında kaldığı için bozulmaz;
   aynı gün içindeki uzun mesai (08:00→22:00) da etkilenmez.
2. **Personel bildirimi** (`pdks_requests`, migration `d2f4a6c8e1b3`):
   personel `POST /api/pdks/requests` ile saat + gerekçe bildirir (ofis
   şartı ARANMAZ — zaten ofis dışından yapılır), yönetici
   `/requests/{id}/approve|reject` ile karara bağlar. **Onaya kadar puantajda
   sıfır etki**; onayda `source='request'` olan gerçek olay yazılır ve
   `_work_date_for` + `_resync_out_work_dates` normal yoldan çalışır.
   Güvenliği sağlayan şey onaydır, doğrulama zayıflamaz. Sınırlar: gelecek
   zaman yasak, en fazla `REQUEST_MAX_AGE_DAYS`=14 gün geriye, aynı
   gün+tip için tek bekleyen bildirim.

**TUZAK — "program tanımsız" ≠ "hafta tatili".** `schedule_for()` İKİ ayrı
durumda da `None` döner: (a) o tarihi kapsayan program versiyonu HİÇ yok,
(b) versiyon var ama o gün boş bırakılmış (gerçek hafta tatili). Bunlar
karıştırılırsa (a) da hafta tatili sayılır, beklenen süre 0 kabul edilir ve o
gün çalışılan sürenin **TAMAMI fazla mesai** yazılır. SAHADA YAŞANDI
(2026-08-20): programlar 04.08'de başlıyordu, 03.08 Pazartesi 08:30–17:45
çalışıldığı hâlde "Hafta tatili" görünüyor ve 9 sa 15 dk fazla mesai
yazıyordu. Ayrım `has_schedule_version(schedules, work_date)` ile yapılır;
`compute_day(..., unscheduled=True)` durumu **`programsiz`** ("Program
tanımsız") yapar ve fazla mesai/eksik süreyi HESAPLAMAZ (çalışılan süre yine
görünür). `compute_month` bayrağı kendi hesaplar; `compute_day`'i doğrudan
çağıran router uçları (`/me/today`, `/day`) geçirmek ZORUNDA. Geriye dönük
düzeltme yolu: eksik dönemi kapsayan **YENİ bir program versiyonu** ekle
(`PUT /employees/{id}/schedule`, farklı `effective_from` = yeni versiyon) —
mevcut versiyonu mutate etme. Ama bunu yalnız o dönemde **gerçekten olay
kaydı olan** personel için yap; kayıt yokken program geriye çekilirse o
günler "Devamsız"a döner ve olmayan devamsızlık uydurulur.

**Geç gelme / erken çıkma bayrağı YOKTUR** (kullanıcı kararı, 2026-08-03:
"dakika dakikasına bakma") — saatler dakika dakika kaydedilir ama kimse
işaretlenmez; puantajda giriş/çıkış saati, çalışma, fazla mesai ve eksik süre
görünür. `late_minutes`/`early_leave_minutes`/`gec_sayisi` alanları
KALDIRILDI; geri eklemeden önce bu kararı teyit et. **work_date =
TR-yerel gün** — `'in'` kendi TR günü, `'out'` kapattığı açık `'in'` <16
saatlikse ONUN günü (gece yarısı kuralı; router `_work_date_for` + check
akışı aynı sabiti kullanır). **Cross-cutting — NOT domain-scoped** (CRM/Drive
gibi; domain kolonu/dependency yok). RBAC kategorisi **`pdks`**
(`check/view_own/view_all/manage/report`; Staff dahil herkes check+view_own,
Manager tümü). Manuel olay düzeltmeleri `correction_note` zorunlu +
`log_admin_event` audit'li; olay silme soft-delete. Excel puantaj:
`GET /api/pdks/report/excel` → `core/pdks_report.py` (Özet + personel başına
sayfa); `GET /api/pdks/report/pdf` → `core/pdks_pdf.py` — patronun aylık
puantaj formatı: tik'li tek sayfa grid (✓/R/İ/D/!/—), süre dökümü YOK
(o Excel'de kalır). İkisi de `_report_data` sözleşmesini paylaşır. Tablolar `pdks_*` — create_all ile gelir (migration
`f3a5c7e9b2d4`). Not: buradaki "izin" devamsızlık mazeretidir; RBAC
"permission" kavramıyla karıştırma.

**Molalar ŞİRKET GENELİ SABİT** (`core/pdks.BREAKS` — 09:30/15 Kahvaltı ·
12:45/45 Öğle · 16:00/15 Mola = **75 dk**; standart mesai
`DEFAULT_WORK_START/END` = **08:30–17:45** → 555 − 75 = net 8 saat). Kişi
bazlı "öğle molası (dk)" girişi UI'dan KALDIRILDI; `pdks_schedules.
lunch_break_minutes` kolonu eski kayıtlar için DB'de durur ama **hesapta
kullanılmaz** (`ScheduleBody` alanı hâlâ kabul edilir, yok sayılır).
Kesinti kuralı: gün TEK kapalı çiftse o çiftin TR saat penceresine düşen
molalar `break_minutes_within()` ile **kısmi kesişim orantılı** düşülür
(yarım gün çalışandan tam günün molası düşmez); çoklu çift = personel molada
çıkış basmış, ikinci kez kesinti YOK. Gece yarısını aşan pencerede (end ≤
start) şema uygulanmaz. Gün çıktısında `break_minutes` + `break_deducted`
(eski ad `lunch_deducted`), `schedule_for()` çıktısında `break_minutes`
(eski `lunch_minutes`). Şema tek kaynak: `/pdks` sayfası `breaks_view()`
sonucunu `window.PDKS_BREAKS`/`WORK_START`/`WORK_END` olarak alır — şablon
kendi saat sabitini tutmaz.

**TR tarih/saat girişi** (`static/tr-datetime.js`): `<input type=date|time>`
TARAYICI DİLİNE göre render edilir (İngilizce tarayıcıda `08/04/2026` ve
`04:30 PM`); `lang="tr"` bunu değiştirmez. Yardımcı, input'u `text`e çevirip
TR maskesi uygular (`gg.aa.yyyy` · `ss:dd`, 24 saat) ve **`el.value`
property'sini override ederek DAİMA ISO döndürür** — mevcut JS ve sunucu
sözleşmesi hiç değişmez. `innerHTML` ile sonradan basılan input'lar
MutationObserver ile yakalanır (`data-tr-dt` guard'ı çift sarmalamayı önler).
Şu an yalnız `/pdks` sayfasında yüklü; başka sayfaya eklemek = tek `<script>`
satırı (+ `sw.js` CACHE_NAME bump).

**PDKS üçlü doğrulama (imza güvenliği).** Self giriş/çıkış, master anahtar
açıkken **üçü birden** sağlanmadan yazılmaz — sıra: **IP → QR → konum**
(`routers/pdks.check_in_out`):
1. **Ofis ağı** — tarayıcı Wi-Fi SSID'sini GÖREMEZ; "Minerva108 ağında ol"
   şartı sunucu tarafı **IP allowlist**'iyle karşılanır (`ip_allowed`; virgüllü
   liste, nokta ile biten girdi = prefiks, ör. `78.180.`). Gerçek IP
   `_real_client_ip()` ile bulunur: **peer loopback/`testclient` ise
   `X-Real-IP`'ye güvenilir** (nginx bu başlığı kendisi yazar, istemci değerini
   ezer), doğrudan dış peer'de header ASLA dikkate alınmaz (sahtelenebilirdi).
   *Prod zinciri doğrulandı (2026-08-03):* nginx `sites-available/minerva` ims
   bloğu `X-Real-IP` + `X-Forwarded-For $proxy_add_x_forwarded_for` yazıyor;
   systemd `ExecStart` uvicorn'u `--proxy-headers
   --forwarded-allow-ips="127.0.0.1"` ile başlatıyor → uvicorn XFF zincirini
   SONDAN okuyup ilk güvenilmeyen adresi alır (nginx'in kendi eklediği
   `$remote_addr`), yani `request.client.host` prod'da **zaten gerçek ofis
   IP'si ve istemci tarafından sahtelenemez**. Doğrulama sekmesindeki
   "Bu ağın IP'sini ekle" bu değeri gösterir — `127.0.0.1` görünüyorsa zincir
   bozulmuş demektir, allowlist'e ASLA `127.0.0.1` yazma (herkes geçer).
2. **Konum** — tarayıcı geolocation, `haversine_m` ile ofis koordinatına
   uzaklık; tolerans `radius_m + min(accuracy, 100)`. `Permissions-Policy`
   bu yüzden `geolocation=(self)` (eskiden `()` idi = tüm sayfalarda kapalı).
3. **QR** — İKİ MOD var, `AppSetting pdks.checkin.qr_mode`:
   **(a) `static` — VARSAYILAN, girişe asılan BASILI QR** (müşterinin girişte
   açık tutabileceği ekranı yok). İçerik sabit: `PDKSQRS1:<kod>`, kod 8 karakter
   ve karışan harfler yok (`STATIC_CODE_ALPHABET`: 0/O, 1/I/L, 5/S, 8/B içermez)
   — afişte yazılı olduğu için kamerası çalışmayan personel **elle de girebilir**
   (`normalize_static_code`: büyük/küçük harf, boşluk, tire toleranslı). Kod
   yalnız yönetici "yeni kod üret" deyince değişir (`POST /api/pdks/qr/static/
   regenerate`, audit'li) ve o an **eski afiş geçersizleşir**. Afiş: `/pdks-qr-
   yazdir` (perm `pdks.manage`, A4 `@media print`). **static_code personele
   ASLA gönderilmez** — `_today_status` yalnız `{enforce, qr_mode}` paylaşır;
   sızarsa ofise gelmeden imza atılabilirdi. Elle giriş `_code_fails` kaba
   kuvvet limitine tabidir. Basılı kod fotoğraflanabilir → **kabul edilmiş
   risk**; asıl güvence IP + konumdur, QR "kapıya kadar geldim" kanıtıdır.
   **TUZAK — QR üretiminde `segno.make_qr()` kullan, `segno.make()` DEĞİL**
   (`_qr_svg`): `make()` en küçük sembolü seçer ve kısa metinlerde **Micro QR**
   (M1–M4) üretir; Micro QR'ı telefon kameraları, html5-qrcode, OpenCV ve jsQR
   OKUMAZ → basılı afiş hiç taranmaz (SAHADA YAŞANDI 2026-08-05: `PDKSQRS1:
   <8 hane>` = 17 karakter → M4; kiosk token'ı 33 karakter olduğu için normal
   QR'a düşüyordu, bu yüzden yalnız basılı afiş bozuktu). `test_qr_is_not_
   micro_qr` kilitler. **Bir QR değişikliğini yalnız "SVG üretildi mi" ile
   doğrulama — gerçek bir decoder'la okut** (`cv2.QRCodeDetector`).
   **TUZAK — `normalize_static_code` `isascii()` de filtrelemeli**: yalnız
   `isalnum()` Unicode harfleri (Ç,Ğ,Ş…) geçirir, sonraki
   `hmac.compare_digest` ASCII-dışı str'de TypeError atıp isteği 500'e
   düşürür (Türkçe klavyeyle 'g' yerine 'ğ' yazan personel — kardeş
   fonksiyonlar `verify_qr_token`/`verify_numeric_code` buna zaten korumalı).
   **TUZAK — `templates/pdks_qr_yazdir.html`'e içerik eklersen `--print-to-pdf`
   ile sayfa sayısını doğrula**: A4 tek-sayfa bütçesi dar (kullanılabilir
   ~281mm); ekran görünümünü bozmadan yalnız `@media print` bloğundaki
   marj/boyut kısıtlamalarıyla sığdırılıyor. Ayrıca arka-plan-renkli öğeler
   (adım rozetleri, ayraç çizgisi) tarayıcının "Arka plan grafikleri"
   varsayılan KAPALI ayarında kaybolur — print bloğunda `border` tabanlı
   alternatifleri kullan, `background-color`'a güvenme.
   **(b) `rotating` — kiosk ekranı** (tablet varsa; daha güvenli):
   `/pdks-qr` kiosk sayfası (perm **`pdks.kiosk`**, yalnız özel
   cihaz hesabına per-user override ile verilir) 30 sn'de bir yenilenen imzalı
   token gösterir: `PDKSQR1:<bucket>:<hmac16>`, `bucket = unix//30`, HMAC
   domain prefix `pdks-qr:`, secret `core.auth.SECRET_KEY`. Sunucu ±1 bucket
   kabul eder → ekran görüntüsü/fotoğraf ~1 dk sonra ölür. **Stateless** (DB'de
   token tutulmaz).
   **TUZAK — `segno...save(..., omitsize=True)` ŞART** (`kiosk_qr`): omitsize
   olmadan segno sabit `width/height` yazar, `viewBox` YAZMAZ; viewBox'sız kök
   `<svg>`'de CSS `width:100%` yalnız viewport'u değiştirir, koordinat sistemi
   1:1 px kalır → sembol kiosk kartında ~%29 KIRPILIR ve hiçbir kamera decode
   edemez (doğrulama açıldığı an kimse imza atamaz). `test_qr_svg_is_scalable_
   not_clipped` bunu kilitler — `startswith("<svg")` yeterli DEĞİL.
   **Yedek sayısal kod** (kamerası olmayan/çalışmayan personel): kiosk ekranı
   QR'ın ALTINDA aynı bucket'tan türeyen **6 haneli** bir kod da gösterir
   (`make_numeric_code`, RFC 4226 dinamik kırpma; HMAC domain `pdks-code:` —
   QR'dan AYRI, biri diğerini ele vermez). İstemci `qr_token` yerine
   `manual_code` yollar; **IP ve konum şartı aynen uygulanır**, kod yalnız
   QR'ın yerine geçer. Kabul penceresi ±2 bucket (60–90 sn; kod elle yazılır,
   QR'dan uzun sürer) — dışında ama son ~10 dk içindeyse `expired`. 6 hane
   kaba kuvvete QR'dan açık olduğu için **personel başına deneme penceresi**
   var (`_code_fails`, 5 hata / 5 dk → **429**; doğru kod sayacı sıfırlar, QR
   yolu bundan etkilenmez). Sayaç süreç belleğinde — restart'ta sıfırlanır
   (kabul edilebilir: saldırgan zaten ofis ağında + ofis konumunda olmalı).
Ayarlar `AppSetting`'te: `pdks.checkin.enforce|allowed_ips|lat|lon|radius_m|
qr_mode|static_code|static_fallback`; UI PDKS → **Doğrulama** sekmesi
(`pdks.manage`), uçlar `GET/PUT /api/pdks/checkin-config` +
`GET /api/pdks/qr/static`.
Statik modda **kod üretilmeden enforce açılamaz** (PUT 400 döner).
**enforce KAPALI başlar**; kapatmak tek
PUT'tur (deploysuz anında geri dönüş) ve `/checkin-config` doğrulamadan
etkilenmez — yönetici kendini kilitleyemez. **Yönetici manuel olay girişi
bilinçli olarak MUAF** (kasıtlı fallback). Olaylara `geo_lat/geo_lon/
geo_accuracy_m` (migration `a4c8e2f6b9d1`) her zaman yazılır.

**11.08.2026 KESİNTİSİ — sebebi ve alınan önlemler.** O gün patron hasta
olduğu için hiç kimse imza atamadı; atılan 8 olayın TAMAMI `source='manual'`
girildi. Sebep: `qr_mode=rotating` seçiliydi ama kiosk ekranını (`/pdks-qr`,
perm `pdks.kiosk`) **yalnız SuperAdmin açabiliyordu** — o gidince ekran yok,
QR yok, imza yok. Üstelik girişe asılı basılı afiş rotating modda
reddediliyordu (`PDKSQRS1:` token'ı `verify_qr_token`'a düşüp "geçersiz"
oluyordu). Üç önlem:
1. **`static_fallback`** (varsayılan KAPALI): açıkken rotating modda basılı
   afiş DE kabul edilir → ekran açılmayan günde imza atılabilir. Bedeli
   açıkça kabul edilmiştir (afiş fotoğrafı geçerli olur; asıl güvence ofis
   IP + konum). Kabul eden yol `verify_method='static_fallback'` yazar.
2. **Fail-closed guard**: `PUT /checkin-config`, *sonuç durumu*
   `enforce + rotating + kiosk hesabı yok + fallback kapalı` olacaksa 400
   döner — kimsenin imza atamayacağı ayar kaydedilemez. Yalnız sonuç
   durumuna bakılır, enforce'u kapatmak asla engellenmez.
3. **Sağlık uyarıları**: `GET /checkin-config` → `warnings[]`
   (`rotating_no_kiosk_account` · `enforce_static_no_code` ·
   `enforce_no_ip_or_geo` · `ip_allowlist_loopback`). Kritik olanlar Günlük
   Durum sekmesinin başına da basılır — Doğrulama sekmesini bir şey bozulana
   kadar kimse açmıyor. `_kiosk_account_exists` **SuperAdmin'i saymaz**;
   kesintinin sebebi tam olarak buydu.
`pdks_events.verify_method` (migration `c7e9b1d3f5a2`) hangi yolla imza
atıldığını tutar: `qr|code|static|static_fallback|off`.
Yanlış moddaki kanıt jenerik "geçersiz" yerine ne yapılacağını söyleyen mesaj
+ `code="qr_mode_mismatch"` döndürür (istemci ölü uç yerine bildirim yolunu
gösterir). Bilinçli ertelenen: tek-kullanımlık QR/replay önleme, IPv6/CIDR,
kiosk cihaz token'ı, yetki-birleştirme (yedek onaycı istenmedi).

**Kiosk hesabı — oturum hiç dolmasın** (`routers/auth.py::login`,
2026-08-14). `/pdks-qr` ekranı hiç tıklanmadığı için idle-watch zaten yüklü
değil (bkz. `api_main.pdks_qr_page` docstring), ama normal JWT/cookie 8
saatte doluyordu — kiosk ekranı her gece "giriş yapın" ekranına düşüyor,
ertesi sabah kimse imza atamıyordu (11 Ağustos'la aynı aile: kiosk yönetici
olmadan çalışmıyor, farklı sebep). Çözüm `core.auth.create_access_token`/
`set_auth_cookie`'ye eklenen opsiyonel `days=` override'ı: login'de kullanıcı
`pdks.kiosk` yetkiliyse **ve SuperAdmin DEĞİLSE** token/cookie ömrü
`remember_me` kutucuğundan bağımsız `KIOSK_SESSION_DAYS=3650`'ye (~10 yıl,
pratikte süresiz) sabitlenir. **TUZAK — SuperAdmin istisnası şart**:
`_resolve_permissions` SuperAdmin'i her kategoride otomatik `True` döndürür
(`core/permissions.py`), yani bu istisna olmadan patronun kendi normal
girişi de sessizce 10 yıllık oturuma dönerdi —
`test_non_kiosk_login_keeps_default_8h_session` bunu kilitler,
`_kiosk_account_exists`'teki "SuperAdmin sayılmaz" kuralıyla aynı sebep.
Bu yalnız "yeniden girişe gerek yok" demektir, **yetkiyi kalıcı yapmaz**:
`/pdks-qr` yetki denetimi (`_resolve_active_user` + `_user_can`) her istekte
DB'den taze okur, kiosk yetkisi geri alınırsa uzun token hâlâ geçerliyken
bile sayfa aynı anda kapanır (`test_kiosk_permission_still_checked_live_
despite_long_token`).

**Rapor/izin bildirimi — personel bildirir, yönetici onaylar** (`pdks_leave_
requests`, migration `e1a3c5b7d9f2`, `b8d2f4a6c9e1`→`c7e9b1d3f5a2`→
`e1a3c5b7d9f2` zinciri). 11 Ağustos'taki asıl eksik buydu: rapor hiç sisteme
girmiyor, gün "Devamsız" görünüyordu. Akış: personel `POST /api/pdks/leave-
requests` ile tür + tarih aralığı + **zorunlu açıklama** + opsiyonel
`document_no` (e-rapor no) bildirir (perm `pdks.check` — Staff dahil herkes);
`employee_id` her zaman `_employee_for_user(current_user)`'dan alınır, body'den
DEĞİL (personel başkasının adına talep açamaz). Kayıt `status='pending'`
başlar, **puantajda sıfır etki** (henüz `LeaveRecord` yok). Yönetici
`GET /leave-requests?status=pending` (`view_all`) görür,
`POST /leave-requests/{id}/approve|reject` (`manage`) ile karara bağlar.
**Onay anında `_validate_leave` TEKRAR çalışır** (aynı fonksiyonu doğrudan
yönetici girişi `create_leave`/`update_leave` de kullanır — tek kaynak):
bildirim beklerken yönetici elle çakışan bir izin girmişse onay **400** döner
ve talep `pending` KALIR, sessizce çift kayıt oluşmaz
(`test_leave_request_revalidated_at_approval`). Onay `LeaveRecord` yaratır,
`LeaveRequest.leave_id`'ye bağlar, `document_no` yönetici onayda da
girilebilir (personel evden yazarken e-rapor no henüz elinde olmayabilir).
Reddetme hiçbir `LeaveRecord` yaratmaz. Tarih sınırları asimetrik — rapor
geçmişe, yıllık izin geleceğe akar: `LEAVE_REQUEST_MAX_PAST_DAYS=60` (kapanmış
bordro ayını korur), `MAX_FUTURE_DAYS=365`, `MAX_SPAN_DAYS=90`; dışı ⇒
"yöneticinize başvurun" (yönetici `POST /leaves` ile sınırsız girebilir).
Personel kendi taleplerini (`GET /leave-requests/mine`) ve kendi yürürlükteki
izinlerini (`GET /leaves/mine`, `view_own`, employee_id'ye scope'lu — önceden
bu uç hiç yoktu, personel kendi raporunu dolaylı gün etiketinden bile
göremiyordu) görür. `core.pdks.leave_detail_for(leaves, work_date)` artık
iznin TAMAMINI döner (`leave_for` türe indirger, geriye uyum için kalır);
`compute_day(..., leave=…)` `leave_note`/`leave_document_no`'yu gün çıktısına
taşır, `compute_month`'taki `totals["izin_gunleri"][tür]` sayacı `.get(tür, 0)`
ile artar — DB'de elle/gelecekte silinmiş bir `leave_type` görürse 500 atmaz
(`test_totals_view_unknown_leave_type_does_not_500`). Excel (`core/pdks_
report.py`): rapor artık yıllık izinden **ayrı renkte** (`leave_fills`:
turkuaz/kırmızı/sarı/gri), Özet sayfasında tür başına ayrı **sayısal** sütun
(`Yıllık İzin (Gün)`/`Raporlu (Gün)`/`Ücretsiz (Gün)` — muhasebeci string
ayrıştırmasın), personel sayfası Not sütununda `Belge no: X`. UI: yeni
**"İzinlerim"** sekmesi (`pdks.view_own`) — hero'daki "Rapor / izin bildir"
kısayolu hasta personeli oraya götürür; yönetici sekmesi `İzinler` →
**"İzin Yönetimi"** (panel id'leri değişmedi). `badgeSt()` artık `lv-${tür}`
class'ı da üretir (Bootstrap'le çakışmayan önek, bkz. gölgeleme tuzağı).
Bilinçli ertelenen: talebi personelin geri çekmesi (yönetici silebiliyor),
yarım gün izin, rapor PDF yükleme, onay push bildirimi (7 kişilik ekipte
rozet yeterli).

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

**TUZAK — güvenlik başlıkları İKİ yerden gelir.** nginx (`sites-available/minerva`)
*ve* `SecurityHeadersMiddleware` ayrı `Permissions-Policy` + `Content-Security-Policy`
gönderir; aynı özellik iki header'da geçtiğinde **en kısıtlayıcısı uygulanır**.
nginx'teki eski değer `camera=(), geolocation=()` idi → PDKS QR okuyucusu ve
konum doğrulaması canlıda HİÇ çalışmıyordu (uygulama izin verse bile). 2026-08-04'te
ikisi de `geolocation=(self), camera=(self), microphone=(), payment=(), usb=()`
değerine hizalandı (nginx yedeği `/root/minerva-nginx.bak.*`). Tarayıcı API'si
(kamera/konum/mikrofon) gerektiren bir özellik eklerken **iki tarafı birden**
güncelle; `test_security_headers_on_all_responses` uygulama tarafını kilitler,
nginx tarafını `curl -sI https://ims.minerva108.com/login | grep -i permissions`
ile doğrula.

**TUZAK — Bootstrap sınıf gölgelemesi.** Şablonlar Bootstrap 5.3 CSS'i yüklüyor.
Yerel bir sınıfa Bootstrap'te var olan bir ad verilirse, **yerel kuralın
tanımlamadığı property'ler Bootstrap'ten sızar**. Yaşandı: PDKS'nin
`.btn-check` sınıfı Bootstrap'in `.btn-check{position:absolute;
clip:rect(0,0,0,0);pointer-events:none}` kuralıyla çakıştı → "GİRİŞ/ÇIKIŞ YAP"
butonu ekranda yok ve tıklanamaz oldu (modül haftalarca kullanılamadı; sınıf
`check-action` olarak yeniden adlandırıldı). Bootstrap yüklü şablonlarda
`btn-*` önekli ad KULLANMA. `test_no_bootstrap_shadowed_class_names` kilitler.

**Public ürün dosyaları — Amazon görseli + Walmart SDS, nginx'te İZOLE**
(`routers/product_images.py`, 2026-08-06; SDS 2026-08-29).
`/public/product-images/<SKU>.MAIN.jpg` kimlik doğrulaması olmayan,
Amazon'un flat-file'ının kendi sunucusundan çektiği bir yol. **uvicorn TEK
süreçte çalışıyor** (systemd `minerva`, worker sayısı yok) — auth'suz + rate
limit'ten muaf bir uç aynı süreçte kalırsa, biri bombaladığında tüm ERP
(login dahil) etkilenirdi. Çözüm: nginx `sites-available/minerva` bu yolu
Python'a HİÇ SORMADAN disk'ten servis eder (`alias` + `core/product_images.
FILENAME_RE` ile AYNI regex, `limit_req zone=pubimg rate=60r/s burst=120`).
**TUZAK — nginx regex'inde `{n,m}` gibi süslü parantez varsa regex'i ÇİFT
TIRNAKLA sarmalamak ŞART**, yoksa nginx'in kendi config tokenlayıcısı `{`'yi
blok başlangıcı sanıp "pcre_compile() failed: missing )" hatası verir (regex
tamamen doğru olsa bile). Python route'u SİLİNMEDİ — lokal `make dev`'de
(nginx yok) ve nginx config'i devre dışı kalırsa tek doğrulama katmanı bu;
`tests/test_product_images.py` doğrudan Python route'a karşı çalışır, nginx
tarafını doğrulama `curl -sI .../public/product-images/<ad>.jpg` ile ETag
formatına bak (nginx: `"mtime-boyut"` hex; Python/Starlette: 32 haneli md5 —
farklıysa nginx servis ediyor demektir). nginx yedeği `/root/minerva-nginx.bak.*`.
Yüklenen görseller `product_images/` dizininde, git'e girmez (`.gitignore`);
`ops/snapshot/minerva-snapshot.sh` günde bir tarball alır (Drive ile aynı kalıp).
**AÇIK NOT:** uvicorn `User=root` ile çalışıyor (systemd) — bu ayrı, daha büyük
bir sertleştirme konusu, bilinçli olarak ertelendi.

**Walmart SDS PDF'i aynı uçtan** (2026-08-29). Walmart WFS, `isChemical=Yes` olan
her ürün için public bir Güvenlik Bilgi Formu adresi ister
(`safetyDataSheet` alanı) → `/public/product-images/<SKU>.SDS.pdf`.
`FILENAME_RE` artık `(MAIN|PT0[1-8]|SDS)\.(jpg|pdf)` kabul ediyor ama
**slot/uzantı ÇİFTİ zorunlu** (`SLOT_EXT` haritası; `MAIN.pdf` ve `SDS.jpg`
REDDEDİLİR) — public uç Content-Type'ı uzantıdan seçtiği için bu ayrım
güvenlik meselesi. Yükleme `content_matches()` ile sihirli baytı doğrular
(`%PDF-`), ve **`strip_jpeg_metadata` PDF'e UYGULANMAZ**: SDS bir mevzuat
belgesi, JPEG segment ayrıştırıcısından geçerse bozulur.
`build_csv` yalnız MAIN + PT01..PT08 okur → SDS Amazon flat-file'ına SIZMAZ
(`test_sds_excluded_from_amazon_csv` kilitler).

**TUZAK — nginx `add_header` DEVRALMASI.** Bir `location` kendi `add_header`'ını
tanımlarsa **sunucu bloğundaki TÜM `add_header`'lar düşer**. Canlıda ölçüldü
(2026-08-29): public görsel ucu `nosniff`/CSP/`X-Frame-Options`/HSTS'in
hiçbirini döndürmüyordu, çünkü location yalnız Cache-Control + CORS
tanımlıyordu. JPEG'de düşük riskti, **PDF'te ciddi** (gömülü JavaScript
taşıyan belge `ims.minerva108.com` origin'inde çalışabilirdi). Çözüm: location
bloğu güvenlik başlıklarını TEKRAR yazar + CSP `sandbox; default-src 'none'`
(belge görüntülenir, script/plugin/form çalışmaz). Uygulama tarafında aynı
politika `SecurityHeadersMiddleware`'in `/public/product-images/` dalından
gelir — route'un kendi header'ını koyması İŞE YARAMAZ, middleware ezer
(Drive `preview/raw` dalıyla aynı kalıp). Yeni bir `location` bloğu
eklerken güvenlik başlıklarını oraya da yazdığını `curl -sI` ile DOĞRULA.

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
- **Lot düşümü TEK KAYNAK: `core/stock_lots.py`.** `Item.current_stock` ile
  `Inventory` lot satırları uzun süre İKİ AYRI SAYAÇTI: lot satırı açan tek yol
  üretim çıktısı + mal kabuldü, buna karşılık teslimat (`routers/delivery.py`
  içinde `Inventory` kelimesi HİÇ geçmiyordu), Shopify satışı ve elle stok
  düzeltmesi yalnız `current_stock`'u hareket ettiriyordu → lotlar bir kez
  açılıp bir daha azalmıyordu. Ölçüldü (09.09.2026): 25 bitmiş üründe 274
  adetlik sapma (ör. Serenida Bikini Area 200 ml — defter 0, lot tablosu 15).
  Artık her fiziksel çıkış `stock_lots.consume(db, item, qty, note=, actor=)`
  üzerinden geçer: FIFO lot düşer + **lot başına ayrı `Transaction(Output)`**
  (`lot_number` dolu) yazar + `current_stock`'u düşer. Yazılan Output'ların
  TOPLAMI daima miktara eşittir → `compute_stock_at` rekonstrüksiyonu değişmez.
  Lot yetmiyorsa artık lotsuz tek Output olarak yazılır, **iş engellenmez**.
  Elle düzeltme (negatif delta, lot seçilmemiş) `stock_lots.draw_down()`
  kullanır: yalnız lotu düşer, defter kaydı yine TEK imzalı `Adjustment`'tır
  (birleştirme script'leri bu konvansiyona dayanıyor). Numune lotu
  (`is_sample`) hiçbir tüketimde kullanılmaz. `production._plan_lot_allocation`
  da aynı motoru çağırır — FIFO'nun ikinci bir kopyası YAZILMAZ.
  **Bilinçli açık:** girişler (iade, '+' düzeltme) lot AÇMAZ; stok lottan fazla
  kalabilir, `uncovered` bunu sorunsuz karşılar. `tests/test_stock_lots.py`
- **Samples (numune)**: `Inventory.is_sample=True` marks a lot received from an
  *alternate* supplier for an existing raw material (entered from the Items page
  "Numune" tab → `/api/inventory/receive` with `is_sample`). Sample lots never merge
  with normal lots (upsert key includes `is_sample`, but NOT `supplier_id` — a second
  sample on the same lot number merges into the first, supplier is COALESCE-only).
  **Samples are NOT stock** (2026-08-24 fix): `current_stock` is untouched and no
  `Transaction` is written on sample receive — only the `Inventory` row + an
  `admin_audit_log` entry. Production's lot pool (`_plan_lot_allocation` in
  `routers/production.py`, `/api/inventory/available-lots`) excludes
  `is_sample=True` lots entirely, so a sample can never be silently consumed by
  FIFO or picked by id. To promote a sample into real stock, use
  `POST /api/inventory/samples/{id}/convert` — that's the ONE place a sample
  Inventory row gets an `Input` Transaction + `current_stock` bump; it flips
  `is_sample=False` (or merges into a same-lot normal row if one exists).
  `receive_stock` also gates on `require_permission("inventory","receive")` +
  `active_domain` + `with_for_update()` (it originally had none of the three).
  **24.08.2026 incident**: an intern entered 47 samples through this tab in one
  session; because samples counted as stock, ~40 raw-material cards inflated
  (units sometimes wrong too — e.g. +50 kg on an item with 5 kg real stock),
  and because the item picker was a plain `<select>` with no search and the
  items list search didn't fold Turkish characters or check `name_tr`, the
  intern couldn't find several existing cards and created 8 duplicates via
  "Yeni Ürün Ekle" (whose unit `<select>` defaulted to 'adet' — now the select
  has no default option and empty unit is a 422). Fixed with a repair script
  (`scripts/repair_numune_20260824.py`, compensating `Adjustment` transactions
  per `scripts/merge_duplicate_products.py`'s pattern — never delete
  Transaction rows, see the ledger-invariant note below) plus `Item.name`/
  `name_tr` duplicate-guard on create/update (`_find_name_conflict`, Turkish-
  folded via `core.supplier_prices.normalize`, 409 + `force:true` override).
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
