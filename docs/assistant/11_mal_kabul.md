# Mal kabul (hammadde / ambalaj girişi)
Yetki: inventory.receive
Sayfa: /receiving

1. Mal Kabul sayfasında "Yeni Mal Kabul Formu"nda "Hammadde / Ambalaj" kartını ve "Tedarikçi"yi seç.
2. "Lot / Parti No" (zorunlu), "Son Kullanma Tarihi", "Miktar" (kart birimiyle) ve "Depo Konumu" gir.
3. Seçilen tedarikçi kartın tedarikçisinden farklıysa uyarı bandı çıkar: "Bu tedarikçinin kartı «…» — oraya kabul et", "Yeni kart aç (aynı malzeme grubuna)" ya da "Kartı aç ve seç". Her tedarikçinin kendi kartı olmalıdır.
4. "Kabul Et ve Kaydet": lot APPROVED statüsüyle açılır, Input hareketi yazılır, kart stoğu artar; "Mevcut Depo Durumu" yenilenir. Aynı kart ve lot numarası tekrar girilirse mevcut lota eklenir.

Dikkat: Alternatif tedarikçi numunesi buradan değil, Ürünler → "Numune" sekmesi → "Numune Girişi" ile girilir (stok sayılmaz). Fason iade/mamul kabulü Fason Üretim sayfasından yapılır. Bitmiş ürün girişi yalnız üretimle olur. Yanlış karta girilen lot "Taşı" ile düzeltilir, silinmez.
