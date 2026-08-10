"""Çok boylu bitmiş ürünleri ana ürün + boy varyasyonu yapısına getirir.

Şahit numune dolabının boyu ayırt edebilmesi için her çok boylu ürünün
**ana ürün (soyut, barkodsuz, stoksuz) + boy başına 1 varyasyon** olması
gerekiyor — boy bu sistemde ayrı bir kolon değil, varyasyon `Item` satırının
kendisidir (`parent_id` + `variation_name`).

Durum (10.08.2026):
  • 6 Evanira ürünü ......... ✅ zaten ana + 2 varyasyon
  • Su Bazlı Saç Maskesi ..... ✅ 61 → 62 (100ml) / 63 (200ml)
  • Vücut Peelingi ........... ⏳ `merge_duplicate_products.py` kuruyor (715 → 549/42)
  • Yoğun Saç Maskesi ........ ❌ ana ürün yok — bu script kuruyor (302/630)

Ayrıca **denetim**: ana ürünler soyuttur, `current_stock` ve `lot_seq`
taşımamalıdır (taşırsa üretim ekranı soyut ürüne lot önerir).  İhlal varsa
raporlanır — script kendiliğinden stok düzeltmez, o ayrı bir karardır.

NOT: Yoğun Saç Maskesi 200 ml'nin (id 630) GS1 barkodu YOK — master listede
yalnız 100 ml'lik kayıtlı.  Script barkod UYDURMAZ, yalnız raporlar.

Kullanım:
    venv/bin/python scripts/fix_product_variations.py            # kuru çalıştırma
    venv/bin/python scripts/fix_product_variations.py --commit   # yaz
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, Item                            # noqa: E402

COMMIT = "--commit" in sys.argv

# ana_ad → {varyasyon_item_id: boy}
FAMILIES = {
    "Minerva 108 Intense Hair Mask": {302: "100ml", 630: "200ml"},
}


def main() -> int:
    db = SessionLocal()
    created, linked, audit = [], [], []
    try:
        for parent_name, members in FAMILIES.items():
            kids = {iid: db.query(Item).filter(Item.id == iid).first()
                    for iid in members}
            if any(k is None for k in kids.values()):
                audit.append(f"⚠ «{parent_name}» için eksik ürün: "
                             f"{[i for i, k in kids.items() if k is None]}")
                continue
            sample = next(iter(kids.values()))
            parent = (db.query(Item)
                      .filter(Item.name == parent_name,
                              Item.category == "Bitmiş Ürün").first())
            if parent is None:
                parent = Item(name=parent_name, category="Bitmiş Ürün",
                              unit=sample.unit or "adet", current_stock=0,
                              domain=sample.domain or "cosmetics", is_active=True)
                created.append(parent_name)
                if COMMIT:
                    db.add(parent)
                    db.flush()
            for iid, size in members.items():
                it = kids[iid]
                want_parent = parent.id if (COMMIT or parent.id) else None
                if it.parent_id != want_parent or (it.variation_name or "") != size:
                    linked.append((iid, it.name, it.parent_id, want_parent,
                                   it.variation_name or "—", size))
                    if COMMIT:
                        it.parent_id = parent.id
                        it.variation_name = size

        if COMMIT:
            db.flush()

        # ── Denetim: ana ürünlerde stok / lot sayacı / barkod ──
        parent_ids = {row[0] for row in db.query(Item.parent_id)
                      .filter(Item.parent_id.isnot(None)).distinct().all()}
        for p in db.query(Item).filter(Item.id.in_(parent_ids)).order_by(Item.name).all():
            if (p.current_stock or 0) != 0:
                audit.append(f"ANA ÜRÜNDE STOK  id={p.id} «{p.name}» → {p.current_stock:g}")
            if (p.lot_seq or 0) != 0:
                audit.append(f"ANA ÜRÜNDE SAYAÇ id={p.id} «{p.name}» → lot_seq {p.lot_seq}")
            if (p.barcode or "").strip():
                audit.append(f"ANA ÜRÜNDE BARKOD id={p.id} «{p.name}» → {p.barcode}")
        # Barkodsuz varyasyonlar — Shopify'a hiç senkronlanmazlar
        for v in (db.query(Item)
                  .filter(Item.parent_id.isnot(None), Item.is_active == True,   # noqa: E712
                          Item.category == "Bitmiş Ürün").order_by(Item.name).all()):
            if not (v.barcode or "").strip():
                audit.append(f"BARKODSUZ VARYASYON id={v.id} «{v.name}»")

        if COMMIT:
            db.commit()
    finally:
        db.close()

    print(f"=== AÇILAN ANA ÜRÜN ({len(created)}) ===")
    for n in created:
        print(f"  + {n}")
    if not created:
        print("  (yok — zaten vardı)")

    print(f"\n=== VARYASYON BAĞLARI ({len(linked)}) ===")
    for iid, name, old_p, new_p, old_v, new_v in linked:
        print(f"  id={iid:3} {name[:44]:44} parent {old_p or '-'} → {new_p or '(yeni)'}"
              f" · '{old_v}' → '{new_v}'")

    print(f"\n=== DENETİM ({len(audit)}) ===")
    for a in audit:
        print(f"  {a}")
    if not audit:
        print("  ✓ temiz")

    print()
    print("✓ YAZILDI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
