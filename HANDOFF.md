# Minerva 108 — Oturum Handoff (2026-07-31)

Canlı sürüm: **`fe0ad1c`** (main = origin/main = prod, doğrulandı) · alembic head:
**`f3a5c7e9b2d4`** (tek) · testler **398 passed** · Prod: `turhost`
(root@136.144.251.26:23422), `/var/www/minerva`, systemd `minerva`, PostgreSQL
`minerva_db` · deploy **yalnız `./deploy.sh`** (test gate + HTTP doğrulama).

> `./deploy.sh` commit mesajını **interaktif** sorar. Ajans/otomasyon içinden çalıştırırken
> `echo "mesaj" | ./deploy.sh` — yoksa Step 2'de `read` patlar (yaşandı).

---

## A. Bu oturumda eklenenler (hepsi CANLI)

| Özellik | Ana dosyalar | Not |
|---|---|---|
| **İçindekiler Raporu** (Ürünler → esnek seçim → Excel/PDF) | `core/ingredients_report.py`, `routers/reports.py`, `templates/items.html` | Marka/ürün çoklu seçim; Excel **ürün-blok** düzeni (hammadde + ambalaj ayrı sheet), A4 yazdırılabilir; antetli PDF |
| **Numune Analiz Formu (FR.KK.01)** | `core/sample_questions.py`, `core/sample_report.py`, `routers/sample_analysis.py`, `templates/numune_analiz.html` | Songül Hanım'ın kâğıt formunun dijital ikizi; formülasyon/tarih/lot/açıklama; antetli PDF; tablo `sample_analyses` |
| **Shopify Faz 1 — IMS→Shopify stok push** | `core/shopify.py`, `routers/shopify.py`, `templates/shopify_sync.html` | 3 mağaza (Minerva/Evanira/Serenida); eşleşme **barkod**; `*/5 dk` scheduler job; `ShopifySyncState` |
| **Shopify Faz 2 — sipariş → stok → fatura → tahsilat** | `core/shopify.py` (`handle_order_paid`/`_process_order`/`retry_order`), `core/parasut.py` | `orders/paid` webhook (HMAC) → stok Output → Paraşüt faturası → iyzico tahsilatı; `ShopifyOrder` durum makinesi |
| **Paraşüt entegrasyonu** | `core/parasut.py` | password grant + 10 istek/10sn rate guard; contact/product find-or-create; KDV-hariç kalem; e-Arşiv/e-Fatura + trackable job poll |
| **PDKS** (personel giriş/çıkış + puantaj) | `routers/pdks.py`, `core/pdks.py`, `core/pdks_report.py`, `templates/pdks.html` | Detay CLAUDE.md'de; tablolar `pdks_*` (migration `f3a5c7e9b2d4`) |
| **Barkod reconciliation** | `scripts/reconcile_shopify_barcodes.py` | GS1 master otorite; 18 düzeltme prod'da uygulandı; idempotent (tekrar çalışır) |

Testler: `test_ingredients_report` (16) · `test_sample_analysis` (13) · `test_shopify` (20) ·
`test_shopify_orders` (38) · `test_pdks`.

---

## B. Shopify + Paraşüt — operasyonel durum

### Şu an ne çalışıyor

| Mağaza | Stok senkronu (IMS→Shopify) | Sipariş → fatura |
|---|---|---|
| **Minerva** `dkkwdr-rr` | ✓ 29 ürün | ✓ TAM OTOMATİK |
| **Evanira** `j0jfuy-j9` | ✓ 12 ürün | ✗ kendi app'i + ORDERS_PAID webhook kurulmadı |
| **Serenida** | — mağaza yok | — |

**Akış:** `orders/paid` → HMAC doğrula → ülke TR mi (değilse `skipped_export` + bildirim)
→ stok Output (**yalnız bir kez**, `stock_applied`) → Paraşüt müşteri/fatura → iyzico
tahsilatı → (official modda) e-belge. Yarım kalanları `*/10 dk` retry job kaldığı
ADIMDAN tamamlar (15 dk'dan yeni kayda dokunmaz, `attempts` limiti 5).

### ⚠️ Açık iş #1 — fatura hâlâ RESMİ DEĞİL
Prod `.env`: `INVOICE_MODE=draft` (env adı **PARASUT_ öneksiz**; `invoice_mode()`
doğrudan `os.getenv("INVOICE_MODE")` okur — diğer tüm Paraşüt env'leri `PARASUT_*`).
Draft = fatura Paraşüt'te oluşur, tutar/tahsilat doğru, ama **GİB'e gönderilmez**.
`official` yapıldığında: VKN varsa e-Fatura kutusu sorgulanır → mükellefse **e-Fatura**,
değilse **e-Arşiv**. Geçiş = prod `.env` tek satır + `systemctl restart minerva`
(kullanıcının işi; ad-hoc prod restart hook'la bloklu).

### ⚠️ Açık iş #2 — kullanıcı Minerva'yı ELLE de faturalıyor
Sistem otomatik kesiyor. Elle kesmeye devam ederse **çift fatura**. Kullanıcıya
defalarca söylendi, teyit alınmadı.

### ⚠️ Açık iş #3 — TEST VERİSİ DURUYOR (kullanıcı incelesin diye bilinçli bırakıldı)
Sipariş **#1008**: Shopify siparişi (`IMS-TEST` etiketli) · Paraşüt faturası
**1094665360** (1.450 TL, ödendi) · `shopify_orders` id=5 (durum `paid`) + Transaction ·
Face Scrub 60ml stok **69→68**. Kullanıcı "temizle" deyince: Shopify iptal → Paraşüt
fatura sil → IMS satır + Transaction sil → stok 68→69 → Shopify'a geri push.
Not: id=5'in `last_error` alanında eski hata metni duruyor (kayıt bir daha
işlenmeyeceği için temizlenmedi) — temizlikte gidecek.

### Diğer bekleyenler
- Evanira sipariş→fatura: o mağazaya `read_orders` izinli kendi custom app'i + panelden
  "Webhook Kurulumu". (Minerva'daki app adı **PARASUT**, Client ID `3f00e4ef…`.)
- Serenida mağazası kurulunca `SHOPIFY_SERENIDA_*` env'leri doldur → stok otomatik başlar.
- **Komisyon gideri manuel** (muhasebeci kararı): sistem faturayı satış tutarının
  TAMAMI kadar ödendi işler; iyzico komisyonu ayrıca gider kaydedilecek.
- Shopify client secret'ları geçmişte sohbete düştü → **rotate** önerildi, yapılmadı.

---

## C. Kritik kararlar / tuzaklar (İLERİDE BOZMA)

### Bu oturumdan
1. **Tahsilat idempotan olmalı** (`P.add_payment`): POST öncesi `invoice_remaining()`
   bakılır; kalan ≤0.01 → hiç yazma; tutar kalandan büyükse **kırp**; yarışta gelen
   `"bigger than remaining"` hatası **yutulur**, diğer hatalar fırlatılır. Yaşandı:
   Shopify aynı `orders/paid`'i iki kez gönderdi → 400 → sipariş boşuna `failed` oldu.
2. **`row.last_error`**: stok eşleşme uyarıları `"Stok: "` önekiyle yazılır ve
   **asla silinmez**; sipariş `paid`/`legalized` olunca önek*siz* (Paraşüt) hata
   metni temizlenir. Panelde "paid ama kırmızı hata" görünmesin diye.
3. **`stock_applied` bayrağı stok commit'iyle BİRLİKTE yazılır.** Hata sonrası
   `db.rollback()` `step` alanını geri sarıyordu → retry stoğu ÇİFT düşürüyordu.
   Retry kararı `status`/`step`'e değil bu bayrağa bakar.
4. **Shopify sipariş webhook'u line_item'da BARKOD GÖNDERMEZ** (yalnız `sku` +
   `variant_id`). Eşleşme zinciri: barkod → sku → `_variant_barcode()` (variant_id→barkod
   mağaza haritası, 10 dk cache). Bu olmadan stok HİÇ düşmüyordu (canlı testte yakalandı).
5. **`billing_address.country` boş gelebilir** (draft order→complete akışı) → TR siparişi
   `skipped_export` oluyordu. `_country_code()`: billing → shipping → müşteri varsayılan
   adresi, hem ISO kodu hem ülke ADI ("Turkey"→TR).
6. **Shopify auth = client_credentials** (yeni Dev Dashboard'da statik `shpat_` token YOK).
   App mağazaya kurulur, sonra Client ID+Secret ile `POST /admin/oauth/access_token`.
   **Her Shopify org'u kendi app'ini ister** — Minerva ve Evanira ayrı org'ta.
7. **Test yöntemi:** API ile `financial_status:"paid"` vererek order create etmek
   `orders/paid` webhook'unu TETİKLEMEZ. Gerçek olay için: `draft_orders` → `complete`
   (`payment_pending=false`) → sale transaction doğar → webhook gelir.
8. **openpyxl A4 yazdırma** (`_setup_a4`): `paperSize=PAPERSIZE_A4` + `fitToWidth=1` +
   `fitToHeight=0` + `sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)`
   + `print_title_rows`. Üçü birden olmadan fitToPage çalışmaz.
9. **Antetli PDF performansı**: her sayfaya ayrı letterhead merge etmek 25 MB / dakikalarca
   sürüyordu → TEK letterhead page objesi tüm sayfalara merge + `compress_identical_objects`
   (1.7 MB / ~3 sn). Ayrıca 2'den fazla ürün varsa `render_autofit(steps=())` — autofit
   tüm dokümanı 5 kez yeniden kuruyordu.

### Önceki oturumlardan (hâlâ geçerli)
10. **İade Transaction tipi = `Input`** ('Return' tipi EKLEME): `core/snapshots.py` +
    `core/monthly_report.py` stok rekonstrüksiyonu yalnız Input/Output/Adjustment tanır.
    Ayrım `notes` ön ekiyle: `İade (RET-…) ← kaynak`.
11. **Antetli PDF marjları `(20,20,48,40)mm`** — antetli footer ~34mm. İçerik yoğun
    formlarda (İçindekiler Raporu) **alt marj 50mm**, yoksa footer'a taşar (raster
    doğrulandı). Antetli `static/letterhead.pdf` US Letter; `merge_letterhead` A4'e
    `scale_to` ile normalize eder.
12. **`render_autofit` politikası**: tek sayfa öncelikli → maks %15 küçült → yetmezse
    çok sayfa (öksüz satır olmaz). `test_pdf_a4` kilitler.
13. **Content-Disposition daima `core.delivery_note.content_disposition()`** (RFC 5987) —
    Türkçe dosya adı doğrudan header'a konursa 500.
14. **`/preview/raw` için app X-Frame-Options SET ETMEZ** — nginx zaten SAMEORIGIN ekliyor;
    çiftlenirse Chrome iframe'i keser.
15. **print.css cascade**: dark.css → print.css → sayfa-içi `<style>`. Yeni modal id'si
    `…Modal` ile bitmeli (baskıda otomatik gizlenir).
16. **Statik dosya değişince `static/sw.js` CACHE_NAME bump** — yoksa PWA kullanıcılarına
    ulaşmaz.
17. **TR sıralama sunucuda `core.items_report.tr_key`** (`locale` modülü kullanma).
18. **Şema kuralı:** her yeni KOLON hem Alembic migration hem `init_db()` alter_safe;
    yeni TABLO ise create_all otomatik (yalnız migration yeter).

---

## D. Diğer açık işler

- **Işık Hanım'ın STOK SON DURUM.xlsx** tedarikçi fiyat importu — UI hazır, kullanıcı
  yükleyecek (bu oturumda başlandı, İçindekiler Raporu talebi araya girdi).
- **EN/TR ölçü çelişkileri** (kaynak veri, kullanıcı karar verecek): `…206222` EN
  "Toner 200ml" ↔ TR "TONİK 100 ML"; `…206192` EN "Serum 30ml" ↔ TR "SERUMU 20 ML".
- Portal opsiyonelleri: distribütöre push, portal proforma PDF'i, min sipariş miktarı.
  Native push (FCM) ayrı faz.
- Fikir kuyruğu: diğer liste sayfalarına Yazdır butonu · Drive Word/video önizleme ·
  kısmi kargo sevkiyatı · iade onay akışı · Shopify Faz 3 (iade/iptal faturası,
  fatura PDF'ini Shopify'a geri yazma).

## E. Operasyon notları

- **⚠️ İki-oturum çakışması:** test/deploy öncesi `pgrep -f 'bin/pytest'` BOŞ olmalı;
  iki pytest aynı `minerva_test` DB'sinde çakışıp sqlalchemy hataları üretir — **kod
  hatası DEĞİL**, temiz pencerede tekrar dene. (PDKS deploy'unda yaşandı: "deploy error"
  sanılan şey buydu.)
- **Lokal PostgreSQL** (brew postgresql@16): makine yeniden başlarsa stale
  `postmaster.pid` kalabilir → pid dosyasını sil + `brew services restart postgresql@16`.
- **Prod okuma** serbest: `ssh turhost 'sudo -n -u postgres psql -d minerva_db -tAc "SELECT …"'`
  (`-A` çıktısı `|` ayraçlı; SQL literali gerekiyorsa tırnak kaçışına dikkat).
  Ad-hoc prod deploy/restart hook'la **bloklu**.
- **Secret'lar sohbete YAZILMAZ** — yalnız prod `.env`'e. Kullanıcı `.env`'e yapıştırırken
  şablon `<...>` parantezlerini bırakıp eski satırı silmemişti → `source` syntax hatası;
  parantez strip + son-kopya dedup ile düzeltildi (yedek `.env.bak.*`).
- Idempotent şablon scriptleri (yeni şablon eklenince tekrar çalıştır):
  `scripts/add_print_css.py` · `scripts/add_returns_nav.py` · `scripts/add_numune_nav.py` ·
  `scripts/add_shopify_nav.py`.
- Hafıza: `~/.claude/projects/-Users-dogukan-Desktop-Claude/memory/` (MEMORY.md indeks;
  bu konu → `shopify_ims_entegrasyon.md`).
