# Tedarikçi satın alma durumu ve fiyatları
Yetki: suppliers.status (durum) · suppliers.prices (elle fiyat) · suppliers.merge (birleştirme) · items.create/edit (kart ekle/düzenle)
Sayfa: /suppliers

Satın alma durumu:
1. Tedarikçiler sayfasında firmanın satırındaki "Satın alma durumu" simgesine bas.
2. "Normal", "Tercih edilen" ya da "Bitirilecek — alma" seç; bitirilecek için "Sebep" zorunludur. "Kaydet". Rozet listede görünür; üstteki filtre "Tercih edilen" / "Bitirilecek — alma" süzer.
3. Etkisi: Satın Alma Planı tercih edilen firmayı öne alır; bitirilecek firmadan eldeki bitince yeni alım önermez.

Fiyatlar:
4. Satırdaki "Fiyatlar" simgesi firmanın kayıtlı fiyatlarını açar; "Fiyat ekle" ile malzemeyi ara, "Birim fiyat", "Para birimi", "Alınabilecek miktar", "KDV dahil mi?" (dahilse "KDV oranı (%)" zorunlu) ve "Not" gir, kaydet. Aynı form Raporlar → "Tedarikçi Fiyatları" panelinden de açılır.
5. Excel'den toplu fiyat (Stok Son Durum) yalnız Raporlar → "Tedarikçi Fiyatları" → "Excel'den İçe Aktar" ile ve finans yetkili rollerle yapılır.

Birleştirme:
6. Mükerrer firma için "Birleştir…" simgesi; "Yönü değiştir" ile kalacak kartı seç, "Birleştir". Pasife alınan kartın ürün kartları, lotları, fiyatları ve tercihleri kalan karta taşınır; geri alma yoktur.

Dikkat: Karşılaştırma ve plan tutarı NET fiyatla yapılır; KDV dahil satır her yerde "KDV %… dahil" etiketiyle görünür. "Seçilenleri Pasife Al" firmayı silmez, listeden gizler ("Pasifleri göster" ile geri açılır).
