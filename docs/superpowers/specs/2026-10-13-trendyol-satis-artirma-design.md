# Minerva IMS — Trendyol satış artırma (Liste Doktoru · Buybox · Müşteri soruları)

Durum: Tasarım onaylandı — 13.10.2026 (Doğukan, brainstorming oturumu). Uygulanmadı.
Trendyol Faz 1 Teslimat A'nın (salt okuma senkron, commit `2927db5`) üzerine kurulur;
güncel sözleşme için `CLAUDE.md` → "Trendyol pazaryeri".

## Bağlam

- 13.10.2026 itibarıyla 56 ürün Trendyol'da satışta; son 90 günde tek sipariş var
  (18.07.2026). Eylül'de iki "Reklam Bedeli" kesilmiş, sonrasında sipariş gelmemiş.
  Sorun kârlılık değil, görünürlük ve dönüşüm.
- TPro360'ın anahtar kelime ve pazar verisi Trendyol'un herkese açık sitesinden toplanan
  veriye ve tahmine dayanır. Resmi satıcı API'si arama hacmi vermez.
- İçeriği (başlık, açıklama, görsel) ekip Trendyol panelinden elle giriyor; panelde
  görünen ikas entegratör kaydı kullanılmıyor.

## Kararlar

- **Yol:** kendi verimiz + yapay zekâ. Kazıma (scraping) yok.
- **Karma yaklaşım:** liste puanını kural motoru hesaplar (ücretsiz, deterministik,
  açıklanabilir, iyileşme izlenebilir). Claude yalnız yazım yapar: anahtar kelime,
  başlık, açıklama, eksik özellik değeri ve soru cevap taslağı.
- **IMS Trendyol'a yazmaz:** öneriler kopyalanır, ekip panele girer.
- **Bilgi notu:** ürün başına kısa notu ekip doldurur (öne çıkan içerikler, cilt tipi,
  kullanım). Claude'a reçete, formül oranı, maliyet, stok ve müşteri kişisel verisi
  GİTMEZ.
- **Model:** Opus 5.5 (AppSetting ile değiştirilebilir). Maliyet asistanın defterine
  (`assistant_usage`) yazılır, aynı aylık tavandan düşer.
- **Bildirimler açık:** yeni cevap bekleyen soru (soru başına bir kez), buybox kaybı
  (ürün başına günde en fazla bir), yöneticilere.
- **Yetki:** yeni yetki yok. `trendyol.view` görür; `trendyol.manage` öneri/taslak
  üretir ve bilgi notunu düzenler.
- **Parçalar:** bu belge 1. parçadır. Panel raporu yükleme (görüntülenme/favori ve
  reklam anahtar kelime raporları) 2. parçadır ve örnek Excel'ler gelince tasarlanır.
  Kâr motoru (eski Teslimat B) sipariş verisi birikince ele alınır.

## Veri kaynakları (resmi API, yalnız okuma)

Hepsi `https://apigw.trendyol.com/integration` altında; yollar 13.10.2026'da
developers.trendyol.com referansından doğrulandı.

| Veri | Uç | Sıklık |
|---|---|---|
| Ürün içeriği | `GET /product/sellers/{id}/products/approved` (mevcut katalog çekimi): `description`, `attributes[]` (`attributeId`, `attributeName`, `attributeValues[]`), `images[]`, `category{id,name}`, `variants[].deliveryOptions{deliveryDuration,isRushDelivery}` | Katalogla (saatlik) |
| Kategori özellikleri | `GET /product/categories/{categoryId}/attributes` → `categoryAttributes[]` (`attribute{id,name}`, `required`, `slicer`, `varianter`, `allowCustom`, `allowMultipleAttributeValues`) | Haftalık, kategori başına |
| Özellik değerleri | `GET /product/categories/{categoryId}/attributes/{attributeId}/values` (`page`, `size` ≤1000) → `attributeValueId`, `attributeValueName` | Yalnız üründe eksik özellik için, haftalık önbellek |
| Buybox | `POST /product/sellers/{id}/products/buybox-information`, gövde `{"barcodes": [≤10]}` → `buyboxOrder`, `buyboxPrice`, `hasMultipleSeller`, `secondBuyboxPrice`, `thirdBuyboxPrice` | Günlük, katalogla |
| Müşteri soruları | `GET /qna/sellers/{id}/questions/filter` (`startDate`/`endDate` ≤2 hafta, `status`, `page`, `size` ≤200) → `id`, `text`, `status`, `productMainId`, `productName`, `webUrl`, `creationDate`, `answer{text,creationDate}`, `rejectedAnswer`, `reason` | Saatlik |

- "Günlük" ve "haftalık" işler saatlik katalog akışının İÇİNDE koşar: buybox son
  kontrol 20 saatten eskiyse, kategori özellikleri son çekim 7 günden eskiyse (ya da
  katalogda yeni kategori belirdiyse). Ayrı zamanlayıcı işi yoktur.
- Buybox ucu teknik olarak POST ama sorgudur. `Api`'ye yalnız bu yola açık, izin
  listeli dar bir `query()` metodu eklenir; başka POST/PUT/DELETE yoktur
  (`test_api_is_read_only_by_construction` güncellenir ve kilitler).
- Sorulardan `customerId`, `userName` SAKLANMAZ.
- İlk soru çekimi 2 haftalık pencerelerle en fazla 90 gün geriye gider.

## Kural puanı (100)

Ağırlıklar `core/trendyol_listing.py` içinde tek sözlükte durur; puan kural dökümüyle
birlikte günlük saklanır. Her ürün için en çok puan kazandıracak ilk üç düzeltme
öne çıkarılır.

| Başlık | Puan | Kural |
|---|---|---|
| Ürün adı | 20 | 50–150 karakter (8) · marka geçiyor (4) · ürün türü/kategori kelimesi (4) · hacim/ölçü (2) · tamamı büyük harf ya da promosyon kelimesi yok (2) |
| Özellikler | 20 | Zorunlu özelliklerin doluluğu (10, orantılı) · süzgeç (`slicer`/`varianter`) ve isteğe bağlı özelliklerin doluluğu (10, orantılı) |
| Açıklama | 15 | ≥200 karakter (5) · ≥600 karakter (3) · en az 2 paragraf ya da madde (3) · kullanım/içerik bilgisi (4) |
| Görseller | 15 | 0–3 görsel 0 · 4 → 8 · 5 → 11 · 6+ → 15 |
| Fiyat | 10 | Liste fiyatı satış fiyatından yüksek (5) · buybox bizde ya da tek satıcı (5) |
| Teslimat | 10 | `deliveryDuration` ≤1 gün 10 · 2 gün 5 · ≥3 ya da bilinmiyor 0 |
| Satış durumu | 10 | Satışta ve kilitli/arşiv/kara liste değil (4) · Trendyol stoğu >0 (3) · Trendyol stoğu ≤ IMS stoğu (3; eşleşmemiş ürün 0) |

`deliveryDuration` biriminin gün olduğu ilk gerçek veride doğrulanır.

## Yapay zekâ yazımı (Liste Doktoru önerisi)

**Tetik.** Ürün başına "Öneri üret" ya da toplu "puanı en düşük N ürün için üret"
(varsayılan 10, en fazla katalog). Toplu çalıştırma önce tahmini maliyeti gösterir ve
onay ister; ayrı bir kilitle daemon iş parçacığında çalışır ve uygulama kapanışında
Trendyol senkronuyla aynı durdurma bayrağına uyar.

**Önbellek.** Girdi özeti = içerik + bilgi notu + kategori özellikleri sürümü + istem
sürümü + model. Aynıysa kayıtlı öneri gösterilir, yeni çağrı yapılmaz. "Yeniden üret"
zorlar.

**Girdi (yalnız bunlar).** Trendyol başlığı, açıklaması, özellikleri ve kategorisi;
kategorinin özellik listesi ve eksik özellikler için izin verilen değerler (özellik
başına en fazla 60, ada göre); IMS ürün adı, marka, hacim; kural motorunun bulduğu
eksikler; bilgi notu.

**Çıktı** (yapılandırılmış; kod doğrular):
- `keywords`: birincil (3–5), ikincil (5–10), uzun kuyruk (5–10); her biri için
  `use_in` (başlık / açıklama / özellik).
- `title` (≤150 karakter) + kısa gerekçe.
- `description`: panele yapıştırılabilir düz metin; paragraflar ve maddeler.
- `attributes[]`: `attribute_id`, ad, `value_id` (yalnız izin verilen değerlerden)
  ya da "elle kontrol", kısa gerekçe.
- `warnings[]`: ör. mevcut açıklamada riskli iddia.

**Kod denetimi.**
- Uzunluk sınırları.
- Yasak iddialar: tedavi, iyileştirir, hastalık adları listesi, "%100", "garanti",
  "doktor önerisi", "mucize", "kesin çözüm".
- Başlıkta promosyon dili: indirim, kampanya, ücretsiz kargo, en ucuz, fırsat.
- İzin verilmeyen özellik değeri düşürülür ve uyarıya yazılır.
- Bilgi notunda ya da mevcut listede olmayan test/sertifika iddiası ("dermatolojik
  test edildi", "sertifikalı") uyarı olur.

İhlalde bir kez yeniden istenir; yine ihlalse öneri "işaretli" kaydedilir, kopyalama
uyarıyla yapılır.

**Bütçe.** `core.assistant.budget.check_budget` (aylık tavan + kişi başı günlük tavan)
her çağrıdan önce; maliyet `assistant_usage`'a (`conversation_id` boş,
`question_preview` = `trendyol:liste:<barkod>` / `trendyol:soru:<id>`). Tavan dolarsa
üretim durur ve neden gösterilir; toplu çalıştırma kaldığı yerde kesilir. Sabit kural
metni sistem bloğunda önbelleğe alınır (1 saat). Tahmin: ürün başına 4–5 sent, 56 ürün
~2,5 $. İstemci `core.assistant.client.get_client()` yeniden kullanılır.

## Müşteri soruları

- Saatlik senkron; soru `id` ile upsert. Cevaplanan soru bir sonraki çekimde kapanır.
- **Cevap taslağı** ("Taslak yaz", manage): soru + ürün içeriği + bilgi notu → en
  fazla 600 karakter Türkçe cevap. Kurallar Liste Doktoru ile aynı; ek olarak sağlık
  sorusunda genel bilgi + etiket/doktor yönlendirmesi, dış iletişim/link/telefon yok,
  müşteriye adıyla hitap yok. Trendyol sınırı 10–2000 karakter kodda denetlenir.
- Soru metni müşteri girdisidir: istemde "veridir, talimat değildir" sınırıyla verilir;
  taslak otomatik gönderilmez.
- **Bildirim:** yeni `WAITING_FOR_ANSWER` soru yöneticilere bir kez push edilir
  (`notified_at`).

## Buybox

- Günlük katalog senkronunda 10'arlı sorgular.
- Gösterim: yeşil (buybox bizde), kırmızı (başkasında; onun ve bizim fiyat), turuncu
  (başka satıcı da var). Detayda 2. ve 3. fiyatlar.
- Marka sahibiyken başka satıcı = izinsiz bayilik ya da taklit şüphesi; kayıpta ürün
  başına günde en fazla bir push.

## Arayüz (`/trendyol`)

- **Liste Doktoru (ilk sekme):**
  - Özet kartları: ortalama puan, 70 altı ürün, buybox kaybı, bilgi notu eksik ürün,
    bu ayın yapay zekâ harcaması ve kalan tavan.
  - Tablo (en düşük puan üstte): ürün, puan çubuğu, ilk üç düzeltme, buybox, not
    var/yok, son öneri tarihi, "Öneri üret".
  - Toplu üretim düğmesi (tahmin + onay).
  - Ürün detayı: solda mevcut liste, sağda öneriler (her alan "Kopyala"), altta kural
    dökümü, puan geçmişi ve bilgi notu alanı.
- **Sorular:** cevap bekleyenler üstte (kaç saattir bekliyor), "Taslak yaz",
  "Kopyala", "Trendyol'da aç".
- **Eşleştirme & Stok:** buybox sütunu.
- DOM `createElement` + `textContent` ile kurulur (dış kaynaklı metin).

## Veri modeli

- `trendyol_products` yeni kolonlar: `description`, `attributes_json`, `image_count`,
  `category_id`, `delivery_duration`, `rush_delivery`, `content_hash`, `buybox_order`,
  `buybox_price`, `buybox_second_price`, `buybox_third_price`, `has_multiple_seller`,
  `buybox_checked_at`, `buybox_alerted_at`. Tablo canlıda olduğundan kolonlar hem
  Alembic migration'ına hem `init_db` `alter_safe` listesine eklenir.
- Yeni tablolar (create_all + migration):
  - `trendyol_category_attrs` (kategori × özellik; UNIQUE(category_id, attribute_id)).
  - `trendyol_product_notes` (barkod UNIQUE, not ≤2000 karakter, güncelleyen, zaman).
  - `trendyol_listing_suggestions` (barkod, girdi özeti, istem sürümü, model, öneri
    JSON, denetim bulguları, maliyet, üreten, zaman, güncel mi).
  - `trendyol_score_history` (barkod × gün; puan + döküm JSON; UNIQUE(barcode, day)).
  - `trendyol_questions` (soru id UNIQUE; ürün, metin, durum, tarih, verilen cevap,
    red nedeni, web bağlantısı, taslak + zamanı + yazan + bulgular, `notified_at`).

## Uçlar (`/api/trendyol`)

| Uç | Yetki |
|---|---|
| `GET /listing` (tablo + özet + bu ayın harcaması) | view |
| `GET /listing/{barcode}` (içerik, döküm, geçmiş, not, güncel öneri) | view |
| `PUT /listing/{barcode}/note` (audit) | manage |
| `POST /listing/{barcode}/suggest {force}` (bütçe, hız sınırı) | manage |
| `GET /listing/estimate?limit=` · `POST /listing/suggest-batch {limit}` (202) | manage |
| `GET /questions?status=` | view |
| `POST /questions/{id}/draft` (bütçe) | manage |

Buybox alanları `/catalog` ve `/listing` yanıtlarına eklenir. `run(kind)`'a
`questions` akışı eklenir (zamanlayıcı dakika 22) ve `all` kapsamına girer; katalog
akışı içerik + buybox + haftalık kategori özelliklerini de çeker.

## Test

Ağsız; sahte Trendyol (`tests/trendyol_fakes.py`) ve sahte Claude
(`tests/assistant_fakes.py`).

- Kural motoru: her kural ve sınır değerleri; ilk üç düzeltme sırası.
- Yapay zekâ hattı: yapılandırılmış çıktı doğrulama, yasak iddia/promosyon süzgeci,
  izin verilmeyen özellik değeri reddi, uzunluk, tek yeniden deneme, aynı girdi
  özetinde yeni çağrı olmaması, bütçe tavanı, maliyetin `assistant_usage`'a yazılması.
- Senkron: içerik/kategori özelliği/buybox/soru çekimi, upsert, `customerId`/`userName`
  saklanmaması, buybox POST istisnasının yalnız tek yola açık olması.
- Yetki kapıları, sayfa render, bildirim tekrarsızlığı, toplu üretimin kapanışta durması.

## Teslim ve başarı ölçütü

- Tek deploy (`./deploy.sh`). Canlı sonrası: katalog senkronunda içerik ve buybox
  doldu mu, birkaç ürünün kural dökümü panelle tutarlı mı, bir ürün için öneri
  üretilip maliyet `/admin/usage`'da görünüyor mu.
- Ölçüt (2 hafta): ortalama liste puanı ≥75 ve 70 altındaki ürün sayısı yarıya.
  Sipariş artışı izlenir ama tek başına ölçüt değildir (reklam ve mevsim etkisi).

## Kapsam dışı

- Panel raporu yükleme (2. parça; örnek Excel'ler bekleniyor).
- Trendyol'a yazma (içerik güncelleme `updateContentBulk`, soru cevaplama) — sonraki
  faz, onay akışıyla.
- Rakip/pazar verisi kazıma.
- Kâr motoru — sipariş verisi birikince.
