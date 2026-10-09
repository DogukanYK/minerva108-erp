# Claude için Minerva IMS devralma notları

Kayıt: 9 Ekim 2026, Türkiye saati.

## Son kullanıcı talimatı ve iki planın durumu

Kullanıcı iki planı Markdown olarak istedi; aktif çalışmanın devamını değil, kayıt/devralma belgesini talep etti. Geliştirme durduruldu. Bu not ve ekli planlar kendi başına uygulama/deploy izni değildir; daha sonraki açık kullanıcı talimatına göre yalnız seçilen plana devam edilecek.

1. **B2B siparişinden onay, proforma ve üretime geçiş:** Önceden onaylandı, kullanıcı özellikle “başlama, kaydet, sonra söyleyeceğim” dedi. Uygulama HİÇ başlamadı ve hâlâ bekliyor.
2. **Kodlu fason üretim:** Onaylandı ve kısmen uygulanmaya başladı; ajanların kullanım limiti bitti, ardından kullanıcı kayıt/devralma istedi. Yerel kod yarım, canlıya aktarılmadı. Bu plan B2B planına bağımlı değil.

**Onayları karıştırma:** B2B planında Işık'ın şifre doğrulaması ve çizilen imzası var. Fason planında üç kişinin kendi hesabından onayı var; şifre tekrar doğrulama ve çizilen imza YOK.

## Kaynak belgeler ve çalışma ortamı

- Proje: `/Users/dogukan/Desktop/Claude/MINERVA108IMSV2`
- Workspace kuralları: `/Users/dogukan/Desktop/Claude/AGENTS.md`; önce projenin `/Users/dogukan/Desktop/Claude/MINERVA108IMSV2/AGENTS.md` dosyasını oku.
- B2B tam planı: `/Users/dogukan/Desktop/Claude/MINERVA108IMSV2/docs/superpowers/specs/2026-10-09-b2b-onay-proforma-uretim-design.md`
- Fason planı: `/Users/dogukan/Desktop/Claude/MINERVA108IMSV2/docs/superpowers/specs/2026-10-09-kodlu-fason-uretim-design.md`
- Başlangıç branch: `main`; son commit `fa6f103 feat(production): plan source splits and align purchase stock`.
- Bütün yeni fason değişiklikleri commit edilmemiş çalışma ağacında. Mevcut dosyaları koru; temiz checkout varsayımıyla üzerine yazma veya resetleme.
- Canlı: `https://ims.minerva108.com`, SSH alias `hetzner`, `/var/www/minerva`, servis `minerva`.
- FastAPI + Jinja2 + PostgreSQL. Kozmetik/Takviye domain izolasyonu zorunlu.
- `Item.current_stock` otorite; lot kayıtlarıyla birlikte güncellenir. Aylık stok hesabı Input/Output/Adjustment üzerinden rekonstrüksiyon yapar.
- `production.source_choice.enabled=0` kalacak; doğrulanmamış material group eşdeğerliği kullanılmayacak.
- İlk gerçek fason firma, ürün ve adet **henüz belli değil**. Kullanıcı bunu açıkça söyledi. Canlıya örnek firma, onay veya stok hareketi yazılmadı.

## Fason kodunda şu anda bulunan parçalar

Bu liste dosyaların mevcut olduğunu belirtir; bütününün tamamlandığı veya çalıştığı anlamına gelmez.

- `database.py`: OutsourcingPartner, OutsourcingMaterialCode, OutsourcingJob, OutsourcingPacketRevision, OutsourcingApproval, OutsourcingContainer, OutsourcingOperation, OutsourcingShipment, OutsourcingShipmentLine, OutsourcingMovement, OutsourcingReceipt modelleri ve `Inventory.outsourcing_receipt_id` alanı eklendi. FK cycle için named/use_alter düzeltmesi başlatıldı; migration ve init_db eşliği ayrıca kontrol edilmeli.
- `core/outsourcing.py`: Miktar/birim/SKT doğrulama, özel dış metin kontrolü, firma/kod oluşturma, reçete önizleme, iş/sürüm hazırlama, kap hazırlama/iptal ve üç hesap onayı kodu var. Dosya `approve_job(...)` fonksiyonuyla bitiyor. Sevk/tüketim/iade/kabul/QC/kapanış ve serializer/DTO motoru tamamlanmadı.
- `core/outsourcing_documents.py`: `render_packet_pdf(packet, document)` ve `outsourcing_filename(packet, document)` var. Belge türleri `sheet`, `labels`, `manifest`, `report`; etiket 100×70 mm, diğerleri A4. QR yalnız `container_uid`.
- `tests/test_outsourcing_documents.py`: PDF gizliliği, QR, miktar/faz, sayfa ve ölçü testleri var. PDF ajanı ayrı `minerva_test2` veritabanında **16 PDF testinin geçtiğini ve örnek PDF'leri görsel kontrol ettiğini bildirdi**. Bu, backend entegrasyonunun veya tam test kapısının geçtiği anlamına gelmez.
- `templates/outsourcing.html`, `static/outsourcing.js`: Firma/kod/iş hazırlığı, dış önizleme, üç onay, kap/sevk/tüketim/iade/kabul/kapanış ekranları yazıldı. Gerçek API ile uçtan uca doğrulanmadı.
- `templates/production.html`: `/outsourcing` giriş bağlantısı eklendi.
- `api_main.py`: `/outsourcing` sayfa rotası ve `routers.outsourcing` import/include satırları eklendi.
- `core/permissions.py`: `outsourcing` kategorisi `view`, `manage`, `mapping`, `approve`, `dispatch`, `record` ile eklendi. Manager ve LabLead rol varsayılanları var; kişisel override mantığı değişmedi.
- `templates/admin.html`: Yeni yetki kategorisi ve eylem başlıkları eklendi.
- `routers/production.py`: İki QC yoluna `approve_receipt` çağrısı, fason domain kontrolü ve QC listesinde Fason kaynak etiketi eklendi. Çağrılan helper henüz tamamlanmadı.
- `routers/inventory.py`: Normal mal kabul upsert'i fason kabul satırlarını dışlıyor; pending fason lotuna doğrudan stok düzeltmesi engelleniyor.
- `core/stock_lots.py`: `find_twin` fason kabul lotlarıyla birleşmiyor; fason kabul lotunu başka karta taşıma engellendi.
- `core/outsourcing_reporting.py`: Sevk transaction ID'lerini ve fiilî tüketim/fire toplamlarını okumak için yardımcılar var.
- `routers/reports.py`: Top-usage raporunda fason transferini ayırıp fiilî tüketim/fireyi dahil etme kodu eklendi. İlgili importların doğru fonksiyonda olduğu ve davranışın test edildiği ayrıca kontrol edilmeli.

## Devamdan önce bilinmesi gereken eksikler

**Yerel çalışma ağacı şu anda çalışır uygulama paketi değildir.** `api_main.py` henüz mevcut olmayan `routers/outsourcing.py` dosyasını import ediyor. QC yolları henüz mevcut olmayan `core.outsourcing.approve_receipt` fonksiyonuna bağlandı. Kullanıcı devam talimatı verirse ilk iş bu eksik entegrasyonları tamamlamak ve normal uygulama/QC regresyonlarını doğrulamak olacak. Kullanıcı durdurduğu için bu notu hazırlarken kod düzeltmesi yapılmadı.

Henüz olmayan/tamamlanmayanlar:

1. `routers/outsourcing.py` ve backend bootstrap/list/detail/allowlist DTO'ları.
2. Transactional sevk, idempotent operasyon anahtarı, dış tüketim/fire, fiziksel iade, bitmiş ürün/şahit kabulü, QC stok postinglemesi, kapanış/iptal motorları.
3. Alembic migration ve `init_db()` alter_safe eşliği; prod/dev migration çalıştırılmadı.
4. İki QC yolu ile mal kabul/upsert izolasyonunun entegrasyon testleri.
5. Eşzamanlı sevk/QC, stale revision, assigned approver, domain ve gizli eşleştirme erişim testleri.
6. Aylık ayrıntılı PDF/Excel raporunda transfer ile tüketimin ayrılması. `core/monthly_report.py` henüz değişmedi.
7. Frontend/API sözleşmesi, tarayıcı uçtan uca ve basılı etiket denemesi.
8. Tam test kapısı, yedek, deploy ve servis/canlı ekran doğrulaması.

## Üzerinde uzlaşılan teknik sözleşmeler

- API ana yolu `/api/outsourcing`; `bootstrap`, `partners`, `material-codes`, `preview`, `jobs`, `jobs/{id}`; işte `approve`, `dispatch`, `consumption`, `returns`, `receipts`, `close`, `cancel`, `documents/{sheet|labels|manifest|report}` eylemleri. UI'nin mevcut payload ve response beklentilerini API yazarken kontrol et.
- Onay: `revision` ve `packet_hash`; sunucuda atanan farklı kullanıcı kimliği, aktif hesap ve sıra kontrolü. `technical` ilk; `owner` ve `manager` sonra. İş alanları `technical_user_id`, `owner_user_id`, `manager_user_id`.
- Bir kap tek kaynak lotu ve tek dolum. Etaplı gönderim farklı hazırlanmış kaplardan olur; aynı kap ikinci kez sevk edilmez.
- İlk sevkten sonra paket ve kap atamaları değişmez. Üretim değişikliği yeni iş/parti açar. Eski kağıtları sessizce yeni sürüme bağlama.
- Dış belgeler güncel kart isimlerinden değil dondurulmuş dış izin listesinden üretilir. İç snapshot dış PDF'ye gömülmez. QR gerçek isim, kart ID, kaynak lot veya iç URL içermez.
- Core hata biçimi `OutsourcingError(status, detail, code)`; API hata halinde rollback ve anlamlı JSON üretir.
- Root tarafından beklenen helper: `approve_receipt(db, inv, actor, decision=None) -> bool`. Nonfason için False. Fason için True; bu durumda mevcut QC stock branch'i tamamen atlanır. Çağrı anında `inv.status` yeni karara set edilmiş olabilir; önceki durum receipt üzerinden doğrulanmalı.
- Fason onayında receipt/inventory/item/domain bağı doğrulanır, `input_transaction_id` üzerinden tek gerçek Input ve kullanılabilir stok artışı oluşturulur. Red sıfır stok. `finished_sample`/is_sample lotu kullanılabilir stok artırmaz.
- QC satırları normal mal kabul veya `find_twin` ile aynı item/lot adı üzerinden birleşmemeli; immutability ve idempotency yalnız not metnine dayanmaz.
- Sevk yerel defterde Output, açıklama prefix'i `Fason sevk`. Sevk transaction'ı `OutsourcingShipmentLine.transaction_id` ile bağlıdır; tüketim raporu bu transferleri gerçek kullanım olarak saymaz. Aylık stok rekonstrüksiyonu yerel Output'u saymaya devam eder.
- Actual consumption/waste `OutsourcingMovement` satırlarıyla dış bakiyeyi azaltır; yerel hammaddeleri tekrar düşürmez. Miktarlar Item stok birimine kesin dönüştürülmeli; g/kg ve ml/l olabilir, kg/l tahmini yok.
- Kullanılmamış iade fiziksel teslimde dış bakiyeyi azaltır, ayrı karantina lotuna girer; kullanılabilir stok ancak QC onayında artar. Source snapshot'ları kaynak Inventory sonradan değişse de korunur.
- Bitmiş kabulde `quantity` gerçek toplam, `sample_quantity` bunun şahit alt kümesi. Ayrı karantina receipt/lotları oluştur; standart `start_production` çağrısı malzemeyi ikinci kez düşüreceği için kullanılmaz.
- Kapanışta bütün kabul/QC kararları tamamlanır ve `gönderilen = tüketim + belgeli fire/kayıp + fiziksel iade`; kalan dış bakiye sıfır. İptal otomatik hammadde iadesi değildir.

## Kritik altyapı uyarıları

- `core.audit.log_admin_event` kendi içinde COMMIT yapar. Stok işlemi yarımken çağırma; outsourcing motorunda transaction içinde `AdminAuditLog` ekleyip tek API commit'i kullan.
- Mevcut QC kilitlemesi Item → Inventory sırasındadır. Fason helper, sevk ve kapanıştaki kilit sıralarını deadlock oluşturmayacak şekilde tasarla.
- `Inventory → OutsourcingReceipt → OutsourcingContainer → Inventory` FK cycle'ı test drop_all sırasında hata verdi. Named constraint/use_alter çözümü migration ve modelde tutarlı olmalı.
- Mevcut QC onayları normal karantinada sadece `QC Approval` transaction'ı yazabilir; aylık rekonstrüksiyon bunu stok girişi saymaz. Fason kabulü için tek gerçek Input zorunlu.
- Drive cross-domain ve paylaşılabilir; gizli eşleştirmeyi ortak/public Drive belgesine koyma.
- Per-user permissions JSON rol varsayılanlarını tamamen değiştirir. Yeni kategori için genel fallback ekleme veya kullanıcının diğer izinlerini sıfırlama.
- Distributor hesabı, yanlışlıkla outsourcing izni verilmiş olsa bile bu iç modüle erişmemeli.

## Canlıda salt okunur kontrol sonucu

`dogukan`: aktif SuperAdmin, override yok. `songul`: aktif LabLead, override yok. `isik`: aktif Manager, mevcut özel permissions JSON var, `outsourcing` kategorisi yok.

Kullanıcı devam talimatı verdiğinde modülün üçlü onayı çalışsın diye Işık'ın mevcut özel JSON'una yalnız gerekli `outsourcing.view` ve `outsourcing.approve` eklenmesi planlandı; diğer kategoriler ve eylemler korunacak, değişiklik denetim kaydıyla yapılacak. **Bu yetki değişikliği henüz yapılmadı.**

## Test ve canlı geçiş sırası

Bu sıra yalnız kullanıcı ilgili plana devam talimatı verirse uygulanır.

1. Kısmi kodu ve dosya sözleşmelerini oku; eksik API, motor ve migration/init_db eşliğini tamamla.
2. Test veritabanında örnek firma/ürünle kod → üç onay → sevk → tüketim/fire → fiziksel iade/ürün kabul → QC → kapanış akışını çalıştır.
3. PDF/QR/metadata gizliliği, fiziksel etiket okunabilirliği, domain/izin, idempotency ve concurrency, eski üretim/mal kabul/QC ve aylık stok hesaplarını test et.
4. Ortak test DB'sinde paralel pytest koşma: her test drop_all/create_all yapar. `MINERVA_TEST_DB=minerva_test2` PDF ajanına ayrılmıştı; artık yeni oturum kendi çakışmasız DB'sini seçsin.
5. Tam test kapısı geçmeden push/deploy yok. Canlı yedek al; zorunlu yol `./deploy.sh` (tam pytest + push + servis restart + HTTP verification). Ad-hoc restart/pull/reset veya --skip-tests kullanma.
6. `deploy.sh` git add . yaptığı için önceden bekletilen B2B planı ve diğer yerel dosyaları da stage edebilir; commit kapsamını bilinçli kontrol et.
7. Servis ve canlı ekranı doğrula; gerçek firma/ürün adı henüz bilinmediği için onaylı kodları veya stok hareketlerini uydurarak seed etme.

Geliştirme, canlı değişiklik veya test çalıştırması bu devralma belgesi hazırlanırken devam ettirilmedi.
