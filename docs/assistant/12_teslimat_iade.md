# Teslimat (müşteriye gönderim, kargo) ve iade
Yetki: inventory.view (görme) · inventory.adjust (teslimat / iade kaydı)
Sayfa: /delivery · /returns

Teslimat:
1. Teslimat sayfasında "Anında Teslim" (elden) ya da "Kargo / Sevkiyat" modunu seç.
2. "Ürün Okut" alanında barkodu okut ("Telefon Kamerasıyla Tara") ya da "Barkodsuz Bitmiş Ürünler" listesinden ekle; "Teslim Edilecekler"de adedi − / + ile ayarla.
3. "Alıcı Bilgileri": "Ad Soyad", "Firma / Kurum", "Telefon", "Tür" (Hediye / Numune / Proforma Fatura / Diğer), "Yöntem" (Elden / Kargo), "Belge Dili"; proformada adres, ülke, para birimi, şartlar ve "Proformadaki banka hesapları".
4. "Teslimatı Tamamla": elden teslimde stok hemen düşer. Kargoda kayıt "Bekleyen Kargo Sevkiyatları"na girer; "Takip No Gir & Kargola" → "Taşıyıcı Firma" + takip no → "Kargola (stok düş)" ile stok o anda düşer; "Kargo Bacağı Ekle" parçalı sevkiyat içindir.
5. Proforma türünde belge "Onay Bekleyen Proformalar"a düşer; yetkili "Onayla" / "Reddet" der, "Düzenle" ile düzeltir. "Son Teslimatlar" geçmişi listeler.

İade:
6. İadeler sayfasında "Belgeden İade" ("Teslimat Belgesi Seç" → "Geri Gelen Kalemler") ya da "Serbest İade" ("Ürün Ekle"). Her kalemde durum: Sağlam / Hasarlı / Açılmış.
7. "İade Bilgisi": "Kanal" (Teslimat dönüşü / Toplantı dönüşü / Online satış iadesi / Diğer), "İade Eden", "Gerekçe / Not" → "İadeyi Al (sağlamlar stoğa döner)". Yalnız Sağlam kalemler stoğa döner.

Dikkat: Düşüş lot bazında FIFO ile, lot başına ayrı Output hareketiyle yazılır. QC bekleyen ve şahit numuneye bağlı lotlar sevke uygun sayılmaz. B2B siparişlerinin sevki buradan değil, B2B Siparişleri sayfasındaki "Siparişi sevk et" adımından yapılır.
