"""Lot / stok sapmasının onarımı — 09.09.2026.

NEDEN
─────
`Item.current_stock` ile `Inventory` lot satırları uzun süre iki ayrı sayaçtı:
lot satırı açan tek yol üretim çıktısı + mal kabuldü; teslimat, Shopify satışı
ve elle stok düzeltmesi yalnız `current_stock`'u hareket ettiriyordu.  Lotlar
bir kez açılıp bir daha azalmadı.  Kod tarafı `core/stock_lots.py` ile kapandı;
bu script GEÇMİŞTE birikmiş farkı temizler.

NE YAPAR / NE YAPMAZ
────────────────────
✔ Yalnız `Inventory.quantity` düzeltir — lot fazlaysa FIFO ile (en eski önce)
  eritir.
✘ `Item.current_stock`'a DOKUNMAZ, `Transaction` YAZMAZ.  Defter zaten
  DOĞRU: item 24'te 23 hareketin toplamı tam olarak `current_stock`'u veriyor.
  Bu bir stok düzeltmesi değil, LOT TABLOSU uzlaştırmasıdır.
✘ Lot toplamı stoktan AZ olan kalemlere dokunmaz — hayalî lot AÇMAK veri
  uydurmaktır; onlar fiziksel sayımla çözülür (rapor edilir).
✘ Ambalaj/etiket kapsam DIŞI — onlar tasarım gereği lot tutmaz
  (CLAUDE.md: "Ambalaj/etiket stay aggregate").

ŞAHİT NUMUNE KORUMASI
─────────────────────
Konumu "Şahit Numune…" olan lotlar EN SON eritilir ve ayrıca raporlanır:
dolapta fiziksel olarak durabilirler.  Bir şahit numune lotuna dokunulacaksa
script bunu `⚠ ŞAHİT` etiketiyle gösterir — lab dolabı kontrol etmeden
`--commit` çalıştırma.

KULLANIM
────────
    venv/bin/python scripts/repair_lot_drift_20260909.py            # kuru
    venv/bin/python scripts/repair_lot_drift_20260909.py --commit   # yaz
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Inventory, Item, SessionLocal                  # noqa: E402

COMMIT = "--commit" in sys.argv
EPS = 1e-6
#: Lot tutan kategoriler — Ambalaj/Etiket bilinçli olarak DIŞARIDA.
LOT_CATEGORIES = ("Bitmiş Ürün", "Hammadde")
RETENTION_PREFIX = "şahit numune"


def _is_retention(inv: Inventory) -> bool:
    return (inv.location or "").strip().lower().startswith(RETENTION_PREFIX)


def main() -> int:
    db = SessionLocal()
    fixed = drained = 0
    retention_hits = []
    under = []           # lot < stok — dokunulmaz, raporlanır
    try:
        items = (db.query(Item)
                 .filter(Item.is_active == True,                    # noqa: E712
                         Item.category.in_(LOT_CATEGORIES))
                 .order_by(Item.category, Item.name).all())

        print(f"{'ÜRÜN':46} {'STOK':>10} {'LOT':>10} {'FARK':>9}")
        print("─" * 80)

        for it in items:
            lots = (db.query(Inventory)
                    .filter(Inventory.item_id == it.id,
                            Inventory.status == "APPROVED",
                            Inventory.is_sample == False,           # noqa: E712
                            Inventory.quantity > 0)
                    .with_for_update()
                    .all())
            lot_total = round(sum(float(l.quantity or 0) for l in lots), 6)
            stock = round(float(it.current_stock or 0), 6)
            diff = round(lot_total - stock, 6)
            if abs(diff) <= EPS:
                continue
            if diff < 0:
                if lots or stock > 0:
                    under.append((it, stock, lot_total, diff))
                continue

            # ── Lot FAZLA → FIFO erit (şahit numune lotları EN SON) ──────
            print(f"{it.name[:46]:46} {stock:>10.2f} {lot_total:>10.2f} "
                  f"{diff:>+9.2f}")
            ordered = sorted(
                lots,
                key=lambda l: (_is_retention(l), l.created_at or 0, l.id))
            remaining = diff
            for lot in ordered:
                if remaining <= EPS:
                    break
                take = min(float(lot.quantity or 0), remaining)
                if take <= EPS:
                    continue
                flag = " ⚠ ŞAHİT" if _is_retention(lot) else ""
                print(f"    − {lot.lot_number or '—':32} "
                      f"{take:>8.2f} / {float(lot.quantity):.2f}"
                      f"  [{(lot.location or '—')[:24]}]{flag}")
                if _is_retention(lot):
                    retention_hits.append((it.name, lot.lot_number, take))
                if COMMIT:
                    lot.quantity = round(float(lot.quantity) - take, 6)
                remaining = round(remaining - take, 6)
                drained += 1
            fixed += 1

        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — hiçbir şey yazılmadı: {exc}")
        return 1
    finally:
        db.close()

    print("\n" + "═" * 80)
    print(f"Lot fazlası düzeltilen ürün: {fixed} · dokunulan lot satırı: {drained}")

    if retention_hits:
        print(f"\n⚠ ŞAHİT NUMUNE LOTUNA DOKUNULDU ({len(retention_hits)}) — "
              f"dolabı fiziksel kontrol et:")
        for name, lot, qty in retention_hits:
            print(f"   · {name[:44]:44} {lot or '—':28} {qty:g}")

    if under:
        print(f"\nℹ Lot toplamı stoktan AZ ({len(under)}) — DOKUNULMADI "
              f"(hayalî lot açılmaz, fiziksel sayım gerekir):")
        for it, stock, lot_total, diff in sorted(
                under, key=lambda r: r[3])[:20]:
            print(f"   · {it.name[:40]:40} stok {stock:>10.2f} "
                  f"lot {lot_total:>10.2f} {diff:>+9.2f} {it.unit or ''}")
        if len(under) > 20:
            print(f"   … ve {len(under) - 20} kalem daha")

    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
