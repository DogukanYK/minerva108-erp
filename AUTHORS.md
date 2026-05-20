# AUTHORS — Eser Sahibi Kaydı

## Eser Sahibi

**Doğukan Yalçınkaya**
İletişim: dogukanuguryalcinkaya@gmail.com
Rol: Tasarım + Mimari + Geliştirme + Operasyon (single author)

## Çalışma Özeti

| Metrik | Değer |
|---|---|
| İlk commit | 2026-04-29 |
| Son commit | 2026-05-15 |
| Toplam commit sayısı | **72** |
| Eklenen kod satırı | **34.829** |
| Silinen kod satırı | 4.320 |
| Etkilenen benzersiz dosya | **125** |

Bu sayılar `git log --author=dogukan --numstat` çıktısından üretilmiştir.
Her commit kriptografik olarak imzalı SHA hash'i ile doğrulanabilir.

## Yapılan İş Kapsamı (özet)

- FastAPI + Jinja2 + PostgreSQL tüm sistem mimarisi
- 11+ sayfa (Dashboard, Ürünler, Tedarikçiler, Mal Kabul, Reçeteler,
  Üretim, Kalite Kontrol, Stoklar, İşlem Defteri, Raporlar, İzlenebilirlik,
  B2B Teklifler, Yönetim Paneli)
- 12 API router (auth, users, inventory, recipes, production, b2b,
  reports, notifications, backup, undo, debug + admin sayfa route'ları)
- RBAC 2.0 — rol bazlı + per-user permission override
- JWT auth + bcrypt + HttpOnly+SameSite cookie + CSRF + CSP + rate limiting
- Üretim modülü — fire/brüt hesabı, ambalaj fire muafiyeti, etiket dil
  çözümü (TR/EN), üretim föyü Excel export
- Undo sistemi — 50 derinlikli per-user ring buffer, Ctrl+Z desteği
- Saatlik otomatik snapshot timer (07:00–19:00) + manuel backup/restore
- Web push bildirimleri + APScheduler tabanlı expiry tarama
- Türkçe alfabetik sıralama, etiket dil grupları, soft-delete pattern
- Alembic migration zinciri + dual schema yönetimi (init_db alter_safe)
- 48 pytest fonksiyonu (auth + RBAC + backup + sayfa render testleri)

## Eser Sahipliği Beyanı

Bu yazılım, **5846 sayılı Fikir ve Sanat Eserleri Kanunu (FSEK) m.2/1**
anlamında bir bilgisayar programı eseridir.  Aksi yazılı bir sözleşmeyle
açıkça ve FSEK m.48-52'ye uygun biçimde devredilmedikçe:

- **Manevi haklar** (FSEK m.14-17) eser sahibinde kalır ve devredilemez.
- **Mali haklar** (FSEK m.21-25) eser sahibindedir.
- **İş ilişkisi** çerçevesinde yapılmış olması mali hak devri sayılmaz
  (FSEK m.18/2 + m.48 birlikte uygulanır).
- **Gizlilik sözleşmesi (NDA)** yalnızca bilginin açığa çıkarılmamasına
  ilişkindir; eser sahipliğini ve mali hakları etkilemez.

Tam bildirim için bkz. `LICENSE` dosyası.

## Commit Geçmişi (tam)

Aşağıdaki liste, repo'nun git geçmişinden eser sahibinin yaptığı tüm
commit'lerin tam zaman damgalı kaydıdır.  Bu kayıt sahteleştirilemez —
her hash SHA-256 ile içeriğe bağlıdır ve değiştirilirse aşağı zincir kırılır.

```
2026-04-29  fb0ab78  İlk Canlı Yayın
2026-04-30  f66caeb  feat: Phase 3 - Urun Varyasyonlari ve Hiyerarsi Mimarisi
2026-04-30  cb18490  New Updates
2026-04-30  8eded30  Role based access granting
2026-04-30  c21de75  feat: auto-deploy update
2026-04-30  8799251  chore: add postgres support + migration script
2026-04-30  089cf67  fix: PG-safe migrations + truncate flow for migrate script
2026-04-30  d978728  feat: rate limiting on /api/login (5/min per IP)
2026-04-30  a60b2fa  proforma update
2026-04-30  ddcd17b  feat: real proforma data + 49 product catalog seed script
2026-04-30  9fb19e9  feat: ingredient_cost + packaging_cost columns + seed for 32 products
2026-04-30  eba01f7  feat: bulk recipe seeder — 31 recipes from cost Excel BOMs
2026-04-30  2876d77  fix: NFKC unicode normalization + auto-import hammadde excel
2026-04-30  7f9fdd5  feat: Minerva 108 recipes from formulations + fire default 10%
2026-04-30  5faf2ba  feat: edit recipe — PUT endpoint + UI button + dual-mode modal
2026-04-30  5ccbd8f  feat: brand refresh + dark mode + responsive fixes + recipes 2.0 filter
2026-05-01  fca6b9b  mobile-app
2026-05-01  aba1450  web update and notifications
2026-05-01  0c5cbfd  notifications on mobile support
2026-05-01  cab11c3  barcode system implemented
2026-05-01  54cd264  cam fix and database match
2026-05-03  8bf442d  proforma update
2026-05-06  f926741  üretim önizlemesi fireli + stok düzeltme + audit hijyen
2026-05-07  3469029  ambalaj alt-tipi (pkg_type) + toplu envanter import script'i
2026-05-07  0d1b521  bulk import: MINERVA_BULK_DOWNLOADS env var ile path override
2026-05-07  6fa1114  dev DB'yi PostgreSQL'e taşı + lokal SQLite'ı untrack et
2026-05-11  846d043  güvenlik sertleştirme: dalga A + B
2026-05-11  603002c  güvenlik sertleştirme: dalga C — olgunlaştırma
2026-05-11  d9c6603  backup/restore: admin sayfasından manuel yedekleme + felaket kurtarma
2026-05-11  42a1d60  güvenlik olgunlaştırma: R2-R7 + otomatik backup
2026-05-11  a5edb5e  R7 + Alembic + pytest test suite (40/40)
2026-05-11  5abf945  test: sayfa render + script tag dengesi regresyon koruması
2026-05-12  bdfdf08  reçete %-bileşim + items supplier + receiving auto-fill + QC kontrol mekanizması
2026-05-12  35b382d  fix: admin permission matrix'te adjust + backup action'ları görünmüyordu
2026-05-12  7bdf278  reçete: % primary input + ürünün ml'inden otomatik gram hesabı
2026-05-13  178b95b  oturum + tedarikçi + idle timeout
2026-05-13  dad43ad  fix: backup endpoint 'pg_dump bulunamadı' — binary path detection
2026-05-13  5f4d844  perf + ux: B2B fix + TR sort + DB indexes + N+1 joinedload + items cache
2026-05-13  5f97b1b  hot-fix: items DELETE 500 + items page boş kalma
2026-05-13  b555a35  perf debug endpoint + saatlik snapshot timer
2026-05-13  d1eea4a  undo system: Ctrl+Z + son 50 hareket geri alma
2026-05-13  52011a9  soft-delete: audit'li ürünleri arşivle, hard-delete'i sadece temiz satırlara
2026-05-14  e284256  production: canlı önizlemeye 'Sonrası Stok' kolonu
2026-05-14  102d198  production: 'Toplam Hammadde' kombine kartı (g+ml, ambalaj hariç)
2026-05-15  c684356  stoklar: Hammadde / Ambalaj / Etiket ayrı filtreler + KPI
2026-05-15  2f3c575  etiket dil ayrımı: üretimde TR/EN seç → doğru etiket stoktan düşer
2026-05-15  aea4402  reçete ekranı: etiket TR/EN ikilisini tek satır göster
2026-05-15  54fb8a1  üretim föyü: geçmiş satırına tıkla → detay + Excel indir + yazdır
```

> Bu listenin doğrulanması için: `git log --all --author=dogukan --pretty=format:"%ad  %h  %s" --date=short`

---

© 2026 Doğukan Yalçınkaya — Tüm hakları saklıdır.
