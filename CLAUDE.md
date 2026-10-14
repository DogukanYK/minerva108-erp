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

**Molalar ŞİRKET GENELİ ve TARİHE BAĞLI** (`core/pdks.BREAK_REGIMES`,
`breaks_for(work_date)`). **23.09.2026'dan itibaren mesai 09:00–18:00, tek
60 dk öğle arası (12:30–13:30) düşülür → net 8 saat** (patron kararı
01.10.2026; 29.09'da önce "mola düşme" denmiş, 23.09 satırı yerinde
düzeltildi). Öncesi `LEGACY_BREAKS` — 09:30/15 Kahvaltı · 12:45/45 Öğle ·
16:00/15 Mola = 75 dk, mesai 08:30–17:45 → net 8 saat. Şema tarihe bağlı ki
kapanmış aylar geriye dönük değişmesin: yeni mola kararı = `BREAK_REGIMES`'e
yeni satır, eskisini DÜZENLEME. Puantaj hesabı `break_minutes_within(...,
work_date)` ile günün şemasını kullanır; tarihsiz çağrı ve UI (`BREAKS`,
`breaks_view()`, `TOTAL_BREAK_MINUTES`) güncel şemayı gösterir. Programlar
`scripts/pdks_mesai_0900_1800_20260923.py` ile 23.09 versiyonuna taşındı. Kişi
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
Yüklü sayfalar: `/pdks`, Raporlar, Satın Alma, Influencer, İzlenebilirlik (SKT
düzeltme); başka sayfaya eklemek = tek `<script>`
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

**Satın Alma Planı** (`/satin-alma`; `routers/purchase_plan.py`, motor
`core/purchase_plan.py`, fiyat/tedarikçi `core/purchase_pricing.py`, çıktılar
`core/purchase_plan_pdf.py` + `core/purchase_plan_xlsx.py` + `core/xlsx_cache.py`,
istek modeli `core/purchase_plan_models.py`, UI `templates/satin_alma.html` +
`static/satin-alma.js`) — Rusya siparişi için yazılan tek seferlik betiklerin
(`~/Desktop/Claude/Rusya-Siparis`) sistemleşmiş hâli: ürün × adet (+ ⚙ başka
ürünün reçetesi / ölçek / ek ambalaj / etiket yüzü) → hangi malzemeden ne kadar,
kimden, kaça; "Tedarikçili ve Fiyatlı Alım Listesi" PDF (yatay A4) + Excel
(formül + gömülü önbellek değeri). **Domain-scoped**, RBAC `reports.view` —
fiyatlar raporu gören herkese açık (kullanıcı kararı 05.10.2026, rol bazlı
gizleme YOK). Uçlar `/api/purchase-plan/*`: `GET /products` (aktif reçeteler
`r:<id>` + reçetesiz somut bitmiş ürünler `i:<id>`, soyut varyasyon ebeveyni
hariç; marka `core.brands`; varsayılan hariçler), `POST /preview` (rapor +
`sections` — malzeme bölümlerinde satır yerine `row_keys`; satırda sunucu
biçimli `ui.sup`/`ui.amount`, TR sayı biçimi JS'te yeniden yazılmasın),
`POST /export?format=pdf|xlsx` (audit `purchase_plan.export`, 20/dk),
`GET /history-fill` (`production_history_report.history_quantities` →
`{target_item_id: adet}`), senaryo CRUD `/scenarios[/{id}]`. Akış
`build_report`: `load_inputs` (TEK DB okuyucu; panel dışı id →
`PlanInputError` → 400 "Bu panelde değil", pasif reçete → 400) → `compute`
(SAF) → `load_price_inputs` → `attach`; hiçbir satırın reçetesi yoksa 400.
Kur (`core.fx.today_rates`, TCMB) YALNIZ rapor para biriminden farklı fiyatlı
teklif varsa çekilir — USD liste + USD rapor ağa çıkmaz. **Senaryolar**
`purchase_plans` (create_all + migration `b7d9f1a3c5e8`): `config` =
PlanRequest JSON'u `{"version": 1, …}` — modele yeni alan DAİMA varsayılanlı
eklenir, eski senaryo açılabilsin. Ad AKTİF kayıtlarda panel içinde tekil
(TR-katlanmış uygulama kontrolü + `is_active` koşullu kısmi tekil indeks),
silme yumuşak; düzenleme/silme sahibi (`created_by_id`), SuperAdmin ya da
Manager — rol DB'den CANLI okunur (JWT'deki değil); create/update/delete
audit'li. `GET /scenarios/{id}` artık olmayan/pasif reçete ve kartları
`missing[]` ile döner; UI bunları "eksik kayıt" kutusunda gösterir,
önizlemeye/çıktıya KATMAZ ama kaydederken KORUR (sessiz düşürme yok; kullanıcı
"Kaldır" ile çıkarır). `?scenario_id=` önizleme/dışa aktarma `last_run_at`'i
damgalar (`updated_at`'e dokunmadan). Taslak tarayıcıda (`localStorage`
`sap.draft.v1.<domain>`) otomatik saklanır; geri yüklenirken kart durumu
SUNUCUDAN sorulur (`GET /item-refs?ids=` → senaryodaki `missing[]` biçimi) —
`/api/items`'a bakılmaz: o liste pasif kartı vermez ve `items-cache.js`
önbelleği panele göre ayrılmaz (sayfa listeyi `fetchItems(true)` ile taze çeker).
Kapasite ("elimizdekiyle üretilebilir") hariç kalemleri kısıt saymaz (su kartı
0 stokta durur). **Grup stoğu** (13.10.2026, `PlanOptionsIn.count_group_stock`
varsayılan True — eski senaryolar da açık; yalnız net mod): `compute` iki geçiş —
satır başına kendi stok/ihtiyaç (`base`), sonra `_allocate_group_stock`.
Yalnız satırın kök kartının YERİNE üretimde düşülebilen kartlar
(`group_stock.can_substitute`; etiket satırı hiç) sayılır. Havuz = planda satırı
olmayan, hariç/bekletilmemiş uygun kartlar + satırların ihtiyaç fazlası; eksik
satırlar plan sırasıyla alır (bitirilecek/alma → tercih edilen → diğer → plan içi
fazla), artan havuz kartı kullanabilen SON satıra yazılır, fazlası verilen satır
`kendi − verilen` gösterir → grubun satır stokları toplamı grup toplamını AŞMAZ
(XHANTAN: 180 g + 24.687,81 g → "24.868 gram", alınacak 0). Satıra `group_stock`
{own, taken[], given[], open_from[], total, breakdown_text, aside_text, note};
`stock_note` önizlemede `ui.stock_note`, PDF stok hücresinde ve Excel AA
"Grup stoğu (kart dökümü)" sütununda (24–26. sütunlar ve formüller aynı).
Uyarılar `group_stock_counted`/`group_stock_given` (bilgi); `group_alt_stock`
yalnız SAYILMAYAN kartlar (g↔kg dahil — üretim birim çevirmez). Açık sipariş
miktarı kendi biriminden satır birimine çevrilir (çevrilemezse
`open_order_unit_mismatch`). Kapasite grup toplamını kullanır; aynı üründe aynı
havuzu paylaşan satırlar havuzu BİR kez harcar. Açıkken "bitirilecek stok"
kutusu da kapsanır. Router: üretimde kaynak seçimi kapalıysa
`count_group_stock` + `count_phase_out_stock` hesap kopyasında kapanır,
`source_choice_disabled` uyarısı (senaryo değişmez).
Varsayılanlar: alım firesi %0, USD,
"1 l ≈ 1 kg" notu, DİSTİLE SU / SAF SU hariç (AppSetting
`purchase_plan.default_excluded[.<domain>]`), bitmiş ürün stoğunu düşme kapalı;
kontrol listesi sorumlusu senaryoda girilir (kişi adı SABİT YAZILMAZ).
Raporlar'daki "Üretim Stok Analizi" paneli hızlı araç olarak kalır, başlığında
"Gelişmiş: Satın Alma Planı →" bağlantısı var. Nav linki
`scripts/add_satin_alma_nav.py` (idempotent, `reports.view` kapılı). Paraşüt
alış faturası fiyatları SONRAKİ AŞAMA. Testler `tests/test_purchase_plan_api.py`
(+ `_engine`, `_render`, `_acceptance` — sonuncusu `RUSYA_FIXTURE_DIR` ile
açılır) ve `tests/test_purchase_pricing.py`.

**"Aynı malzeme" grupları + hedefli numune çevirme + lot taşıma** (2026-10-06).
Lab'ın düzeni: aynı malzemenin her TEDARİKÇİSİ AYRI KART (kopya-kart popup'ında
"ayrı kalsın" kararları; ör. SETİL STEARİL ALKOL / CETYL STEARYL ALCOHOL /
CETEARYL ALCOHOL üç firma). Bu kartlar `material_groups` + `items.material_group_id`
ile bağlanır (`core/material_groups.py`, `routers/material_groups.py`
`/api/material-groups`: okuma items.view, yazma items.edit; kart tek grupta,
<2 aktif üyede grup dağılır). `material_key()` TR/EN eşanlamlılarla (alkol↔alcohol,
setil↔cetyl, uçucu yağ↔essential oil …) öneri üretir; lab Ürünler → "Aynı malzeme
önerileri"nden onaylar — SİSTEM KENDİLİĞİNDEN GRUPLAMAZ (tohum: lab'ın "kept"
kopya kararları, sentinel `backfill.material_groups_from_kept.v1`). Satın Alma
Planı gruptaki firmaları alternatif tedarikçi olarak gösterir (`relations.firms[]` /
`alternatives[]`, önizlemede "Tedarikçiler (N)"). 06.10'da gruptaki başka kartın
stoğu ihtiyaçtan düşülmüyordu (yalnız `group_alt_stock`); **13.10.2026'dan beri
grup = eşdeğer malzeme**: stok raporlarda toplanır, Satın Alma ve üretim
birbirinin yerine kullanır (aşağıda `core/group_stock.py`). Ürünler sayfasındaki
öneri/grup metinleri lab'a bunu söyler ("yalnız eşdeğer kartları gruplayın").
"Stoğa çevir" hedef sorar (bu kart / başka kart / yeni kart → `convert-options`);
hedef kartta numune geldikten sonra numune miktarını karşılayan elle "Stok
düzeltme…" varsa 409 `maybe_already_counted` → "yalnız lotu bağla" (`link_only`:
Transaction YOK, stok değişmez, `sample_converted_at` iz tutar). `POST
/api/inventory/lots/{id}/move` lotu karta taşır — Output/Input DEĞİL **Adjustment
çifti** ("Lot taşındı", aylık raporda sahte tüketim/alım olmasın), normal lot için
`inventory.adjust` şart, `moved_from_item_id` iz tutar. Not önekleri sözleşmedir:
"Numune stoğa çevrildi" (purchase_pricing çevrilmiş numuneyi buradan tanır), "Lot
taşındı", "Stok düzeltme" (guard yalnız bunu elle düzeltme sayar). 05.10.2026
vakası: Naturalya numuneleri KRK/İpeda ana kartlarında durduğu için çevirme stoğu
oraya yazdı; 758 kartı elle +20 + çevirme +20 ile çift sayıldı
(`scripts/fix_numune_double_count_20261006.py`).

**Grup stoğu çekirdeği `core/group_stock.py`** (13.10.2026; plan
`~/.claude/plans/robust-orbiting-hartmanis.md`). Kural TEK yerde:
`convert_qty` yalnız aynı `unit_norm` (1:1), g↔kg, ml↔l (×/÷1000) çevirir, başka
her şey `None` — g ile ml TOPLANMAZ. `can_substitute(a, b)`: b aktif, aynı panel,
aynı `lot_kind` (bitmiş ürün asla), aynı tüketim türü (etiket ≠ şişe), BİREBİR
aynı `unit_norm` (üretim birim çevirmez); `production_plan._option_cards` ve
Satın Alma bunu kullanır. `load_groups(db, domain)` tek sorgu: aktif grup + aktif
aynı-panel üye + tedarikçi adı; anahtar `(grup, kova)` — kova
hammadde/ambalaj/etiket/finished/other, `classify` = Stoklar'daki `classify`
(`ledger.html`) BİREBİR; kovada <2 üye kalırsa grup yok. Stok `max(0, current)`
(numune zaten dışarıda). `pick_base_unit`: en çok stoklu birim ailesi taban,
başka ailedeki kart `aside` ("ayrı birim"). `group_min` = sayılan üye
minimumlarının çevrilmiş EN BÜYÜĞÜ (toplam sahte alarm üretirdi). Metinler
Python'da: `amount_short` ("180 g", "24,7 kg"), `breakdown_text`
("HAMMADDESEPETİ 180 g · YİĞİTOGLU KİMYA 24,7 kg"; tedarikçi yoksa/ikiyse kart
adı) — yalnız kart dökümü; Eksik Hammaddeler grup satırı/notu/PDF ve dashboard
kritik grup mevcut/min/eksik/numune `amount_exact` (taban birim korunur, kg/l ≤3,
g/ml ≤2 ondalık, pozitif eksik asla "0": 999,6 g / 1.000 g, eksik "0,4 g"). **Tek anahtar** `group_stock_enabled(db)` = `production_plan.
source_choice_enabled`: "yeterli mi" sonucunu değiştiren her şey (Satın Alma
düşümü, Eksik Hammaddeler/dashboard grup kuralı, düşük stok push'u) ona bağlı;
ayar `0` → hepsi eski kart başı davranış, deploysuz. Stoklar ve Aylık rapor
yalnız görünüm, DAİMA gruplar. Yüzeyler: `GET /api/inventory/summary` karta
`group_id`/`group_key`/`unit_family`/`bar_value`; `GET /api/inventory/groups-summary`
(summary ile aynı izin + panel) sunucu biçimli toplam/döküm; `ledger.html` grup
satırı (açınca üyeler kendi "Düzelt"iyle, arama üyeyi bulursa grup açık);
Aylık rapor ve Excel'i grup satırı + girintili üye (Excel'de toplam METİN — SUM
çift saymasın), bölüm toplamları aynı. **Bilinen sınır:** dondurulmuş eski
ayların kaydında grup yok, BUGÜNKÜ gruplamayla gösterilir (dipnot). **Eksik
Hammaddeler** (`core/stock_gaps.assemble`): kural açıkken gruplu kartın durumu
`classify(toplam, group_min, numune)`; eksik grup tek satır (`kind="group"`,
`members[]`), grubu yeterli kart listeden DÜŞER; sipariş bayrağı KART başına
(grup "Sipariş verildi · 1/2 kart"); seçim modu (`ids=`) kart başı kalır, karta
`group` notu (Excel "Grup dökümü", PDF ad hücresi); kural kapalıyken Excel eski 13
sütun. Dashboard sıfır/kritik sayısı grubu bir sayar. Push: dört çağrı noktası
(`routers/inventory.py`, `routers/production.py`, `routers/b2b.py`,
`core/shopify._low_stock_after`) `low_stock_alerts(db, items, domain)` →
`notify_low_stock(*t)`; grup yeterliyse sessiz, değilse grup başına TEK uyarı;
hesap commit SONRASI try/except içinde (bildirim hatası başarılı işi 500 yapmasın).
Asistan `item_detail.group` grup toplamı + döküm, `stock_gaps` grup satırı.
Testler `tests/test_group_stock.py`, `tests/test_group_stock_reports.py`.

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
(`core/production_cancel`) B2B partisinin üretimini ENGELLER. **Kaynak seçimi
otomatik** (13.10.2026): BAŞLAT `plan(..., auto_sources=True)` — cevapsız
"Hangi tedarikçiden?" satırı öneri sırasını alır (bitirilecek/alma → reçete kartı
→ tercih → diğer; havuz `lot_filter`'a uyar — yalnız sıkı lotlar; satırlar arası
ortak), karşılanamayan kısım süzgeçsiz kart stoğu (planın öteki satırlarının
payı düşülmüş) onu karşılayan İLK seçenekte kalır → `lot_missing` o kartı (SKT/QC
sorunlu kardeşi) adıyla söyler; hiçbiri karşılamazsa reçete kartında →
`insufficient_stock` (eksikler yenilenir). Kararlar
`plan_snapshot["sources"]` + `batch_start` audit'inde (`batch_sources`),
`order_detail` partilerinde `sources`; iç teknik föy "X (reçetede: Y)". İptal
iadesi kaynak kartın ilk lotuna, TAMAMLA `ProductionConsumption.recipe_item_id`
= reçete kartı. Ayar `0` → seçenek yalnız reçete kartı (eski davranış).
**Tek sevkiyat**: açık parti yok +
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
**Sertleştirme (2026-09-24, Hetzner):** uvicorn artık ayrı `minerva` kullanıcısıyla,
systemd sandbox'ı içinde çalışıyor (`ProtectSystem=strict`, yazma yalnız
`/var/www/minerva`); Turhost'taki `User=root` açığı taşımayla kapandı.

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

**Minerva Asistan** (`core/assistant/`, `routers/assistant.py`, `static/assistant.js`,
`docs/assistant/*.md`, `templates/admin.html` kartı) — IMS içi, **yalnız okuma** yapan
Claude tabanlı soru-cevap + nasıl-yapılır asistanı (sağ alttaki düğme; 28 iç sayfa,
public/kiosk/portal yollarında yok). SDK `anthropic==0.125.0` (son Py3.9 uyumlu
seri; typed olmayan alanlar `extra_body`), model varsayılan Opus 5.5 / efor `low`.

- **Mimari.** `settings.py` (AppSetting `assistant.*`, `Settings` frozen dataclass) ·
  `client.py` (süreç başına TEK `AsyncAnthropic`, anahtar YALNIZ `ANTHROPIC_API_KEY`
  env'den çağrı anında; `max_retries=1`, connect 5 s / read 60 s) · `prompt.py`
  (DONDURULMUŞ Türkçe kurallar + `<rehber>` bloğu = `00_giris.md` gövdesi (menü
  haritası + panel değiştirme) + 01..17 konu DİZİNİ `- id: başlık` → `SYSTEM_BLOCKS`;
  `GUIDE_MAX_CHARS=4500`, konu başına `HELP_DOC_MAX_CHARS=2800` testle kilitli) ·
  `help_docs.py` (DB'siz rehber yükleme + `search_help` araması: id tam eşleşme,
  TR-katlı ağırlıklı puan, ≤2 konu tam metin + ≤4 aday; birinci konu kırpılmaz) ·
  `tools.py` + `handlers.py` (15 araç,
  `REGISTRY` ada göre sıralı, strict şema, araç başına any-of izin, her araç kendi
  `SessionLocal()`'ını thread'de açar, çıktı ≤ `MAX_RESULT_CHARS`) · `budget.py`
  (`PRICES`, `compute_cost`, ay/gün toplamları TR takvimde, `check_budget`,
  `record_usage`, `maybe_warn`) · `history.py` (sohbet/mesaj kalıcılığı, `api_messages`
  replay, ROTASYON) · `runner.py` (async döngü, NDJSON olayları, 150 s tur sınırı).
  `POST /api/assistant/chat` NDJSON akışı (`meta` → `ping`/`tool`/`token` → `done`|`error`);
  `Depends(get_db)` oturumu akış başlamadan kapanır → üreteç `asyncio.Queue` + `run_turn`
  görevi, kalıcılık (`_persist`: satırlar + çağrı başına `assistant_usage` + rotasyon
  alanları) `done`/`error` sonrası YENİ `SessionLocal()` ile ve THREAD'de; bağlantı
  kopsa da model çağrısı iptal edilmez, bütçeye yazılır. Tablolar
  `assistant_conversations` / `assistant_messages` / `assistant_usage` (create_all +
  Alembic `b4d6f8a0c2e4`); yetki kategorisi `assistant.use` (iç roller True, Distributor
  False; override'lı hesaplara `_backfill_perm_assistant` sentinel'i dağıtır).
  SDK yalnız sohbet ucunda TEMBEL import edilir (`api_main` import'u `anthropic`'i yüklemez).
- **Rehber `search_help` aracında, önekte DEĞİL.** 13.10.2026 `count_tokens` ölçümü:
  önek 19.661 token (SSS bloğu 14.196, araçlar 3.533, kurallar 1.924; Türkçe ≈1,8
  karakter/token), soğuk 1h yazma ≈0,16 $ → konu metinleri araca taşındı, önek ≈7K
  (araçlar ~3,9K + kurallar ~2K + rehber ~1,3K). Yeni konu = `docs/assistant/NN_id.md`
  (`# Başlık`, `Yetki:`, `Sayfa:`, gövde) — dizine kendiliğinden girer, `PROMPT_HASH`
  değişir (rotasyon); `tests/test_assistant_tools.py::HELP_CASES`'e gerçekçi bir soru ekle.
- **Önbellek disiplini (prompt-caching.md).** İstek sırası tools → system → messages;
  araçlar ve sistem istemi her istekte byte-aynı (tarih/isim/panel/rol istemde YOK —
  `PROMPT_HASH` sohbete yazılır, değişince rotasyon). Son sistem bloğunda
  `cache_control ephemeral ttl=1h` + üst düzey otomatik 5 dk kuyruk. Oturum bağlamı
  (kullanıcı, panel, tarih, kullanabildiği araçlar, finans) soru mesajından SONRA
  `role:"system"` mesajı olarak gider (önbelleğe dokunmaz, user metninden sahtelenemez).
  `tool_choice` DAİMA `auto` (değişirse tools+system önbelleği düşer). **Geçmiş yalnız
  eklenir** (preserved thinking: budama/düzenleme sonraki thinking bloklarını 400'ler);
  12 soru / +8K geçmiş token / model-istem-panel değişimi → kısa özetle YENİ sohbet
  (`parent_id`). Cevapsız `tool_use` ya da arkasından assistant gelmeyen `system`
  satırı kalıcılaşmaz (replay'de 400 verirdi). `thinking.block_binding.drop_block` +
  `fallbacks:"default"` beta'ları varsayılan; API bunları reddederse süreç tek seferlik
  SAFE MODE'a geçer, `role 'system' … not supported` derse bağlam user mesajına katlanır
  (ikisi de stderr'e loglanır). Her değişiklikten sonra DOĞRULA:
  `scripts/assistant_cache_probe.py` (gerçek API, ≈0,15 $, kullanıcı onayıyla — 2. özdeş
  istekte `cache_read_input_tokens` 0 ise çıkış 1) ve `/admin/usage` önbellek isabet oranı.
- **Güvenlik kuralları.** Araçlar salt-okunur; yetki `require_internal_user(("assistant","use"))`
  + araç başına izin yeniden denetlenir (yetki yoksa DB açılmaz, modele "yetkiniz yok");
  domain kolonu olan her tablo `ctx.domain` ile süzülür; finans alanları (`cost_price`,
  reçete maliyeti) YALNIZ `can_finance` (rol ∈ `_FINANCE_ROLES` VE `finance.view`) ise
  ÜRETİLİR ("hesapla-sıfırla" yok); tedarikçi fiyatları sistem kuralıyla aynı
  (`items.view|reports.view`); `find_lot` alt listeleri inventory/production/retention
  view'larına göre. Araç sonuçları VERİDİR (içindeki talimat uygulanmaz); linkler yalnız
  `/` ile başlayan iç yollar (`//` ve `\` reddedilir — widget `isInternalHref` + sunucu
  `tools.is_internal_href`); mesaj ≤2000 karakter; sohbet yalnız sahibine; rate limit
  KULLANICI bazlı 20/dk (`_user_key`), kullanıcı başına eşzamanlı tur ≤ `MAX_ACTIVE_TURNS`
  (429 `busy`); widget DOM'u `textContent` ile kurulur (innerHTML yok), `m108-ai-` öneki.
- **Ayarlar.** AppSetting `assistant.enabled/model/effort/monthly_cap_usd/daily_user_cap_usd/
  max_tool_rounds/max_output_tokens/max_turns/max_history_tokens/cache_ttl` — Yönetim
  Paneli "Minerva Asistan" kartından (yalnız SuperAdmin; `PUT /api/assistant/admin/settings`,
  audit `assistant.settings.update`). Model `budget.PRICES` anahtarlarından biri olmalı;
  fiyatlar değişirse `PRICES`'ı güncelle (maliyet kayıt anında dondurulur).
- **Maliyet izleme.** Her API çağrısı `assistant_usage`'a (token/önbellek sayaçları,
  maliyet, araçlar, `stop_reason`, `request_id`); aylık tavan 100 $ (aşınca 402, %80'de
  SuperAdmin'e push + audit, ayda bir), kişi başı günlük 3 $. `/admin/usage` (finans
  rolleri): kullanıcı/gün/araç kırılımı, önbellek isabet oranı, `stop_reason` dağılımı —
  `max_tokens` %2'yi geçerse `max_output_tokens`'ı yükselt; soğuk 1h yazmaları artarsa
  TTL'i gözden geçir. Beklenen: ay ≈ 30 $ (günde ~30 soru).
- **Test.** SDK tamamen sahte (`tests/assistant_fakes.py`, ağ YOK, gerçek anahtar
  gerekmez); `tests/test_assistant_{tools,runner,api,budget_history,rbac,widget}.py`.
  Deploy öncesi canlı `.env`'e `ANTHROPIC_API_KEY` eklenir (sohbete yapıştırılmaz);
  tablolar `init_db()` ile gelir, alembic için `stamp head`.

**Trendyol pazaryeri — Faz 1, YALNIZ OKUMA** (`/trendyol`; `core/trendyol.py`,
`routers/trendyol.py`, `templates/trendyol.html` + `static/trendyol.js`, 13.10.2026).
TPro360 gibi SaaS yerine kendi mağazamızın verisi: tek satıcı hesabından siparişler,
cari hesap ekstresi (hakediş + diğer finans), kargo faturası kalemleri ve onaylı katalog
(stok + fiyat) çekilir.  **Trendyol'a hiçbir şey yazılmaz** — `Api` sınıfının HTTP
metotları `get` + izin listeli `query` (yalnız buybox SORGUSU POST'u; aşağıda satış
artırma) — `test_api_is_read_only_by_construction` kilitler; IMS `current_stock` ve
`Transaction` defteri bu modülden ETKİLENMEZ.  Rakip/pazar verisi (scraping) kapsam dışı.
Kâr motoru (`core/trendyol_profit.py`, Özet/Ürün Kârlılığı sekmeleri) Teslimat B'dir.

- **Yapılandırma** env'den çağrı anında: `TRENDYOL_SELLER_ID` (yalnız rakam),
  `TRENDYOL_API_KEY`, `TRENDYOL_API_SECRET`; isteğe bağlı `TRENDYOL_BASE_URL` (https,
  varsayılan `https://apigw.trendyol.com/integration`) ve `TRENDYOL_STOREFRONT_CODE`.
  Değerler satıcı panelinde yalnız ana kullanıcıya açık (Hesap Bilgilerim → Entegrasyon
  Bilgileri); DB'ye yazılmaz, loglanmaz, hata metnine girmez, UI yalnız eksik ADLARI
  gösterir.  Basic auth + `User-Agent: "{satıcı id} - SelfIntegration"` (yoksa 403).
  Canlıda IP izni gerekmez; stage ortamı (IP izinli, ayrı anahtar) KULLANILMAZ.
  systemd `.env`'i yalnız restart'ta okur ve işler açılışta kaydedilir → anahtar
  eklendikten sonra restart (deploy) şart.
- **Uçlar (13.10.2026):** eski `/orders` ve Product V1 **15.10.2026'da kapanıyor** —
  yalnız `orders/stream`, `finance/che/.../settlements|otherfinancials|cargo-invoice`,
  `products/approved` + `products/approved/inventory-and-price` kullanılır.
  **TUZAK — `products/approved` stok ve komisyon VERMEZ**; stok/fiyat ayrı
  `inventory-and-price` ucundan sayfalı çekilip barkodla birleştirilir.
  **TUZAK — hakediş yanıtındaki `transactionType` Türkçe ETİKETTİR** ("Satış", "İade"):
  her tür AYRI istenir, `type_param` = istenen enum, kova eşlemesi enum'dan.
  **TUZAK — sipariş satırı tutarları BİRİMDİR** (`lineGrossAmount`, `lineUnitPrice`);
  toplamlar `discountDetails`'ten (birim başına kayıt), yoksa birim × adet.
  `commission` satırda ORANDIR, tutar değil (kesin tutar hakediş satırında).
- **Senkron** (`run(kind)` — orders | finance | catalog | questions | all; süreç içi
  `threading.Lock`, elle/backfill/zamanlayıcı aynı kilitten geçer, dolu ise
  `{"skipped": "running"}` / uçta 409).  Siparişler: lastModified penceresi ≤13 gün,
  ilk çalıştırma 89 gün geriye (stream verisi ~3 ay), sonra watermark − 6 sa örtüşme;
  `lookback_days` başlangıcı yalnız DAHA GERİYE çeker — watermark'ın gerisindeki
  taranmamış boşluğun üstünden atlatmaz; her sayfadan sonra commit, watermark yalnız
  TAMAMLANAN pencereyle ve yalnız ileri (restart kaldığı yerden sürer, backfill geri
  sarmaz); aynı imleç tekrar ederse durur; eski `lastModified` yeniyi ezmez; satırlar
  yerinde güncellenir.  **Yanıt doğrulaması:** 3xx (yönlendirme izlenmez) ve `content`
  alanı olmayan gövde HATADIR — "boş sayfa" sayılsaydı pencere tamamlanmış sayılıp
  paketler sessizce kaçardı (yalnız 204/boş gövde boş sayfadır).  Finans: tür × 14
  günlük pencere, EN YENİDEN eskiye; en yeni pencerede 400 = tür kabul edilmedi
  (`rejected`), eski pencerede 400 = geçmiş sınırı; hiçbir tür kabul edilmezse akış
  HATA.  Kapsam: ilk çalıştırma 90 gün, sonrası **son BAŞARIDAN bu yana gün + 45**
  (uzun kesinti/duraklatma sonrası boşluk kalmaz; açıkça istenen gün bunun altına
  inemez), en fazla 365.  Aynı id iki türün sorgusundan gelirse ilk kayıt OLDUĞU GİBİ
  kalır (`conflict` sayacı).  `DeductionInvoices` bir kez de
  `transactionSubType=PlatformServiceFee` ile çekilip `sub_type` etiketlenir (sonraki
  genel çekim silmez).  Etiketi "Kargo Fatura(sı)" olan satırın id'si fatura serisidir;
  kalemleri bir kez çekilir (`detail_fetched_at`); hata FATURA BAŞINA yalıtılır —
  400/404/422 damgalanıp atlanır, geçici hata (5xx/429/ağ) yalnız o faturayı sonraki
  çalıştırmaya bırakır, kimlik hatası akışı durdurur.  Katalog: görülmeyen ürün
  silinmez (`last_seen_at` eski kalır, görünümde adedi karşılaştırılmaz); stok
  yanıtında olmayan varyantın adedi `None` yazılır (bayat stok yanlış "fazla stok"
  uyarısı üretmesin).  Bölünen paketin aslı (`UnPacked`) liste/toplam/sayımlara
  girmez (yalnız durum süzgecinde ayrıca seçilirse görünür).
- **Kapanış — TUZAK.**  Elle senkron/geçmiş çekme istekten BAĞIMSIZ daemon iş
  parçacığında (`start_in_background`), **FastAPI BackgroundTasks DEĞİL**: uvicorn
  kapanışta süren arka plan görevini süresiz bekler, 365 günlük bir geçmiş çekimi
  deploy'u systemd'nin 90 sn'lik SIGKILL'ine kadar ERP kesintisine çevirirdi.
  `api_main` shutdown'ı `request_stop()` çağırır: süren iş (zamanlayıcınınki dahil)
  bir istek ya da bekleme içinde `TrendyolStopped` ile durur — bu HATA SAYILMAZ
  (ardışık hata sayacı ve bildirim yok), watermark sayesinde sonraki çalıştırma
  kaldığı yerden sürer; startup `reset_stop()` ile bayrağı temizler.  Testlerde her
  TestClient kapanışı bayrağı açar → Trendyol test modülleri
  `trendyol_stop_reset` (autouse, `tests/trendyol_fakes.py`) import eder.
- **Zaman:** Trendyol epoch ms ("GMT+3" notlu) → tek dönüşüm `ty_ts`/`to_ty_ms`;
  `TS_TR_WALLCLOCK` (varsayılan False = standart epoch) ilk gerçek siparişin saati
  panelle karşılaştırılarak kalibre edilir (`/probe`).
- **Tablolar** `trendyol_packages`/`_lines`/`_fin_rows`/`_cargo_items`/`_products`/
  `_barcode_map`/`_sync_state` — create_all (migration `c3e5a7b9d1f2` kayıt için).
  **KVKK:** parser alan beyaz listesiyle çalışır; müşteri adı/adresi/telefonu/e-postası/
  vergi no ve ham JSON SAKLANMAZ (`test_parse_package_whitelists_and_totals`).
- **Cross-cutting — domain kuralının BİLİNÇLİ istisnası.**  Tek satıcı hesabı iki
  panelin ürününü birlikte satar, kargo/ücret panele bölünemez: tablolarda `domain`
  yok, `active_domain` cookie'si yok sayılır; "Panel" süzgeci eşleşen kartın
  domain'inden türetilir (Kozmetik / Food Supplement / Karma / Eşleşmemiş).
- **Eşleştirme** (`Resolver`, satırlarda `item_id` YOK — her istekte çözülür):
  elle eşleme (`trendyol_barcode_map`; çok satır = set ürün, bileşen × adet) →
  aktif "Bitmiş Ürün" kartlarında (iki panel) TEK birebir barkod → ≥2 kart =
  "Belirsiz" (asla tahmin yok, Shopify ile aynı) → yoksa "Eşleşmedi"
  (`data/barcode_aliases.json` eski barkodu ise yalnız ÖNERİ).  Elle eşlenen kart
  pasifleşirse "Eşleme geçersiz".  Set stoğu = min ⌊stok / adet⌋.  Eşleme yalnız aktif
  bitmiş ürüne; audit `trendyol.mapping` / `trendyol.mapping_delete`.
- **RBAC** kategori `trendyol` (`view` / `profit` / `manage`; Manager üçü, diğer roller
  hiçbiri; Distributor `require_internal_user` ile 403).  **Kâr/maliyet alanları**
  (satır komisyon oranı, kart maliyeti, finans/kargo satırları, kalibrasyon) yalnız
  `core.trendyol.can_profit` = `trendyol.profit` + rol ∈ `_FINANCE_ROLES` +
  `finance.view` (asistan kuralı) — koşul yoksa alan yanıtta HİÇ yoktur.  Override'lı
  kullanıcılara `_backfill_perm_trendyol` (sentinel `backfill.perm.trendyol.v1`) rol
  varsayılanını yalnız override'da `finance.view` varsa yazar.  `/probe` (maskeli ham
  örnek) yalnız SuperAdmin.
- **Zamanlayıcı** (yalnız env tanımlıysa kaydolur; UI'daki "Otomatik senkron"
  AppSetting `trendyol.enabled` deploysuz durdurur): `trendyol_orders_sync` dakika
  7,37 · `trendyol_catalog_sync` dakika 52 · `trendyol_questions_sync` dakika 22 ·
  `trendyol_finance_sync` 02:23 UTC (05:23 TR).
  Bildirim (`alerts_due` → `notify_trendyol_sync_failure`, yöneticilere): kimlik hatası
  ilk seferde, orders/catalog üst üste 3 hatada, finans ilk hatada; en sık 24 sa'te bir;
  başarı sayacı sessizce sıfırlar.
- **Nav** `scripts/add_trendyol_nav.py` (idempotent, Satın Alma linkinin altı,
  `trendyol.view` kapılı; CRM atlanır).  Testler `tests/test_trendyol_{client,sync,api}.py`
  (`tests/trendyol_fakes.py` — sahte API, AĞ YOK).
- **Yayına alma:** anahtarlar prod `.env`'e (sohbete yapıştırılmaz) → `./deploy.sh` →
  `alembic stamp head` → ilk çalıştırmalar 89 gün sipariş + 90 gün finansı kendiliğinden
  çeker; durum Bağlantı sekmesinde.  Kalibrasyon (Teslimat B'den önce): `/test`,
  `/probe` ile alan adları + zaman damgası, kapanmış bir ayın Kalibrasyon toplamlarını
  panelden indirilen Cari Hesap Ekstresi ile tür tür karşılaştır, kargo faturası
  "oran"ı (fatura/kalemler) ile KDV durumunu teyit et, KDV varsayımlarını muhasebeciyle
  doğrula.

**Trendyol satış artırma — Liste Doktoru · Buybox · Müşteri soruları** (13.10.2026;
spec `docs/superpowers/specs/2026-10-13-trendyol-satis-artirma-design.md`).  Kendi
verimiz + yapay zekâ; kazıma YOK; IMS yine Trendyol'a YAZMAZ — öneri/taslak kopyalanıp
panele elle girilir.  Yeni yetki yok: `trendyol.view` görür, `trendyol.manage` üretir
ve bilgi notunu düzenler.  Panel raporu yükleme (görüntülenme/reklam kelimeleri) 2.
parça — örnek Excel'ler bekleniyor.

- **Tek POST istisnası:** `Api.query` yalnız `QUERY_PATHS` izin listesine (buybox
  SORGUSU `…/products/buybox-information`) POST atar, aksi `ValueError`;
  `test_api_is_read_only_by_construction` kaynakta tam bir `self._http.post(` olduğunu
  ve `query` içinde durduğunu kilitler.  `tests/trendyol_fakes.FakeApi.query` aynı
  denetimi yapar.
- **Katalog akışı genişledi:** içerik (açıklama ≤30000, içerik+varyant özellikleri
  `parse_attributes`, görsel sayısı, kategori, teslimat süresi) + `content_hash` →
  ardından `sync_category_attrs` (haftalık; listeden çıkan özellik silinir; izinli
  değerler `ensure_attribute_values` ile TEMBEL, haftalık önbellek) + `sync_buybox`
  (20 sa'ten taze ürün sorulmaz, 10'arlı; kayıpta `notify_trendyol_buybox_lost`, ürün
  başına 24 sa'te bir — `buybox_alerted_at`) + `trendyol_boost.snapshot_scores`
  (günlük puan, `trendyol_score_history`).  Alt işin geçici hatası katalog akışını
  düşürmez (sonuçta `error`), kimlik hatası/kapanış düşürür.
- **Sorular akışı** (`run('questions')`, zamanlayıcı dakika 22): 13 günlük pencere,
  günde bir (ve ilk çalıştırmada) 90 gün; `customerId`/`userName` SAKLANMAZ; barkod
  `product_main_id`'den; yeni `WAITING_FOR_ANSWER` soru yöneticilere TEK push
  (`notified_at`, url `/trendyol#sorular`).  Taslak yalnız bekleyen soruya (aksi 400).
- **Kural puanı** `core/trendyol_listing.py` (SAF; ağırlıklar `WEIGHTS` tek yer:
  başlık 20 · özellik 20 · açıklama 15 · görsel 15 · fiyat 10 · teslimat 10 · durum 10;
  süzgeç özelliği ×2; ilk 3 düzeltme kazanca göre).  Metin süzgeçleri
  (`claim_flags`, `promo_words_in`, `unverified_claims`) yapay zekâ denetimiyle ORTAK.
  **TUZAK — kanıt kalıpları boşluklu yazılır** (`dermatolojik(?:\s+olarak)?\s+test`):
  eski `dermatolojik(olarak)? test` "dermatolojik olarak test"i yakalamıyordu.
  DB görünümleri `core/trendyol_boost.py`; `current_products` = `last_seen_at ==
  max` (senkron hepsine aynı `now`'u yazar — test tohumları aynı zamanı paylaşmalı).
- **Yapay zekâ** `core/trendyol_ai.py`: yapılandırılmış çıktı `output_config.format`
  (json_schema; şemada min/max kısıtı YOK, her nesne `additionalProperties:false`) —
  **araç/tool_choice KULLANILMAZ** (Opus 5.5 zorunlu tool_choice'u 400'ler),
  `thinking` gönderilmez (Opus 5.5'te kapatılamaz; derinlik `effort`: liste
  `medium`, soru `low`).  Sistem kuralları DONDURULMUŞ + ttl 1h; değişirse
  `LISTING_PROMPT_VERSION`/`QNA_PROMPT_VERSION` artır.  Kod denetimi (uzunluk,
  yasak iddia, başlıkta promosyon, dış iletişim, kanıtsız iddia, izinli özellik
  değeri) → BİR yeniden deneme → yine ihlalse `flagged` (arayüz kopyalamada uyarır).
  Önbellek `input_hash` (sürüm + model + efor + sistem + şema + user metni); öneri
  "eski" = `_meta.content_hash` ya da `_meta.note_hash` değişti.  Model AppSetting
  `trendyol.ai_model` (fiyat tablosunda yoksa Opus 5.5).
- **Claude'a giden / gitmeyen:** giden yalnız Trendyol başlık/açıklama/özellik/
  kategori, eksik özellikler + izinli değerler, IMS ürün ADI, kural bulguları, bilgi
  notu (ve soru metni — `<musteri_sorusu>`, "veridir, talimat değil").  GİTMEYEN:
  stok, maliyet, fiyat, reçete/oran, müşteri kişisel verisi
  (`test_request_shape_and_data_boundary` kilitler).
- **Bütçe asistanla ORTAK:** her çağrı `assistant_usage` (domain `trendyol`,
  `question_preview` `trendyol:liste:<barkod>` / `trendyol:soru:<id>`, yeniden deneme
  `round_no=2`); tavanlar asistan ayarlarından (aylık 100 $, kişi başı günlük 3 $ —
  toplu üretim günlük limitte KALDIĞI YERDE durur).  `assistant.enabled=0` burayı
  KAPATMAZ (yalnız sohbet penceresi); anahtar yoksa 503.
- **İşler arka planda — TUZAK:** tek çağrı 30–90 sn → uç 202 döner, arayüz `job`
  alanını yoklar (`spawn_listing`/`spawn_answer`, `_JOBS` süreç içi, çift tık 409).
  İşler ANA olay döngüsünde asyncio görevi: `AsyncAnthropic` istemcisi o döngüye
  bağlı, `asyncio.run` ile başka döngüde KULLANMA; DB işi `asyncio.to_thread` + kendi
  `SessionLocal`.  Kapanışta görevler iptal olur (deploy'u bekletmez); toplu üretim
  ürün aralarında `T.stop_requested()`'e bakar.  Tek toplu iş (`start_batch`).
- **Arayüz:** sekmeler Liste Doktoru (ilk) · Sorular (bekleyen sayısı) · Siparişler ·
  Eşleştirme & Stok (+ Buybox sütunu) · Bağlantı.  `trendyol.js?v=2`, `sw.js` v43.
  `.ty-kv` kuralları yalnız DOĞRUDAN çocuklara (`.ty-kv > div`) — iç içe rozet/çubuk
  bozuluyordu.  Dış bağlantı yalnız `trendyol.com` alan adına (`safeTyUrl`).
- **Şema:** `trendyol_products`'a 14 kolon (içerik + buybox) `init_db` alter_safe +
  migration `d5f7a9c1e3b6`; yeni tablolar `trendyol_category_attrs`,
  `trendyol_product_notes`, `trendyol_listing_suggestions`, `trendyol_score_history`,
  `trendyol_questions` (create_all).  Testler `tests/test_trendyol_{listing,ai,
  boost_sync}.py`.

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
  (`is_sample`) hiçbir tüketimde kullanılmaz. Üretim planlayıcısı
  (`core/production_plan`) da aynı motoru çağırır (`plan_fifo(..., reserved=)`)
  — FIFO'nun ikinci bir kopyası YAZILMAZ.
  **Bilinçli açık:** girişler (iade, '+' düzeltme) lot AÇMAZ; stok lottan fazla
  kalabilir, `uncovered` bunu sorunsuz karşılar. `tests/test_stock_lots.py`
- **Samples (numune)**: `Inventory.is_sample=True` marks a lot received from an
  *alternate* supplier for an existing raw material (entered from the Items page
  "Numune" tab → `/api/inventory/receive` with `is_sample`). Sample lots never merge
  with normal lots (upsert key includes `is_sample`, but NOT `supplier_id` — a second
  sample on the same lot number merges into the first, supplier is COALESCE-only).
  **Samples are NOT stock** (2026-08-24 fix): `current_stock` is untouched and no
  `Transaction` is written on sample receive — only the `Inventory` row + an
  `admin_audit_log` entry. Production's lot pool (`core/production_plan._lot_query`,
  `/api/inventory/available-lots`) excludes
  `is_sample=True` lots entirely, so a sample can never be silently consumed by
  FIFO or picked by id. To promote a sample into real stock, use
  `POST /api/inventory/samples/{id}/convert` — that's the ONE place a sample
  Inventory row gets an `Input` Transaction + `current_stock` bump; it flips
  `is_sample=False` (or merges into a same-lot normal row if one exists).
  **Second consumer of sample quantity (2026-09-09): the Numune Analizi form.**
  `sample_analysis_ingredients` rows (`routers/sample_analysis.py`, engine
  `core/sample_trial_stock.py`) record per-ingredient source — `sample` (a
  specific `is_sample` lot: only `Inventory.quantity` drops, no Transaction, no
  `current_stock`), `stock` (`stock_lots.consume` → FIFO + Output per lot +
  `current_stock`; short stock = 400) or `pending` (no effect). `consumed_qty`
  is the amount physically taken; PUT applies only the delta, DELETE releases
  everything (sample → quantity back; stock → signed `+Adjustment`, Output rows
  untouched, no lot re-opened). `inventory_id` FK is `ondelete=SET NULL` because
  `convert` deletes a sample row when merging. `sample_analyses.mode`
  (`existing`/`new`) is added via `init_db` alter_safe + migration c7e9a1b3d5f7.
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
  decrement (`core/production_plan._allocate_lots`). Available lots:
  `POST /api/inventory/available-lots`.
- **Üretimde "Hangi tedarikçiden?"** (P2, 08.10.2026; `core/production_plan.py`,
  `POST /api/production/preview` perm `production.create` + `active_domain`).
  Önizleme ve başlatma TEK planlayıcıyı çağırır (`plan(..., lock=)`; satırlar
  `expand_recipe` — aynı kart birleşir, `parts` reçete satırlarını tutar ve
  yazımda her reçete satırı eskisi gibi kendi Output'unu/fazını alır).
  Hammadde/ambalaj satırının kartı AKTİF bir "aynı malzeme" grubundaysa
  seçenekler = reçete kartı + gruptaki aktif, aynı panel/tür/birim
  (`group_stock.can_substitute`) kartlar; reçete kartı dışındaki birinde
  stok varsa `needs_choice` ve
  başlatma `ingredient_sources {reçete kartı id: [{item_id, quantity,
  inventory_id?}]}` ister (yoksa 400 `source_choice_required`, hiçbir şey
  yazılmaz). Bölme serbest; toplam = brüt (tolerans içinde son giriş kalana
  oturur). Öneri ortak havuzla: bitirilecek/alma → reçete kartı → tercih →
  diğer. Kart başına TOPLAM stok kapısı (eskiden satır başınaydı — çift satır
  taşıyordu) ve `reserved` ile aynı lot iki satıra verilmez. Not: kart
  farklıysa "Tedarikçi:"den önce `| Kaynak kart: X (reçetede: Y)`, son ek
  `Üretim Lot: {lot}` SÖZLEŞME. Döküm `recipe_item_id` ≠ `item_id`; föy
  dökümden kurulur (reçete değişse/silinse de aynı; `sources` alt satırları,
  Excel'de "Tüketim Kaynakları" sayfası). Eski `ingredient_lot_choices`
  geriye uyumlu ama seçim gereken satırı cevaplamaz. **Kill switch:**
  `INSERT INTO app_setting(key,value) VALUES ('production.source_choice.enabled','0')
  ON CONFLICT (key) DO UPDATE SET value='0'` → seçenek yalnız reçete kartı
  (eski davranış, deploysuz). Hata kodları: `source_choice_required`,
  `invalid_source`, `lot_not_found`, `lot_insufficient`, `insufficient_stock`
  (400 + `errors` + `lines`). Testler `tests/test_production_sources.py`.
  08.10.2026'da bu ayar canlıda `0`'a çekilmişti (lab eşdeğerlik teyidi
  beklenirken); **13.10.2026 kullanıcı kararıyla `1`** (grup = eşdeğer malzeme,
  deploy sonrası `pg_dump` + yukarıdaki INSERT'in `'1'`li hâli). Aynı anahtar
  `core.group_stock.group_stock_enabled` olarak grup toplamını da yönetir:
  Satın Alma grup/bitirilecek stok düşümü, Eksik Hammaddeler + dashboard grup
  kuralı ve düşük stok push bastırma ayar `0` iken eski kart başı davranışa
  döner (Satın Alma `source_choice_disabled` uyarısı verir, kaydedilmiş
  senaryonun tercihini değiştirmez); Stoklar/Aylık rapor yine gruplu görünür.
  Seçenek kartı `group_stock.can_substitute` ile süzülür: aynı tüketim türü
  (etiket stoğu şişe ihtiyacını karşılayamaz), aynı `lot_kind`, BİREBİR aynı
  `unit_norm`. `plan(auto_sources=True)` (yalnız B2B BAŞLAT) cevapsız seçim
  satırını öneri sırasıyla doldurur (`Line.auto`, önizlemede `"auto"`);
  gönderilen `sources` DAİMA önceliklidir, parametresiz çağrı eskisi gibi
  `source_choice_required` verir.
- **Üretimi iptal et** (`core/production_cancel.py`; `GET /api/production/{id}/
  cancel-preview` + `POST …/cancel {reason, fingerprint, release_lot}`, perm
  `production.cancel`, 07.10.2026). `start_production` her Output/Input'u
  `production_consumptions`'a döker (source='live'); iptal her satıra +Adjustment
  (kaynak lota iade), bitmiş ürüne Input'u aynalayan TEK −Adjustment yazar, lotlar
  `CANCELLED`/0, şahit pasif — **hiçbir Transaction silinmez**. Önizleme ile POST
  arası değişiklik `fingerprint` ile 409 `preview_stale`. Dökümü olmayan **eski
  üretim** defter imzasıyla kurulur (Output notu `"Üretim tüketimi — Reçete: {ad} | "`
  ile başlar + `"Üretim Lot: {lot}"` ile biter, aynı kişi, `produced_at −10/+1 dk`)
  ve iptalde `source='ledger'` olarak kalıcılaşır. **TUZAK — imza tekil değil**:
  `Recipe.name` benzersiz değil + lot sayacı ürün bazlı → aynı kişinin aynı
  dakikalarda aynı reçete adı + aynı lot no'lu iki eski üretimi ayrılamaz; bu
  durum ENGELDİR (`_ambiguity`), tahmin YAPILMAZ. Lot satırını silip ikizine katan
  her yol (`stock_lots.absorb_row`) dökümü de ikize yönlendirmeli (FK SET NULL).
  Pasif karta iade yapılmaz (engel). Kilit sırası kart → lot → şahit kaydı; QC
  kararı ve şahit çıkış/imha da bu sırayla kilitler (bayat okumayla çift düşüm
  olmasın). Lot no ürün bazlı olduğundan `trace_lot` lot satırını ve üretimi aynı
  ürüne bağlar (`?item_id=`, `other_items`). Testler `tests/test_production_cancel.py`.
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
  **Fiyat temeli** (2026-10-05): fiyat `currency` + `price_unit` (`kg`|`l`|`adet`)
  ile birlikte okunur (+ `source`/`source_label`/`quoted_at`; migration
  `a3c5e7f9b1d4` + `init_db` alter_safe). Stok Son Durum listesi **USD/kg**'dir —
  eski satırlar model varsayılanıyla yanlışlıkla TRY + birimsiz yazılmıştı;
  `_backfill_supplier_price_units()` (sentinel `backfill.supplier_price_units.v1`)
  bir kez USD + kg (adet birimli malzemede `adet`) işaretledi. İçe aktarma formu
  `currency` / `price_unit` / `label` / `quoted_at` alır ve `supplier_prices.import`
  audit'i yazar; panel kayıttaki para birimini/birimi gösterir (sabit ₺ değil).
  `price_per_purchase_unit(offer, item_unit)`: g/kg kalemi kg'dan, ml/l kalemi
  litreden, adet adetten fiyatlanır; kg fiyatı ml kalemine uygulanırsa "1 l ≈ 1 kg
  kabulüyle" notu düşer. **Kur `core/fx.py`** (TCMB ForexSelling, 1 saat önbellek →
  bayat önbellek → sabit yedek, uyarılı; `today_rates()` / `convert()`, testlerde
  fetcher enjekte edilir — ağa çıkılmaz); `b2b.get_currency_rates` aynı yanıtı
  veren ince sarmalayıcıdır.
  **KDV** (08.10.2026): `supplier_prices.vat_included` (NULL = bilinmiyor,
  False = hariç, True = KDV DAHİL brüt) + `vat_rate` (%; migration
  `d8f0b2c4e6a9` + `init_db` alter_safe). Karşılaştırma/tutar NET fiyatla
  (`supplier_prices.net_unit_price`): satın alma planı seçimi + tutarı, Raporlar
  paneli sırası, `prices_for_items`. KDV dahil satır her yerde etiketli
  (`vat_label` → "KDV %20 dahil"; planda "… fiyattan düşüldü", parantezde brüt).
  Elle fiyat formu/uçları alanları opsiyonel taşır; dahilse oran zorunlu (400).
  İki alan boşken her çıktı eskisiyle birebir aynı (testli).
  **Lab fiyat notları** (`scripts/import_lab_price_notes_20261007.py`, veri
  `scripts/data/lab_fiyat_notlari_202606.json`): kuru varsayılan; `--xlsx`
  kontrol Excel'i (Songül Hanım'ın onayı) · `--onayli ONAYLI.xlsx --commit
  [--create-suppliers]` (onaylı Excel'siz `--commit` ÇALIŞMAZ) `source`
  lab_notu|proforma|fatura|siparis, `source_label` "Lab fiyat notları
  07.10.2026 — s.N" önekli satırları yeniden yazar (idempotent; manual /
  stok_son_durum'a dokunmaz, aynı firmanın elle fiyatı varsa yazmaz; (kart,
  firma) başına tek kayıt, en yeni belge > lab tablosu; `cizili: true` belge
  satırı ve `tur: fiyat_listesi` (s.11 Doalin — plan: D yüklenmez) yazılmaz,
  yarışmaz; belge, ipucu satırında AYNI firmanın kaydı hangi karttaysa oraya
  gider, yoksa adı ↔ ipucu farklı karttaysa belirsiz).  Birden çok aday kart
  (lab her tedarikçiye ayrı kart tutar): (a) tedarikçinin kendi kartı (aynı
  gruptaki dahil) → (b) adaylar tek gruptaysa grup ana kartı (reçetedeki tek
  kart, yoksa stoğu en yüksek) → (c) yalnız biri reçetedeyse o kart ama
  eşleşme güveni BELİRSİZ (Excel "Eşleşmeyenler"de doğrulanır) → (d) belirsiz;
  veri dosyasında `malzeme_takma_adlari` (belge adı → kart adı; `lab_belirlesin`
  biçimi adayları listeler, seçmez); toz kaydı yalnız ALOEVERA EKSTRAKT TOZU
  kartına · `--apply-prefs ONAYLI.xlsx [--commit]` yalnız E onaylı sarı
  seçimleri `material_supplier_prefs`'e, E onaylı küçük satıcıları
  `phase_out`'a yazar — otomatik tercih/bitirme YOK.  Onay hücreleri YALNIZ
  E/H ("X", "✓" hata; Excel listesi `showErrorMessage` ile reddeder); yeni
  firma kartı yalnız "Yeni tedarikçiler"de E ise açılır; "Doğru değer" /
  "Doğru IMS kartı" düzeltmeleri OTOMATİK işlenmez — veri dosyasına (fiyat /
  `elle_eslesme.kart`) aktarılmadan iki adım da hiçbir şey yazmaz; aynı
  firmaya tercih + bitirilecek E'si (ya da zaten bitirilecek firmaya tercih)
  hatadır.
  Kısmi fiyat aktarımı: `--record-ids SECIM.json --onayli ONAYLI.xlsx
  [--commit]`; JSON `[{"id":"A1-02-S1","item_id":160,"supplier_id":10}]`.
  Kaynak veri dosyası daraltılmaz: tam plandaki kazanan ve kart/firma
  kimlikleri doğrulanır. Yalnız seçilen çiftlerin bu kaynağa ait fiyatları
  yenilenir; diğer lab ve elle fiyatlar korunur. Excel'de `Aktarım seçimi`
  sayfası varsa dış manifest zorunludur ve üç kimlik birebir eşleşmelidir.
  İlk kapsam 93 fiyat; A1-36-S1, C-s12-1, C-s12-2 birim/paket teyidi bekler.
  Firma politikası: `scripts/apply_songul_supplier_policy_20261008.py
  [--commit]`, kuru varsayılan, tek transaction + kart bazında audit.
  UMAYCHEM/NATURALYA/YİĞİTOGLU KİMYA/DOGASA eşit `preferred`;
  TATLIDİLİMLER/KRK GIDA tüm aktif aliaslarıyla `phase_out`. DOGASA,
  DOALİNN değildir. Eksik/çelişkili firma veya bitirilecek firmaya mevcut
  açık malzeme tercihi işlemi durdurur. Yeni firma/stok/minimum değiştirmez.
  `import_prices` (Stok Son Durum) lab satırlarını silmez; aynı firmanın Excel
  satırını yalnız ELLE (`manual`) satır engeller — lab + Stok Son Durum yan yana kalır.
  **Para birimi karışık malzeme** (lab notları EUR/TRY + Stok Son Durum USD):
  `prices_for_items(..., rates=today_rates_or_none)` (Excel uçları) sıralamayı
  net fiyatın USD karşılığıyla yapar (kur yalnız karışık malzeme varsa, bir
  kez); kur yoksa ham sıra + `comparable: False` → Üretim Planı Excel'i
  Tedarikçi-1'i kalın göstermez.  Fiyat hücreleri sayı kalır, biçimi para
  birimi/birimi gösterir (`price_cell_format` → `#,##0.00## €/kg`).

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
(`.claude/settings.json` → `.claude/hooks/block-adhoc-deploy.sh`) **blocks** any Bash
command that ssh's to prod (the server in `ops/hetzner/active-server.env`, plus the
old `turhost` / `136.144.251.26`) and runs `systemctl
restart`, `git reset --hard`, or `git pull`. Read-only ssh (`journalctl`, `systemctl
status`) is allowed; `./deploy.sh`'s own internal ssh is not affected (it runs as a
script subprocess the hook never sees). Emergency hotfix only: `./deploy.sh
--skip-tests` bypasses the test gate with a loud warning; still verify prod manually.

The lab works live — bundle related changes into one restart, and take a `pg_dump`
backup before any destructive DB operation. An hourly auto-snapshot timer
(`ops/snapshot/`) runs 07:00–19:00.
