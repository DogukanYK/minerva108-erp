"""Mevcut şahit numune lotlarını dolap modülüne görünür kılar (idempotent).

Bugüne kadar üretimde şahit numuneye ayrılan adetler yalnız `Inventory`'de
`-S` lotu + `location='Şahit Numune Dolabı — {marka}'` olarak duruyordu.  Bu
script onlar için `RetentionSample` yönetim kaydı üretir; böylece /sahit-numune
ekranında görünür ve yönetilebilir olurlar.

STOĞA DOKUNMAZ.  Adetler zaten `Item.current_stock` içinde; dolaptaki numuneler
satılabilir stokta sayılmaya devam eder (kullanıcı kararı).  Bu script yalnız
görünürlük ekler — hiçbir `Transaction` yazmaz, hiçbir bakiye değiştirmez.

İDEMPOTENT: `retention_samples.inventory_id` UNIQUE.  Kaydı olan lot atlanır,
ikinci çalıştırma no-op'tur.  Deploy sonrası tekrar çalıştırılıp arada oluşan
yeni kayıtlar da süpürülebilir.

Kullanım (prod):
    cd /var/www/minerva && set -a && source .env && set +a \
      && venv/bin/python scripts/backfill_retention_samples.py           # kuru çalıştırma
      && venv/bin/python scripts/backfill_retention_samples.py --commit  # yaz
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (SessionLocal, Inventory, Item,           # noqa: E402
                      RetentionSample, RetentionSampleMovement)
from core.brands import brand_from_location, cabinet_of        # noqa: E402
from core.retention import (DEFAULT_EXTRA_MONTHS,              # noqa: E402
                            DEFAULT_SHELF_LIFE_MONTHS, retention_until)

COMMIT = "--commit" in sys.argv
LOCATION_PREFIX = "Şahit Numune Dolabı"


def main() -> int:
    db = SessionLocal()
    created = skipped_existing = skipped_empty = 0
    rows_out = []
    try:
        rows = (db.query(Inventory)
                .filter(Inventory.location.ilike(f"{LOCATION_PREFIX}%"))
                .order_by(Inventory.id).all())
        existing = {row[0] for row in db.query(RetentionSample.inventory_id)
                    .filter(RetentionSample.inventory_id.isnot(None)).all()}

        for inv in rows:
            if inv.id in existing:
                skipped_existing += 1
                continue
            if (inv.quantity or 0) <= 0:
                skipped_empty += 1
                continue
            item = db.query(Item).filter(Item.id == inv.item_id).first()
            name = item.name if item else ""
            brand = brand_from_location(inv.location) or (cabinet_of(name) if name else "Genel")
            until = retention_until(inv.created_at, DEFAULT_SHELF_LIFE_MONTHS,
                                    DEFAULT_EXTRA_MONTHS)
            rows_out.append((inv.id, name[:38], inv.lot_number, inv.quantity, brand,
                             until.isoformat() if until else "—"))
            if COMMIT:
                rs = RetentionSample(
                    inventory_id=inv.id, item_id=inv.item_id, item_name=name,
                    lot_number=inv.lot_number, brand=brand,
                    quantity=inv.quantity, initial_quantity=inv.quantity,
                    unit=(item.unit if item else None),
                    produced_at=inv.created_at, retention_until=until,
                    status="stored", source="backfill",
                    placed_by=inv.received_by, domain=(inv.domain or "cosmetics"),
                )
                db.add(rs)
                db.flush()
                db.add(RetentionSampleMovement(
                    sample_id=rs.id, movement_type="giris", quantity=inv.quantity,
                    note="Mevcut dolap stoğundan aktarıldı", performed_by="sistem"))
            created += 1

        if COMMIT:
            db.commit()
    finally:
        db.close()

    print(f"{'ID':>5}  {'ÜRÜN':38}  {'LOT':22}  {'ADET':>6}  {'DOLAP':16}  SAKLAMA SONU")
    print("-" * 108)
    for r in rows_out:
        print(f"{r[0]:>5}  {r[1]:38}  {r[2]:22}  {r[3]:>6g}  {r[4]:16}  {r[5]}")
    print("-" * 108)
    print(f"Aktarılacak : {created}")
    print(f"Zaten var   : {skipped_existing}")
    print(f"Boş lot     : {skipped_empty}")
    print()
    if COMMIT:
        print("✓ YAZILDI — stok rakamlarına DOKUNULMADI (yalnız dolap görünürlüğü eklendi).")
    else:
        print("KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
