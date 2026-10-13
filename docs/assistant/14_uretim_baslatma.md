# Üretim başlatma
Yetki: production.create
Sayfa: /production → "Yeni Üretim Başlat"

1. Üretim sayfasında "Yeni Üretim Başlat"a bas; "Reçete Seç", "Üretilecek Miktar", "Üretim Dili (Etiket)" (Türkçe / İngilizce) doldur.
2. "Lot Numarası": reçete seçilince o üründen üretilen son lot numarası gösterilir ve sıradaki önerilir; gerekirse düzenle. "Şahit Numune Adedi" dolaba ayrılacak adettir.
3. Önizleme tablosunda her hammaddenin brüt ihtiyacı (fire dahil; ambalaj/etiket firesiz) ve stoğu görünür; eksik satır işaretlenir. Bir hammaddede birden çok lot varsa "— lot seçimi:" alanından kaynak lotu seç; seçmezsen en eski APPROVED lot (FIFO) kullanılır.
4. "Üretimi Başlat": hammadde/ambalaj lot bazında düşer (her lot ayrı Output hareketi), bitmiş ürün lotu açılır, şahit numune dolaba yazılır, "Üretim Föyü" oluşur ("Yazdır", "Excel İndir").

Dikkat: Seçilen lot yetersizse üretim durur, başka lota taşmaz. Numune lotları ve QC kararı verilmemiş lotlar havuzda yoktur. "Hangi tedarikçiden?" kaynak kartı seçimi ("Tek kart" / "Böl" / "Öneriyi kullan") şu an kapalıdır; tüketim yalnız reçetedeki karttan yapılır. Etiket, reçetedeki etiket grubundan doğru dilli kartı kendisi seçer. B2B sipariş partileri Üretim sayfasındaki "Müşteri Siparişleri" panelinden değil, B2B Siparişleri sayfasından başlatılır.
