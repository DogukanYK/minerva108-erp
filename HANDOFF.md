# Minerva 108 — Oturum Handoff (2026-07-03)

Canlı sürüm: `317b0dd` (main = origin/main = prod, doğrulandı) · alembic head:
`c4f6a8b2d1e5` (tek) · Prod: `turhost` (root@136.144.251.26:23422), `/var/www/minerva`,
systemd `minerva`, PostgreSQL `minerva_db` · deploy **yalnız `./deploy.sh`**
(test gate + HTTP doğrulama). İki paralel oturum çalıştı: **A = ERP çekirdek**
(teslimat/iade/yazdırma), **B = portal + mobil**. Bu dosya ikisini de kapsar.

---

## A. ERP çekirdek akışı (bu oturum — CANLI)

| Özellik | Ana dosyalar | Not |
|---|---|---|
| Teslimat: çift-dilli arama + zorunlu belge dili + antetli belge | `routers/delivery.py`, `core/delivery_note.py`, `templates/delivery.html` | `Item.name_tr` + `Delivery.doc_lang`; arama sonuçları inline (overflow fix) |
| Türkçe ad toplu import (barkodla) | `routers/inventory.py` → `/api/items/import-names` | Fuar Excel'leri BARCODE→TR ad; ölçü ada eklenir (100/500 ML ayrışır) |
| Proforma Fatura + SuperAdmin onayı | `core/proforma_invoice.py` + delivery router | Stok **ONAYDA** düşer; `PRF-` belge no |
| Kargo/Sevkiyat (ertelenmiş stok) | delivery router `ship/cancel/shipments`, `core/shipment_note.py` | `KRG-`; stok **takip no girilince** düşer (idempotent, hep-ya-hiç); hazırlık + master toplama PDF |
| Tek-tık + çok-bacaklı kargo takibi | `Delivery.tracking_legs` (JSON), delivery.html `legsModal` | Yerel TR → Global → Varış; her bacak 📍 tek tık; **her teslimata** (hediye/numune dahil) takip eklenebilir |
| Sevkiyat düzenleme | `PUT /api/delivery/{id}` | Taslakta ürünler dahil; kargolanmışta yalnız meta — **stok güvenli** |
| **Ürün İadesi** (yan menü → İadeler) | `routers/returns.py`, `core/return_note.py`, `templates/returns.html` | `RET-`; belgeye bağlı KISMİ iade (aşırı-iade SUM guard) + serbest iade; SAĞLAM→stok+`Transaction(Input)`, HASARLI→fire izi (stok değişmez) |
| Drive Quick Look önizleme | `routers/drive.py` `preview/meta+raw`, drive.html | foto/PDF/Excel/metin; boşluk tuşu; beyaz-liste + nosniff (XSS guard korunur) |
| **Sistem geneli yazdırma** | `static/print.css` (21 şablon), `theme.js` beforeprint | Cmd+P her sayfada temiz; koyu tema baskıda otomatik light |
| **Ürünler → Yazdır (PDF)** | `core/items_report.py`, `GET /api/items/print`, items.html | Sekme+alt-tip+arama filtreli antetli A4; Maliyet **finance-gated** (sunucuda) |
| Rol görünen adları + hata mesajı düzeltmeleri | `core/permissions.py get_role_labels`, admin.html `errMsg` | AppSetting tabanlı; 422 "[object Object]" fix |

Testler: `test_shipment` (17) · `test_returns` (7) · `test_items_print` (7) ·
`test_pdf_a4` (A4+autofit+marj) · `test_drive_preview` (7) · `test_import_names` (3) ·
`test_proforma` · `test_role_labels`.

### Kritik kararlar / tuzaklar (İLERİDE BOZMA)
1. **İade Transaction tipi = `Input`** ('Return' tipi ekleme!): `core/snapshots.py` +
   `core/monthly_report.py` stok rekonstrüksiyonu yalnız Input/Output/Adjustment tanır.
   Ayrım `notes` ön eki: `İade (RET-…) ← kaynak`.
2. **Antetli PDF marjları `(20,20,48,40)mm`** — antetli footer ~34mm; alt marjı 30'a
   düşürme (footer'a taşar — yaşandı). Antetli `static/letterhead.pdf` **US Letter**;
   `merge_letterhead` A4'e `scale_to` ile normalize eder (~%6 esneme; sıfır bozulma
   için kullanıcıdan gerçek A4 antetli istenmeli).
3. **`render_autofit` politikası**: tek sayfa öncelikli → maks %15 küçült → yetmezse
   çok sayfa (öksüz satır olmaz). `test_pdf_a4` kilitler.
4. **Content-Disposition daima `core.delivery_note.content_disposition()`** (RFC 5987)
   — Türkçe dosya adı doğrudan header'a konursa 500 (yaşandı).
5. **`/preview/raw` için app X-Frame-Options SET ETMEZ** — nginx zaten SAMEORIGIN
   ekliyor; çiftlenirse Chrome iframe'i keser ("refused to connect", yaşandı).
   CSP `frame-ancestors 'self'` yeterli.
6. **print.css cascade**: dark.css → print.css → sayfa-içi `<style>`. production föyü +
   quotations invoice kendi @media print kurallarıyla korunur. Yeni modal id'si
   `…Modal` ile bitmeli (baskıda otomatik gizlenir).
7. **Statik dosya değişince `static/sw.js` CACHE_NAME bump** (şu an v20) — yoksa
   PWA kullanıcılarına ulaşmaz (theme.js/scanner.js'te yaşandı).
8. **TR sıralama sunucuda `core.items_report.tr_key`** (order-map; `locale` modülü kullanma).
9. **Şema kuralı:** her yeni KOLON hem Alembic migration hem `init_db()` alter_safe;
   yeni TABLO ise create_all otomatik (yalnız migration yeter).

---

## B. Distribütör Portalı + Native uygulamalar (paralel oturum — CANLI, 29.06)

- **Portal:** `siparis.minerva108.com` — Distribütör = `role="Distributor"` User + 1:1
  `Distributor` profili; sipariş = `distributor_id`'li PENDING `Quotation` → personel
  B2B Teklifler'den onaylar (stok düşer) / reddeder. Fiyat daima sunucudan
  (`DistributorPrice` = katalog). Stok distribütöre yalnız var/yok. Dosyalar:
  `core/distributor.py`, `routers/{distributors,portal}.py`, `templates/distributor_portal.html`,
  `static/portal.js`. DNS+nginx+TLS kuruldu (`ops/siparis-subdomain.md`).
  İlk distribütör: ims → Distribütörler → Yeni + Fiyatlar → kullanıcıya siparis adresi ver.
- **Native Android (Capacitor WebView, TWA değil):** IMS `~/Desktop/Claude/minerva-mobile/ims`
  (`com.minerva108.ims`, APK v1.0.7) + CRM `…/crm`. Native ML Kit barkod, DownloadManager,
  adjustResize, splash. **Keystore'lar `<app>/signing/` — SAKLA.** Build: JDK17 + SDK,
  `./gradlew assembleRelease`. Detay: `~/Desktop/Claude/minerva-mobile/POLISH-LOG.md`.
- Diğer: login IP limiti 5→40/15dk (`routers/auth.py`); iOS PWA viewport-fit geri alındı;
  mobilde daima açık tema.

---

## C. Açık işler / bekleyenler

- **Işık Hanım'ın STOK SON DURUM.xlsx** tedarikçi fiyat importu (UI hazır — kullanıcı yükleyecek).
- **TR ad importunda eşleşmeyen 5 MINERVA ürünü** (barkod sistemde yok): KOYU LEKE KREMİ
  50ML `…206598` · YOĞUN SAÇ MASKESİ 200ML `…206451` · ROLL ON DEODORANT 50ML `…206345` ·
  YÜZ TEMİZLEME JELİ 200ML `…206420` · TIRNAK BAKIM SERUMU 7ml `…206376`. İlk 2'nin ürünü
  sistemde FARKLI barkodla var (`…206208`, `…206529`) — kullanıcı kararı: barkod düzelt vs yeni ürün.
- **EN/TR ölçü çelişkileri** (kaynak veri, kullanıcı karar verecek): `…206222` EN
  "Toner 200ml" ↔ TR "TONİK 100 ML"; `…206192` EN "Serum 30ml" ↔ TR "SERUMU 20 ML".
- **print.css manuel smoke** (kullanıcı doğrulaması): koyu temada Cmd+P · modal açıkken
  Cmd+P · production föyü + quotations regresyonu · 200+ satırda thead tekrarı.
- Portal opsiyonelleri: distribütöre push, portal proforma PDF'i, min sipariş miktarı,
  distribütör rolü için ungated-read denetimi. Native push (FCM) ayrı faz.
- Fikir kuyruğu: diğer liste sayfalarına Yazdır butonu · Drive Word/video önizleme ·
  kısmi kargo sevkiyatı · iade onay akışı.

## D. Operasyon notları

- **⚠️ İki-oturum çakışması:** test/deploy öncesi `pgrep -f 'bin/pytest'` BOŞ olmalı;
  iki pytest aynı `minerva_test` DB'de çakışıp sqlalchemy hataları üretir (kod hatası
  DEĞİL — temiz pencerede tekrar dene). Paralel oturum bazen diğerinin dosyalarını kendi
  commitine süpürür (zararsız; git log okurken bil).
- **Lokal PostgreSQL** (brew postgresql@16): makine yeniden başlarsa stale
  `postmaster.pid` kalabilir (PID başka sürece gider) → pid dosyasını sil +
  `brew services restart postgresql@16` (yaşandı).
- Prod nginx/DNS/TLS elle (kullanıcı `ssh turhost`); ad-hoc prod deploy hook'la bloklu.
- Idempotent şablon scriptleri (yeni şablon eklenince tekrar çalıştır):
  `scripts/add_print_css.py` · `scripts/add_returns_nav.py`.
- Hafıza: `~/.claude/projects/.../memory/` (MEMORY.md indeks).
