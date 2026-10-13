# Satın Alma Planı
Yetki: reports.view
Sayfa: /satin-alma

Ürün × adet girince hangi malzemeden ne kadar, kimden ve kaça alınacağını hesaplar; PDF/Excel alım listesi üretir.
1. "Ürünler" bölümünde reçeteli ürünleri seç ("Ara", "Tümü", "Temizle", "Yalnız seçilenler") ve adet yaz; "Toplu adet" ile "Seçililere" / "Markaya" / "Tümüne" aynı adedi dağıt. "Geçmiş üretimden doldur" tarih aralığındaki üretim adetlerini getirir.
2. Satırın ⚙ simgesiyle başka ürünün reçetesini, ölçeği ya da ek ambalajı tanımla; "Serbest satırlar" ile koli/palet gibi reçete dışı kalemleri ekle ("Satır ekle").
3. "Seçenekler": "Stok modu" (net/brüt), "Etiket" (Türkçe / İngilizce / Hariç / Yeni basılacak), "Alım firesi (%)", "Para birimi" (USD/EUR/TRY), "Hariç tutulan kalemler" (su kartları varsayılan hariç).
4. "Önizle": malzeme bölümleri, her satırda seçilen tedarikçi, fiyat, tutar ve "Tedarikçiler (N)" alternatifleri; eksik tanımlı satırlar "Bekletilen kalemler"de.
5. "PDF indir" / "Excel indir" (Excel formüllüdür). "Rapor" bölümündeki "Başlık", "Kontrol listesi sorumlusu" ve "Notlar" çıktıya basılır.
6. Senaryo: "Kaydet" / "Farklı kaydet" ile adlandırıp sakla, "Yeni plan" ile sıfırla, "Sil" ile kaldır. Taslak tarayıcıda kendiliğinden saklanır. Açılan senaryoda artık olmayan kart/reçete "eksik kayıt" kutusunda görünür; "Kaldır" denmedikçe korunur ama hesaba katılmaz.

Dikkat: Fiyatlar raporu gören herkese açıktır. Kur yalnız rapor para biriminden farklı fiyat varsa çekilir; litre kalemine kg fiyatı uygulanınca "1 l ≈ 1 kg" notu düşer. Senaryoyu yalnız sahibi, Manager ya da SuperAdmin düzenler/siler. Raporlar'daki "Üretim Stok Analizi" hızlı sürümdür; "Gelişmiş: Satın Alma Planı →" bağlantısı buraya getirir.
