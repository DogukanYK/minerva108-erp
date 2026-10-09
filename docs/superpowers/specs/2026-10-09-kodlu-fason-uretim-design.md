# Minerva IMS — Kodlu fason üretim

Durum: Uygulandı ve canlıda — 09.10.2026, commit `929f891` (`./deploy.sh`, test kapısı 1468 geçti); Işık'ın `outsourcing.view` + `approve` yetkisi `scripts/grant_outsourcing_isik_20261009.py --commit` ile yazıldı. Devralma öncesi durum: uygulama kısmen başlamış, kullanım limiti sırasında yarım kalmıştı (bkz. `2026-10-09-claude-devralma-notlari.md`). Güncel sözleşme: `CLAUDE.md` → "Kodlu fason üretim".

Bu belgeyi okumak tek başına yeniden uygulama veya canlı geçiş talimatı değildir. Kullanıcının seçtiği plan için sonraki açık devam talimatı beklenecek.

## Kararlar

- Üretim ekranında Fason Üretim bölümü; tam üretim, dolum ve ambalajlama.
- Üreticiye özel kalıcı kodlar; aynı malzeme ve doğrulanmış tanım için aynı kod.
- Gerçek malzeme/kod eşleştirmesi Minerva içinde ayrıca yetkilendirilir.
- Dış firma yalnız kodlu PDF ve etiket alır; portal veya IMS hesabı yok.
- Songül teknik onayı ilk; Doğukan ve Işık kendi hesaplarından sonraki onayları verir. Üç ayrı kişi zorunlu; çizilen imza/şifre yeniden doğrulaması yok.
- Artan tüm malzemeler iş sonunda geri gelir; işler arasında dış stok devri yok.

## Kodlar ve paket

- Kodlar hammadde kartı ve laboratuvarca doğrulanan özellik sürümüne bağlanır; grup eşdeğerliği otomatik kabul edilmez.
- Kod başka malzemeye verilmez, eski eşleştirmeler değiştirilmez. Ad değişikliği kod değiştirmez; malzeme niteliği değişikliği yeni kimlik ve kod gerektirir.
- Kap kimliği tek doluma ve tek kaynak lotuna aittir; bölme/yeniden dolum yeni kimlik üretir.
- QR yalnız kap kimliği taşır. Kaynak isim/tedarikçi/lot/SKU/kart ID/maliyet veya iç URL içermez.
- Kodlu föy, kap etiketleri, sevk manifestosu ve tüketim/fire/iade formu aynı sabit paket sürümünden üretilir.
- Dış DTO alanları izin listesiyle oluşturulur. Serbest iç notlar dışarı kopyalanmaz; ayrı kodlu süreç ve güvenlik talimatı Songül tarafından hazırlanır.
- Dosya adları, PDF metin/metadata/ekler ve fiziksel ambalajlar isim sızıntısı için kontrol edilir.
- Mevcut reçete ölçekleme ve fire hesabı; ambalaj/etiket fireden muaf. Tartım hedefi ile sevk fazlası ayrı. g/kg ve ml/l dönüşümü kesin, kg/l dönüşümü yok.

## Akış

1. Firma, reçete, adet, dil, kaynak lot ve kaplar hazırlanır. Önizleme/etiket stok hareketi değildir.
2. Paket sürümü, belge özeti, kullanıcı ve zamanla üç hesap onayı alınır. Sevk öncesi değişiklik onayları yeniler.
3. Sevkte güncel Item.current_stock ve lotlar yeniden doğrulanır; hammadde onaylı, QC'siz, numune olmayan, SKT/lotu dolu ve geçerli olmalıdır. Yerel stok bir kez düşer, fason bakiyesi oluşur.
4. Songül kod/kap bazında fiilî tüketim, fire ve sapma gerekçesini kaydeder. Yerel stok tekrar düşmez.
5. Bitmiş ürün ve fiziksel kullanılmamış iade ayrı karantina lotuna alınır. İade miktarı kalan kap bakiyesini aşamaz; özgün lot/tedarikçi/SKT korunur, kaynak satır sonradan değişse/silinse de snapshot kalır.
6. QC onayı tek Input ve kullanılabilir stok artışı oluşturur; red stok oluşturmaz. Şahit ayrı tutulur. Tüm QC ve kabul işleri bitmeden kapanış olmaz.

Gönderilen = fiilî tüketim + belgeli fire/kayıp + fiziksel iade. Kapanışta dış bakiye sıfırdır. İptal otomatik stok iadesi değildir.

İlk fiziksel sevkten sonra paket değişmez. Etaplı sevk aynı sürümün onaylı miktarı içindedir; üretim değişikliği yeni iş/parti gerektirir.

## Entegrasyon ve doğrulama

- Ayrı üretici, malzeme kodu, kap, iş/sürüm, onay, sevk, tüketim ve kabul kaynakları; /api/outsourcing API.
- Sunucuda yetki, atanan kullanıcı, aktif domain, paket sürümü kontrolü; gizli eşleştirme ayrı izin.
- Karantina lotları normal mal kabul/upsert ile birleşmez. İki QC endpoint'i ortak fason kaynak kontrolüyle tek stock posting oluşturur.
- Sevk transferi/tüketim ayrı raporlanır; aylık stok Input/Output hesapları tutar.
- Alembic ve init_db birlikte güncellenir; production.source_choice.enabled kapalı kalır.
- Testler: kalıcı/ayrı kodlar, faz/kap/lot karışıklığı, PDF/QR/metadata gizliliği, üç hesap/sürüm/domain/yetki, mükerrer ve eşzamanlı stok işlemleri, karantina/iadeler/fire/bakiye, QC ve aylık rapor, mevcut günlük üretim/mal kabul regresyonları.
- Önce test veritabanında örnek ürün/firma ve fiziksel ölçüde etiket/PDF kontrolü; ardından yedek ve tam test kapısıyla ./deploy.sh.

## Gizlilik sınırı

Kodlama işlem adımları ve miktarlardan tahmini bütünüyle engellemez. Gerekli güvenlik bilgileri korunur; malzeme bazında kodlu etiket uygunluğu laboratuvarca kontrol edilir.
Kaynak: https://webdosya.csb.gov.tr/db/cygm/icerikler/sea-alternat-f_ad_taleb-_rehber-_20210827-20210827145957.pdf
