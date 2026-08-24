"""24.08.2026 numune olayının stok onarımı (idempotent, kuru çalıştırma varsayılan).

**Ne oldu:** Bir stajyer 24.08.2026 11:19–12:03 arası (Songül Akgül hesabıyla)
Ürünler → Numune sekmesinden 47 hammadde numunesi girdi.  O sıradaki kod
numuneyi de normal mal kabul gibi işliyordu: her giriş `Item.current_stock`'u
artırıyor ve bir `Transaction(Input, "Numune kabul…")` yazıyordu.  Sonuç:
~40 hammadde kartında stok şişti (bazı birim hataları da var — ör. GİNSENG
kartına "50" yazılınca birim kg olduğu için gerçekte 5 kg olan stoğa +50 kg
eklendi).  Ayrıca stajyer bazı ürünleri Türkçe harf farkıyla (İ/ı, çift
boşluk) bulamayıp "Yeni Ürün Ekle" ile 8 kopya kart açtı; 4 numune de eski
kopya kartlara (573/574/581/582) gitti.  Kod tarafı ayrıca düzeltildi —
numune artık HİÇ stoğa/deftere dokunmuyor (`routers/inventory.py::
receive_stock`, `core/audit.py` ile kaydediliyor); bu script yalnız o günkü
GEÇMİŞ hasarı onarır, kod düzeltmesi gelecekteki girişleri korur.

**Kaynak veri elle hardcode EDİLMEZ** — script her çalıştığında canlı
`Inventory.is_sample=True` satırlarını okur (44 satır, 2026-08-24 12:07
itibarıyla doğrulandı) ve her satırın `quantity`'sini o ürüne o gün eklenmiş
toplam numune miktarı olarak kabul eder (bir üründe birden fazla numune
girişi olsa bile `(item_id, lot, is_sample)` upsert anahtarı yüzünden TEK
satırda birikir — doğrulama adımı bunu `transactions` tablosundaki "Numune
kabul" toplamıyla çapraz kontrol eder).

Yaptıkları — sırayla, TEK commit'te:

  ① **Telafi Adjustment'ları** — her numune satırının `quantity`'si kadar
     `Item.current_stock`'tan düşülür (`Transaction(Adjustment, delta<0)`).
     Transaction SİLİNMEZ (`core/snapshots.py` yalnız Input/Output/
     Adjustment sayar, geçmiş rapor rekonstrüksiyonu Transaction'ların
     durmasına bağlı) — kopya karttaki "Numune kabul" Input'u aynı karttaki
     bu Adjustment'la NET SIFIRLANIR, ikisi de yerinde kalır.
     **İstisna: item 573 (BADEM YAGI, kopya kart).**  Songül aynı gün
     11:22'de bunu KENDİSİ elle 50→0 düzeltmişti (Adjustment zaten var) —
     ikinci kez -50 yazmak onu -50'ye düşürürdü.  Bu tek item atlanır.
  ② **Kopya kart satır taşıma** — 574 (KAHVE EKSTRAKTI) → 238, 581
     (CİTRONELLA UÇUCU YAGI) → 548 (CITRONELLA), 582 (ÇAY AĞACI UÇUCU YAĞI)
     → 189 (ÇAY AĞACI UÇUCU YAĞI): numune Inventory satırı doğru karta
     taşınır (`item_id` repoint), stok/Transaction geçmişi kopya kartta
     kalır (① zaten sıfırladı).  573'ün satırı (id 80) ise SİLİNİR — 156
     (BADEM YAĞI) için AYRICA girilmiş aynı fiziksel şişenin (id 81, aynı
     lot, 2 dk arayla) mükerrer kaydı, taşınacak "doğru" bir yer yok çünkü
     zaten var.
  ③ **Birim düzeltme** — bugün açılan 8 yeni kart + eski tek-kart 3 madde
     `adet` yerine `ml` (hepsi sıvı yağ/ekstrakt; hiçbiri gerçekten "adet"le
     satılmıyor — modalin ön-seçili birimi yüzünden dokunulmadan kaydedilmiş).
  ④ **Kopya kartları pasifleştirme** — 573/574/581/582: stok 0 + reçete
     referansı yok doğrulanarak `is_active=False` (silinmez — Transaction
     geçmişleri hâlâ bağlı).

STOK ETKİSİ DIŞINDA HİÇBİR ŞEYE DOKUNULMAZ: 40 hammaddenin GERÇEK numune
kaydı (Inventory satırı, `is_sample=True`) hiç silinmez/değişmez — Numune
sekmesinde görünmeye devam eder, artık doğru kartın altında ve stoğa
karışmadan.

Kullanım:
    venv/bin/python scripts/repair_numune_20260824.py            # kuru çalıştırma
    venv/bin/python scripts/repair_numune_20260824.py --commit   # yaz
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (SessionLocal, Item, Inventory, Transaction,        # noqa: E402
                      RetentionSample, RecipeIngredient, Recipe,
                      ProductionHistory)

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (numune onarımı 24.08.2026)"
WINDOW_START = datetime(2026, 8, 23, 21, 0)   # 24.08.2026 00:00 TR = 23.08 21:00 UTC

SKIP_COMPENSATION = {573}                      # Songül elle sıfırlamıştı — dokunma
DROP_ROWS = {80}                               # 156'nın numunesiyle aynı fiziksel şişe
MOVE_TARGET = {574: 238, 581: 548, 582: 189}   # kopya kart → doğru kart
UNIT_FIX = {                                   # adet → ml (hepsi sıvı yağ/ekstrakt)
    729: "ml", 730: "ml", 731: "ml", 732: "ml", 733: "ml",
    734: "ml", 735: "ml", 736: "ml", 566: "ml", 567: "ml", 577: "ml",
}
DEACTIVATE = {573, 574, 581, 582}


def adjust(db, item, delta, note):
    """merge_duplicate_products.py::adjust() ile aynı kalıp — imzalı delta,
    Adjustment Transaction yazar, Transaction SİLMEZ/DEĞİŞTİRMEZ."""
    if abs(delta) < 1e-9:
        return
    if COMMIT:
        db.add(Transaction(item_id=item.id, transaction_type="Adjustment",
                           quantity=round(delta, 6), timestamp=datetime.utcnow(),
                           notes=note, performed_by=ACTOR))
        item.current_stock = round((item.current_stock or 0.0) + delta, 6)


def has_references(db, item_id) -> list:
    refs = []
    if db.query(RecipeIngredient.id).filter(RecipeIngredient.item_id == item_id).first():
        refs.append("recipe_ingredients")
    if db.query(Recipe.id).filter(Recipe.target_item_id == item_id).first():
        refs.append("recipes.target_item_id")
    if db.query(ProductionHistory.id).filter(ProductionHistory.target_item_id == item_id).first():
        refs.append("production_history.target_item_id")
    if db.query(Item.id).filter(Item.parent_id == item_id).first():
        refs.append("items.parent_id (varyasyon çocuğu)")
    return refs


def main() -> int:
    db = SessionLocal()
    comp_rows, moved_rows, dropped_rows, unit_rows, deactivated = [], [], [], [], []
    # `adjust()` yalnız --commit'te item.current_stock'u yazar (kuru çalıştırma
    # DB'ye dokunmaz) — ama ④. adım "stok şimdi 0 mı?" diye ① sonrası duruma
    # bakmalı, kuru çalıştırmada da.  Bu yüzden projeksiyon ayrı bir dict'te
    # tutulur; ④ ORM'deki (henüz yazılmamış) değere değil BUNA bakar.
    projected_stock: dict = {}
    try:
        rows = (db.query(Inventory)
               .filter(Inventory.is_sample == True).all())   # noqa: E712
        by_id = {r.id: r for r in rows}

        # ── Doğrulama geçişi — uyumsuzlukta İPTAL ──────────────────────────
        tx_rows = (db.query(Transaction)
                  .filter(Transaction.notes.like("Numune kabul%")).all())
        if any(t.timestamp < WINDOW_START for t in tx_rows):
            print("⛔ DURDURULDU — pencere dışında 'Numune kabul' tx var, "
                  "bu script yalnız 24.08.2026 olayı için tasarlandı.")
            return 2
        tx_total = sum(t.quantity for t in tx_rows)
        inv_total = sum(r.quantity for r in rows)
        if abs(tx_total - inv_total) > 1e-6:
            print(f"⛔ DURDURULDU — tx toplamı ({tx_total}) ile numune satır "
                  f"toplamı ({inv_total}) uyuşmuyor; veri script yazıldığından "
                  f"beri değişmiş olabilir, elle kontrol et.")
            return 2
        if db.query(Transaction.id).filter(Transaction.performed_by == ACTOR).first():
            print("⛔ DURDURULDU — bu onarım zaten uygulanmış (ACTOR damgalı "
                  "Adjustment mevcut).")
            return 2
        touched_inv_ids = list(by_id.keys())
        ret_fk = (db.query(RetentionSample.id)
                 .filter(RetentionSample.inventory_id.in_(touched_inv_ids)).count()
                 if touched_inv_ids else 0)
        if ret_fk:
            print(f"⛔ DURDURULDU — {ret_fk} şahit numune kaydı bu envanter "
                  f"satırlarına bağlı, elle incele.")
            return 2
        it573 = db.query(Item).filter(Item.id == 573).first()
        if it573 and abs(float(it573.current_stock or 0)) > 1e-6:
            print(f"⛔ DURDURULDU — item 573 stoğu 0 değil ({it573.current_stock}); "
                  f"'zaten elle düzeltilmiş' varsayımı artık geçerli değil.")
            return 2
        for src_id, tgt_id in MOVE_TARGET.items():
            clash = (db.query(Inventory)
                    .filter(Inventory.item_id == tgt_id, Inventory.is_sample == True,   # noqa: E712
                            Inventory.lot_number == "MİNERVA").first())
            if clash:
                print(f"⛔ DURDURULDU — hedef item {tgt_id}'de zaten 'MİNERVA' "
                      f"numune satırı var (id={clash.id}); birleştirme mantığı "
                      f"bu script'te yok, elle incele.")
                return 2

        # ── ① Telafi Adjustment'ları ────────────────────────────────────────
        for r in rows:
            item = db.query(Item).filter(Item.id == r.item_id).first()
            if not item:
                print(f"  ⚠ item id={r.item_id} bulunamadı (inv id={r.id}) — atlandı")
                continue
            if item.id in SKIP_COMPENSATION:
                comp_rows.append((item.id, item.name, r.quantity, "ATLANDI — zaten elle sıfırlanmış"))
                projected_stock[item.id] = item.current_stock or 0.0
                continue
            old = item.current_stock or 0.0
            projected_stock[item.id] = old - r.quantity
            comp_rows.append((item.id, item.name, r.quantity, f"{old:g} → {old - r.quantity:g} {item.unit or ''}"))
            adjust(db, item, -r.quantity,
                  f"Eski: {old:g} {item.unit or ''} → Yeni: {old - r.quantity:g} {item.unit or ''} | "
                  f"Sebep: Numune kabulü stok saymaz — 24.08.2026 numune girişleri "
                  f"stoktan çıkarıldı (numune kaydı korunur, bkz. inv id={r.id})")

        if COMMIT:
            db.flush()

        # ── ② Kopya kart satır taşıma / silme ──────────────────────────────
        for inv_id in DROP_ROWS:
            r = by_id.get(inv_id)
            if not r:
                print(f"  ⚠ silinecek inv id={inv_id} bulunamadı — atlandı")
                continue
            dropped_rows.append((r.id, r.item_id, r.quantity))
            if COMMIT:
                db.delete(r)

        for src_item, tgt_item in MOVE_TARGET.items():
            r = next((x for x in rows if x.item_id == src_item), None)
            if not r:
                print(f"  ⚠ item {src_item} için numune satırı bulunamadı — atlandı")
                continue
            tgt = db.query(Item).filter(Item.id == tgt_item).first()
            if not tgt:
                print(f"  ⚠ hedef item {tgt_item} bulunamadı — atlandı")
                continue
            moved_rows.append((r.id, src_item, tgt_item, tgt.name, r.quantity))
            if COMMIT:
                r.item_id = tgt_item
                r.domain = tgt.domain or "cosmetics"

        # ── ③ Birim düzeltme ────────────────────────────────────────────────
        for item_id, new_unit in UNIT_FIX.items():
            item = db.query(Item).filter(Item.id == item_id).first()
            if not item:
                continue
            if (item.unit or "") != new_unit:
                unit_rows.append((item.id, item.name, item.unit, new_unit))
                if COMMIT:
                    item.unit = new_unit

        # ── ④ Kopya kartları pasifleştirme ─────────────────────────────────
        for item_id in DEACTIVATE:
            item = db.query(Item).filter(Item.id == item_id).first()
            if not item:
                continue
            stock = float(projected_stock.get(item_id, item.current_stock or 0.0))
            if abs(stock) > 1e-6:
                print(f"  ⚠ item {item_id} '{item.name}' stok {stock:g} — pasifleştirilmedi "
                      f"(beklenen: telafi sonrası 0).")
                continue
            refs = has_references(db, item_id)
            if refs:
                print(f"  ⚠ item {item_id} '{item.name}' referanslı ({', '.join(refs)}) — "
                      f"pasifleştirilmedi.")
                continue
            deactivated.append((item.id, item.name))
            if COMMIT:
                item.is_active = False

        if COMMIT:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"=== ① TELAFİ ADJUSTMENT ({len(comp_rows)}) ===")
    for iid, name, qty, note in comp_rows:
        print(f"  id={iid:3} {name[:44]:44} -{qty:g}  {note}")

    print(f"\n=== ② SATIR TAŞIMA/SİLME ===")
    for iid, itemid, qty in dropped_rows:
        print(f"  SİL   inv id={iid} (item {itemid}, {qty:g}) — 156'nın numunesiyle aynı şişe")
    for iid, src, tgt, tgtname, qty in moved_rows:
        print(f"  TAŞI  inv id={iid}: item {src} → {tgt} ({tgtname}), {qty:g}")

    print(f"\n=== ③ BİRİM DÜZELTME ({len(unit_rows)}) ===")
    for iid, name, old, new in unit_rows:
        print(f"  id={iid:3} {name[:44]:44} {old or '—':6} → {new}")

    print(f"\n=== ④ PASİFLEŞTİRİLEN ({len(deactivated)}) ===")
    for iid, name in deactivated:
        print(f"  id={iid:3} {name}")

    print()
    print("✓ YAZILDI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
