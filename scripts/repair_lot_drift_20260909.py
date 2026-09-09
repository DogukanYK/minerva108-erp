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
✔ Lot fazlaysa `Inventory.quantity`'yi FIFO ile (en eski önce) eritir.
✔ YALNIZ `STOK_DUZELTME` listesindeki, fiziksel sayımla teyit edilmiş kalemde
  `current_stock` değiştirir + imzalı `Adjustment` yazar.  Onun dışında deftere
  DOKUNMAZ: defter zaten doğru (item 24'te 23 hareketin toplamı tam olarak
  `current_stock`'u veriyordu).  Geri kalanı LOT TABLOSU uzlaştırmasıdır.
✔ `SAHIT_KAYIT` — dolapta fiziksel duran ama modüle hiç girmemiş numuneleri
  Şahit Numune modülüne kaydeder (stoğa etki etmez, adet zaten stokta sayılı).
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

from database import (Inventory, Item, RetentionSample,           # noqa: E402
                      RetentionSampleMovement, SessionLocal, Transaction)

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (lot uzlaştırma 09.09.2026)"

# ── Fiziksel sayımla teyit edilmiş STOK düzeltmeleri ────────────────────────
# Patron 09.09.2026'da dolabı saydı: Serenida Bikini Area 200 ml → showroom'da
# 9, şahit numune dolabında 2 = TOPLAM 11.  Meltem Hanım aynı gün stoğu 0→9
# yaparken dolaptaki 2 adedi saymamıştı; sistemin kuralı gereği dolapta duran
# şahit numune de `current_stock` içinde SAYILIR (CLAUDE.md).  Bu yüzden stok
# 9 → 11 çıkar.  Defter kaydı normal imzalı Adjustment'tır.
STOK_DUZELTME = [
    (24, 11.0, "Şahit numune dolabındaki 2 adet stoğa dahil değildi "
                "(09.09.2026 fiziksel sayım: 9 showroom + 2 dolap)"),
]

# ── Şahit numune modülüne KAYIT EDİLMEMİŞ dolap adetleri ────────────────────
# Üretim `-S` lotunu açmış ama şahit numune kaydı hiç oluşmamış; dolapta
# fiziksel olarak duruyorlar (patron teyidi).  Modül listesinde görünsünler.
SAHIT_KAYIT = [
    {"item_id": 24, "lot_number": "PRD-20260625-113617-S", "quantity": 2.0},
]
EPS = 1e-6
#: Lot tutan kategoriler — Ambalaj/Etiket bilinçli olarak DIŞARIDA.
LOT_CATEGORIES = ("Bitmiş Ürün", "Hammadde")
RETENTION_PREFIX = "şahit numune"


def _is_retention(inv: Inventory) -> bool:
    return (inv.location or "").strip().lower().startswith(RETENTION_PREFIX)


def main() -> int:
    db = SessionLocal()
    _hedef_stok = {iid: hedef for iid, hedef, _ in STOK_DUZELTME}
    fixed = drained = 0
    retention_hits = []
    under = []           # lot < stok — dokunulmaz, raporlanır
    try:
        # ── ① Fiziksel sayım stok düzeltmeleri ──────────────────────────
        if STOK_DUZELTME:
            print("① STOK DÜZELTME (fiziksel sayım)")
            for iid, hedef, sebep in STOK_DUZELTME:
                it = db.query(Item).filter(Item.id == iid).with_for_update().first()
                if not it:
                    print(f"   ✖ id {iid} bulunamadı — atlandı")
                    continue
                eski = round(float(it.current_stock or 0), 6)
                delta = round(hedef - eski, 6)
                if abs(delta) <= EPS:
                    print(f"   · {it.name[:44]:44} zaten {hedef:g} — atlandı")
                    continue
                print(f"   ~ {it.name[:44]:44} {eski:g} → {hedef:g} "
                      f"(Δ {delta:+g})")
                if COMMIT:
                    it.current_stock = hedef
                    db.add(Transaction(
                        item_id=it.id, transaction_type="Adjustment",
                        quantity=delta, performed_by=ACTOR,
                        notes=(f"Stok düzeltme — Eski: {eski:g} {it.unit or ''} → "
                               f"Yeni: {hedef:g} {it.unit or ''} "
                               f"(Δ {delta:+g}) | Sebep: {sebep}")[:500],
                    ))
            print()

        # ── ② Şahit numune modülüne eksik kayıt ─────────────────────────
        if SAHIT_KAYIT:
            print("② ŞAHİT NUMUNE KAYDI (dolapta var, modülde yoktu)")
            for rec in SAHIT_KAYIT:
                it = db.query(Item).filter(Item.id == rec["item_id"]).first()
                inv = (db.query(Inventory)
                       .filter(Inventory.item_id == rec["item_id"],
                               Inventory.lot_number == rec["lot_number"]).first())
                var = (db.query(RetentionSample)
                       .filter(RetentionSample.item_id == rec["item_id"],
                               RetentionSample.lot_number == rec["lot_number"],
                               RetentionSample.is_active == True)      # noqa: E712
                       .first())
                if var:
                    print(f"   · {rec['lot_number']} zaten kayıtlı — atlandı")
                    continue
                if not it:
                    print(f"   ✖ id {rec['item_id']} bulunamadı — atlandı")
                    continue
                from core.brands import cabinet_of
                marka = cabinet_of(it.name or "") or "Minerva"
                print(f"   + {it.name[:40]:40} {rec['lot_number']:26} "
                      f"{rec['quantity']:g} {it.unit or ''} · dolap: {marka}")
                if COMMIT:
                    rs = RetentionSample(
                        inventory_id=inv.id if inv else None,
                        item_id=it.id, item_name=it.name,
                        lot_number=rec["lot_number"], brand=marka,
                        quantity=rec["quantity"],
                        initial_quantity=rec["quantity"],
                        unit=it.unit or "adet",
                        produced_at=inv.created_at if inv else None,
                    )
                    db.add(rs)
                    db.flush()
                    db.add(RetentionSampleMovement(
                        sample_id=rs.id, movement_type="giris",
                        quantity=rec["quantity"], reason="diger",
                        performed_by=ACTOR,
                        note=("Geriye dönük kayıt — üretim -S lotu açmış ama "
                              "şahit numune kaydı oluşmamıştı (09.09.2026 "
                              "fiziksel teyit)."),
                    ))
            print()

        # ── ③ Lot uzlaştırma ────────────────────────────────────────────
        print("③ LOT UZLAŞTIRMA")
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
            # KURU ÇALIŞTIRMA SADAKATİ: ① adımı --commit olmadan yazmaz, ama
            # planı hedef stoğa göre kurmalıyız; yoksa kuru çıktı gerçekte
            # olacak düşümden fazlasını gösterir.
            stock = round(float(_hedef_stok.get(it.id, it.current_stock) or 0), 6)
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
