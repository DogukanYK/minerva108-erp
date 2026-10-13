# B2B sipariş akışı (teklif → onay → proforma → üretim → sevk)
Yetki: b2b_orders.view · manage · tech_review · sign · payment · produce · ship; fiyat/banka/proforma ayrıca b2b.view
Sayfa: /quotations → /b2b-siparisler

1. B2B Teklifler sayfasında taslak teklifte "Siparişe dönüştür": etiket dili, hedef tarih, yükleme günü, banka hesapları (en fazla 3) ve şartlar → teklif ORDER olur, sipariş SUBMITTED durumunda açılır. Stok bu adımda düşmez ("Onayla & Düş" bu teklifte kullanılmaz).
2. B2B Siparişleri → "Siparişler" sekmesi → "Aç". Atanan teknik kişi "Teknik değerlendirme" yapar (satır başına stoktan kullan / üret, reçete, eksikler) → TECH_REVIEWED.
3. Atanan imzacı "İmzala ve onayla": şifre + çizilen imza → APPROVED; proforma PDF imzalı ticari sürümden basılır. Siparişi açan kişi imzacı olamaz (dört göz).
4. "Ödeme kaydet": prepaid (%100 üretim öncesi) / advance (%X üretim, kalanı sevk öncesi) / net (ödeme durdurmaz). "Son hazırlık kontrolü" → "Hazırlık tamam" alım satırlarını kapatır.
5. "Parti başlat": hammadde o an lot bazında düşer (yalnız lot numaralı, APPROVED, SKT'si geçerli lotlar; yetmezse hiçbir şey yazılmaz). Reçetedeki kart bir "aynı malzeme" grubundaysa kaynak kart otomatik seçilir (bitirilecek/alma firma → reçetedeki kart → tercih edilen → diğer); iç teknik föyde "X (reçetede: Y)" yazar. "Tamamla": bitmiş ürün + şahit stoğa girer; şahit müşteri adedinden düşer, eksik yeni partiyle tamamlanır.
6. "Siparişi sevk et": açık parti yok + ödeme koşulu sağlanmış + her satırda sevke uygun stok ≥ sipariş → tek sevkiyat, stok bir kez düşer → SHIPPED.
7. Değişiklik: "Ticari revizyon" → "Yeni ticari sürüm kaydet"; ürün/adet/dil değişirse teknik + imza, yalnız fiyat/şart/banka değişirse yalnız imza yenilenir. "Sorumluları değiştir" atamaları günceller. "İç teknik föy" (fiyatsız) sipariş ekranından indirilir, müşteriye gönderilmez.

Dikkat: Başlamış parti iptali iade yazmaz; fiziksel iade gerekçeli +Düzeltme ile ve tüketimle sınırlıdır. Banka profilleri "Banka hesapları" sekmesinden (ülke + para birimi kuralı) yönetilir. Bekleyen adımlar Dashboard'daki "Benden bekleyen işler" kutusunda ve push bildirimiyle gelir. Beş hatalı şifre imzayı 15 dakika kilitler.
