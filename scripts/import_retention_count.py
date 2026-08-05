"""03/08/2026 fiziksel şahit numune dolabı sayımını sisteme aktarır (idempotent).

Kaynak: labın el yazısı 11 sayfalık sayım listesi (fotoğraflardan çift okumayla
çıkarıldı, /tmp/sahit_import.json).  Yaptıkları:

  ① `Item.lot_seq` senkronizasyonu — labın fiilen kullandığı en yüksek parti
     numarasına çekilir.  Bu OLMADAN sistem zaten kullanılmış bir numarayı
     önerir (ör. lab MNR005'te, sistem MNR001 önerirdi).
  ② Dolap kayıtları — sayımdaki her satır bir `RetentionSample` olur
     (source='sayim'), parti no + üretim + SKT ile.  Önceki `backfill`
     kayıtları silinir (onlar PRD-…-S lot numaralıydı ve eksikti: 61 adet
     görünüyordu, gerçekte 220 adet var).

STOĞA DOKUNMAZ.  Dolaptaki numuneler `Item.current_stock` içinde sayılmaya
devam eder (kullanıcı kararı); bu script hiçbir `Transaction` yazmaz, hiçbir
bakiye değiştirmez.  Yalnız dolap görünürlüğü + lot sayacı.

İDEMPOTENT: her çalıştırmada source in ('backfill','sayim') kayıtlarını
silip sayımdan yeniden kurar.  `production` kaynaklı kayıtlara (yeni
üretimlerden gelenler) DOKUNMAZ.

Kullanım:
    venv/bin/python scripts/import_retention_count.py            # kuru çalıştırma
    venv/bin/python scripts/import_retention_count.py --commit   # yaz
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (SessionLocal, Item, RetentionSample,          # noqa: E402
                      RetentionSampleMovement)
from core.brands import cabinet_of                                  # noqa: E402

COMMIT = "--commit" in sys.argv
DATA = Path(__file__).resolve().parent.parent / "data" / "sahit_sayim_20260803.json"
COUNT_DATE = "03.08.2026"


def parse_date(s):
    try:
        return datetime.strptime((s or "").strip(), "%d/%m/%Y")
    except ValueError:
        return None


def main() -> int:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    rows = payload["satirlar"]
    db = SessionLocal()
    created = skipped = 0
    seq_updates = []
    notes_out = []
    try:
        # ── ① Lot sayaçları ──
        best = {}
        for r in rows:
            iid = r.get("item_id")
            if iid and r.get("seq"):
                best[iid] = max(best.get(iid, 0), int(r["seq"]))
        for iid, seq in sorted(best.items()):
            it = db.query(Item).filter(Item.id == iid).first()
            if not it:
                continue
            if (it.lot_seq or 0) < seq:
                seq_updates.append((iid, it.name, it.lot_seq or 0, seq))
                if COMMIT:
                    it.lot_seq = seq

        # ── ② Dolap kayıtları — önceki sayım/backfill kayıtlarını tazele ──
        old = (db.query(RetentionSample)
               .filter(RetentionSample.source.in_(("backfill", "sayim"))).all())
        if COMMIT:
            for o in old:
                db.query(RetentionSampleMovement).filter(
                    RetentionSampleMovement.sample_id == o.id).delete()
                db.delete(o)
            db.flush()

        for r in rows:
            iid = r.get("item_id")
            if not iid:
                skipped += 1
                notes_out.append(f"ATLANDI (ürün eşleşmedi): {r['parti_no']} · {r['urun']}")
                continue
            it = db.query(Item).filter(Item.id == iid).first()
            if not it:
                skipped += 1
                continue
            urt = parse_date(r.get("urt"))
            skt = parse_date(r.get("skt"))
            note = f"Sayım {COUNT_DATE} · listede: {r['urun']}"
            if r.get("uyari"):
                note += f" · ⚠ {r['uyari']}"
            created += 1
            if COMMIT:
                rs = RetentionSample(
                    inventory_id=None,                # sayım kaydı — lot bağı yok
                    item_id=it.id, item_name=it.name,
                    lot_number=r["parti_no"], brand=cabinet_of(it.name),
                    quantity=float(r["adet"]), initial_quantity=float(r["adet"]),
                    unit=it.unit or "adet",
                    produced_at=urt,
                    retention_until=skt.date() if skt else None,
                    status="stored", source="sayim",
                    placed_by="sayım 03.08.2026", note=note,
                    domain=it.domain or "cosmetics",
                )
                db.add(rs)
                db.flush()
                db.add(RetentionSampleMovement(
                    sample_id=rs.id, movement_type="giris", quantity=float(r["adet"]),
                    note=f"Fiziksel sayım {COUNT_DATE}", performed_by="sistem"))

        if COMMIT:
            db.commit()
    finally:
        db.close()

    print(f"=== LOT SAYACI ({len(seq_updates)} ürün) ===")
    for iid, name, old_s, new_s in seq_updates[:60]:
        print(f"  id={iid:3} {name[:52]:52} {old_s:3} → {new_s:3}")
    if len(seq_updates) > 60:
        print(f"  … +{len(seq_updates)-60} ürün daha")
    print(f"\n=== DOLAP ===")
    print(f"  Silinecek eski kayıt (backfill/sayim) : {len(old)}")
    print(f"  Yazılacak sayım kaydı                 : {created}")
    print(f"  Atlanan (ürün eşleşmedi)              : {skipped}")
    for n in notes_out:
        print(f"    · {n}")
    total = sum(float(r["adet"]) for r in rows if r.get("item_id"))
    print(f"  Toplam adet                            : {total:g}")
    print()
    print("✓ YAZILDI — stok rakamlarına DOKUNULMADI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
