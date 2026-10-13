# Fason (dış) üretim
Yetki: outsourcing.view · manage (firma/iş) · mapping (malzeme kodu, kap) · approve (onay) · dispatch (sevk) · record (tüketim/iade/kabul)
Sayfa: /outsourcing

Dış üreticiye yalnız KODLU föy, etiket ve sevk listesi gider; gerçek malzeme adları yalnız "Malzeme kodları" sekmesinde görünür.
1. "Fason üreticiler" sekmesi → "Firma adı", "İlgili kişi" → "Üreticiyi kaydet".
2. "Malzeme kodları" sekmesi → üretici + "Malzeme kartı" seç, "Dışarıda kullanılacak kod" ve güvenlik talimatını yaz, "Malzeme kimliğini kontrol ettim…" onayını işaretle → "Kimliği ve kodu kaydet". Kod kalıcıdır; kart adı değişse de değişmez.
3. "Üretim işleri" → "Yeni iş hazırla": üretici, "Reçete / ürün", "Üretilecek adet", etiket dili, föyde görünecek ürün adı, üç onaylayıcı ("Teknik onay" = LabLead, "İş sahibi onayı" = SuperAdmin, "Yönetici onayı" = Manager; üç farklı hesap) ve kodlu talimatlar → "İşi hazırla".
4. "İşi aç" → "Malzeme ve kap hazırlığı": "Kaynak lottan kap hazırla" — her kap tek kaynak lot + tek dolum; hammaddede lot zorunlu (APPROVED, QC'si bitmiş, numune değil, SKT'si geçerli). Her değişiklik yeni paket sürümü yapar ve verilen onayları sıfırlar.
5. "Paket onayları": önce teknik, sonra iş sahibi ve yönetici "Kendi hesabımla onayla" der (çizilen imza/şifre yok).
6. "Gönderim ve kapanış": "Kapları gönder" → kapları seç → "Seçilen kapları gönder" (etaplı; kap başına Output, yerel stok düşer; ilk sevkten sonra paket donar). Üreticiden gelen rakamlarla "Tüketim / fire kaydet" (yerel stok tekrar düşmez).
7. "Fiziksel iade al" ve "Bitmiş ürün al" (toplam + şahit adedi) ayrı KARANTİNA lotları açar → Kalite Kontrol sayfasında karar. "İşi tamamla" için dış bakiye 0 ve tüm QC kararları verilmiş olmalı.

Dikkat: "İşi iptal et" stoğu kendiliğinden iade etmez; sevk sonrası bakiye sıfırlanmadan iptal yoktur. Fason şahidi "Numune" sekmesine düşmez ve stoğa çevrilemez. Distribütör hesabı yetki verilse bile sayfayı açamaz. Birim dönüşümü g↔kg ve ml↔l kesindir, kg↔l yapılmaz.
