# B2B siparişinden onay, proforma ve üretime geçiş

**Kayıt tarihi:** 9 Ekim 2026 (Türkiye).

**Durum:** Uygulandı ve canlıda — 09.10.2026, commit `246d48d` (`./deploy.sh`, test kapısı 1483 geçti). Aynı gün (Faz 7) beğenilen proforma şablonu + belge başına 1–3 banka (RUB IBAN dahil) ve düzenlenebilir şartlar, §2'deki iç teknik föy (`GET /api/b2b-orders/{id}/technical-sheet`) ve SKT kontrol raporu eklendi. Ayrıntılı ve güncel sözleşme: `CLAUDE.md` → "B2B sipariş akışı", "Proforma şablonu", "SKT kontrol raporu".

**Devam koşulu (tarihçe):** Plan 9 Ekim 2026'da "başlama, kaydet" talimatıyla bekletilmişti; kullanıcı aynı gün devam talimatı verdi ve plan uygulandı.

---

## 1. Amaç ve akış

Mevcut **B2B ekranı, hacimli müşteri siparişlerinin merkezi** olacak. Songül Hanım günlük küçük laboratuvar üretimlerini mevcut yetkisiyle sürdürecek; bu işler Işık Hanım’ın onayını gerektirmeyecek. Ayrım adet eşiğiyle değil, siparişin B2B akışına bağlı olmasıyla yapılacak.

| Aşama | Sorumlu | Yapılacak işlem |
|---|---|---|
| Taslak | Doğukan | Müşteri, ülke, ürün, adet, fiyat, etiket dili, hedef tarih ve ödeme koşulunu girer; ticari onayla laboratuvara gönderir. |
| Teknik değerlendirme | Songül | Reçeteyi, ambalajı, etiketleri ve stokları kontrol eder. Mevcut bitmiş stoktan kullanılacak adedi ürün bazında seçer; kalan üretimi ve eksikleri çıkarır. |
| Yönetim onayı | Işık | Son siparişi, eksik alım listesini ve üretim planını kendi hesabı, şifre doğrulaması ve çizilen imzasıyla onaylar. |
| Proforma | Sistem | Gerekli onaylar tamamlandığında onaylı ticari sürümden proforma PDF’si oluşturur. |
| Hazırlık | Işık + Songül | Gereken ödeme doğrulanır; eksik malzemeler teslim alınır ve Songül son hazırlık kontrolünü yapar. |
| Üretim | Laboratuvar | Siparişe bağlı partiler ayrı ayrı başlatılır ve tamamlanır. |
| Sevkiyat | Yetkili kullanıcı | Siparişin bütün adetleri ve gerekli kalite kontrolleri tamamlanınca **tek sevkiyat** yapılır. |

Malzeme ve ödeme bekleme koşulları aynı anda görünebilir. Işık’ın tek imzası alımı ve üretim iznini kapsar; mal kabulünden sonraki Songül kontrolü ikinci bir yönetim imzası gerektirmez.

## 2. Ekranlar, belgeler ve onaylar

- **B2B:** Taslaklar, onay bekleyenler ve aktif siparişler; her siparişte sorumlu kişi, beklenen işlem ve zaman çizelgesi bulunacak.
- **Songül’ün Üretim ekranı:** “Müşteri Siparişleri” bölümü ürün/adet, reçete, hazırlık durumu ve eksikleri gösterecek. Banka ve müşteri satış fiyatları teknik görünümde sunulmayacak.
- **Işık’ın onay ekranı:** Ticari toplam, satın alma ihtiyaçları, önerilen tedarikçiler, banka seçimi ve üretim planı birlikte gösterilecek.
- **Ana ekran:** Kalıcı “Benden bekleyen işler” kartı eklenecek. Bildirim izinleri varsa ilgili kişiye sipariş bağlantılı push gönderilecek; işin görünürlüğü push teslimatına bağlı olmayacak.
- Mevcut fiyatlar ve tedarikçi öncelikleri alım listesinde kullanılacak. Alım satırları siparişe bağlanarak sipariş verilme, teslim alınma ve kalan miktar takip edilecek.

Siparişin ticari sürümü ve laboratuvar planı ayrı sürümlenecek. Ürün, adet, fiyat, ülke, banka veya ticari şart değişikliği yeni ticari sürüm ve yeniden onay gerektirecek. Reçete veya üretim kapsamı değişirse teknik ve yönetim onayı yenilenecek. Canlı stok hareketleri mevcut imzalı belgeyi değiştirmeyecek.

İmza; kullanıcı kimliği, zaman, onaylanan sürüm ve belge özetiyle kaydedilecek. Şifre saklanmayacak. Müşteri proforması ticari bilgileri taşıyacak; reçete, alım maliyetleri ve teknik değerlendirme ayrı iç belgede kalacak.

**Banka seçimi:** Mevcut üç banka kayıtlı profillere aktarılacak. Ülke ve para birimi kuralı hesap önerecek; Doğukan veya Işık seçimi doğrulayacak. Gerçek Rusya/diğer ülke eşlemesi açıkça yapılandırılacak; eşleme bulunmazsa elle seçim istenecek. Onaylanan hesap bilgileri belgede sabitlenecek.

## 3. Üretim ve stok davranışı

- **Önceden stok rezervasyonu yapılmayacak.** Her parti başlangıcında güncel stok yeniden kontrol edilecek. Yetersizlik varsa hiçbir tüketim yazılmadan siparişin eksikleri güncellenecek.
- Fiziksel başlangıçta hammadde, ambalaj ve etiket çıkışları ile parti başlangıcı tek işlemde kaydedilecek. Bitmiş ürün stoğu bu aşamada artmayacak.
- Tamamlamada gerçek üretilen adet, lot ve şahit numune kaydedilecek; yalnız bitmiş ürün girişi yapılacak. Başlangıçtaki malzemeler ikinci kez düşülmeyecek.
- Müşteriye teslim adedi, şahit numuneden ayrı takip edilecek. Eksik/fazla çıktı siparişin kalan ihtiyacını doğru gösterecek.
- Yeni sipariş akışında QC bekleyen/reddedilen, numune veya süresi geçmiş hammaddeler üretime uygun sayılmayacak. Eksik lot/SKT bilgisi laboratuvarca çözümlenmeden parti başlamayacak.
- Kaynak seçimi mevcut **kapalı** durumda kalacak; alternatif kartlar kendiliğinden eşdeğer kabul edilmeyecek.
- Başlamış parti onaylı reçete ve tüketim kayıtlarını koruyacak. Sonradan yapılan revizyon kalan işleri etkileyecek.
- Başlamış partinin iptali tüketilmiş malzemeleri otomatik geri vermeyecek. Fiziksel iadeler gerekçeli, tüketim miktarıyla sınırlı ve denetlenebilir hareketlerle yapılacak.
- QC bekleyen ürünler ve şahit lotları sevkiyata uygun miktara katılmayacak. Tam sipariş sevk edilirken stok ve lotlar tekrar doğrulanıp **bir kez** düşülecek.

## 4. Teknik değişiklikler

- Mevcut siparişlere sürüm, teknik plan, onay/imza, ödeme koşulu ve banka profili bağlantıları eklenecek. Parti işleri, satın alma satırları ve sevkiyat sipariş satırlarına bağlanacak.
- Yeni iş API’leri taslak güncelleme, onaya gönderme, lab değerlendirmesi, imzalama, ödeme doğrulama, parti başlatma/tamamlama ve proforma indirmeyi destekleyecek.
- İşlem yetkisi, atanmış kullanıcı, aktif Kozmetik/Takviye alanı ve onaylanan sürüm **sunucuda** doğrulanacak.
- Yeni akışın onayı mevcut B2B **“Onayla & stoktan düş”** işlemini çağırmayacak. Onay ve proforma stok hareketi oluşturmayacak.
- Yeni partiler mevcut tüketim hesaplarını kullanacak; başlangıç ve tamamlama kayıtları ayrılarak mükerrer tüketim önlenecek.
- Eski onaylanmış B2B kayıtlarının stok etkileri korunacak. Distribütör portalı ve günlük üretim ilk sürümde mevcut davranışını sürdürecek.
- Şema değişiklikleri Alembic ve `init_db()` içinde birlikte uygulanacak.

## 5. Test ve canlı geçiş

Şu senaryolar doğrulanacak:

- Eksikli/eksiksiz sipariş, koşullu ödeme, revizyon ve yeniden onay.
- Şifre + çizilen imza zorunluluğu; yetkisiz kullanıcı ve farklı panel erişiminin engellenmesi.
- Ortak hammaddelerin sipariş genelinde doğru toplanması; tedarikçi politikasının korunması.
- Eşzamanlı veya tekrarlanan başlangıç/tamamlama/sevkiyatta tek stok hareketi.
- Başlangıçta bitmiş stok oluşmaması; tamamlamada ikinci malzeme düşümü yapılmaması.
- Parti üretimi, şahit ayrımı, QC reddi ve sipariş tamamlanmadan sevkiyatın engellenmesi.
- Banka/sipariş değişikliklerinin eski proformayı değiştirmemesi; günlük üretimin Işık onaysız devam etmesi.

Önce örnek bir hacimli siparişle süreç kontrol edilecek. Banka kuralları ve görev atamaları doğrulandıktan sonra yedek alınacak; tam test kapısı bulunan **`./deploy.sh`** ile canlıya geçilecek.
