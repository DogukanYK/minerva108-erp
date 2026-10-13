# "Aynı malzeme" grupları
Yetki: items.view (görme) · items.edit (grup kurma/dağıtma)
Sayfa: /items → "Aynı malzeme önerileri"

Aynı malzemenin her tedarikçisi ayrı kart olarak tutulur; bu kartlar bir "aynı malzeme" grubuyla bağlanır. Sistem kendiliğinden gruplamaz, lab onaylar. Yalnız gerçekten eşdeğer (birbirinin yerine kullanılabilen) kartları gruplayın.
1. Ürünler sayfasında "Aynı malzeme önerileri (N)" düğmesine bas; TR/EN eşanlamlı adlardan üretilen öneriler listelenir.
2. Her öneri için "Aynı malzeme — grupla" ya da "Farklı" seç ("Gruba bağlama" öneriyi kapatır). Aynı tedarikçinin mükerrer kartıysa gruplama yerine "Kopya Kararları"nda birleştir.
3. Kart listesinde grup rozeti görünür ("Aynı malzeme · N kart"); rozetten "Adını değiştir" ve "Grubu dağıt" yapılır. Bir kart tek grupta olabilir; ikiden az aktif üye kalınca grup kendiliğinden dağılır.
4. Grubun etkisi:
- Stoklar ve Aylık Stok Raporu: grup tek satırda toplam stokla görünür, altında kart dökümü (ör. "HAMMADDESEPETİ 180 g · YİĞİTOGLU KİMYA 24,7 kg"); satıra tıklayınca kartlar kendi "Düzelt" düğmesiyle açılır.
- Eksik Hammaddeler, Dashboard kritik stok ve düşük stok bildirimi grubun toplamına bakar: toplam, kartların en büyük minimumunun altına inmedikçe uyarı çıkmaz.
- Satın Alma Planı gruptaki kartların stoğunu ihtiyaçtan düşer ("Aynı malzeme grubundaki stokları say").
- Üretim ve B2B partileri gerekirse gruptaki başka karttan düşer ("Hangi tedarikçiden?").
- Lot taşıma ve numune çevirme pencereleri gruptaki kartları önce önerir; Mal Kabul'de "Yeni kart aç (aynı malzeme grubuna)" çıkar.

Dikkat: Yalnız çevrilebilen birimler toplanır (g ↔ kg, ml ↔ l); g ile ml toplanmaz, "ayrı birim" olarak gösterilir. Üretim ve Satın Alma ise yalnız aynı birim ve türdeki aktif kartı yerine sayar (etiket stoğu şişe yerine geçmez). Grup kart adını ya da stoğunu birleştirmez; her stok kendi kartında kalır. Üretimde kaynak seçimi yönetici ayarıyla kapatılırsa Satın Alma, Eksik, Dashboard ve bildirim eski kart başı kurala döner; Stoklar ve Aylık rapor yine gruplu görünür. Geçmiş aylar bugünkü gruplamayla gösterilir.
