"""01.06.2026'da toplu içe aktarımdan açılmış, hiç kullanılmamış hammadde
kartlarını pasifleştirir (kuru çalıştırma varsayılan).

Bunlar 24.08.2026 numune olayıyla İLGİSİZ değil — aynı Türkçe-katlanmış ad
taraması onları da ortaya çıkardı: sistemde ayrıntılı bir maddenin (ör.
"GLİSERİN" id=136, 29 kg stoklu, 52 reçetede kullanılıyor) yanında, aynı adı
taşıyan ama hiç dokunulmamış bir kopyası duruyor (id=600, stok 0, 0 reçete,
0 işlem, `unit='adet'` — 01.06.2026'da toplu bir "smart-import" ile açılmış,
o dosyada `.upper()` Türkçe-duyarsız eşleştirme kullanmıştı).  Bunlar veri
onarımını karıştırmadan, kendi başlarına güvenle kapatılabilir kartlar.

**Hiçbir liste körü körüne güvenilmez** — her aday script İÇİNDE, o an
canlı veriye karşı doğrulanır: aktif + stok tam 0 + şu tablolarda 0 satır:
`transactions`, `recipe_ingredients`, `inventory`, `supplier_prices`,
`delivery_items`, `product_return_items`, `quotation_items`,
`distributor_prices` + ana ürün/varyasyon/üretim hedefi değil.  Doğrulamayı
geçemeyen kart atlanır ve raporlanır — script hiçbir zaman "iptal" ile
durmaz, adaylar birbirinden bağımsızdır.

Kullanım:
    venv/bin/python scripts/deactivate_zombie_items_20260824.py            # kuru çalıştırma
    venv/bin/python scripts/deactivate_zombie_items_20260824.py --commit   # yaz
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (SessionLocal, Item, Transaction, RecipeIngredient,     # noqa: E402
                      Recipe, Inventory, ProductionHistory, DeliveryItem,
                      ProductReturnItem, QuotationItem, DistributorPrice,
                      SupplierPrice)

COMMIT = "--commit" in sys.argv

CANDIDATES = {565, 568, 570, 571, 572, 576, 578, 579, 583, 584, 585,
             591, 594, 595, 597, 600}


def blockers(db, item_id) -> list:
    b = []
    if db.query(Transaction.id).filter(Transaction.item_id == item_id).first():
        b.append("transactions")
    if db.query(RecipeIngredient.id).filter(RecipeIngredient.item_id == item_id).first():
        b.append("recipe_ingredients")
    if db.query(Inventory.id).filter(Inventory.item_id == item_id).first():
        b.append("inventory")
    if db.query(SupplierPrice.id).filter(SupplierPrice.item_id == item_id).first():
        b.append("supplier_prices")
    if db.query(DeliveryItem.id).filter(DeliveryItem.item_id == item_id).first():
        b.append("delivery_items")
    if db.query(ProductReturnItem.id).filter(ProductReturnItem.item_id == item_id).first():
        b.append("product_return_items")
    if db.query(QuotationItem.id).filter(QuotationItem.item_id == item_id).first():
        b.append("quotation_items")
    if db.query(DistributorPrice.id).filter(DistributorPrice.item_id == item_id).first():
        b.append("distributor_prices")
    if db.query(Item.id).filter(Item.parent_id == item_id).first():
        b.append("items.parent_id (varyasyon çocuğu)")
    if db.query(Recipe.id).filter(Recipe.target_item_id == item_id).first():
        b.append("recipes.target_item_id")
    if db.query(ProductionHistory.id).filter(ProductionHistory.target_item_id == item_id).first():
        b.append("production_history.target_item_id")
    return b


def main() -> int:
    db = SessionLocal()
    passed, skipped = [], []
    try:
        for item_id in sorted(CANDIDATES):
            item = db.query(Item).filter(Item.id == item_id).first()
            if not item:
                skipped.append((item_id, "?", "kart bulunamadı"))
                continue
            if not item.is_active:
                skipped.append((item.id, item.name, "zaten pasif"))
                continue
            if abs(float(item.current_stock or 0.0)) > 1e-6:
                skipped.append((item.id, item.name, f"stok sıfır değil ({item.current_stock:g})"))
                continue
            refs = blockers(db, item_id)
            if refs:
                skipped.append((item.id, item.name, f"referanslı: {', '.join(refs)}"))
                continue
            passed.append((item.id, item.name))
            if COMMIT:
                item.is_active = False

        if COMMIT:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"=== PASİFLEŞTİRİLEN ({len(passed)}/{len(CANDIDATES)}) ===")
    for iid, name in passed:
        print(f"  id={iid:3} {name}")

    print(f"\n=== ATLANAN ({len(skipped)}) ===")
    for iid, name, why in skipped:
        print(f"  id={iid:3} {name[:44]:44} — {why}")

    print()
    print("✓ YAZILDI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
