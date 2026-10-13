# Numuneyi stoğa çevirme
Yetki: inventory.receive
Sayfa: /items → "Numune" sekmesi

Alternatif tedarikçiden gelen numune lotları stok DEĞİLDİR: üretimde kullanılmaz, kart stoğuna girmez. Değerlendirme olumluysa deftere şöyle işlenir:
1. Ürünler sayfasında "Numune" sekmesini aç; ilgili satırda "Stoğa Çevir"e bas.
2. "Numuneyi stoğa çevir" penceresinde hedefi seç: "Bu kart" (numunenin girildiği kart), "Başka kart" (aynı malzemenin başka tedarikçi kartı — adaylar listelenir, arama yapılabilir) ya da "Yeni kart aç" (numunenin tedarikçisiyle, aynı birim ve kategoride; ad yazılır).
3. Hedef kartta numune miktarını zaten karşılayan elle "Stok düzeltme" varsa çift sayım bandı çıkar: miktar daha önce elle eklendiyse "Zaten sayıldı — yalnız lotu bağla" (stok ve defter değişmez, lot normal lot olur); eklenmediyse "Yine de stoğa ekle".
4. Alt düğmeye bas: "Bu karta stoğa çevir" / "Seçilen karta stoğa çevir" / "Yeni kart aç ve stoğa çevir". Input hareketi yazılır, kart stoğu artar, lot "Numune stoğa çevrildi" notuyla normal lota döner.
5. Yeni kart adı mevcut bir kartla çakışırsa pencere o kartı önerir: "Bu kartı seç" ya da "Yine de yeni kart aç".

Dikkat: Numune yanlış karta girildiyse önce Numune sekmesindeki "Karta taşı" ile doğru karta taşı, sonra çevir — yoksa stok yanlış tedarikçinin kartına yazılır. Fason şahit lotları bu sekmede görünmez ve çevrilemez. Numune girişi aynı sekmedeki "Numune Girişi" → "Numuneyi Kaydet" ile yapılır.
