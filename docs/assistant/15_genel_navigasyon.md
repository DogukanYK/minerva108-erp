# Hangi iş hangi sayfada
Yetki: her sayfanın kendi görme yetkisi
Sayfa: sol menü

1. Kart arama: Ürünler sayfasında arama kutusu ad, Türkçe ad, kategori ve barkoda bakar (Türkçe karaktere duyarsız); "Hammadde / Ambalaj / Bitmiş Ürün / Numune" sekmeleri türe göre süzer. Kart satırına tıklamak düzenlemeyi açar; "Yeni Ürün Ekle"de birim seçimi zorunludur.
2. Lot takibi: İzlenebilirlik sayfasında lot numarasını yazıp "İzle": kaynak lotlar, üretim detayı, QC formu ve işlem geçmişi tek ekranda. Lot numarası ürün bazlıdır; aynı numara birden çok üründe olabilir, listeden ürünü seç.
3. Stok ve hareketler: Stoklar sayfası kart bazlı güncel stok + "Stok Lokasyon Detayı" (lotlar); İşlem Defteri yalnız hareketlerdir ("Hepsi / Hammadde / Ambalaj / Etiket / Bitmiş Ürün" filtresi). Elle düzeltme Stoklar → "Düzelt" → "Stok Düzeltme" (miktar + gerekçe) → "Düzeltmeyi Kaydet" (inventory.adjust).
4. Raporlar sayfası panelleri: "Üretim Stok Analizi" (hızlı simülasyon + Excel), "Üretim Geçmişi", "Tedarikçi Fiyatları", "Aylık Stok Raporu" (DONDURULMUŞ KAYIT / CANLI HESAP rozeti), "En Çok Tüketilen Hammaddeler", "Haftalık Üretim Performansı", "Eksik Hammaddeler" ("Kritik Altı" sayacı, "Sipariş ver" işareti).
5. Dashboard: "Kritik Stok Uyarıları", "Son İşlemler", "Son Üretim Faaliyetleri" ve "Benden bekleyen işler" (B2B sipariş adımları + fason onayları).
6. Panel: ürün verisi aktif panele göre süzülür; kayıt görünmüyorsa üst çubuktaki "Panel değiştir" düğmesiyle diğer panele geç. PDKS, Dosya Paylaşım, CRM panelden bağımsızdır.
7. Yönetim: kullanıcılar, roller ve yetki matrisi "Yönetim Paneli"nde (admin.*); "Sistem Durumu" yalnız SuperAdmin.

Dikkat: Menüde görünmeyen sayfa = o yetki yok; yöneticiye başvur. Sayfa adresleri giriş bölümündeki listede.
